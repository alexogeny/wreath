/* RouteTable: a segment trie over request paths.
 *
 * Paths are split as path[1:].split("/"), so trailing slashes and empty
 * segments stay significant. Static children are matched with memcmp and
 * take precedence over the parameter child at each level, with backtracking
 * so a parameter route can still match when a static branch dead-ends.
 * Parameter names live on each terminal, so "/a/{x}" and "/a/{y}/b" can
 * share nodes safely. Matching allocates nothing until a parameter value
 * or the result tuple is built.
 */
#include "wreathcore.h"

#define MAX_SEGMENTS 255

typedef struct RNode {
    struct RNode **kids;
    char **kid_segs;
    Py_ssize_t *kid_seg_lens;
    Py_ssize_t kid_count;
    Py_ssize_t kid_cap;
    struct RNode *param_kid;
    /* dict: method (str) -> (handler, tuple of parameter names); NULL when
     * this node is not a terminal. */
    PyObject *routes;
} RNode;

typedef struct {
    const char *start;
    Py_ssize_t len;
} Segment;

typedef struct {
    PyObject_HEAD
    RNode *root;
} RouteTable;

static RNode *
rnode_new(void)
{
    RNode *node = PyMem_Calloc(1, sizeof(RNode));
    if (node == NULL) {
        PyErr_NoMemory();
    }
    return node;
}

static void
rnode_free(RNode *node)
{
    if (node == NULL) {
        return;
    }
    for (Py_ssize_t i = 0; i < node->kid_count; i++) {
        rnode_free(node->kids[i]);
        PyMem_Free(node->kid_segs[i]);
    }
    PyMem_Free(node->kids);
    PyMem_Free(node->kid_segs);
    PyMem_Free(node->kid_seg_lens);
    rnode_free(node->param_kid);
    Py_XDECREF(node->routes);
    PyMem_Free(node);
}

/* Order two segments: memcmp over the common prefix, length as the tie-break.
 * This is a total order over byte strings; its only job is to keep each node's
 * children in a consistent sequence, so it never affects match semantics. */
static int
seg_compare(const char *a, Py_ssize_t alen, const char *b, Py_ssize_t blen)
{
    Py_ssize_t common = alen < blen ? alen : blen;
    int rc = common > 0 ? memcmp(a, b, (size_t)common) : 0;
    if (rc != 0) {
        return rc;
    }
    if (alen == blen) {
        return 0;
    }
    return alen < blen ? -1 : 1;
}

/* Locate the insertion point for `seg`, and report whether it already exists.
 * Children are kept sorted, so this is a binary search rather than a scan over
 * every child -- a node with wide static fanout is looked up in log time. */
static Py_ssize_t
rnode_search_kid(RNode *node, const char *seg, Py_ssize_t len, int *found)
{
    Py_ssize_t lo = 0;
    Py_ssize_t hi = node->kid_count;
    *found = 0;
    while (lo < hi) {
        Py_ssize_t mid = lo + (hi - lo) / 2;
        int rc = seg_compare(node->kid_segs[mid], node->kid_seg_lens[mid], seg, len);
        if (rc == 0) {
            *found = 1;
            return mid;
        }
        if (rc < 0) {
            lo = mid + 1;
        }
        else {
            hi = mid;
        }
    }
    return lo;
}

static RNode *
rnode_find_kid(RNode *node, const char *seg, Py_ssize_t len)
{
    int found;
    Py_ssize_t i = rnode_search_kid(node, seg, len, &found);
    return found ? node->kids[i] : NULL;
}

static RNode *
rnode_add_kid(RNode *node, const char *seg, Py_ssize_t len)
{
    if (node->kid_count == node->kid_cap) {
        Py_ssize_t cap = node->kid_cap ? node->kid_cap * 2 : 4;
        RNode **kids = PyMem_Realloc(node->kids, (size_t)cap * sizeof(RNode *));
        if (kids == NULL) {
            PyErr_NoMemory();
            return NULL;
        }
        node->kids = kids;
        char **segs = PyMem_Realloc(node->kid_segs, (size_t)cap * sizeof(char *));
        if (segs == NULL) {
            PyErr_NoMemory();
            return NULL;
        }
        node->kid_segs = segs;
        Py_ssize_t *lens =
            PyMem_Realloc(node->kid_seg_lens, (size_t)cap * sizeof(Py_ssize_t));
        if (lens == NULL) {
            PyErr_NoMemory();
            return NULL;
        }
        node->kid_seg_lens = lens;
        node->kid_cap = cap;
    }

    char *copy = PyMem_Malloc(len ? (size_t)len : 1);
    if (copy == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    memcpy(copy, seg, (size_t)len);
    RNode *kid = rnode_new();
    if (kid == NULL) {
        PyMem_Free(copy);
        return NULL;
    }
    /* Insert in sorted position so lookups can binary search. Registration
     * order therefore cannot affect matching. Shift the suffix of all three
     * parallel arrays once. */
    int found;
    Py_ssize_t i = rnode_search_kid(node, seg, len, &found);
    Py_ssize_t tail = node->kid_count - i;
    if (tail > 0) {
        memmove(node->kids + i + 1, node->kids + i, (size_t)tail * sizeof(RNode *));
        memmove(node->kid_segs + i + 1, node->kid_segs + i, (size_t)tail * sizeof(char *));
        memmove(node->kid_seg_lens + i + 1, node->kid_seg_lens + i,
                (size_t)tail * sizeof(Py_ssize_t));
    }
    node->kids[i] = kid;
    node->kid_segs[i] = copy;
    node->kid_seg_lens[i] = len;
    node->kid_count++;
    return kid;
}

/* Split path[1:] on '/'; returns the segment count or -1. */
static Py_ssize_t
split_path(const char *path, Py_ssize_t path_len, Segment *segments)
{
    const char *p = path + 1;
    const char *end = path + path_len;
    Py_ssize_t count = 0;
    const char *seg_start = p;
    for (;; p++) {
        if (p == end || *p == '/') {
            if (count == MAX_SEGMENTS) {
                return -1;
            }
            segments[count].start = seg_start;
            segments[count].len = p - seg_start;
            count++;
            if (p == end) {
                return count;
            }
            seg_start = p + 1;
        }
    }
}

static PyObject *
rt_add(RouteTable *self, PyObject *args)
{
    PyObject *path_obj, *method_obj, *handler;
    if (!PyArg_ParseTuple(args, "UUO:add", &path_obj, &method_obj, &handler)) {
        return NULL;
    }
    Py_ssize_t path_len;
    const char *path = PyUnicode_AsUTF8AndSize(path_obj, &path_len);
    if (path == NULL) {
        return NULL;
    }
    if (path_len == 0 || path[0] != '/') {
        PyErr_SetString(PyExc_ValueError, "route paths must begin with '/'");
        return NULL;
    }

    Segment segments[MAX_SEGMENTS];
    Py_ssize_t count = split_path(path, path_len, segments);
    if (count < 0) {
        PyErr_SetString(PyExc_ValueError, "route path has too many segments");
        return NULL;
    }

    PyObject *names = PyList_New(0);
    if (names == NULL) {
        return NULL;
    }

    RNode *node = self->root;
    for (Py_ssize_t i = 0; i < count; i++) {
        const char *seg = segments[i].start;
        Py_ssize_t seg_len = segments[i].len;
        if (seg_len >= 2 && seg[0] == '{' && seg[seg_len - 1] == '}') {
            Py_ssize_t name_len = seg_len - 2;
            if (name_len == 0 || memchr(seg + 1, '{', (size_t)name_len) ||
                memchr(seg + 1, '}', (size_t)name_len)) {
                PyErr_Format(PyExc_ValueError, "invalid path parameter: '%.200s'", seg);
                goto fail;
            }
            PyObject *name = PyUnicode_DecodeUTF8(seg + 1, name_len, NULL);
            if (name == NULL || PyList_Append(names, name) < 0) {
                Py_XDECREF(name);
                goto fail;
            }
            Py_DECREF(name);
            if (node->param_kid == NULL) {
                node->param_kid = rnode_new();
                if (node->param_kid == NULL) {
                    goto fail;
                }
            }
            node = node->param_kid;
        }
        else if (memchr(seg, '{', (size_t)seg_len) || memchr(seg, '}', (size_t)seg_len)) {
            PyErr_SetString(PyExc_ValueError,
                            "path parameters must occupy an entire segment");
            goto fail;
        }
        else {
            RNode *kid = rnode_find_kid(node, seg, seg_len);
            if (kid == NULL) {
                kid = rnode_add_kid(node, seg, seg_len);
                if (kid == NULL) {
                    goto fail;
                }
            }
            node = kid;
        }
    }

    if (node->routes == NULL) {
        node->routes = PyDict_New();
        if (node->routes == NULL) {
            goto fail;
        }
    }
    int exists = PyDict_Contains(node->routes, method_obj);
    if (exists < 0) {
        goto fail;
    }
    if (exists) {
        PyErr_Format(PyExc_ValueError, "%s route: %U %U",
                     PyList_GET_SIZE(names) > 0 ? "conflicting" : "duplicate",
                     method_obj, path_obj);
        goto fail;
    }

    PyObject *names_tuple = PyList_AsTuple(names);
    if (names_tuple == NULL) {
        goto fail;
    }
    PyObject *entry = PyTuple_Pack(2, handler, names_tuple);
    Py_DECREF(names_tuple);
    if (entry == NULL || PyDict_SetItem(node->routes, method_obj, entry) < 0) {
        Py_XDECREF(entry);
        goto fail;
    }
    Py_DECREF(entry);
    Py_DECREF(names);
    Py_RETURN_NONE;

fail:
    Py_DECREF(names);
    return NULL;
}

/* Depth-first match with static-over-parameter precedence and backtracking.
 * Fills values[] with the parameter segments along the winning path. */
static RNode *
match_node(RNode *node, const Segment *segments, Py_ssize_t count, Py_ssize_t index,
           PyObject *method, Segment *values, Py_ssize_t *value_count, int *error)
{
    if (index == count) {
        if (node->routes != NULL) {
            PyObject *entry = PyDict_GetItemWithError(node->routes, method);
            if (entry != NULL) {
                return node;
            }
            if (PyErr_Occurred()) {
                *error = 1;
            }
        }
        return NULL;
    }
    const Segment *seg = &segments[index];
    RNode *kid = rnode_find_kid(node, seg->start, seg->len);
    if (kid != NULL) {
        RNode *found = match_node(kid, segments, count, index + 1, method, values,
                                  value_count, error);
        if (found != NULL || *error) {
            return found;
        }
    }
    if (node->param_kid != NULL) {
        values[*value_count] = *seg;
        (*value_count)++;
        RNode *found = match_node(node->param_kid, segments, count, index + 1, method,
                                  values, value_count, error);
        if (found != NULL || *error) {
            return found;
        }
        (*value_count)--;
    }
    return NULL;
}

static PyObject *
rt_match(RouteTable *self, PyObject *args)
{
    PyObject *method_obj, *path_obj;
    if (!PyArg_ParseTuple(args, "UU:match", &method_obj, &path_obj)) {
        return NULL;
    }
    Py_ssize_t path_len;
    const char *path = PyUnicode_AsUTF8AndSize(path_obj, &path_len);
    if (path == NULL) {
        return NULL;
    }
    if (path_len == 0 || path[0] != '/') {
        Py_RETURN_NONE;
    }

    Segment segments[MAX_SEGMENTS];
    Py_ssize_t count = split_path(path, path_len, segments);
    if (count < 0) {
        Py_RETURN_NONE;
    }

    Segment values[MAX_SEGMENTS];
    Py_ssize_t value_count = 0;
    int error = 0;
    PyObject *method_used = method_obj;
    RNode *node = match_node(self->root, segments, count, 0, method_obj, values,
                             &value_count, &error);
    if (error) {
        return NULL;
    }
    if (node == NULL &&
        PyUnicode_CompareWithASCIIString(method_obj, "HEAD") == 0) {
        /* Cached once; interned strings are immortal in CPython 3.14. */
        static PyObject *get_method = NULL;
        if (get_method == NULL) {
            get_method = PyUnicode_InternFromString("GET");
            if (get_method == NULL) {
                return NULL;
            }
        }
        value_count = 0;
        node = match_node(self->root, segments, count, 0, get_method, values,
                          &value_count, &error);
        method_used = get_method;
        if (error) {
            return NULL;
        }
    }
    if (node == NULL) {
        Py_RETURN_NONE;
    }

    PyObject *entry = PyDict_GetItemWithError(node->routes, method_used);
    if (entry == NULL) {
        return PyErr_Occurred() ? NULL : Py_NewRef(Py_None);
    }
    PyObject *handler = PyTuple_GET_ITEM(entry, 0);
    PyObject *names = PyTuple_GET_ITEM(entry, 1);
    Py_ssize_t name_count = PyTuple_GET_SIZE(names);

    if (name_count == 0) {
        return PyTuple_Pack(2, handler, Py_None);
    }
    PyObject *params = PyDict_New();
    if (params == NULL) {
        return NULL;
    }
    for (Py_ssize_t i = 0; i < name_count && i < value_count; i++) {
        PyObject *value = PyUnicode_DecodeUTF8(values[i].start, values[i].len, NULL);
        if (value == NULL ||
            PyDict_SetItem(params, PyTuple_GET_ITEM(names, i), value) < 0) {
            Py_XDECREF(value);
            Py_DECREF(params);
            return NULL;
        }
        Py_DECREF(value);
    }
    PyObject *result = PyTuple_Pack(2, handler, params);
    Py_DECREF(params);
    return result;
}

static PyObject *
rt_new(PyTypeObject *type, PyObject *Py_UNUSED(args), PyObject *Py_UNUSED(kwargs))
{
    RouteTable *self = (RouteTable *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->root = rnode_new();
    if (self->root == NULL) {
        Py_DECREF(self);
        return NULL;
    }
    return (PyObject *)self;
}

static void
rt_dealloc(RouteTable *self)
{
    rnode_free(self->root);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyMethodDef rt_methods[] = {
    {"add", (PyCFunction)rt_add, METH_VARARGS,
     "add(path, method, handler)\nRegister a route; method must be uppercase."},
    {"match", (PyCFunction)rt_match, METH_VARARGS,
     "match(method, path) -> (handler, params | None) | None"},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject RouteTableType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._core.RouteTable",
    .tp_basicsize = sizeof(RouteTable),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "Segment-trie route table with a static fast path.",
    .tp_new = rt_new,
    .tp_dealloc = (destructor)rt_dealloc,
    .tp_methods = rt_methods,
};

int
wreath_register_router(PyObject *module)
{
    if (PyType_Ready(&RouteTableType) < 0) {
        return -1;
    }
    return PyModule_AddObjectRef(module, "RouteTable", (PyObject *)&RouteTableType);
}
