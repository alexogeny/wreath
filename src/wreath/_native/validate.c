/* Native body validator.
 *
 * Python compiles each annotation into a normalized "plan" once (see
 * wreath.binding._compile_plan); this executes that plan against a decoded-JSON
 * value in a single call per body, accumulating the same {loc, msg, type}
 * errors as wreath.binding._validate and constructing dataclass
 * instances on success. C never inspects annotations -- it only reads the
 * plan tuples and the dataclass class objects the plan carries.
 *
 * A plan node is a tuple whose first item is an opcode:
 *   (0,)                      ANY          pass the value through
 *   (1,)                      NULL         value must be None
 *   (2,)                      INT
 *   (3,)                      FLOAT        ints accepted as floats
 *   (4,)                      BOOL
 *   (5,)                      STR
 *   (6, item_plan)            LIST
 *   (7, value_plan)           DICT         string-keyed record
 *   (8, has_none, options, msg)  UNION
 *   (9, cls, fields)          DATACLASS    fields = ((name, plan, required), ...)
 *   (10, message)             UNSUPPORTED
 */

#include "wreathcore.h"

enum {
    OP_ANY = 0,
    OP_NULL = 1,
    OP_INT = 2,
    OP_FLOAT = 3,
    OP_BOOL = 4,
    OP_STR = 5,
    OP_LIST = 6,
    OP_DICT = 7,
    OP_UNION = 8,
    OP_DATACLASS = 9,
    OP_UNSUPPORTED = 10,
};

/* Total node visits allowed per validation. A OP_UNION tries every option
 * against the whole value, so nested unions that fail deep re-explore each
 * branch at every level -- O(2^depth) work from a small body (a validation
 * bomb). This ceiling bounds the worst case: the densest legitimate body under
 * the default max_body_bytes (1 MiB) decodes to ~500k nodes, so 2M leaves ~4x
 * headroom and is never reached by real input, while a bomb is stopped with
 * one "too_complex" error instead of hanging. The pure binding._validate uses
 * the same ceiling; raise both together if max_body_bytes is raised far above
 * the default. */
#define WREATH_VALIDATE_MAX_STEPS 2000000

/* Append {"loc": list(loc), "msg": msg, "type": kind} to errors. Returns -1 on
 * a C-API failure with an exception set, 0 otherwise. */
static int
emit(PyObject *errors, PyObject *loc, const char *msg, const char *kind)
{
    PyObject *loc_copy = PyList_GetSlice(loc, 0, PyList_GET_SIZE(loc));
    if (loc_copy == NULL) {
        return -1;
    }
    PyObject *err = Py_BuildValue("{s:N,s:s,s:s}", "loc", loc_copy, "msg", msg, "type", kind);
    if (err == NULL) {
        return -1;
    }
    int rc = PyList_Append(errors, err);
    Py_DECREF(err);
    return rc;
}

static int
loc_push(PyObject *loc, PyObject *item)
{
    return PyList_Append(loc, item);
}

static void
loc_pop(PyObject *loc)
{
    Py_ssize_t size = PyList_GET_SIZE(loc);
    if (size > 0) {
        /* Delete the last element; PyList_SetSlice with an empty replacement. */
        PyList_SetSlice(loc, size - 1, size, NULL);
    }
}

/* Returns a new reference to the validated value, or NULL only on a hard
 * C-API failure (exception set). Validation failures are accumulated into
 * errors and still return a (new-reference) value, mirroring the Python code. */
static PyObject *
validate_node(PyObject *plan, PyObject *value, PyObject *loc, PyObject *errors,
              long *steps)
{
    if (*steps <= 0) {
        /* Budget exhausted: mark it (negative sentinel) and stop descending.
         * The single "too_complex" error is added by wreath_run_validation, so
         * it reaches the top even when exhaustion happens inside a union branch
         * whose per-attempt error list is discarded. */
        *steps = -1;
        return Py_NewRef(value);
    }
    (*steps)--;
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(plan, 0));
    if (opcode == -1 && PyErr_Occurred()) {
        return NULL;
    }

    switch (opcode) {
    case OP_ANY:
        return Py_NewRef(value);

    case OP_NULL:
        if (value != Py_None && emit(errors, loc, "value must be null", "null") < 0) {
            return NULL;
        }
        Py_RETURN_NONE;

    case OP_INT:
        if (PyLong_CheckExact(value) || (PyLong_Check(value) && !PyBool_Check(value))) {
            return Py_NewRef(value);
        }
        if (emit(errors, loc, "value is not an integer", "int") < 0) {
            return NULL;
        }
        return Py_NewRef(value);

    case OP_FLOAT:
        if (PyFloat_Check(value)) {
            return Py_NewRef(value);
        }
        if (PyLong_Check(value) && !PyBool_Check(value)) {
            double as_double = PyLong_AsDouble(value);
            if (as_double == -1.0 && PyErr_Occurred()) {
                return NULL;
            }
            return PyFloat_FromDouble(as_double);
        }
        if (emit(errors, loc, "value is not a number", "float") < 0) {
            return NULL;
        }
        return Py_NewRef(value);

    case OP_BOOL:
        if (PyBool_Check(value)) {
            return Py_NewRef(value);
        }
        if (emit(errors, loc, "value is not a boolean", "bool") < 0) {
            return NULL;
        }
        return Py_NewRef(value);

    case OP_STR:
        if (PyUnicode_Check(value)) {
            return Py_NewRef(value);
        }
        if (emit(errors, loc, "value is not a string", "str") < 0) {
            return NULL;
        }
        return Py_NewRef(value);

    case OP_LIST: {
        if (!PyList_Check(value)) {
            if (emit(errors, loc, "value is not an array", "list") < 0) {
                return NULL;
            }
            return Py_NewRef(value);
        }
        PyObject *item_plan = PyTuple_GET_ITEM(plan, 1);
        Py_ssize_t count = PyList_GET_SIZE(value);
        PyObject *result = PyList_New(count);
        if (result == NULL) {
            return NULL;
        }
        for (Py_ssize_t index = 0; index < count; index++) {
            PyObject *key = PyLong_FromSsize_t(index);
            if (key == NULL || loc_push(loc, key) < 0) {
                Py_XDECREF(key);
                Py_DECREF(result);
                return NULL;
            }
            Py_DECREF(key);
            PyObject *item = validate_node(item_plan, PyList_GET_ITEM(value, index), loc,
                                           errors, steps);
            loc_pop(loc);
            if (item == NULL) {
                Py_DECREF(result);
                return NULL;
            }
            PyList_SET_ITEM(result, index, item); /* steals reference */
        }
        return result;
    }

    case OP_DICT: {
        if (!PyDict_Check(value)) {
            if (emit(errors, loc, "value is not an object", "dict") < 0) {
                return NULL;
            }
            return Py_NewRef(value);
        }
        PyObject *value_plan = PyTuple_GET_ITEM(plan, 1);
        /* ``dict[str, Any]`` has already proved the only property its plan can
         * enforce once the outer value is a dict.  Rebuilding every key/value
         * pair into an identical dict was work whose answer cannot differ;
         * response projection and JSON conversion remain separate stages. */
        long value_opcode = PyLong_AsLong(PyTuple_GET_ITEM(value_plan, 0));
        if (value_opcode == -1 && PyErr_Occurred()) {
            return NULL;
        }
        if (value_opcode == OP_ANY) {
            return Py_NewRef(value);
        }
        PyObject *result = PyDict_New();
        if (result == NULL) {
            return NULL;
        }
        PyObject *key, *item;
        Py_ssize_t pos = 0;
        while (PyDict_Next(value, &pos, &key, &item)) {
            if (loc_push(loc, key) < 0) {
                Py_DECREF(result);
                return NULL;
            }
            PyObject *validated = validate_node(value_plan, item, loc, errors, steps);
            loc_pop(loc);
            if (validated == NULL) {
                Py_DECREF(result);
                return NULL;
            }
            int rc = PyDict_SetItem(result, key, validated);
            Py_DECREF(validated);
            if (rc < 0) {
                Py_DECREF(result);
                return NULL;
            }
        }
        return result;
    }

    case OP_UNION: {
        long has_none = PyLong_AsLong(PyTuple_GET_ITEM(plan, 1));
        if (has_none == -1 && PyErr_Occurred()) {
            return NULL;
        }
        if (value == Py_None && has_none) {
            Py_RETURN_NONE;
        }
        PyObject *options = PyTuple_GET_ITEM(plan, 2);
        Py_ssize_t count = PyTuple_GET_SIZE(options);
        for (Py_ssize_t index = 0; index < count; index++) {
            PyObject *attempt = PyList_New(0);
            if (attempt == NULL) {
                return NULL;
            }
            PyObject *result = validate_node(PyTuple_GET_ITEM(options, index), value, loc,
                                             attempt, steps);
            if (result == NULL) {
                Py_DECREF(attempt);
                return NULL;
            }
            if (PyList_GET_SIZE(attempt) == 0 && *steps >= 0) {
                Py_DECREF(attempt);
                return result; /* first clean match wins */
            }
            Py_DECREF(result);
            Py_DECREF(attempt);
            if (*steps < 0) {
                /* Budget ran out mid-union: stop trying options. An empty
                 * attempt from here on is a silent bail, not a real match, so
                 * declaring success would wrongly accept the value. Fall
                 * through; run_validation reports too_complex. */
                return Py_NewRef(value);
            }
        }
        PyObject *msg = PyTuple_GET_ITEM(plan, 3);
        PyObject *loc_copy = PyList_GetSlice(loc, 0, PyList_GET_SIZE(loc));
        if (loc_copy == NULL) {
            return NULL;
        }
        PyObject *err = Py_BuildValue("{s:N,s:O,s:s}", "loc", loc_copy, "msg", msg,
                                      "type", "union");
        if (err == NULL) {
            return NULL;
        }
        int rc = PyList_Append(errors, err);
        Py_DECREF(err);
        if (rc < 0) {
            return NULL;
        }
        return Py_NewRef(value);
    }

    case OP_DATACLASS: {
        PyObject *cls = PyTuple_GET_ITEM(plan, 1);
        int is_instance = PyObject_IsInstance(value, cls);
        if (is_instance < 0) {
            return NULL;
        }
        if (is_instance) {
            return Py_NewRef(value);
        }
        if (!PyDict_Check(value)) {
            if (emit(errors, loc, "value is not an object", "dict") < 0) {
                return NULL;
            }
            return Py_NewRef(value);
        }
        PyObject *fields = PyTuple_GET_ITEM(plan, 2);
        Py_ssize_t field_count = PyTuple_GET_SIZE(fields);
        PyObject *kwargs = PyDict_New();
        if (kwargs == NULL) {
            return NULL;
        }
        for (Py_ssize_t index = 0; index < field_count; index++) {
            PyObject *field = PyTuple_GET_ITEM(fields, index);
            PyObject *name = PyTuple_GET_ITEM(field, 0);
            PyObject *field_plan = PyTuple_GET_ITEM(field, 1);
            long required = PyLong_AsLong(PyTuple_GET_ITEM(field, 2));
            if (required == -1 && PyErr_Occurred()) {
                Py_DECREF(kwargs);
                return NULL;
            }
            PyObject *present = PyDict_GetItemWithError(value, name); /* borrowed */
            if (present == NULL && PyErr_Occurred()) {
                Py_DECREF(kwargs);
                return NULL;
            }
            if (present != NULL) {
                if (loc_push(loc, name) < 0) {
                    Py_DECREF(kwargs);
                    return NULL;
                }
                PyObject *validated = validate_node(field_plan, present, loc, errors,
                                                    steps);
                loc_pop(loc);
                if (validated == NULL) {
                    Py_DECREF(kwargs);
                    return NULL;
                }
                int rc = PyDict_SetItem(kwargs, name, validated);
                Py_DECREF(validated);
                if (rc < 0) {
                    Py_DECREF(kwargs);
                    return NULL;
                }
            }
            else if (required) {
                if (loc_push(loc, name) < 0) {
                    Py_DECREF(kwargs);
                    return NULL;
                }
                int rc = emit(errors, loc, "field is required", "missing");
                loc_pop(loc);
                if (rc < 0) {
                    Py_DECREF(kwargs);
                    return NULL;
                }
            }
        }
        /* Unexpected fields: any value key not named by a field. Iterated in
         * value's insertion order to match `wreath.binding._validate`. */
        if (PyDict_GET_SIZE(value) > PyDict_GET_SIZE(kwargs)) {
            /* Build the field-name set once (O(F)) so each value key is an O(1)
             * membership test. The old per-key linear rescan of every field
             * name was O(V*F) -- quadratic when a large body is validated
             * against a wide schema. Mirrors pure _validate_dataclass, which
             * builds the same `known` set. */
            PyObject *known = PySet_New(NULL);
            if (known == NULL) {
                Py_DECREF(kwargs);
                return NULL;
            }
            for (Py_ssize_t index = 0; index < field_count; index++) {
                PyObject *name = PyTuple_GET_ITEM(PyTuple_GET_ITEM(fields, index), 0);
                if (PySet_Add(known, name) < 0) {
                    Py_DECREF(known);
                    Py_DECREF(kwargs);
                    return NULL;
                }
            }
            PyObject *key, *item;
            Py_ssize_t pos = 0;
            while (PyDict_Next(value, &pos, &key, &item)) {
                int contains = PySet_Contains(known, key);
                if (contains < 0) {
                    Py_DECREF(known);
                    Py_DECREF(kwargs);
                    return NULL;
                }
                if (!contains) {
                    if (loc_push(loc, key) < 0) {
                        Py_DECREF(known);
                        Py_DECREF(kwargs);
                        return NULL;
                    }
                    int rc = emit(errors, loc, "unexpected field", "extra");
                    loc_pop(loc);
                    if (rc < 0) {
                        Py_DECREF(known);
                        Py_DECREF(kwargs);
                        return NULL;
                    }
                }
            }
            Py_DECREF(known);
        }
        /* Any error anywhere (shared list) means we cannot construct. A
         * negative step budget means validation was cut short mid-tree, so the
         * kwargs may be incomplete -- never construct from a truncated body. */
        if (PyList_GET_SIZE(errors) > 0 || *steps < 0) {
            Py_DECREF(kwargs);
            return Py_NewRef(value);
        }
        PyObject *empty = PyTuple_New(0);
        if (empty == NULL) {
            Py_DECREF(kwargs);
            return NULL;
        }
        PyObject *instance = PyObject_Call(cls, empty, kwargs);
        Py_DECREF(empty);
        Py_DECREF(kwargs);
        return instance;
    }

    case OP_UNSUPPORTED: {
        PyObject *msg = PyTuple_GET_ITEM(plan, 1);
        PyObject *loc_copy = PyList_GetSlice(loc, 0, PyList_GET_SIZE(loc));
        if (loc_copy == NULL) {
            return NULL;
        }
        PyObject *err = Py_BuildValue("{s:N,s:O,s:s}", "loc", loc_copy, "msg", msg,
                                      "type", "unsupported");
        if (err == NULL) {
            return NULL;
        }
        int rc = PyList_Append(errors, err);
        Py_DECREF(err);
        if (rc < 0) {
            return NULL;
        }
        return Py_NewRef(value);
    }

    default:
        PyErr_Format(PyExc_ValueError, "unknown validation opcode %ld", opcode);
        return NULL;
    }
}

/* run_validation(plan, value, loc) -> (result, errors)
 *
 * loc is the starting location tuple (e.g. ("body",)). errors is a fresh list;
 * the Python caller raises ValidationError(errors) when it is non-empty. */
PyObject *
wreath_run_validation(PyObject *self, PyObject *args)
{
    (void)self;
    PyObject *plan, *value, *loc_seq;
    if (!PyArg_ParseTuple(args, "OOO", &plan, &value, &loc_seq)) {
        return NULL;
    }
    PyObject *loc = PySequence_List(loc_seq);
    if (loc == NULL) {
        return NULL;
    }
    PyObject *errors = PyList_New(0);
    if (errors == NULL) {
        Py_DECREF(loc);
        return NULL;
    }
    long steps = WREATH_VALIDATE_MAX_STEPS;
    PyObject *result = validate_node(plan, value, loc, errors, &steps);
    if (result != NULL && steps < 0) {
        /* Validation was cut short by the step budget: report it once, at the
         * root, regardless of which subtree exhausted it. */
        if (emit(errors, loc, "value is too complex to validate",
                 "too_complex") < 0) {
            Py_DECREF(result);
            Py_DECREF(loc);
            Py_DECREF(errors);
            return NULL;
        }
    }
    Py_DECREF(loc);
    if (result == NULL) {
        Py_DECREF(errors);
        return NULL;
    }
    PyObject *pair = PyTuple_Pack(2, result, errors);
    Py_DECREF(result);
    Py_DECREF(errors);
    return pair;
}

/* run_validation_json(plan, value, loc) -> (body | None, errors)
 *
 * Response validation used to return a Python object, rebuild that object in
 * ``_compile_jsonable``, and only then enter the native JSON encoder.  The
 * overwhelmingly common response contract is ``dict[str, Any]``: once the
 * outer object check succeeds its child plan cannot reject or transform a
 * value, so validation and emission are one JSON traversal here.  Other flat
 * native plans still benefit by keeping their validated result and JSON
 * emission inside one C entry; their validator may transform values (notably
 * int -> float), so they deliberately retain the validation walk.
 *
 * A TypeError from the JSON encoder is not swallowed.  It tells the compiled
 * wrapper to take the full jsonable conversion path for values such as UUID,
 * Decimal, bytes and sets. */
PyObject *
wreath_run_validation_json(PyObject *self, PyObject *args)
{
    (void)self;
    PyObject *plan, *value, *loc_seq;
    if (!PyArg_UnpackTuple(
            args, "run_validation_json", 3, 3, &plan, &value, &loc_seq)) {
        return NULL;
    }

    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(plan, 0));
    if (opcode == -1 && PyErr_Occurred()) {
        return NULL;
    }
    if (opcode == OP_DICT && PyDict_Check(value)) {
        PyObject *value_plan = PyTuple_GET_ITEM(plan, 1);
        long value_opcode = PyLong_AsLong(PyTuple_GET_ITEM(value_plan, 0));
        if (value_opcode == -1 && PyErr_Occurred()) {
            return NULL;
        }
        if (value_opcode == OP_ANY) {
            PyObject *body = wreath_json_dumps(NULL, value);
            if (body == NULL) {
                return NULL;
            }
            PyObject *errors = PyList_New(0);
            if (errors == NULL) {
                Py_DECREF(body);
                return NULL;
            }
            PyObject *pair = PyTuple_Pack(2, body, errors);
            Py_DECREF(body);
            Py_DECREF(errors);
            return pair;
        }
    }

    PyObject *validation_args = PyTuple_Pack(3, plan, value, loc_seq);
    if (validation_args == NULL) {
        return NULL;
    }
    PyObject *validated = wreath_run_validation(NULL, validation_args);
    Py_DECREF(validation_args);
    if (validated == NULL) {
        return NULL;
    }
    PyObject *result = PyTuple_GET_ITEM(validated, 0);
    PyObject *errors = PyTuple_GET_ITEM(validated, 1);
    PyObject *body;
    if (PyList_GET_SIZE(errors) == 0) {
        body = wreath_json_dumps(NULL, result);
        if (body == NULL) {
            Py_DECREF(validated);
            return NULL;
        }
    }
    else {
        body = Py_NewRef(Py_None);
    }
    PyObject *pair = PyTuple_Pack(2, body, errors);
    Py_DECREF(body);
    Py_DECREF(validated);
    return pair;
}
