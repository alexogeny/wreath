/* Native ORM query cache-key builder.
 *
 * Mirrors wreath.orm.compiler.shape_of / _shape_expression / _shape_loads exactly:
 * it produces the same bytes (pieces joined by 0x1e), so walked and native keys
 * are interchangeable in the compiled-SQL cache. The win over the Python code is
 * eliminating the per-node Python recursion frame and building the key straight
 * into one growable buffer instead of a list of bytes objects joined at the end.
 *
 * C dispatches on node type by exact-type pointer comparison against the
 * Expression classes, which wreath.orm.compiler hands over once via
 * orm_shape_configure(); it never imports wreath.orm itself (that would be a
 * cycle). Attribute names are interned once at configure time.
 */

#include "wreathcore.h"

/* Expression type objects, owned references set by orm_shape_configure. */
static PyObject *T_Column = NULL;
static PyObject *T_Related = NULL;
static PyObject *T_Value = NULL;
static PyObject *T_Binary = NULL;
static PyObject *T_In = NULL;
static PyObject *T_InSubquery = NULL;
static PyObject *T_Boolean = NULL;
static PyObject *T_Unary = NULL;
static PyObject *ORMError = NULL;

/* Interned attribute names. */
static PyObject *A_column, *A_shape_ref, *A_shape_projection, *A_shape_value;
static PyObject *A_pg_type, *A_operator, *A_left, *A_right, *A_values;
static PyObject *A_operands, *A_operand, *A_path, *A_expression, *A_direction;
static PyObject *A_projection, *A_predicates, *A_orderings, *A_includes;
static PyObject *A_select;
static PyObject *A_model, *A_for_update, *A_limit, *A_offset, *A_fingerprint;
static PyObject *A_relationship, *A_strategy, *A_nested, *A_python_name, *A_qualname;

typedef struct {
    char *data;
    Py_ssize_t len;
    Py_ssize_t cap;
    int first;
} Buf;

static int
buf_ensure(Buf *b, Py_ssize_t extra)
{
    if (b->len + extra <= b->cap) {
        return 0;
    }
    Py_ssize_t newcap = b->cap ? b->cap : 256;
    while (newcap < b->len + extra) {
        newcap *= 2;
    }
    char *grown = PyMem_Realloc(b->data, (size_t)newcap);
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    b->data = grown;
    b->cap = newcap;
    return 0;
}

static int
buf_raw(Buf *b, const char *src, Py_ssize_t n)
{
    if (buf_ensure(b, n) < 0) {
        return -1;
    }
    memcpy(b->data + b->len, src, (size_t)n);
    b->len += n;
    return 0;
}

/* Start one 0x1e-separated piece, matching `b"\x1e".join(out)`. */
static int
buf_sep(Buf *b)
{
    if (b->first) {
        b->first = 0;
        return 0;
    }
    return buf_raw(b, "\x1e", 1);
}

static int
buf_bytes_attr(Buf *b, PyObject *obj, PyObject *name)
{
    PyObject *value = PyObject_GetAttr(obj, name);
    if (value == NULL) {
        return -1;
    }
    if (!PyBytes_Check(value)) {
        PyErr_Format(PyExc_TypeError, "shape attribute %S is not bytes", name);
        Py_DECREF(value);
        return -1;
    }
    int rc = buf_raw(b, PyBytes_AS_STRING(value), PyBytes_GET_SIZE(value));
    Py_DECREF(value);
    return rc;
}

/* Append a str attribute's UTF-8, like `operator.encode("ascii")`. */
static int
buf_str_attr(Buf *b, PyObject *obj, PyObject *name)
{
    PyObject *value = PyObject_GetAttr(obj, name);
    if (value == NULL) {
        return -1;
    }
    Py_ssize_t size;
    const char *utf8 = PyUnicode_AsUTF8AndSize(value, &size);
    if (utf8 == NULL) {
        Py_DECREF(value);
        return -1;
    }
    int rc = buf_raw(b, utf8, size);
    Py_DECREF(value);
    return rc;
}

static int
buf_ssize(Buf *b, Py_ssize_t n)
{
    char digits[24];
    int written = PyOS_snprintf(digits, sizeof(digits), "%zd", n);
    return buf_raw(b, digits, written);
}

static int shape_expr(Buf *b, PyObject *node);

static int
shape_sequence(Buf *b, PyObject *node, PyObject *attr)
{
    PyObject *seq = PyObject_GetAttr(node, attr);
    if (seq == NULL) {
        return -1;
    }
    PyObject *fast = PySequence_Fast(seq, "expected a sequence");
    Py_DECREF(seq);
    if (fast == NULL) {
        return -1;
    }
    Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);
    for (Py_ssize_t i = 0; i < count; i++) {
        if (shape_expr(b, PySequence_Fast_GET_ITEM(fast, i)) < 0) {
            Py_DECREF(fast);
            return -1;
        }
    }
    Py_DECREF(fast);
    return 0;
}

static int
shape_expr(Buf *b, PyObject *node)
{
    PyObject *type = (PyObject *)Py_TYPE(node);

    if (type == T_Related) {
        if (buf_sep(b) < 0 || buf_raw(b, "j", 1) < 0) {
            return -1;
        }
        PyObject *path = PyObject_GetAttr(node, A_path);
        if (path == NULL) {
            return -1;
        }
        PyObject *fast = PySequence_Fast(path, "path is not a sequence");
        Py_DECREF(path);
        if (fast == NULL) {
            return -1;
        }
        Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);
        for (Py_ssize_t i = 0; i < count; i++) {
            if (buf_sep(b) < 0
                || buf_bytes_attr(b, PySequence_Fast_GET_ITEM(fast, i), A_shape_ref) < 0) {
                Py_DECREF(fast);
                return -1;
            }
        }
        Py_DECREF(fast);
        PyObject *column = PyObject_GetAttr(node, A_column);
        if (column == NULL) {
            return -1;
        }
        int rc = buf_sep(b) < 0 || buf_bytes_attr(b, column, A_shape_ref) < 0 ? -1 : 0;
        Py_DECREF(column);
        return rc;
    }
    if (type == T_Column) {
        PyObject *column = PyObject_GetAttr(node, A_column);
        if (column == NULL) {
            return -1;
        }
        int rc = buf_sep(b) < 0 || buf_bytes_attr(b, column, A_shape_ref) < 0 ? -1 : 0;
        Py_DECREF(column);
        return rc;
    }
    if (type == T_Value) {
        PyObject *pg_type = PyObject_GetAttr(node, A_pg_type);
        if (pg_type == NULL) {
            return -1;
        }
        int rc = buf_sep(b) < 0 || buf_bytes_attr(b, pg_type, A_shape_value) < 0 ? -1 : 0;
        Py_DECREF(pg_type);
        return rc;
    }
    if (type == T_Binary) {
        if (buf_sep(b) < 0 || buf_raw(b, "b", 1) < 0 || buf_str_attr(b, node, A_operator) < 0) {
            return -1;
        }
        PyObject *left = PyObject_GetAttr(node, A_left);
        if (left == NULL) {
            return -1;
        }
        int rc = shape_expr(b, left);
        Py_DECREF(left);
        if (rc < 0) {
            return -1;
        }
        PyObject *right = PyObject_GetAttr(node, A_right);
        if (right == NULL) {
            return -1;
        }
        rc = shape_expr(b, right);
        Py_DECREF(right);
        return rc;
    }
    if (type == T_In) {
        PyObject *values = PyObject_GetAttr(node, A_values);
        if (values == NULL) {
            return -1;
        }
        Py_ssize_t count = PySequence_Size(values);
        if (count < 0
            || buf_sep(b) < 0 || buf_raw(b, "i", 1) < 0
            || buf_str_attr(b, node, A_operator) < 0 || buf_ssize(b, count) < 0) {
            Py_DECREF(values);
            return -1;
        }
        PyObject *left = PyObject_GetAttr(node, A_left);
        if (left == NULL) {
            Py_DECREF(values);
            return -1;
        }
        int rc = shape_expr(b, left);
        Py_DECREF(left);
        if (rc == 0) {
            PyObject *fast = PySequence_Fast(values, "values is not a sequence");
            if (fast == NULL) {
                rc = -1;
            }
            else {
                Py_ssize_t n = PySequence_Fast_GET_SIZE(fast);
                for (Py_ssize_t i = 0; i < n; i++) {
                    if (shape_expr(b, PySequence_Fast_GET_ITEM(fast, i)) < 0) {
                        rc = -1;
                        break;
                    }
                }
                Py_DECREF(fast);
            }
        }
        Py_DECREF(values);
        return rc;
    }
    if (type == T_Boolean) {
        PyObject *operands = PyObject_GetAttr(node, A_operands);
        if (operands == NULL) {
            return -1;
        }
        Py_ssize_t count = PySequence_Size(operands);
        if (count < 0
            || buf_sep(b) < 0 || buf_raw(b, "l", 1) < 0
            || buf_str_attr(b, node, A_operator) < 0 || buf_ssize(b, count) < 0) {
            Py_DECREF(operands);
            return -1;
        }
        Py_DECREF(operands);
        return shape_sequence(b, node, A_operands);
    }
    if (type == T_Unary) {
        if (buf_sep(b) < 0 || buf_raw(b, "u", 1) < 0 || buf_str_attr(b, node, A_operator) < 0) {
            return -1;
        }
        PyObject *operand = PyObject_GetAttr(node, A_operand);
        if (operand == NULL) {
            return -1;
        }
        int rc = shape_expr(b, operand);
        Py_DECREF(operand);
        return rc;
    }

    if (type == T_InSubquery) {
        /* Mirrors compiler._shape_expression's InSubqueryExpr branch exactly:
         * everything that changes the subquery's SQL text, and nothing that
         * changes only its bound values. Two subqueries over the same table
         * filtering the same columns share a plan; two over different tables
         * must not, and this is the only place that distinguishes them.
         *
         *   "q" operator len(predicates) | left | model | projection | predicates
         */
        PyObject *select = PyObject_GetAttr(node, A_select);
        if (select == NULL) {
            return -1;
        }
        PyObject *predicates = PyObject_GetAttr(select, A_predicates);
        if (predicates == NULL) {
            Py_DECREF(select);
            return -1;
        }
        Py_ssize_t count = PySequence_Size(predicates);
        if (count < 0
            || buf_sep(b) < 0 || buf_raw(b, "q", 1) < 0
            || buf_str_attr(b, node, A_operator) < 0 || buf_ssize(b, count) < 0) {
            Py_DECREF(predicates);
            Py_DECREF(select);
            return -1;
        }

        PyObject *left = PyObject_GetAttr(node, A_left);
        if (left == NULL) {
            Py_DECREF(predicates);
            Py_DECREF(select);
            return -1;
        }
        int rc = shape_expr(b, left);
        Py_DECREF(left);

        if (rc == 0) {
            PyObject *model = PyObject_GetAttr(select, A_model);
            if (model == NULL) {
                rc = -1;
            }
            else {
                rc = (buf_sep(b) < 0 || buf_str_attr(b, model, A_qualname) < 0) ? -1 : 0;
                Py_DECREF(model);
            }
        }

        if (rc == 0) {
            /* `_check_subquery` refuses a projection that is not exactly one
             * column at construction, so index 0 exists for any node that was
             * built -- but read it through the sequence API rather than trusting
             * an invariant enforced in a different module. */
            PyObject *projection = PyObject_GetAttr(select, A_projection);
            if (projection == NULL) {
                rc = -1;
            }
            else {
                PyObject *item = PySequence_GetItem(projection, 0);
                Py_DECREF(projection);
                if (item == NULL) {
                    rc = -1;
                }
                else {
                    PyObject *column = PyObject_GetAttr(item, A_column);
                    Py_DECREF(item);
                    if (column == NULL) {
                        rc = -1;
                    }
                    else {
                        rc = (buf_sep(b) < 0
                              || buf_bytes_attr(b, column, A_shape_projection) < 0)
                                 ? -1
                                 : 0;
                        Py_DECREF(column);
                    }
                }
            }
        }

        if (rc == 0) {
            PyObject *fast = PySequence_Fast(predicates, "predicates is not a sequence");
            if (fast == NULL) {
                rc = -1;
            }
            else {
                Py_ssize_t n = PySequence_Fast_GET_SIZE(fast);
                for (Py_ssize_t i = 0; i < n; i++) {
                    if (shape_expr(b, PySequence_Fast_GET_ITEM(fast, i)) < 0) {
                        rc = -1;
                        break;
                    }
                }
                Py_DECREF(fast);
            }
        }

        Py_DECREF(predicates);
        Py_DECREF(select);
        return rc;
    }

    PyErr_Format(ORMError, "cannot key %s", Py_TYPE(node)->tp_name);
    return -1;
}

static int
shape_loads(Buf *b, PyObject *options)
{
    PyObject *fast = PySequence_Fast(options, "includes is not a sequence");
    if (fast == NULL) {
        return -1;
    }
    Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *option = PySequence_Fast_GET_ITEM(fast, i);
        PyObject *relationship = PyObject_GetAttr(option, A_relationship);
        if (relationship == NULL) {
            Py_DECREF(fast);
            return -1;
        }
        int fail = buf_sep(b) < 0 || buf_raw(b, "L", 1) < 0
                   || buf_str_attr(b, relationship, A_python_name) < 0
                   || buf_raw(b, ":", 1) < 0 || buf_str_attr(b, option, A_strategy) < 0;
        Py_DECREF(relationship);
        if (fail) {
            Py_DECREF(fast);
            return -1;
        }
        PyObject *nested = PyObject_GetAttr(option, A_nested);
        if (nested == NULL) {
            Py_DECREF(fast);
            return -1;
        }
        int rc = shape_loads(b, nested);
        Py_DECREF(nested);
        if (rc < 0) {
            Py_DECREF(fast);
            return -1;
        }
    }
    Py_DECREF(fast);
    return 0;
}

/* Collect ValueExpr nodes in placeholder order (mirrors _walk_values). The
 * value encoding (pg_type.to_wire) stays in Python -- a per-value Python call
 * must not cross into C -- so this returns the ordered nodes and the flat encode
 * loop runs in the caller. */
static int
collect_values(PyObject *node, PyObject *out)
{
    PyObject *type = (PyObject *)Py_TYPE(node);
    if (type == T_Value) {
        return PyList_Append(out, node);
    }
    if (type == T_Binary) {
        PyObject *left = PyObject_GetAttr(node, A_left);
        if (left == NULL) {
            return -1;
        }
        int rc = collect_values(left, out);
        Py_DECREF(left);
        if (rc < 0) {
            return -1;
        }
        PyObject *right = PyObject_GetAttr(node, A_right);
        if (right == NULL) {
            return -1;
        }
        rc = collect_values(right, out);
        Py_DECREF(right);
        return rc;
    }
    if (type == T_In) {
        PyObject *left = PyObject_GetAttr(node, A_left);
        if (left == NULL) {
            return -1;
        }
        int rc = collect_values(left, out);
        Py_DECREF(left);
        if (rc < 0) {
            return -1;
        }
        PyObject *values = PyObject_GetAttr(node, A_values);
        if (values == NULL) {
            return -1;
        }
        PyObject *fast = PySequence_Fast(values, "values is not a sequence");
        Py_DECREF(values);
        if (fast == NULL) {
            return -1;
        }
        Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);
        for (Py_ssize_t i = 0; i < count; i++) {
            if (collect_values(PySequence_Fast_GET_ITEM(fast, i), out) < 0) {
                Py_DECREF(fast);
                return -1;
            }
        }
        Py_DECREF(fast);
        return 0;
    }
    if (type == T_Boolean) {
        PyObject *operands = PyObject_GetAttr(node, A_operands);
        if (operands == NULL) {
            return -1;
        }
        PyObject *fast = PySequence_Fast(operands, "operands is not a sequence");
        Py_DECREF(operands);
        if (fast == NULL) {
            return -1;
        }
        Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);
        for (Py_ssize_t i = 0; i < count; i++) {
            if (collect_values(PySequence_Fast_GET_ITEM(fast, i), out) < 0) {
                Py_DECREF(fast);
                return -1;
            }
        }
        Py_DECREF(fast);
        return 0;
    }
    if (type == T_Unary) {
        PyObject *operand = PyObject_GetAttr(node, A_operand);
        if (operand == NULL) {
            return -1;
        }
        int rc = collect_values(operand, out);
        Py_DECREF(operand);
        return rc;
    }
    if (type == T_Column || type == T_Related) {
        return 0;  /* a column reference contributes no bind value */
    }
    PyErr_Format(ORMError, "cannot extract values from %s", Py_TYPE(node)->tp_name);
    return -1;
}

/* orm_collect_values(select) -> list[ValueExpr] in placeholder order */
PyObject *
wreath_orm_collect_values(PyObject *self, PyObject *args)
{
    (void)self;
    if (T_Binary == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "orm_shape not configured");
        return NULL;
    }
    PyObject *select;
    if (!PyArg_ParseTuple(args, "O", &select)) {
        return NULL;
    }
    PyObject *predicates = PyObject_GetAttr(select, A_predicates);
    if (predicates == NULL) {
        return NULL;
    }
    PyObject *fast = PySequence_Fast(predicates, "predicates is not a sequence");
    Py_DECREF(predicates);
    if (fast == NULL) {
        return NULL;
    }
    PyObject *out = PyList_New(0);
    if (out == NULL) {
        Py_DECREF(fast);
        return NULL;
    }
    Py_ssize_t count = PySequence_Fast_GET_SIZE(fast);
    for (Py_ssize_t i = 0; i < count; i++) {
        if (collect_values(PySequence_Fast_GET_ITEM(fast, i), out) < 0) {
            Py_DECREF(fast);
            Py_DECREF(out);
            return NULL;
        }
    }
    Py_DECREF(fast);
    return out;
}

/* orm_shape(registry, select) -> bytes */
PyObject *
wreath_orm_shape(PyObject *self, PyObject *args)
{
    (void)self;
    if (T_Binary == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "orm_shape not configured");
        return NULL;
    }
    PyObject *registry, *select;
    if (!PyArg_ParseTuple(args, "OO", &registry, &select)) {
        return NULL;
    }
    Buf b = {NULL, 0, 0, 1};
    PyObject *result = NULL;

    /* fingerprint, model qualname */
    if (buf_sep(&b) < 0 || buf_bytes_attr(&b, registry, A_fingerprint) < 0) {
        goto done;
    }
    PyObject *model = PyObject_GetAttr(select, A_model);
    if (model == NULL) {
        goto done;
    }
    int model_fail = buf_sep(&b) < 0 || buf_str_attr(&b, model, A_qualname) < 0;
    Py_DECREF(model);
    if (model_fail) {
        goto done;
    }

    /* projection: item.column.shape_projection */
    PyObject *projection = PyObject_GetAttr(select, A_projection);
    if (projection == NULL) {
        goto done;
    }
    PyObject *proj_fast = PySequence_Fast(projection, "projection is not a sequence");
    Py_DECREF(projection);
    if (proj_fast == NULL) {
        goto done;
    }
    Py_ssize_t proj_count = PySequence_Fast_GET_SIZE(proj_fast);
    for (Py_ssize_t i = 0; i < proj_count; i++) {
        PyObject *column = PyObject_GetAttr(PySequence_Fast_GET_ITEM(proj_fast, i), A_column);
        if (column == NULL) {
            Py_DECREF(proj_fast);
            goto done;
        }
        int fail = buf_sep(&b) < 0 || buf_bytes_attr(&b, column, A_shape_projection) < 0;
        Py_DECREF(column);
        if (fail) {
            Py_DECREF(proj_fast);
            goto done;
        }
    }
    Py_DECREF(proj_fast);

    if (buf_sep(&b) < 0 || buf_raw(&b, "|", 1) < 0) {
        goto done;
    }

    /* predicates */
    PyObject *predicates = PyObject_GetAttr(select, A_predicates);
    if (predicates == NULL) {
        goto done;
    }
    PyObject *pred_fast = PySequence_Fast(predicates, "predicates is not a sequence");
    Py_DECREF(predicates);
    if (pred_fast == NULL) {
        goto done;
    }
    Py_ssize_t pred_count = PySequence_Fast_GET_SIZE(pred_fast);
    for (Py_ssize_t i = 0; i < pred_count; i++) {
        if (shape_expr(&b, PySequence_Fast_GET_ITEM(pred_fast, i)) < 0) {
            Py_DECREF(pred_fast);
            goto done;
        }
    }
    Py_DECREF(pred_fast);

    if (buf_sep(&b) < 0 || buf_raw(&b, "|", 1) < 0) {
        goto done;
    }

    /* orderings: b"o" + expression.column.shape_ref + direction */
    PyObject *orderings = PyObject_GetAttr(select, A_orderings);
    if (orderings == NULL) {
        goto done;
    }
    PyObject *ord_fast = PySequence_Fast(orderings, "orderings is not a sequence");
    Py_DECREF(orderings);
    if (ord_fast == NULL) {
        goto done;
    }
    Py_ssize_t ord_count = PySequence_Fast_GET_SIZE(ord_fast);
    for (Py_ssize_t i = 0; i < ord_count; i++) {
        PyObject *item = PySequence_Fast_GET_ITEM(ord_fast, i);
        PyObject *expression = PyObject_GetAttr(item, A_expression);
        if (expression == NULL) {
            Py_DECREF(ord_fast);
            goto done;
        }
        PyObject *column = PyObject_GetAttr(expression, A_column);
        Py_DECREF(expression);
        if (column == NULL) {
            Py_DECREF(ord_fast);
            goto done;
        }
        int fail = buf_sep(&b) < 0 || buf_raw(&b, "o", 1) < 0
                   || buf_bytes_attr(&b, column, A_shape_ref) < 0
                   || buf_str_attr(&b, item, A_direction) < 0;
        Py_DECREF(column);
        if (fail) {
            Py_DECREF(ord_fast);
            goto done;
        }
    }
    Py_DECREF(ord_fast);

    /* includes */
    PyObject *includes = PyObject_GetAttr(select, A_includes);
    if (includes == NULL) {
        goto done;
    }
    int loads_fail = shape_loads(&b, includes) < 0;
    Py_DECREF(includes);
    if (loads_fail) {
        goto done;
    }

    /* for_update / limit / offset presence flags */
    PyObject *for_update = PyObject_GetAttr(select, A_for_update);
    if (for_update == NULL) {
        goto done;
    }
    int fu = PyObject_IsTrue(for_update);
    Py_DECREF(for_update);
    if (fu < 0 || buf_sep(&b) < 0 || buf_raw(&b, fu ? "f" : "-", 1) < 0) {
        goto done;
    }
    PyObject *limit = PyObject_GetAttr(select, A_limit);
    if (limit == NULL) {
        goto done;
    }
    int has_limit = limit != Py_None;
    Py_DECREF(limit);
    if (buf_sep(&b) < 0 || buf_raw(&b, has_limit ? "m" : "-", 1) < 0) {
        goto done;
    }
    PyObject *offset = PyObject_GetAttr(select, A_offset);
    if (offset == NULL) {
        goto done;
    }
    int has_offset = offset != Py_None;
    Py_DECREF(offset);
    if (buf_sep(&b) < 0 || buf_raw(&b, has_offset ? "n" : "-", 1) < 0) {
        goto done;
    }

    result = PyBytes_FromStringAndSize(b.data, b.len);

done:
    PyMem_Free(b.data);
    return result;
}

static PyObject *
intern(const char *name)
{
    PyObject *obj = PyUnicode_InternFromString(name);
    return obj;
}

/* orm_shape_configure(ColumnExpr, RelatedColumnExpr, ValueExpr, BinaryExpr,
 *                     InExpr, InSubqueryExpr, BooleanExpr, UnaryExpr,
 *                     ORMError) -> None */
PyObject *
wreath_orm_shape_configure(PyObject *self, PyObject *args)
{
    (void)self;
    PyObject *col, *rel, *val, *bin, *in_, *insub, *boolean, *unary, *err;
    if (!PyArg_ParseTuple(args, "OOOOOOOOO", &col, &rel, &val, &bin, &in_,
                          &insub, &boolean, &unary, &err)) {
        return NULL;
    }
    Py_XSETREF(T_Column, Py_NewRef(col));
    Py_XSETREF(T_Related, Py_NewRef(rel));
    Py_XSETREF(T_Value, Py_NewRef(val));
    Py_XSETREF(T_Binary, Py_NewRef(bin));
    Py_XSETREF(T_In, Py_NewRef(in_));
    Py_XSETREF(T_InSubquery, Py_NewRef(insub));
    Py_XSETREF(T_Boolean, Py_NewRef(boolean));
    Py_XSETREF(T_Unary, Py_NewRef(unary));
    Py_XSETREF(ORMError, Py_NewRef(err));

    A_column = intern("column");
    A_shape_ref = intern("shape_ref");
    A_shape_projection = intern("shape_projection");
    A_shape_value = intern("shape_value");
    A_pg_type = intern("pg_type");
    A_operator = intern("operator");
    A_left = intern("left");
    A_right = intern("right");
    A_values = intern("values");
    A_operands = intern("operands");
    A_operand = intern("operand");
    A_path = intern("path");
    A_expression = intern("expression");
    A_direction = intern("direction");
    A_projection = intern("projection");
    A_predicates = intern("predicates");
    A_select = intern("select");
    A_orderings = intern("orderings");
    A_includes = intern("includes");
    A_model = intern("model");
    A_for_update = intern("for_update_");
    A_limit = intern("limit_");
    A_offset = intern("offset_");
    A_fingerprint = intern("fingerprint");
    A_relationship = intern("relationship");
    A_strategy = intern("strategy");
    A_nested = intern("nested");
    A_python_name = intern("python_name");
    A_qualname = intern("__qualname__");
    if (PyErr_Occurred()) {
        return NULL;
    }
    Py_RETURN_NONE;
}

PyObject *
wreath_orm_shape_dispatch(PyObject *self, PyObject *args)
{
    return wreath_orm_shape(self, args);
}

PyObject *
wreath_orm_shape_configure_dispatch(PyObject *self, PyObject *args)
{
    return wreath_orm_shape_configure(self, args);
}
