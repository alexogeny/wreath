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
 *   (9, cls, fields, known, positional) DATACLASS fields =
 *                             ((python_name, wire_name, plan, required), ...)
 *   (10, message)             UNSUPPORTED
 *   (11, child, comparisons, lengths, pattern)  FIELD
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
    OP_FIELD = 11,
};

typedef struct ValidationNode ValidationNode;

typedef struct {
    PyObject *source;
    ValidationNode *plan;
} ValidationField;

struct ValidationNode {
    int opcode;
    int passthrough;
    PyObject *source;
    ValidationNode *child;
    ValidationNode **options;
    Py_ssize_t option_count;
    ValidationField *fields;
    Py_ssize_t field_count;
};

typedef struct {
    PyObject *source;
    ValidationNode *root;
} ValidationPlan;

#define VALIDATION_PLAN_CAPSULE "wreath.validation.plan"
#define WREATH_VALIDATE_MAX_PLAN_NODES 100000

static void
validation_node_clear(ValidationNode *node)
{
    if (node == NULL) return;
    validation_node_clear(node->child);
    for (Py_ssize_t index = 0; index < node->option_count; index++)
        validation_node_clear(node->options[index]);
    for (Py_ssize_t index = 0; index < node->field_count; index++)
        validation_node_clear(node->fields[index].plan);
    PyMem_Free(node->fields);
    PyMem_Free(node->options);
    PyMem_Free(node);
}

static void
validation_plan_clear(ValidationPlan *plan)
{
    if (plan == NULL) return;
    validation_node_clear(plan->root);
    Py_XDECREF(plan->source);
    PyMem_Free(plan);
}

static void
validation_plan_destructor(PyObject *capsule)
{
    ValidationPlan *plan = PyCapsule_GetPointer(capsule,
                                                VALIDATION_PLAN_CAPSULE);
    if (plan != NULL) validation_plan_clear(plan);
    else PyErr_Clear();
}

static ValidationNode *
validation_node_compile(PyObject *source, int depth, Py_ssize_t *remaining)
{
    ValidationNode *node;
    long opcode;
    if (depth > 256) {
        PyErr_SetString(PyExc_ValueError, "validation plan is nested too deeply");
        return NULL;
    }
    if (*remaining == 0) {
        PyErr_SetString(
            PyExc_ValueError,
            "validation plan expands to more than 100000 nodes; simplify the annotation");
        return NULL;
    }
    --*remaining;
    if (!PyTuple_Check(source) || PyTuple_GET_SIZE(source) == 0) {
        PyErr_SetString(PyExc_TypeError, "validation plan nodes must be tuples");
        return NULL;
    }
    opcode = PyLong_AsLong(PyTuple_GET_ITEM(source, 0));
    if (opcode < OP_ANY || opcode > OP_FIELD) {
        if (!PyErr_Occurred())
            PyErr_Format(PyExc_ValueError, "unknown validation opcode %ld", opcode);
        return NULL;
    }
    node = PyMem_Calloc(1, sizeof(*node));
    if (node == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    node->opcode = (int)opcode;
    node->source = source;
    switch (node->opcode) {
    case OP_ANY:
    case OP_INT:
    case OP_BOOL:
    case OP_STR:
    case OP_UNSUPPORTED:
        node->passthrough = 1;
        break;
    case OP_NULL:
    case OP_FLOAT:
        break;
    case OP_LIST:
    case OP_DICT:
        node->child = validation_node_compile(PyTuple_GET_ITEM(source, 1),
                                              depth + 1, remaining);
        if (node->child == NULL) goto error;
        node->passthrough = node->child->passthrough;
        break;
    case OP_UNION: {
        PyObject *options = PyTuple_GET_ITEM(source, 2);
        node->option_count = PyTuple_GET_SIZE(options);
        if (node->option_count != 0) {
            node->options = PyMem_Calloc((size_t)node->option_count,
                                         sizeof(*node->options));
            if (node->options == NULL) { PyErr_NoMemory(); goto error; }
        }
        node->passthrough = 1;
        for (Py_ssize_t index = 0; index < node->option_count; index++) {
            node->options[index] = validation_node_compile(
                PyTuple_GET_ITEM(options, index), depth + 1, remaining);
            if (node->options[index] == NULL) goto error;
            if (!node->options[index]->passthrough) node->passthrough = 0;
        }
        break;
    }
    case OP_DATACLASS: {
        PyObject *fields = PyTuple_GET_ITEM(source, 2);
        node->field_count = PyTuple_GET_SIZE(fields);
        if (node->field_count != 0) {
            node->fields = PyMem_Calloc((size_t)node->field_count,
                                        sizeof(*node->fields));
            if (node->fields == NULL) { PyErr_NoMemory(); goto error; }
        }
        for (Py_ssize_t index = 0; index < node->field_count; index++) {
            PyObject *field = PyTuple_GET_ITEM(fields, index);
            node->fields[index].source = field;
            node->fields[index].plan = validation_node_compile(
                PyTuple_GET_ITEM(field, 2), depth + 1, remaining);
            if (node->fields[index].plan == NULL) goto error;
        }
        break;
    }
    case OP_FIELD:
        node->child = validation_node_compile(PyTuple_GET_ITEM(source, 1),
                                              depth + 1, remaining);
        if (node->child == NULL) goto error;
        node->passthrough = node->child->passthrough;
        break;
    }
    return node;
error:
    validation_node_clear(node);
    return NULL;
}

static ValidationPlan *
validation_plan_compile(PyObject *source)
{
    Py_ssize_t remaining = WREATH_VALIDATE_MAX_PLAN_NODES;
    ValidationPlan *plan = PyMem_Calloc(1, sizeof(*plan));
    if (plan == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    plan->source = Py_NewRef(source);
    plan->root = validation_node_compile(source, 0, &remaining);
    if (plan->root == NULL) {
        validation_plan_clear(plan);
        return NULL;
    }
    return plan;
}

static ValidationPlan *
validation_plan_get(PyObject *object)
{
    if (!PyCapsule_IsValid(object, VALIDATION_PLAN_CAPSULE)) return NULL;
    return PyCapsule_GetPointer(object, VALIDATION_PLAN_CAPSULE);
}

PyObject *
wreath_compile_validation_plan(PyObject *Py_UNUSED(self), PyObject *source)
{
    ValidationPlan *existing = validation_plan_get(source);
    ValidationPlan *plan;
    PyObject *capsule;
    if (existing != NULL) return Py_NewRef(source);
    plan = validation_plan_compile(source);
    if (plan == NULL) return NULL;
    capsule = PyCapsule_New(plan, VALIDATION_PLAN_CAPSULE,
                            validation_plan_destructor);
    if (capsule == NULL) validation_plan_clear(plan);
    return capsule;
}

PyObject *
wreath_validation_plan_source(PyObject *object)
{
    ValidationPlan *plan = validation_plan_get(object);
    return plan == NULL ? object : plan->source;
}

/* Total node visits allowed per validation. A OP_UNION tries every option
 * against the whole value, so nested unions that fail deep re-explore each
 * branch at every level -- O(2^depth) work from a small body (a validation
 * bomb). This ceiling bounds the worst case: the densest legitimate body under
 * the default max_body_bytes (1 MiB) decodes to ~500k nodes, so 2M leaves ~4x
 * headroom and is never reached by real input, while a bomb is stopped with
 * one "too_complex" error instead of hanging. The pure binding._validate uses
 * the same ceiling; raise both together if max_body_bytes is raised far above
 * the default. */
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
emit_objects(PyObject *errors, PyObject *loc, PyObject *message, PyObject *kind)
{
    PyObject *loc_copy = PyList_GetSlice(loc, 0, PyList_GET_SIZE(loc));
    if (loc_copy == NULL) return -1;
    PyObject *err = Py_BuildValue(
        "{s:N,s:O,s:O}", "loc", loc_copy, "msg", message, "type", kind);
    if (err == NULL) return -1;
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

/* Whether validation can only accept/reject this value graph and can never
 * change one of its nodes.  The answer is derived from the immutable plan, not
 * from the value, so container validators can walk once and return the decoded
 * graph they were handed instead of allocating an identical second graph. */
static int
plan_is_passthrough(ValidationNode *plan)
{
    return plan->passthrough;
}

/* Validate one homogeneous scalar without recursion or location objects.
 *
 *  1: accepted by a scalar plan; 0: rejected by that plan;
 * -1: not a plan this shortcut handles; -2: C-API failure.
 *
 * A rejection deliberately falls back to validate_node(), which owns the
 * exact error spelling and location.  Successful JSON arrays are the hot path:
 * their indices never need to become Python integers merely to be discarded
 * after every accepted item. */
static int
flat_scalar_accepts(ValidationNode *plan, PyObject *value)
{
    if (PyTuple_GET_SIZE(plan->source) != 1) return -1;
    switch (plan->opcode) {
    case OP_ANY:
        return 1;
    case OP_NULL:
        return value == Py_None;
    case OP_INT:
        return PyLong_Check(value) && !PyBool_Check(value);
    case OP_BOOL:
        return PyBool_Check(value);
    case OP_STR:
        return PyUnicode_Check(value);
    default:
        return -1;
    }
}

static PyObject *
validate_node(ValidationNode *compiled, PyObject *value, PyObject *loc,
              PyObject *errors, long *steps);

static PyObject *
validate_list(ValidationNode *compiled, PyObject *value, PyObject *loc,
              PyObject *errors, long *steps)
{
    if (!PyList_Check(value)) {
        if (emit(errors, loc, "value is not an array", "list") < 0) return NULL;
        return Py_NewRef(value);
    }
    ValidationNode *item_plan = compiled->child;
    Py_ssize_t count = PyList_GET_SIZE(value);
    int flat = flat_scalar_accepts(
        item_plan, count == 0 ? Py_None : PyList_GET_ITEM(value, 0));
    if (flat == -2) return NULL;
    if (flat >= 0 && count <= *steps) {
        Py_ssize_t index = flat ? 1 : 0;
        for (; index < count; index++) {
            flat = flat_scalar_accepts(item_plan, PyList_GET_ITEM(value, index));
            if (flat == -2) return NULL;
            if (flat != 1) break;
        }
        if (index == count) {
            *steps -= (long)count;
            return Py_NewRef(value);
        }
    }
    int passthrough = plan_is_passthrough(item_plan);
    if (passthrough < 0) return NULL;
    PyObject *result = passthrough ? Py_NewRef(value) : PyList_New(count);
    if (result == NULL) return NULL;
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *key = PyLong_FromSsize_t(index);
        if (key == NULL || loc_push(loc, key) < 0) {
            Py_XDECREF(key);
            Py_DECREF(result);
            return NULL;
        }
        Py_DECREF(key);
        PyObject *item = validate_node(
            item_plan, PyList_GET_ITEM(value, index), loc, errors, steps);
        loc_pop(loc);
        if (item == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        if (passthrough) Py_DECREF(item);
        else PyList_SET_ITEM(result, index, item); /* steals reference */
    }
    return result;
}

static PyObject *
validate_dict(ValidationNode *compiled, PyObject *value, PyObject *loc,
              PyObject *errors, long *steps)
{
    if (!PyDict_Check(value)) {
        if (emit(errors, loc, "value is not an object", "dict") < 0) return NULL;
        return Py_NewRef(value);
    }
    ValidationNode *value_plan = compiled->child;
    Py_ssize_t count = PyDict_GET_SIZE(value);
    int flat_supported = -1;
    int flat_clean = 1;
    PyObject *key, *item;
    Py_ssize_t pos = 0;
    if (count <= *steps) {
        while (PyDict_Next(value, &pos, &key, &item)) {
            int accepted = flat_scalar_accepts(value_plan, item);
            if (accepted == -2) return NULL;
            if (accepted == -1) {
                flat_supported = -1;
                break;
            }
            flat_supported = 1;
            if (!accepted) {
                flat_clean = 0;
                break;
            }
        }
        if ((count == 0 || flat_supported == 1) && flat_clean) {
            *steps -= (long)count;
            return Py_NewRef(value);
        }
    }
    int passthrough = plan_is_passthrough(value_plan);
    if (passthrough < 0) return NULL;
    PyObject *result = passthrough ? Py_NewRef(value) : PyDict_New();
    if (result == NULL) return NULL;
    pos = 0;
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
        int rc = passthrough ? 0 : PyDict_SetItem(result, key, validated);
        Py_DECREF(validated);
        if (rc < 0) {
            Py_DECREF(result);
            return NULL;
        }
    }
    return result;
}

static PyObject *
validate_union(ValidationNode *compiled, PyObject *value, PyObject *loc,
               PyObject *errors, long *steps)
{
    PyObject *plan = compiled->source;
    long has_none = PyLong_AsLong(PyTuple_GET_ITEM(plan, 1));
    if (has_none == -1 && PyErr_Occurred()) return NULL;
    if (value == Py_None && has_none) Py_RETURN_NONE;
    for (Py_ssize_t index = 0; index < compiled->option_count; index++) {
        PyObject *attempt = PyList_New(0);
        if (attempt == NULL) return NULL;
        PyObject *result = validate_node(
            compiled->options[index], value, loc, attempt, steps);
        if (result == NULL) {
            Py_DECREF(attempt);
            return NULL;
        }
        if (PyList_GET_SIZE(attempt) == 0 && *steps >= 0) {
            Py_DECREF(attempt);
            return result;
        }
        Py_DECREF(result);
        Py_DECREF(attempt);
        if (*steps < 0) return Py_NewRef(value);
    }
    PyObject *msg = PyTuple_GET_ITEM(plan, 3);
    PyObject *loc_copy = PyList_GetSlice(loc, 0, PyList_GET_SIZE(loc));
    if (loc_copy == NULL) return NULL;
    PyObject *err = Py_BuildValue(
        "{s:N,s:O,s:s}", "loc", loc_copy, "msg", msg, "type", "union");
    if (err == NULL) return NULL;
    int rc = PyList_Append(errors, err);
    Py_DECREF(err);
    if (rc < 0) return NULL;
    return Py_NewRef(value);
}

static void
dataclass_values_clear(PyObject **values, Py_ssize_t count, PyObject *kwargs)
{
    if (values != NULL) {
        for (Py_ssize_t index = 0; index < count; index++) {
            Py_XDECREF(values[index]);
        }
        PyMem_Free(values);
    }
    Py_XDECREF(kwargs);
}

static PyObject *
validate_dataclass(ValidationNode *compiled, PyObject *value, PyObject *loc,
                   PyObject *errors, long *steps)
{
    PyObject *plan = compiled->source;
    PyObject *cls = PyTuple_GET_ITEM(plan, 1);
    int is_instance = PyObject_IsInstance(value, cls);
    if (is_instance < 0) return NULL;
    if (is_instance) return Py_NewRef(value);
    if (!PyDict_Check(value)) {
        if (emit(errors, loc, "value is not an object", "dict") < 0) return NULL;
        return Py_NewRef(value);
    }
    PyObject *fields = PyTuple_GET_ITEM(plan, 2);
    PyObject *known = PyTuple_GET_ITEM(plan, 3);
    Py_ssize_t field_count = compiled->field_count;
    int positional = PyObject_IsTrue(PyTuple_GET_ITEM(plan, 4));
    if (positional < 0) return NULL;
    PyObject **values = NULL;
    PyObject *kwargs = NULL;
    if (positional && field_count != 0) {
        values = PyMem_Calloc((size_t)field_count, sizeof(*values));
        if (values == NULL) return PyErr_NoMemory();
    }
    else if (!positional) {
        kwargs = PyDict_New();
        if (kwargs == NULL) return NULL;
    }
    Py_ssize_t present_count = 0;
    for (Py_ssize_t index = 0; index < field_count; index++) {
        PyObject *field = compiled->fields[index].source;
        PyObject *name = PyTuple_GET_ITEM(field, 0);
        PyObject *wire_name = PyTuple_GET_ITEM(field, 1);
        long required = PyLong_AsLong(PyTuple_GET_ITEM(field, 3));
        if (required == -1 && PyErr_Occurred()) goto error;
        PyObject *present = PyDict_GetItemWithError(value, wire_name);
        if (present == NULL && PyErr_Occurred()) goto error;
        if (present != NULL) {
            present_count++;
            if (loc_push(loc, wire_name) < 0) goto error;
            PyObject *validated = validate_node(
                compiled->fields[index].plan, present, loc, errors, steps);
            loc_pop(loc);
            if (validated == NULL) goto error;
            if (positional) values[index] = validated;
            else {
                int rc = PyDict_SetItem(kwargs, name, validated);
                Py_DECREF(validated);
                if (rc < 0) goto error;
            }
        }
        else if (required) {
            if (loc_push(loc, wire_name) < 0) goto error;
            int rc = emit(errors, loc, "field is required", "missing");
            loc_pop(loc);
            if (rc < 0) goto error;
        }
    }
    if (PyDict_GET_SIZE(value) > present_count) {
        PyObject *key, *item;
        Py_ssize_t pos = 0;
        while (PyDict_Next(value, &pos, &key, &item)) {
            int contains = PySet_Contains(known, key);
            if (contains < 0) goto error;
            if (!contains) {
                if (loc_push(loc, key) < 0) goto error;
                int rc = emit(errors, loc, "unexpected field", "extra");
                loc_pop(loc);
                if (rc < 0) goto error;
            }
        }
    }
    if (PyList_GET_SIZE(errors) > 0 || *steps < 0) {
        dataclass_values_clear(values, field_count, kwargs);
        return Py_NewRef(value);
    }
    PyObject *instance;
    if (positional && present_count == field_count) {
        instance = PyObject_Vectorcall(cls, values, (size_t)field_count, NULL);
    }
    else {
        if (kwargs == NULL) {
            kwargs = PyDict_New();
            if (kwargs == NULL) goto error;
            for (Py_ssize_t index = 0; index < field_count; index++) {
                if (values[index] == NULL) continue;
                PyObject *name = PyTuple_GET_ITEM(
                    PyTuple_GET_ITEM(fields, index), 0);
                if (PyDict_SetItem(kwargs, name, values[index]) < 0) goto error;
            }
        }
        instance = PyObject_VectorcallDict(cls, NULL, 0, kwargs);
    }
    dataclass_values_clear(values, field_count, kwargs);
    return instance;

error:
    dataclass_values_clear(values, field_count, kwargs);
    return NULL;
}

static int
validate_field_comparisons(PyObject *comparisons, PyObject *value,
                           PyObject *loc, PyObject *errors)
{
    static const int rich_operations[] = {Py_GT, Py_GE, Py_LT, Py_LE};
    for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(comparisons); index++) {
        PyObject *entry = PyTuple_GET_ITEM(comparisons, index);
        long operation = PyLong_AsLong(PyTuple_GET_ITEM(entry, 0));
        if (operation == -1 && PyErr_Occurred()) return -1;
        if (operation < 0 || operation >= 4) {
            PyErr_SetString(PyExc_ValueError, "invalid field comparison opcode");
            return -1;
        }
        int valid = PyObject_RichCompareBool(
            value, PyTuple_GET_ITEM(entry, 1), rich_operations[operation]);
        if (valid < 0) {
            if (!PyErr_ExceptionMatches(PyExc_TypeError)) return -1;
            PyErr_Clear();
            return emit_objects(
                errors, loc, PyTuple_GET_ITEM(entry, 3),
                PyTuple_GET_ITEM(entry, 4)) < 0 ? -1 : 1;
        }
        if (!valid) {
            return emit_objects(
                errors, loc, PyTuple_GET_ITEM(entry, 2),
                PyTuple_GET_ITEM(entry, 4)) < 0 ? -1 : 1;
        }
    }
    return 0;
}

static int
validate_field_lengths(PyObject *lengths, PyObject *value, PyObject *loc,
                       PyObject *errors)
{
    for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(lengths); index++) {
        PyObject *entry = PyTuple_GET_ITEM(lengths, index);
        long operation = PyLong_AsLong(PyTuple_GET_ITEM(entry, 0));
        Py_ssize_t bound = PyLong_AsSsize_t(PyTuple_GET_ITEM(entry, 1));
        if ((operation == -1 || bound == -1) && PyErr_Occurred()) return -1;
        if (operation < 0 || operation >= 2) {
            PyErr_SetString(PyExc_ValueError, "invalid field length opcode");
            return -1;
        }
        Py_ssize_t length = PyObject_Length(value);
        if (length < 0) {
            if (!PyErr_ExceptionMatches(PyExc_TypeError)) return -1;
            PyErr_Clear();
            return emit_objects(
                errors, loc, PyTuple_GET_ITEM(entry, 3),
                PyTuple_GET_ITEM(entry, 4)) < 0 ? -1 : 1;
        }
        int valid = operation == 0 ? length >= bound : length <= bound;
        if (!valid) {
            return emit_objects(
                errors, loc, PyTuple_GET_ITEM(entry, 2),
                PyTuple_GET_ITEM(entry, 4)) < 0 ? -1 : 1;
        }
    }
    return 0;
}

static int
validate_field_pattern(PyObject *pattern, PyObject *value, PyObject *loc,
                       PyObject *errors)
{
    if (pattern == Py_None) return 0;
    int valid = 0;
    if (PyUnicode_Check(value)) {
        PyObject *match = PyObject_CallOneArg(PyTuple_GET_ITEM(pattern, 0), value);
        if (match == NULL) return -1;
        valid = match != Py_None;
        Py_DECREF(match);
    }
    if (valid) return 0;
    return emit_objects(
        errors, loc, PyTuple_GET_ITEM(pattern, 1),
        PyTuple_GET_ITEM(pattern, 2)) < 0 ? -1 : 1;
}

static PyObject *
validate_field(ValidationNode *compiled, PyObject *value, PyObject *loc,
               PyObject *errors, long *steps)
{
    Py_ssize_t before = PyList_GET_SIZE(errors);
    PyObject *validated = validate_node(
        compiled->child, value, loc, errors, steps);
    if (validated == NULL || PyList_GET_SIZE(errors) != before || *steps < 0) {
        return validated;
    }
    PyObject *plan = compiled->source;
    int rc = validate_field_comparisons(
        PyTuple_GET_ITEM(plan, 2), validated, loc, errors);
    if (rc == 0) {
        rc = validate_field_lengths(
            PyTuple_GET_ITEM(plan, 3), validated, loc, errors);
    }
    if (rc == 0) {
        rc = validate_field_pattern(
            PyTuple_GET_ITEM(plan, 4), validated, loc, errors);
    }
    if (rc < 0) {
        Py_DECREF(validated);
        return NULL;
    }
    return validated;
}

static PyObject *
validate_unsupported(PyObject *plan, PyObject *value, PyObject *loc,
                     PyObject *errors)
{
    PyObject *msg = PyTuple_GET_ITEM(plan, 1);
    PyObject *loc_copy = PyList_GetSlice(loc, 0, PyList_GET_SIZE(loc));
    if (loc_copy == NULL) return NULL;
    PyObject *err = Py_BuildValue(
        "{s:N,s:O,s:s}", "loc", loc_copy, "msg", msg, "type", "unsupported");
    if (err == NULL) return NULL;
    int rc = PyList_Append(errors, err);
    Py_DECREF(err);
    if (rc < 0) return NULL;
    return Py_NewRef(value);
}

/* Returns a new reference to the validated value, or NULL only on a hard
 * C-API failure (exception set). Validation failures are accumulated into
 * errors and still return a (new-reference) value, mirroring the Python code. */
static PyObject *
validate_node(ValidationNode *compiled, PyObject *value, PyObject *loc,
              PyObject *errors, long *steps)
{
    PyObject *plan = compiled->source;
    if (*steps <= 0) {
        /* Budget exhausted: mark it (negative sentinel) and stop descending.
         * The single "too_complex" error is added by wreath_run_validation, so
         * it reaches the top even when exhaustion happens inside a union branch
         * whose per-attempt error list is discarded. */
        *steps = -1;
        return Py_NewRef(value);
    }
    int opcode = compiled->opcode;
    /* Field metadata wraps a child without visiting another value. The Python
     * definition strips the annotation before decrementing its node budget,
     * so the native tape must not charge this wrapper as a second node. */
    if (opcode != OP_FIELD) (*steps)--;

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

    case OP_LIST:
        return validate_list(compiled, value, loc, errors, steps);

    case OP_DICT:
        return validate_dict(compiled, value, loc, errors, steps);

    case OP_UNION:
        return validate_union(compiled, value, loc, errors, steps);

    case OP_DATACLASS:
        return validate_dataclass(compiled, value, loc, errors, steps);

    case OP_UNSUPPORTED:
        return validate_unsupported(plan, value, loc, errors);

    case OP_FIELD:
        return validate_field(compiled, value, loc, errors, steps);

    default:
        PyErr_Format(PyExc_ValueError, "unknown validation opcode %d", opcode);
        return NULL;
    }
}

PyObject *
wreath_validate_node(PyObject *plan_object, PyObject *value, PyObject *loc,
                     PyObject *errors, long *steps)
{
    ValidationPlan *plan = validation_plan_get(plan_object);
    if (plan == NULL) {
        PyErr_SetString(PyExc_TypeError,
                        "validation execution requires a compiled native plan");
        return NULL;
    }
    return validate_node(plan->root, value, loc, errors, steps);
}

PyObject *
wreath_validate_plan_field(PyObject *plan_object, Py_ssize_t field_index,
                           PyObject *value, PyObject *loc, PyObject *errors,
                           long *steps)
{
    ValidationPlan *plan = validation_plan_get(plan_object);
    if (plan == NULL || plan->root->opcode != OP_DATACLASS || field_index < 0 ||
        field_index >= plan->root->field_count) {
        PyErr_SetString(PyExc_ValueError,
                        "compiled dataclass validation field is invalid");
        return NULL;
    }
    return validate_node(plan->root->fields[field_index].plan, value, loc,
                         errors, steps);
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
    PyObject *result = wreath_validate_node(plan, value, loc, errors, &steps);
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
    return wreath_tuple2_from_owned(result, errors);
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

    PyObject *source = wreath_validation_plan_source(plan);
    long opcode = PyLong_AsLong(PyTuple_GET_ITEM(source, 0));
    if (opcode == -1 && PyErr_Occurred()) {
        return NULL;
    }
    if (opcode == OP_DICT && PyDict_Check(value)) {
        PyObject *value_plan = PyTuple_GET_ITEM(source, 1);
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
            return wreath_tuple2_from_owned(body, errors);
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
    PyObject *pair = wreath_tuple2_from_owned(body, Py_NewRef(errors));
    Py_DECREF(validated);
    return pair;
}
