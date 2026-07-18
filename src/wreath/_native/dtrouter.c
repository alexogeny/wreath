/* DecisionRouteTable: routes compiled into a decision tree.
 *
 * Rather than walking a path left-to-right like the trie, the route set is
 * grouped by method and segment count, then within each group a decision tree
 * tests the segment position that best partitions the remaining candidates
 * (compared through a hash-keyed dict, i.e. "selected bytes"). Parameter
 * routes are folded into every literal branch so matching never backtracks
 * across the tree; at the terminal leaf the one (or few) surviving routes are
 * fully verified once and parameters captured. Static routes take a
 * hash-table fast path checked before the tree.
 *
 * Semantics are identical to wreath._native._core.RouteTable and the pure twins;
 * the differential tests assert parity.
 */
#include "wreathcore.h"

#define MAX_SEGMENTS 255

typedef struct {
    char *bytes; /* NULL marks a parameter slot */
    Py_ssize_t len;
} DSeg;

typedef struct {
    PyObject *method; /* borrowed: interned key also held by trees/seen */
    DSeg *segs;
    int nseg;
    PyObject *param_names; /* tuple[str, ...] */
    Py_ssize_t nparams;
    PyObject *handler;
    int specificity; /* number of literal segments */
    PyObject *access_clauses; /* tuple[int, ...] disjunctive capability masks */
    /* The clauses above, precompiled exactly like DNode's; the leaf check runs
     * per surviving candidate on every match that reaches it. */
    unsigned long long *clause_words;
    Py_ssize_t clause_count;
    int always_eligible;
    int all_native;
} DRoute;

typedef struct DNode {
    int is_leaf;
    int position;
    PyObject *branches; /* dict: str -> _DNodeRef(DNode*) */
    struct DNode *wildcard;
    DRoute **candidates; /* borrowed pointers into table->routes */
    Py_ssize_t ncand;
    PyObject *access_clauses; /* distinct clauses represented below this node */
    /* The clauses above, precompiled for the descent.  Pruning runs at every
     * node of every match, and the clauses are compile-time constants, so
     * converting them out of PyLong per node meant PyLong_AsUnsignedLongLong
     * and PyErr_Occurred (which reads the thread state through TLS) on a path
     * that mostly proves public routes are public. */
    unsigned long long *clause_words;
    Py_ssize_t clause_count;
    int always_eligible; /* a zero clause admits every caller: skip the check */
    int all_native;      /* every clause fits a machine word: skip the fallback */
} DNode;

typedef struct {
    PyObject_HEAD
    PyObject *static_routes; /* dict: method -> dict: path -> (handler, None) */
    PyObject *seen;          /* set of (method, signature) for conflicts */
    DRoute *routes;
    Py_ssize_t nroutes;
    Py_ssize_t routes_cap;
    PyObject *trees;         /* dict: method -> dict: nseg -> _DNodeRef(DNode*) */
    int dirty;
} DecisionRouteTable;

/* ------------------------------------------------------------------ */
/* Node lifecycle                                                     */
/* ------------------------------------------------------------------ */

static void
dnode_free(DNode *node)
{
    if (node == NULL) {
        return;
    }
    Py_XDECREF(node->branches); /* frees the child nodes via _DNodeRef */
    Py_XDECREF(node->access_clauses);
    PyMem_Free(node->clause_words);
    dnode_free(node->wildcard);
    PyMem_Free(node->candidates);
    PyMem_Free(node);
}

/* Owning reference to a DNode, held by the branch dicts and the per-method
 * tree map.
 *
 * This was a PyCapsule. Reading one back costs a PyCapsule_GetPointer call that
 * validates the capsule name with strcmp -- on every node of every descent, to
 * re-derive a pointer we own. These objects never leave dtrouter.c, so the
 * name check protected nothing; the type does the same job for free, and the
 * descent becomes a load. */
typedef struct {
    PyObject_HEAD
    DNode *node;
} DNodeRef;

static PyTypeObject *DNodeRefType = NULL;

static void
dnoderef_dealloc(PyObject *self)
{
    dnode_free(((DNodeRef *)self)->node);
    Py_TYPE(self)->tp_free(self);
}

static PyType_Slot dnoderef_slots[] = {
    {Py_tp_dealloc, dnoderef_dealloc},
    {0, NULL},
};

static PyType_Spec dnoderef_spec = {
    .name = "wreath._native._core._DNodeRef",
    .basicsize = sizeof(DNodeRef),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_DISALLOW_INSTANTIATION,
    .slots = dnoderef_slots,
};

/* Takes ownership of `node` only on success, matching the PyCapsule_New this
 * replaced: on failure the caller still owns `node` and frees it. Freeing here
 * as well would double-free it. */
static PyObject *
dnoderef_new(DNode *node)
{
    DNodeRef *ref = PyObject_New(DNodeRef, DNodeRefType);
    if (ref == NULL) {
        return NULL;
    }
    ref->node = node;
    return (PyObject *)ref;
}

/* The dicts hold only DNodeRefs, built here and never exposed to Python. */
#define DNODE_OF(ref) (((DNodeRef *)(ref))->node)

/* Capability summary for one node: the distinct clauses reachable below it.
 *
 * A node is pruned when no clause below it is satisfied, and that answer depends
 * on neither the order nor the multiplicity of the clauses. Concatenating every
 * candidate's clauses therefore stored the same few masks once per route beneath
 * the node -- the duplication grew with the subtree while adding nothing. First
 * occurrence order is kept so the summary is deterministic. Full ordered
 * per-route clauses still live on each DRoute, which is what verify_leaf uses
 * for the actual authorization decision. */
static PyObject *
collect_access_clauses(DRoute **cands, Py_ssize_t ncand)
{
    PyObject *seen = PySet_New(NULL);
    if (seen == NULL) {
        return NULL;
    }
    PyObject *ordered = PyList_New(0);
    if (ordered == NULL) {
        Py_DECREF(seen);
        return NULL;
    }
    for (Py_ssize_t i = 0; i < ncand; i++) {
        PyObject *route_clauses = cands[i]->access_clauses;
        for (Py_ssize_t j = 0; j < PyTuple_GET_SIZE(route_clauses); j++) {
            PyObject *clause = PyTuple_GET_ITEM(route_clauses, j);
            int known = PySet_Contains(seen, clause);
            if (known < 0) {
                goto fail;
            }
            if (known) {
                continue;
            }
            if (PySet_Add(seen, clause) < 0 || PyList_Append(ordered, clause) < 0) {
                goto fail;
            }
        }
    }
    PyObject *clauses = PyList_AsTuple(ordered);
    Py_DECREF(seen);
    Py_DECREF(ordered);
    return clauses;

fail:
    Py_DECREF(seen);
    Py_DECREF(ordered);
    return NULL;
}


/* Precompile a clause tuple for matching.
 *
 * Clauses are compile-time constants, but pruning re-derived them from PyLong
 * at every node of every match -- and the PyErr_Occurred() each conversion
 * needs reads the thread state through TLS. Everything here is decided once:
 *   - `always` when some clause is zero: (caller & 0) == 0 for every caller, so
 *     that check can never prune and is skipped outright. A route table with no
 *     access clauses is entirely this case.
 *   - `words` + `native` when every clause fits a machine word, which is the
 *     normal shape; wider Python integers keep the existing slow path.
 * Returns -1 only on allocation failure. */
static int
prepare_clause_words(PyObject *clauses, unsigned long long **words_out,
                     Py_ssize_t *count_out, int *always_out, int *native_out)
{
    Py_ssize_t count = PyTuple_GET_SIZE(clauses);
    *count_out = count;
    *always_out = 0;
    *native_out = 1;
    *words_out = NULL;
    if (count == 0) {
        /* Nothing below can be satisfied: not eligible, and not "always". The
         * empty loop returns 0, exactly as the tuple scan did. */
        return 0;
    }
    *words_out = PyMem_Malloc((size_t)count * sizeof(unsigned long long));
    if (*words_out == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        unsigned long long word =
            PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(clauses, i));
        if (PyErr_Occurred()) {
            PyErr_Clear();
            *native_out = 0;  /* arbitrary-precision: fall back at match time */
            (*words_out)[i] = 0;
            continue;
        }
        (*words_out)[i] = word;
        if (word == 0) {
            *always_out = 1;
        }
    }
    return 0;
}

static int
dnode_prepare_clauses(DNode *node)
{
    return prepare_clause_words(node->access_clauses, &node->clause_words,
                                &node->clause_count, &node->always_eligible,
                                &node->all_native);
}

static int
access_eligible(PyObject *clauses, PyObject *caller_mask)
{
    /* Capability masks normally fit in one machine word.  Keep arbitrary-size
     * Python integers as a compatibility fallback, but avoid allocating an
     * intersection PyLong at every decision node on the common path. */
    unsigned long long caller = PyLong_AsUnsignedLongLong(caller_mask);
    int native_word = !PyErr_Occurred();
    if (!native_word) {
        PyErr_Clear();
    }

    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(clauses); i++) {
        PyObject *required = PyTuple_GET_ITEM(clauses, i);
        if (native_word) {
            unsigned long long required_word = PyLong_AsUnsignedLongLong(required);
            if (!PyErr_Occurred()) {
                if ((caller & required_word) == required_word) {
                    return 1;
                }
                continue;
            }
            PyErr_Clear();
        }

        PyObject *intersection = PyNumber_And(caller_mask, required);
        if (intersection == NULL) {
            return -1;
        }
        int equal = PyObject_RichCompareBool(intersection, required, Py_EQ);
        Py_DECREF(intersection);
        if (equal != 0) {
            return equal;
        }
    }
    return 0;
}


/* Does this caller satisfy any of the precompiled clauses? Same answer as
 * access_eligible(clauses, caller_mask), reached without touching the Python
 * API on the common paths. `caller_word`/`caller_native` are hoisted once per
 * match: the mask cannot change during one descent. */
static inline int
clauses_eligible(const unsigned long long *words, Py_ssize_t count, int always,
                 int all_native, PyObject *clauses,
                 unsigned long long caller_word, int caller_native,
                 PyObject *caller_mask)
{
    if (always) {
        return 1;
    }
    if (caller_native && all_native) {
        for (Py_ssize_t i = 0; i < count; i++) {
            if ((caller_word & words[i]) == words[i]) {
                return 1;
            }
        }
        return 0;
    }
    return access_eligible(clauses, caller_mask);
}

#define NODE_ELIGIBLE(node, caller_word, caller_native, caller_mask)          \
    clauses_eligible((node)->clause_words, (node)->clause_count,              \
                     (node)->always_eligible, (node)->all_native,             \
                     (node)->access_clauses, (caller_word), (caller_native),  \
                     (caller_mask))

#define ROUTE_ELIGIBLE(route, caller_word, caller_native, caller_mask)        \
    clauses_eligible((route)->clause_words, (route)->clause_count,            \
                     (route)->always_eligible, (route)->all_native,           \
                     (route)->access_clauses, (caller_word), (caller_native), \
                     (caller_mask))

static DNode *
dnode_new_leaf(DRoute **cands, Py_ssize_t ncand)
{
    DNode *node = PyMem_Calloc(1, sizeof(DNode));
    if (node == NULL) {
        return (DNode *)PyErr_NoMemory();
    }
    node->is_leaf = 1;
    node->access_clauses = collect_access_clauses(cands, ncand);
    if (node->access_clauses == NULL || dnode_prepare_clauses(node) < 0) {
        Py_XDECREF(node->access_clauses);
        PyMem_Free(node->clause_words);
        PyMem_Free(node);
        return NULL;
    }
    if (ncand > 0) {
        node->candidates = PyMem_Malloc((size_t)ncand * sizeof(DRoute *));
        if (node->candidates == NULL) {
            PyMem_Free(node);
            return (DNode *)PyErr_NoMemory();
        }
        /* Insertion sort by descending specificity: literal routes win. */
        for (Py_ssize_t i = 0; i < ncand; i++) {
            DRoute *r = cands[i];
            Py_ssize_t j = i;
            while (j > 0 && node->candidates[j - 1]->specificity < r->specificity) {
                node->candidates[j] = node->candidates[j - 1];
                j--;
            }
            node->candidates[j] = r;
        }
    }
    node->ncand = ncand;
    return node;
}

static int
seg_equal(const DSeg *a, const char *bytes, Py_ssize_t len)
{
    return a->bytes != NULL && a->len == len && memcmp(a->bytes, bytes, (size_t)len) == 0;
}

/* Recursively compile a candidate set into a decision (sub)tree. */
static DNode *
dnode_build(DRoute **cands, Py_ssize_t ncand, char *used, int nseg)
{
    if (ncand <= 1) {
        return dnode_new_leaf(cands, ncand);
    }

    int best_position = -1;
    long best_score = -1;
    for (int p = 0; p < nseg; p++) {
        if (used[p]) {
            continue;
        }
        /* Count distinct literal values in one pass through a set, rather than
         * comparing every candidate with its predecessors: a node shared by n
         * routes must not cost O(n^2) to compile. The keys match the grouping
         * dict below, so distinctness here and branch identity there agree. */
        Py_ssize_t literal_count = 0;
        PyObject *seen = PySet_New(NULL);
        if (seen == NULL) {
            return NULL;
        }
        for (Py_ssize_t i = 0; i < ncand; i++) {
            const DSeg *seg = &cands[i]->segs[p];
            if (seg->bytes == NULL) {
                continue;
            }
            literal_count++;
            PyObject *value = PyUnicode_FromStringAndSize(seg->bytes, seg->len);
            if (value == NULL) {
                Py_DECREF(seen);
                return NULL;
            }
            int rc = PySet_Add(seen, value);
            Py_DECREF(value);
            if (rc < 0) {
                Py_DECREF(seen);
                return NULL;
            }
        }
        Py_ssize_t distinct = PySet_GET_SIZE(seen);
        Py_DECREF(seen);
        if (literal_count == 0) {
            continue;
        }
        long score = distinct * 1000 + literal_count;
        if (score > best_score) {
            best_score = score;
            best_position = p;
        }
    }

    if (best_position < 0) {
        return dnode_new_leaf(cands, ncand);
    }

    int p = best_position;
    DRoute **scratch = PyMem_Malloc((size_t)ncand * sizeof(DRoute *));
    if (scratch == NULL) {
        return (DNode *)PyErr_NoMemory();
    }
    PyObject *branches = PyDict_New();
    if (branches == NULL) {
        PyMem_Free(scratch);
        return NULL;
    }

    used[p] = 1;
    DNode *result = NULL;

    for (Py_ssize_t i = 0; i < ncand; i++) {
        const DSeg *seg = &cands[i]->segs[p];
        if (seg->bytes == NULL) {
            continue;
        }
        PyObject *value = PyUnicode_FromStringAndSize(seg->bytes, seg->len);
        if (value == NULL) {
            goto fail;
        }
        int known = PyDict_Contains(branches, value);
        if (known != 0) {
            Py_DECREF(value);
            if (known < 0) {
                goto fail;
            }
            continue; /* group already built */
        }
        /* group = routes with this literal value, plus all parameter routes */
        Py_ssize_t count = 0;
        for (Py_ssize_t j = 0; j < ncand; j++) {
            const DSeg *other = &cands[j]->segs[p];
            if (other->bytes == NULL || seg_equal(other, seg->bytes, seg->len)) {
                scratch[count++] = cands[j];
            }
        }
        DNode *child = dnode_build(scratch, count, used, nseg);
        if (child == NULL) {
            Py_DECREF(value);
            goto fail;
        }
        PyObject *ref = dnoderef_new(child);  /* owns child only on success */
        if (ref == NULL) {
            dnode_free(child);
            Py_DECREF(value);
            goto fail;
        }
        int rc = PyDict_SetItem(branches, value, ref);
        Py_DECREF(value);
        Py_DECREF(ref); /* the dict's reference now owns the child */
        if (rc < 0) {
            goto fail;
        }
    }

    Py_ssize_t nwild = 0;
    for (Py_ssize_t j = 0; j < ncand; j++) {
        if (cands[j]->segs[p].bytes == NULL) {
            scratch[nwild++] = cands[j];
        }
    }
    DNode *wildcard = NULL;
    if (nwild > 0) {
        wildcard = dnode_build(scratch, nwild, used, nseg);
        if (wildcard == NULL) {
            goto fail;
        }
    }

    result = PyMem_Calloc(1, sizeof(DNode));
    if (result == NULL) {
        dnode_free(wildcard);
        PyErr_NoMemory();
        goto fail;
    }
    result->is_leaf = 0;
    result->position = p;
    result->access_clauses = collect_access_clauses(cands, ncand);
    if (result->access_clauses == NULL || dnode_prepare_clauses(result) < 0) {
        Py_XDECREF(result->access_clauses);
        PyMem_Free(result->clause_words);
        PyMem_Free(result);
        result = NULL;
        goto fail;
    }
    result->branches = branches; /* steal reference */
    result->wildcard = wildcard;
    branches = NULL;

fail:
    used[p] = 0;
    Py_XDECREF(branches);
    PyMem_Free(scratch);
    return result;
}

/* ------------------------------------------------------------------ */
/* Path scanning                                                      */
/* ------------------------------------------------------------------ */

static Py_ssize_t
scan_segments(const char *path, Py_ssize_t path_len, DSeg *out)
{
    const char *p = path + 1;
    const char *end = path + path_len;
    Py_ssize_t count = 0;
    const char *start = p;
    for (;; p++) {
        if (p == end || *p == '/') {
            if (count == MAX_SEGMENTS) {
                return -1;
            }
            out[count].bytes = (char *)start;
            out[count].len = p - start;
            count++;
            if (p == end) {
                return count;
            }
            start = p + 1;
        }
    }
}

/* ------------------------------------------------------------------ */
/* compile()                                                          */
/* ------------------------------------------------------------------ */

/* Build one method+nseg group's tree from the collected candidate pointers. */
static int
install_group(PyObject *trees, PyObject *method, int nseg, DRoute **cands,
              Py_ssize_t ncand, char *used)
{
    memset(used, 0, MAX_SEGMENTS);
    DNode *root = dnode_build(cands, ncand, used, nseg);
    if (root == NULL) {
        return -1;
    }
    PyObject *by_count = PyDict_GetItemWithError(trees, method);
    if (by_count == NULL) {
        if (PyErr_Occurred()) {
            dnode_free(root);
            return -1;
        }
        by_count = PyDict_New();
        if (by_count == NULL || PyDict_SetItem(trees, method, by_count) < 0) {
            Py_XDECREF(by_count);
            dnode_free(root);
            return -1;
        }
        Py_DECREF(by_count);
    }
    PyObject *nseg_obj = PyLong_FromLong(nseg);
    PyObject *ref = nseg_obj ? dnoderef_new(root) : NULL;
    if (ref == NULL) {
        Py_XDECREF(nseg_obj);
        dnode_free(root);
        return -1;
    }
    int rc = PyDict_SetItem(by_count, nseg_obj, ref);
    Py_DECREF(nseg_obj);
    Py_DECREF(ref);
    return rc;
}

static int
drt_compile(DecisionRouteTable *self)
{
    PyObject *trees = PyDict_New();
    if (trees == NULL) {
        return -1;
    }
    char *used = PyMem_Malloc(MAX_SEGMENTS);
    DRoute **group = PyMem_Malloc((size_t)(self->nroutes + 1) * sizeof(DRoute *));
    char *done = PyMem_Calloc((size_t)self->nroutes + 1, 1);
    if (used == NULL || group == NULL || done == NULL) {
        PyErr_NoMemory();
        goto fail;
    }

    /* Each unprocessed route seeds a (method, nseg) group; gather all peers. */
    for (Py_ssize_t i = 0; i < self->nroutes; i++) {
        if (done[i]) {
            continue;
        }
        DRoute *seed = &self->routes[i];
        Py_ssize_t count = 0;
        for (Py_ssize_t j = i; j < self->nroutes; j++) {
            DRoute *other = &self->routes[j];
            if (!done[j] && other->nseg == seed->nseg &&
                PyObject_RichCompareBool(other->method, seed->method, Py_EQ) == 1) {
                group[count++] = other;
                done[j] = 1;
            }
        }
        if (install_group(trees, seed->method, seed->nseg, group, count, used) < 0) {
            goto fail;
        }
    }

    PyMem_Free(used);
    PyMem_Free(group);
    PyMem_Free(done);
    Py_XSETREF(self->trees, trees);
    self->dirty = 0;
    return 0;

fail:
    PyMem_Free(used);
    PyMem_Free(group);
    PyMem_Free(done);
    Py_DECREF(trees);
    return -1;
}

/* ------------------------------------------------------------------ */
/* add()                                                              */
/* ------------------------------------------------------------------ */

static PyObject *public_access_clauses = NULL;


static PyObject *
get_public_access_clauses(void)
{
    if (public_access_clauses == NULL) {
        PyObject *zero = PyLong_FromLong(0);
        if (zero == NULL) {
            return NULL;
        }
        public_access_clauses = PyTuple_Pack(1, zero);
        Py_DECREF(zero);
    }
    return public_access_clauses;
}


static int
validate_access_clauses(PyObject *clauses)
{
    if (!PyTuple_Check(clauses) || PyTuple_GET_SIZE(clauses) == 0) {
        PyErr_SetString(
            PyExc_ValueError,
            "access clauses must be a non-empty tuple of non-negative integers"
        );
        return -1;
    }
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(clauses); i++) {
        PyObject *item = PyTuple_GET_ITEM(clauses, i);
        PyObject *zero;
        int negative;
        if (!PyLong_CheckExact(item)) {
            PyErr_SetString(
                PyExc_ValueError,
                "access clauses must be a non-empty tuple of non-negative integers"
            );
            return -1;
        }
        zero = PyLong_FromLong(0);
        if (zero == NULL) {
            return -1;
        }
        negative = PyObject_RichCompareBool(item, zero, Py_LT);
        Py_DECREF(zero);
        if (negative != 0) {
            if (negative > 0) {
                PyErr_SetString(
                    PyExc_ValueError,
                    "access clauses must be a non-empty tuple of non-negative integers"
                );
            }
            return -1;
        }
    }
    return 0;
}


static PyObject *
drt_add(DecisionRouteTable *self, PyObject *args)
{
    PyObject *path_obj, *method_obj, *handler;
    PyObject *access_clauses = Py_None;
    if (!PyArg_ParseTuple(
            args, "UUO|O:add", &path_obj, &method_obj, &handler, &access_clauses)) {
        return NULL;
    }
    if (access_clauses == Py_None) {
        access_clauses = get_public_access_clauses();
        if (access_clauses == NULL) {
            return NULL;
        }
    }
    if (validate_access_clauses(access_clauses) < 0) {
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

    /* Static routes: hash-table fast path, no tree involvement. */
    if (memchr(path, '{', (size_t)path_len) == NULL) {
        PyObject *by_path = PyDict_GetItemWithError(self->static_routes, method_obj);
        if (by_path == NULL) {
            if (PyErr_Occurred()) {
                return NULL;
            }
            by_path = PyDict_New();
            if (by_path == NULL ||
                PyDict_SetItem(self->static_routes, method_obj, by_path) < 0) {
                Py_XDECREF(by_path);
                return NULL;
            }
            Py_DECREF(by_path);
        }
        int exists = PyDict_Contains(by_path, path_obj);
        if (exists < 0) {
            return NULL;
        }
        if (exists) {
            PyErr_Format(PyExc_ValueError, "duplicate route: %U %U", method_obj, path_obj);
            return NULL;
        }
        /* The match result never varies for a static route, so build it once
         * here and let match() return the shared tuple allocation-free. */
        PyObject *match_result = PyTuple_Pack(2, handler, Py_None);
        PyObject *entry = NULL;
        if (match_result != NULL) {
            entry = PyTuple_Pack(2, match_result, access_clauses);
        }
        Py_XDECREF(match_result);
        if (entry == NULL) {
            return NULL;
        }
        int rc = PyDict_SetItem(by_path, path_obj, entry);
        Py_DECREF(entry);
        if (rc < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }

    DSeg segs[MAX_SEGMENTS];
    Py_ssize_t nseg = scan_segments(path, path_len, segs);
    if (nseg < 0) {
        PyErr_SetString(PyExc_ValueError, "route path has too many segments");
        return NULL;
    }

    PyObject *signature = PyTuple_New(nseg);
    PyObject *names = PyList_New(0);
    if (signature == NULL || names == NULL) {
        goto parse_fail;
    }
    int specificity = 0;
    for (Py_ssize_t i = 0; i < nseg; i++) {
        const char *seg = segs[i].bytes;
        Py_ssize_t len = segs[i].len;
        int is_param = (len >= 2 && seg[0] == '{' && seg[len - 1] == '}');
        if (is_param) {
            Py_ssize_t name_len = len - 2;
            if (name_len == 0 || memchr(seg + 1, '{', (size_t)name_len) ||
                memchr(seg + 1, '}', (size_t)name_len)) {
                PyErr_Format(PyExc_ValueError, "invalid path parameter: '%.200s'", seg);
                goto parse_fail;
            }
            PyObject *name = PyUnicode_DecodeUTF8(seg + 1, name_len, NULL);
            if (name == NULL || PyList_Append(names, name) < 0) {
                Py_XDECREF(name);
                goto parse_fail;
            }
            Py_DECREF(name);
            PyTuple_SET_ITEM(signature, i, Py_NewRef(Py_None));
        }
        else if (memchr(seg, '{', (size_t)len) || memchr(seg, '}', (size_t)len)) {
            PyErr_SetString(PyExc_ValueError,
                            "path parameters must occupy an entire segment");
            goto parse_fail;
        }
        else {
            PyObject *literal = PyUnicode_FromStringAndSize(seg, len);
            if (literal == NULL) {
                goto parse_fail;
            }
            PyTuple_SET_ITEM(signature, i, literal);
            specificity++;
        }
    }

    PyObject *key = PyTuple_Pack(2, method_obj, signature);
    if (key == NULL) {
        goto parse_fail;
    }
    int conflict = PySet_Contains(self->seen, key);
    if (conflict != 0) {
        Py_DECREF(key);
        if (conflict > 0) {
            PyErr_Format(PyExc_ValueError, "conflicting route: %U %U", method_obj, path_obj);
        }
        goto parse_fail;
    }
    int added = PySet_Add(self->seen, key);
    Py_DECREF(key);
    if (added < 0) {
        goto parse_fail;
    }

    if (self->nroutes == self->routes_cap) {
        Py_ssize_t cap = self->routes_cap ? self->routes_cap * 2 : 8;
        DRoute *grown = PyMem_Realloc(self->routes, (size_t)cap * sizeof(DRoute));
        if (grown == NULL) {
            PyErr_NoMemory();
            goto parse_fail;
        }
        self->routes = grown;
        self->routes_cap = cap;
    }

    DSeg *stored = PyMem_Calloc((size_t)nseg, sizeof(DSeg));
    if (stored == NULL) {
        PyErr_NoMemory();
        goto parse_fail;
    }
    for (Py_ssize_t i = 0; i < nseg; i++) {
        if (PyTuple_GET_ITEM(signature, i) == Py_None) {
            continue; /* parameter slot: bytes stays NULL (calloc) */
        }
        char *copy = PyMem_Malloc(segs[i].len ? (size_t)segs[i].len : 1);
        if (copy == NULL) {
            for (Py_ssize_t k = 0; k < i; k++) {
                PyMem_Free(stored[k].bytes);
            }
            PyMem_Free(stored);
            PyErr_NoMemory();
            goto parse_fail;
        }
        memcpy(copy, segs[i].bytes, (size_t)segs[i].len);
        stored[i].bytes = copy;
        stored[i].len = segs[i].len;
    }

    PyObject *names_tuple = PyList_AsTuple(names);
    if (names_tuple == NULL) {
        for (Py_ssize_t i = 0; i < nseg; i++) {
            PyMem_Free(stored[i].bytes);
        }
        PyMem_Free(stored);
        goto parse_fail;
    }

    DRoute *route = &self->routes[self->nroutes];
    route->method = Py_NewRef(method_obj);
    route->segs = stored;
    route->nseg = (int)nseg;
    route->param_names = names_tuple;
    route->nparams = PyTuple_GET_SIZE(names_tuple);
    route->handler = Py_NewRef(handler);
    route->specificity = specificity;
    route->access_clauses = Py_NewRef(access_clauses);
    if (prepare_clause_words(route->access_clauses, &route->clause_words,
                             &route->clause_count, &route->always_eligible,
                             &route->all_native) < 0) {
        Py_CLEAR(route->access_clauses);
        Py_CLEAR(route->handler);
        Py_CLEAR(route->param_names);
        Py_CLEAR(route->method);
        PyMem_Free(route->segs);
        goto parse_fail;
    }
    self->nroutes++;
    self->dirty = 1;

    Py_DECREF(signature);
    Py_DECREF(names);
    Py_RETURN_NONE;

parse_fail:
    Py_XDECREF(signature);
    Py_XDECREF(names);
    return NULL;
}

/* ------------------------------------------------------------------ */
/* match()                                                            */
/* ------------------------------------------------------------------ */

/* Decoded segment strings are built on demand: only branch positions the
 * descent actually tests and parameter values that get captured pay for a
 * PyUnicode allocation. Returns a borrowed reference, NULL on error. */
static PyObject *
seg_obj(DSeg *segs, PyObject **seg_objs, int i)
{
    PyObject *obj = seg_objs[i];
    if (obj == NULL) {
        obj = seg_objs[i] = PyUnicode_FromStringAndSize(segs[i].bytes, segs[i].len);
    }
    return obj;
}

static PyObject *
verify_leaf(DNode *leaf, DSeg *segs, PyObject **seg_objs, PyObject *caller_mask,
            unsigned long long caller_word, int caller_native)
{
    for (Py_ssize_t c = 0; c < leaf->ncand; c++) {
        DRoute *route = leaf->candidates[c];
        int eligible = ROUTE_ELIGIBLE(route, caller_word, caller_native, caller_mask);
        if (eligible < 0) {
            return NULL;
        }
        if (!eligible) {
            continue;
        }
        int ok = 1;
        for (int p = 0; p < route->nseg; p++) {
            const DSeg *want = &route->segs[p];
            if (want->bytes != NULL &&
                !(want->len == segs[p].len &&
                  memcmp(want->bytes, segs[p].bytes, (size_t)want->len) == 0)) {
                ok = 0;
                break;
            }
        }
        if (!ok) {
            continue;
        }
        PyObject *params = PyDict_New();
        if (params == NULL) {
            return NULL;
        }
        Py_ssize_t k = 0;
        for (int p = 0; p < route->nseg; p++) {
            if (route->segs[p].bytes == NULL) {
                PyObject *value = seg_obj(segs, seg_objs, p);
                if (value == NULL ||
                    PyDict_SetItem(params, PyTuple_GET_ITEM(route->param_names, k),
                                   value) < 0) {
                    Py_DECREF(params);
                    return NULL;
                }
                k++;
            }
        }
        PyObject *result = PyTuple_Pack(2, route->handler, params);
        Py_DECREF(params);
        return result; /* NULL propagates as error */
    }
    Py_RETURN_NONE;
}

/* Returns a new match tuple, Py_None (borrowed via Py_RETURN_NONE semantics
 * handled by caller), or NULL on error. seg_objs are decoded segment strings. */
static PyObject *
match_group(DecisionRouteTable *self, PyObject *method, DSeg *segs, Py_ssize_t nseg,
            PyObject **seg_objs, PyObject *caller_mask)
{
    PyObject *by_count = PyDict_GetItemWithError(self->trees, method);
    if (by_count == NULL) {
        return NULL; /* caller checks PyErr_Occurred to distinguish miss */
    }
    PyObject *nseg_obj = PyLong_FromSsize_t(nseg);
    if (nseg_obj == NULL) {
        return NULL;
    }
    PyObject *capsule = PyDict_GetItemWithError(by_count, nseg_obj);
    Py_DECREF(nseg_obj);
    if (capsule == NULL) {
        return NULL;
    }
    DNode *node = DNODE_OF(capsule);
    if (node == NULL) {
        return NULL;
    }
    /* The caller mask is constant for this descent: convert it once rather than
     * per node. The conversion itself is cheap, but the PyErr_Occurred() that
     * has to follow it reads the thread state through TLS on every node. */
    unsigned long long caller_word = PyLong_AsUnsignedLongLong(caller_mask);
    int caller_native = !PyErr_Occurred();
    if (!caller_native) {
        PyErr_Clear();
    }

    int eligible = NODE_ELIGIBLE(node, caller_word, caller_native, caller_mask);
    if (eligible <= 0) {
        return NULL;
    }

    while (!node->is_leaf) {
        PyObject *key = seg_obj(segs, seg_objs, node->position);
        if (key == NULL) {
            return NULL;
        }
        PyObject *branch = PyDict_GetItemWithError(node->branches, key);
        if (branch != NULL) {
            node = DNODE_OF(branch);
            if (node == NULL) {
                return NULL;
            }
        }
        else {
            if (PyErr_Occurred()) {
                return NULL;
            }
            if (node->wildcard == NULL) {
                return NULL; /* clean miss */
            }
            node = node->wildcard;
        }
        eligible = NODE_ELIGIBLE(node, caller_word, caller_native, caller_mask);
        if (eligible <= 0) {
            return NULL;
        }
    }
    return verify_leaf(node, segs, seg_objs, caller_mask, caller_word,
                       caller_native);
}

static PyObject *
drt_match(DecisionRouteTable *self, PyObject *args)
{
    PyObject *method_obj, *path_obj;
    PyObject *caller_mask = NULL;
    if (!PyArg_ParseTuple(args, "UU|O:match", &method_obj, &path_obj, &caller_mask)) {
        return NULL;
    }
    if (caller_mask == NULL) {
        static PyObject *public_caller_mask = NULL;
        if (public_caller_mask == NULL) {
            public_caller_mask = PyLong_FromLong(0);
            if (public_caller_mask == NULL) {
                return NULL;
            }
        }
        caller_mask = public_caller_mask;
    }
    if (!PyLong_CheckExact(caller_mask)) {
        PyErr_SetString(PyExc_ValueError, "caller capability mask must be a non-negative integer");
        return NULL;
    }

    /* Static hash fast path: the stored value is the precomputed
     * (handler, None) result tuple, so a hit allocates nothing. */
    PyObject *by_path = PyDict_GetItemWithError(self->static_routes, method_obj);
    if (by_path == NULL && PyErr_Occurred()) {
        return NULL;
    }
    if (by_path != NULL) {
        PyObject *entry = PyDict_GetItemWithError(by_path, path_obj);
        if (entry != NULL) {
            int eligible = access_eligible(PyTuple_GET_ITEM(entry, 1), caller_mask);
            if (eligible > 0) {
                return Py_NewRef(PyTuple_GET_ITEM(entry, 0));
            }
            if (eligible < 0) {
                return NULL;
            }
        }
        if (PyErr_Occurred()) {
            return NULL;
        }
    }
    int is_head = PyUnicode_CompareWithASCIIString(method_obj, "HEAD") == 0;
    static PyObject *get_method = NULL;
    if (is_head) {
        if (get_method == NULL) {
            get_method = PyUnicode_InternFromString("GET");
            if (get_method == NULL) {
                return NULL;
            }
        }
        PyObject *get_paths = PyDict_GetItemWithError(self->static_routes, get_method);
        if (get_paths == NULL && PyErr_Occurred()) {
            return NULL;
        }
        if (get_paths != NULL) {
            PyObject *entry = PyDict_GetItemWithError(get_paths, path_obj);
            if (entry != NULL) {
                int eligible = access_eligible(PyTuple_GET_ITEM(entry, 1), caller_mask);
                if (eligible > 0) {
                    return Py_NewRef(PyTuple_GET_ITEM(entry, 0));
                }
                if (eligible < 0) {
                    return NULL;
                }
            }
            if (PyErr_Occurred()) {
                return NULL;
            }
        }
    }

    if (self->dirty && drt_compile(self) < 0) {
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
    DSeg segs[MAX_SEGMENTS];
    Py_ssize_t nseg = scan_segments(path, path_len, segs);
    if (nseg < 0) {
        Py_RETURN_NONE;
    }

    /* Segment strings are decoded lazily by seg_obj(); a decoded segment is
     * reused as both branch key and captured parameter value. */
    PyObject *seg_objs[MAX_SEGMENTS];
    memset(seg_objs, 0, (size_t)nseg * sizeof(PyObject *));

    PyObject *result = match_group(
        self, method_obj, segs, nseg, seg_objs, caller_mask
    );
    if (is_head && !PyErr_Occurred() && (result == NULL || result == Py_None)) {
        /* A leaf that fails verification reports Py_None; like a missing
         * group (NULL), HEAD must still fall back to the GET tree. */
        Py_XDECREF(result);
        result = match_group(self, get_method, segs, nseg, seg_objs, caller_mask);
    }

    for (Py_ssize_t k = 0; k < nseg; k++) {
        Py_XDECREF(seg_objs[k]);
    }
    if (result == NULL) {
        if (PyErr_Occurred()) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    return result;
}

/* Build all verified candidates at the reached leaf without applying access
 * clauses. The first public candidate is returned immediately; protected
 * candidates are retained as (match_result, access_clauses) ticket entries. */
/* Build the (classification, payload) pair directly rather than through
 * Py_BuildValue, whose format string is parsed on every call. Steals `payload`.
 * The classification codes are small ints, so PyLong_FromLong hands back a
 * cached object. */
static PyObject *
classification_result(long code, PyObject *payload)
{
    PyObject *code_obj = PyLong_FromLong(code);
    if (code_obj == NULL) {
        Py_DECREF(payload);
        return NULL;
    }
    PyObject *result = PyTuple_New(2);
    if (result == NULL) {
        Py_DECREF(code_obj);
        Py_DECREF(payload);
        return NULL;
    }
    PyTuple_SET_ITEM(result, 0, code_obj);
    PyTuple_SET_ITEM(result, 1, payload);
    return result;
}

/* Append a protected candidate, creating the ticket on first use. A public
 * route never records one, and public is the common case, so allocating the
 * list up front spent a list allocation plus a GC-tracked deallocation on every
 * public request. */
static int
ticket_append(PyObject **ticket, PyObject *entry)
{
    if (*ticket == NULL) {
        *ticket = PyList_New(0);
        if (*ticket == NULL) {
            return -1;
        }
    }
    return PyList_Append(*ticket, entry);
}

static int
classify_group(DecisionRouteTable *self, PyObject *method, DSeg *segs,
               Py_ssize_t nseg, PyObject **seg_objs, PyObject *zero,
               PyObject **ticket, PyObject **public_match)
{
    PyObject *by_count = PyDict_GetItemWithError(self->trees, method);
    if (by_count == NULL) {
        return PyErr_Occurred() ? -1 : 0;
    }
    PyObject *nseg_obj = PyLong_FromSsize_t(nseg);
    if (nseg_obj == NULL) {
        return -1;
    }
    PyObject *capsule = PyDict_GetItemWithError(by_count, nseg_obj);
    Py_DECREF(nseg_obj);
    if (capsule == NULL) {
        return PyErr_Occurred() ? -1 : 0;
    }
    DNode *node = DNODE_OF(capsule);
    if (node == NULL) {
        return -1;
    }
    while (!node->is_leaf) {
        PyObject *key = seg_obj(segs, seg_objs, node->position);
        if (key == NULL) {
            return -1;
        }
        PyObject *branch = PyDict_GetItemWithError(node->branches, key);
        if (branch != NULL) {
            node = DNODE_OF(branch);
            if (node == NULL) {
                return -1;
            }
        }
        else {
            if (PyErr_Occurred()) {
                return -1;
            }
            if (node->wildcard == NULL) {
                return 0;
            }
            node = node->wildcard;
        }
    }

    for (Py_ssize_t c = 0; c < node->ncand; c++) {
        DRoute *route = node->candidates[c];
        int ok = 1;
        for (int p = 0; p < route->nseg; p++) {
            const DSeg *want = &route->segs[p];
            if (want->bytes != NULL &&
                !(want->len == segs[p].len &&
                  memcmp(want->bytes, segs[p].bytes, (size_t)want->len) == 0)) {
                ok = 0;
                break;
            }
        }
        if (!ok) {
            continue;
        }
        PyObject *params = PyDict_New();
        if (params == NULL) {
            return -1;
        }
        Py_ssize_t k = 0;
        for (int p = 0; p < route->nseg; p++) {
            if (route->segs[p].bytes == NULL) {
                PyObject *value = seg_obj(segs, seg_objs, p);
                if (value == NULL ||
                    PyDict_SetItem(params, PyTuple_GET_ITEM(route->param_names, k), value) < 0) {
                    Py_DECREF(params);
                    return -1;
                }
                k++;
            }
        }
        PyObject *match_result = PyTuple_Pack(2, route->handler, params);
        Py_DECREF(params);
        if (match_result == NULL) {
            return -1;
        }
        int is_public = access_eligible(route->access_clauses, zero);
        if (is_public < 0) {
            Py_DECREF(match_result);
            return -1;
        }
        if (is_public) {
            *public_match = match_result;
            return 1;
        }
        PyObject *entry = PyTuple_Pack(2, match_result, route->access_clauses);
        Py_DECREF(match_result);
        if (entry == NULL || ticket_append(ticket, entry) < 0) {
            Py_XDECREF(entry);
            return -1;
        }
        Py_DECREF(entry);
    }
    return 0;
}

static int
classify_method(DecisionRouteTable *self, PyObject *method, PyObject *path_obj,
                DSeg *segs, Py_ssize_t nseg, PyObject **seg_objs,
                PyObject *zero, PyObject **ticket, PyObject **public_match)
{
    PyObject *by_path = PyDict_GetItemWithError(self->static_routes, method);
    if (by_path != NULL) {
        PyObject *entry = PyDict_GetItemWithError(by_path, path_obj);
        if (entry != NULL) {
            PyObject *match_result = PyTuple_GET_ITEM(entry, 0);
            PyObject *clauses = PyTuple_GET_ITEM(entry, 1);
            int is_public = access_eligible(clauses, zero);
            if (is_public < 0) {
                return -1;
            }
            if (is_public) {
                Py_INCREF(match_result);
                *public_match = match_result;
                return 1;
            }
            PyObject *ticket_entry = PyTuple_Pack(2, match_result, clauses);
            if (ticket_entry == NULL || ticket_append(ticket, ticket_entry) < 0) {
                Py_XDECREF(ticket_entry);
                return -1;
            }
            Py_DECREF(ticket_entry);
        }
        else if (PyErr_Occurred()) {
            return -1;
        }
    }
    else if (PyErr_Occurred()) {
        return -1;
    }
    return classify_group(
        self, method, segs, nseg, seg_objs, zero, ticket, public_match
    );
}

static PyObject *
drt_classify(DecisionRouteTable *self, PyObject *args)
{
    PyObject *method_obj, *path_obj;
    if (!PyArg_ParseTuple(args, "UU:classify", &method_obj, &path_obj)) {
        return NULL;
    }
    if (self->dirty && drt_compile(self) < 0) {
        return NULL;
    }
    Py_ssize_t path_len;
    const char *path = PyUnicode_AsUTF8AndSize(path_obj, &path_len);
    if (path == NULL) {
        return NULL;
    }
    if (path_len == 0 || path[0] != '/') {
        return classification_result(0, Py_NewRef(Py_None));
    }
    DSeg segs[MAX_SEGMENTS];
    Py_ssize_t nseg = scan_segments(path, path_len, segs);
    if (nseg < 0) {
        return classification_result(0, Py_NewRef(Py_None));
    }
    PyObject *seg_objs[MAX_SEGMENTS];
    memset(seg_objs, 0, (size_t)nseg * sizeof(PyObject *));
    /* Created only if a protected candidate is found; see ticket_append. */
    PyObject *ticket = NULL;
    PyObject *zero = PyLong_FromLong(0);
    PyObject *public_match = NULL;
    if (zero == NULL) {
        return NULL;
    }

    int rc = classify_method(
        self, method_obj, path_obj, segs, nseg, seg_objs,
        zero, &ticket, &public_match
    );
    PyObject *get_method = NULL;
    if (rc == 0 && PyUnicode_CompareWithASCIIString(method_obj, "HEAD") == 0) {
        get_method = PyUnicode_InternFromString("GET");
        if (get_method == NULL) {
            rc = -1;
        }
        else {
            rc = classify_method(
                self, get_method, path_obj, segs, nseg, seg_objs,
                zero, &ticket, &public_match
            );
        }
    }
    Py_XDECREF(get_method);
    Py_DECREF(zero);
    for (Py_ssize_t k = 0; k < nseg; k++) {
        Py_XDECREF(seg_objs[k]);
    }
    if (rc < 0) {
        Py_XDECREF(public_match);
        Py_XDECREF(ticket);
        return NULL;
    }
    if (public_match != NULL) {
        Py_XDECREF(ticket);
        return classification_result(1, public_match);
    }
    if (ticket == NULL) {
        return classification_result(0, Py_NewRef(Py_None));
    }
    PyObject *ticket_tuple = PyList_AsTuple(ticket);
    Py_DECREF(ticket);
    if (ticket_tuple == NULL) {
        return NULL;
    }
    return classification_result(2, ticket_tuple);
}

static PyObject *
drt_resolve(DecisionRouteTable *self, PyObject *args)
{
    (void)self;
    PyObject *ticket, *caller_mask;
    if (!PyArg_ParseTuple(args, "O!O:resolve", &PyTuple_Type, &ticket, &caller_mask)) {
        return NULL;
    }
    if (!PyLong_CheckExact(caller_mask)) {
        PyErr_SetString(PyExc_ValueError, "caller capability mask must be a non-negative integer");
        return NULL;
    }
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(ticket); i++) {
        PyObject *entry = PyTuple_GET_ITEM(ticket, i);
        PyObject *match_result = PyTuple_GET_ITEM(entry, 0);
        PyObject *clauses = PyTuple_GET_ITEM(entry, 1);
        int eligible = access_eligible(clauses, caller_mask);
        if (eligible < 0) {
            return NULL;
        }
        if (eligible) {
            return Py_NewRef(match_result);
        }
    }
    Py_RETURN_NONE;
}

/* Compatibility probe retaining the original protected-handler-hiding API. */
static PyObject *
drt_probe(DecisionRouteTable *self, PyObject *args)
{
    PyObject *method_obj, *path_obj, *all_mask;
    if (!PyArg_ParseTuple(args, "UUO:probe", &method_obj, &path_obj, &all_mask)) {
        return NULL;
    }

    PyObject *public_args = PyTuple_Pack(2, method_obj, path_obj);
    if (public_args == NULL) {
        return NULL;
    }
    PyObject *public_match = drt_match(self, public_args);
    Py_DECREF(public_args);
    if (public_match == NULL) {
        return NULL;
    }
    if (public_match != Py_None) {
        PyObject *status = PyLong_FromLong(1);
        PyObject *result = status ? PyTuple_Pack(2, status, public_match) : NULL;
        Py_XDECREF(status);
        Py_DECREF(public_match);
        return result;
    }
    Py_DECREF(public_match);

    PyObject *protected_args = PyTuple_Pack(3, method_obj, path_obj, all_mask);
    if (protected_args == NULL) {
        return NULL;
    }
    PyObject *protected_match = drt_match(self, protected_args);
    Py_DECREF(protected_args);
    if (protected_match == NULL) {
        return NULL;
    }
    int classification = protected_match == Py_None ? 0 : 2;
    Py_DECREF(protected_match);

    PyObject *status = PyLong_FromLong(classification);
    PyObject *result = status ? PyTuple_Pack(2, status, Py_None) : NULL;
    Py_XDECREF(status);
    return result;
}

/* ------------------------------------------------------------------ */
/* Type plumbing                                                      */
/* ------------------------------------------------------------------ */

static PyObject *
drt_new(PyTypeObject *type, PyObject *Py_UNUSED(a), PyObject *Py_UNUSED(k))
{
    DecisionRouteTable *self = (DecisionRouteTable *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->static_routes = PyDict_New();
    self->seen = PySet_New(NULL);
    self->trees = PyDict_New();
    if (self->static_routes == NULL || self->seen == NULL || self->trees == NULL) {
        Py_DECREF(self);
        return NULL;
    }
    return (PyObject *)self;
}

static void
drt_dealloc(DecisionRouteTable *self)
{
    Py_XDECREF(self->trees); /* frees all DNodes via capsule destructors */
    Py_XDECREF(self->static_routes);
    Py_XDECREF(self->seen);
    for (Py_ssize_t i = 0; i < self->nroutes; i++) {
        DRoute *route = &self->routes[i];
        for (int s = 0; s < route->nseg; s++) {
            PyMem_Free(route->segs[s].bytes);
        }
        PyMem_Free(route->segs);
        Py_XDECREF(route->method);
        Py_XDECREF(route->param_names);
        Py_XDECREF(route->handler);
        Py_XDECREF(route->access_clauses);
        PyMem_Free(route->clause_words);
    }
    PyMem_Free(self->routes);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyMethodDef drt_methods[] = {
    {"add", (PyCFunction)drt_add, METH_VARARGS,
     "add(path, method, handler, access_clauses=(0,))\n"
     "Register a route with disjunctive capability masks."},
    {"match", (PyCFunction)drt_match, METH_VARARGS,
     "match(method, path, caller_mask=0) -> (handler, params | None) | None"},
    {"classify", (PyCFunction)drt_classify, METH_VARARGS,
     "classify(method, path) -> (classification, public_match | ticket | None)"},
    {"resolve", (PyCFunction)drt_resolve, METH_VARARGS,
     "resolve(ticket, caller_mask) -> (handler, params | None) | None"},
    {"probe", (PyCFunction)drt_probe, METH_VARARGS,
     "probe(method, path, all_capability_mask) -> (classification, public_match | None)"},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject DecisionRouteTableType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._core.DecisionRouteTable",
    .tp_basicsize = sizeof(DecisionRouteTable),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "Decision-tree route table with a static hash fast path.",
    .tp_new = drt_new,
    .tp_dealloc = (destructor)drt_dealloc,
    .tp_methods = drt_methods,
};

int
wreath_register_dtrouter(PyObject *module)
{
    if (PyType_Ready(&DecisionRouteTableType) < 0) {
        return -1;
    }
    if (DNodeRefType == NULL) {
        /* Internal: held only by this module's branch dicts, never exposed. */
        DNodeRefType = (PyTypeObject *)PyType_FromSpec(&dnoderef_spec);
        if (DNodeRefType == NULL) {
            return -1;
        }
    }
    return PyModule_AddObjectRef(module, "DecisionRouteTable",
                                 (PyObject *)&DecisionRouteTableType);
}
