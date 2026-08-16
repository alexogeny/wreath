/* Cedar program evaluation for the native core.
 *
 * Walks the compiled tuple program that wreath/_auth/cedar_engine.py produces
 * at startup. tests/test_cedar_engine.py pins the decision, the reason and the
 * diagnostic strings against Cedar's specified semantics, case by case.
 *
 * Value model: PyBool, PyLong (i64), PyUnicode, a (type, id) unicode 2-tuple
 * for an entity uid, a duplicate-free PyList for a set, and a PyDict for a
 * record. Booleans are checked before longs everywhere, because PyBool is a
 * PyLong subtype and Cedar's type system keeps them apart.
 *
 * Error convention: evaluation helpers return NULL in two distinct states.
 * If ctx->error is set (and no Python exception is pending), the failure is a
 * Cedar evaluation error scoped to the one policy being evaluated — it
 * becomes a "skipped" diagnostic. If a Python exception is pending, it is a
 * real error and propagates. This NULL-without-exception contract is
 * deliberate and local to this file.
 */
#include "wreathcore.h"

#define CEDAR_MAX_DEPTH 200

typedef struct {
    PyObject *vars[4];  /* principal, action, resource, context (borrowed) */
    PyObject *store;    /* uid -> (attrs dict, direct-parent tuple) (borrowed) */
    PyObject *error;    /* owned evaluation-error message, or NULL */
} cedar_ctx;

#define CEDAR_DECISION_BATCH_CAPSULE "wreath.cedar.decision_batch"

typedef struct {
    Py_ssize_t count;
    unsigned char *allowed;
    unsigned char *reason;
} CedarDecisionBatch;

static PyObject *cedar_eval(cedar_ctx *ctx, PyObject *node, int depth);

static void
cedar_decision_batch_destroy(PyObject *capsule)
{
    CedarDecisionBatch *batch = PyCapsule_GetPointer(
        capsule, CEDAR_DECISION_BATCH_CAPSULE);
    if (batch == NULL) {
        PyErr_Clear();
        return;
    }
    PyMem_Free(batch->reason);
    PyMem_Free(batch->allowed);
    PyMem_Free(batch);
}

int
wreath_cedar_decision_batch_read(PyObject *object, Py_ssize_t *count,
                                 const unsigned char **allowed,
                                 const unsigned char **reason)
{
    if (!PyCapsule_IsValid(object, CEDAR_DECISION_BATCH_CAPSULE)) return 0;
    CedarDecisionBatch *batch = PyCapsule_GetPointer(
        object, CEDAR_DECISION_BATCH_CAPSULE);
    if (batch == NULL) return -1;
    *count = batch->count;
    *allowed = batch->allowed;
    *reason = batch->reason;
    return 1;
}

static int
cedar_add_i64(int64_t left, int64_t right, int64_t *result)
{
#if defined(__GNUC__) || defined(__clang__)
    return __builtin_add_overflow(left, right, result);
#else
    if ((right > 0 && left > INT64_MAX - right) ||
        (right < 0 && left < INT64_MIN - right)) return 1;
    *result = left + right;
    return 0;
#endif
}

static int
cedar_sub_i64(int64_t left, int64_t right, int64_t *result)
{
#if defined(__GNUC__) || defined(__clang__)
    return __builtin_sub_overflow(left, right, result);
#else
    if ((right > 0 && left < INT64_MIN + right) ||
        (right < 0 && left > INT64_MAX + right)) return 1;
    *result = left - right;
    return 0;
#endif
}

static int
cedar_mul_i64(int64_t left, int64_t right, int64_t *result)
{
#if defined(__GNUC__) || defined(__clang__)
    return __builtin_mul_overflow(left, right, result);
#else
    if (left == 0 || right == 0) {
        *result = 0;
        return 0;
    }
    if ((left == -1 && right == INT64_MIN) ||
        (right == -1 && left == INT64_MIN)) return 1;
    if ((left > 0 && right > 0 && left > INT64_MAX / right) ||
        (left > 0 && right < 0 && right < INT64_MIN / left) ||
        (left < 0 && right > 0 && left < INT64_MIN / right) ||
        (left < 0 && right < 0 && left < INT64_MAX / right)) return 1;
    *result = left * right;
    return 0;
#endif
}

/* Opcodes, operator kinds, variable indexes, and effect/unless flags are
 * small non-negative ints emitted by the compiler in cedar_engine.py; the
 * -1 sentinel cannot collide with a legitimate value. */
static long
cedar_program_int(PyObject *value)
{
    /* native-error-lint: allow NE005 -- compiler-emitted small non-negative int */
    return PyLong_AsLong(value);
}

static PyObject *
cedar_fail(cedar_ctx *ctx, PyObject *message)
{
    /* Takes ownership of message (which may be NULL if formatting failed —
     * then a real Python exception is already pending and propagates). */
    if (message == NULL) {
        return NULL;
    }
    Py_XSETREF(ctx->error, message);
    return NULL;
}

static const char *
cedar_type_name(PyObject *value)
{
    if (PyBool_Check(value)) {
        return "bool";
    }
    if (PyLong_Check(value)) {
        return "long";
    }
    if (PyUnicode_Check(value)) {
        return "string";
    }
    if (PyTuple_Check(value)) {
        return "entity";
    }
    if (PyList_Check(value)) {
        return "set";
    }
    if (PyDict_Check(value)) {
        return "record";
    }
    return Py_TYPE(value)->tp_name;
}

static int
cedar_is_uid(PyObject *value)
{
    return PyTuple_Check(value) && PyTuple_GET_SIZE(value) == 2 &&
           PyUnicode_Check(PyTuple_GET_ITEM(value, 0)) &&
           PyUnicode_Check(PyTuple_GET_ITEM(value, 1));
}

static int cedar_dedupe_key(PyObject *value, PyObject **key);

/* Structural equality: 1 equal, 0 unequal, -1 error (ctx->error or Python). */
static int
cedar_eq(cedar_ctx *ctx, PyObject *a, PyObject *b, int depth)
{
    if (depth > CEDAR_MAX_DEPTH) {
        cedar_fail(ctx, PyUnicode_FromString("value is nested too deeply"));
        return -1;
    }
    if (PyBool_Check(a) || PyBool_Check(b)) {
        return PyBool_Check(a) && PyBool_Check(b) && a == b;
    }
    if (PyLong_Check(a) && PyLong_Check(b)) {
        return PyObject_RichCompareBool(a, b, Py_EQ);
    }
    if (PyUnicode_Check(a) && PyUnicode_Check(b)) {
        int compared = PyUnicode_Compare(a, b);
        if (compared == -1 && PyErr_Occurred()) {
            return -1;
        }
        return compared == 0;
    }
    if (PyTuple_Check(a) && PyTuple_Check(b)) {
        return PyObject_RichCompareBool(a, b, Py_EQ);
    }
    if (PyList_Check(a) && PyList_Check(b)) {
        PyObject *left = NULL, *right = NULL;
        int equal;
        if (PyList_GET_SIZE(a) != PyList_GET_SIZE(b)) return 0;
        if (cedar_dedupe_key(a, &left) < 0 ||
            cedar_dedupe_key(b, &right) < 0) {
            Py_XDECREF(left);
            Py_XDECREF(right);
            return -1;
        }
        equal = PyObject_RichCompareBool(left, right, Py_EQ);
        Py_DECREF(left);
        Py_DECREF(right);
        return equal;
    }
    if (PyDict_Check(a) && PyDict_Check(b)) {
        if (PyDict_Size(a) != PyDict_Size(b)) {
            return 0;
        }
        Py_ssize_t pos = 0;
        PyObject *key, *value;
        while (PyDict_Next(a, &pos, &key, &value)) {
            PyObject *other = PyDict_GetItemWithError(b, key);
            if (other == NULL) {
                return PyErr_Occurred() ? -1 : 0;
            }
            int equal = cedar_eq(ctx, value, other, depth + 1);
            if (equal <= 0) {
                return equal;
            }
        }
        return 1;
    }
    return 0;
}

/* Set membership by structural equality: 1 found, 0 absent, -1 error. */
static int
cedar_set_contains(cedar_ctx *ctx, PyObject *set_list, PyObject *value, int depth)
{
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(set_list); i++) {
        int equal = cedar_eq(ctx, value, PyList_GET_ITEM(set_list, i), depth);
        if (equal != 0) {
            return equal;
        }
    }
    return 0;
}

/* Build a hashable identity for a Cedar value.
 *
 * Returns 0 on success with *key set to an owned reference. Returns -1 with a
 * Python exception set; unlike the evaluation helpers in this file, this one
 * never uses the NULL-without-exception convention.
 *
 * The tag keeps kinds apart because `cedar_eq` does: `True` and `1` are not
 * equal in Cedar's model, but Python compares them equal and hashes them alike,
 * so an untagged key would silently merge them. */
static int
cedar_dedupe_key(PyObject *value, PyObject **key)
{
    const char *tag;
    *key = NULL;
    if (PyBool_Check(value)) {
        tag = "b";               /* before the int test: bool subclasses int */
    }
    else if (PyLong_Check(value)) {
        tag = "i";
    }
    else if (PyUnicode_Check(value)) {
        tag = "s";
    }
    else if (cedar_is_uid(value)) {
        /* A (str, str) entity uid, so hashable by construction. Testing for a
         * uid rather than any tuple is what keeps this total: there is no
         * unhashable case to fall back from, so nothing here clears an
         * exception to signal "not applicable". */
        tag = "t";
    }
    else if (PyList_Check(value)) {
        PyObject *members = PySet_New(NULL);
        PyObject *frozen = NULL;
        if (members == NULL) return -1;
        if (Py_EnterRecursiveCall(" while hashing a Cedar set")) {
            Py_DECREF(members);
            return -1;
        }
        for (Py_ssize_t index = 0; index < PyList_GET_SIZE(value); index++) {
            PyObject *member = NULL;
            if (cedar_dedupe_key(PyList_GET_ITEM(value, index), &member) < 0 ||
                member == NULL || PySet_Add(members, member) < 0) {
                Py_XDECREF(member);
                Py_LeaveRecursiveCall();
                Py_DECREF(members);
                return -1;
            }
            Py_DECREF(member);
        }
        Py_LeaveRecursiveCall();
        frozen = PyFrozenSet_New(members);
        Py_DECREF(members);
        if (frozen == NULL) return -1;
        *key = Py_BuildValue("(sN)", "l", frozen);
        return *key == NULL ? -1 : 0;
    }
    else if (PyDict_Check(value)) {
        PyObject *members = PySet_New(NULL);
        PyObject *frozen = NULL;
        Py_ssize_t position = 0;
        PyObject *name, *item;
        if (members == NULL) return -1;
        if (Py_EnterRecursiveCall(" while hashing a Cedar record")) {
            Py_DECREF(members);
            return -1;
        }
        while (PyDict_Next(value, &position, &name, &item)) {
            PyObject *item_key = NULL;
            PyObject *member = NULL;
            if (cedar_dedupe_key(item, &item_key) < 0 || item_key == NULL) {
                Py_XDECREF(item_key);
                Py_LeaveRecursiveCall();
                Py_DECREF(members);
                return -1;
            }
            member = PyTuple_Pack(2, name, item_key);
            Py_DECREF(item_key);
            if (member == NULL || PySet_Add(members, member) < 0) {
                Py_XDECREF(member);
                Py_LeaveRecursiveCall();
                Py_DECREF(members);
                return -1;
            }
            Py_DECREF(member);
        }
        Py_LeaveRecursiveCall();
        frozen = PyFrozenSet_New(members);
        Py_DECREF(members);
        if (frozen == NULL) return -1;
        *key = Py_BuildValue("(sN)", "r", frozen);
        return *key == NULL ? -1 : 0;
    }
    else {
        PyErr_Format(PyExc_TypeError, "unsupported Cedar value %.200s",
                     Py_TYPE(value)->tp_name);
        return -1;
    }
    *key = Py_BuildValue("(sO)", tag, value);
    return *key == NULL ? -1 : 0;
}

/* Convert caller-owned values into the evaluator's compact value model.
 *
 * This is request work: context is converted for every authorization and
 * entity attributes are converted for every row-level decision.  Keeping the
 * walk here means one Python/C crossing for the complete value graph instead
 * of one interpreter loop per record and set.  Every object below belongs to
 * this conversion; no cache or module-global mutable state survives it. */
static PyObject *
cedar_convert_value(PyObject *value, PyObject *entity_uid_type,
                    PyObject *mapping_type, PyObject *where)
{
    PyObject *result = NULL;
    int is_instance;

    if (Py_EnterRecursiveCall(" while converting a Cedar value")) return NULL;

    if (PyBool_Check(value) || PyUnicode_Check(value)) {
        result = Py_NewRef(value);
        goto done;
    }
    if (PyLong_Check(value)) {
        int overflow = 0;
        (void)PyLong_AsLongLongAndOverflow(value, &overflow);
        if (PyErr_Occurred() && overflow == 0) goto done;
        if (overflow != 0) {
            PyErr_Clear();
            PyErr_Format(
                PyExc_TypeError, "%U: integer %R does not fit in Cedar's i64",
                where, value);
            goto done;
        }
        result = Py_NewRef(value);
        goto done;
    }

    if (PyDict_CheckExact(value)) {
        result = _PyDict_NewPresized(PyDict_GET_SIZE(value));
        if (result == NULL) goto done;
        Py_ssize_t position = 0;
        PyObject *key, *item;
        while (PyDict_Next(value, &position, &key, &item)) {
            if (!PyUnicode_Check(key)) {
                PyErr_Format(
                    PyExc_TypeError,
                    "%U: record keys must be strings, got '%s'",
                    where, Py_TYPE(key)->tp_name);
                Py_CLEAR(result);
                break;
            }
            PyObject *converted = cedar_convert_value(
                item, entity_uid_type, mapping_type, where);
            if (converted == NULL || PyDict_SetItem(result, key, converted) < 0) {
                Py_XDECREF(converted);
                Py_CLEAR(result);
                break;
            }
            Py_DECREF(converted);
        }
        goto done;
    }

    int exact_sequence = PyList_CheckExact(value) || PyTuple_CheckExact(value) ||
                         PySet_CheckExact(value) || PyFrozenSet_CheckExact(value);
    if (exact_sequence && PyObject_Size(value) == 0) {
        result = PyList_New(0);
        goto done;
    }
    if (PySet_CheckExact(value) || PyFrozenSet_CheckExact(value)) {
        PyObject *iterator = PyObject_GetIter(value);
        result = iterator != NULL ? PyList_New(0) : NULL;
        if (result == NULL) {
            Py_XDECREF(iterator);
            goto done;
        }
        PyObject *item;
        while ((item = PyIter_Next(iterator)) != NULL) {
            PyObject *candidate = cedar_convert_value(
                item, entity_uid_type, mapping_type, where);
            Py_DECREF(item);
            if (candidate == NULL || PyList_Append(result, candidate) < 0) {
                Py_XDECREF(candidate);
                Py_CLEAR(result);
                break;
            }
            Py_DECREF(candidate);
        }
        if (result != NULL && PyErr_Occurred()) Py_CLEAR(result);
        Py_DECREF(iterator);
        goto done;
    }
    if (!exact_sequence) {
        is_instance = PyObject_IsInstance(value, entity_uid_type);
        if (is_instance < 0) goto done;
        if (is_instance) {
            PyObject *type = PyObject_GetAttrString(value, "type");
            PyObject *id = type != NULL ? PyObject_GetAttrString(value, "id") : NULL;
            if (id != NULL) result = PyTuple_Pack(2, type, id);
            Py_XDECREF(id);
            Py_XDECREF(type);
            goto done;
        }

        is_instance = PyObject_IsInstance(value, mapping_type);
        if (is_instance < 0) goto done;
        if (is_instance) {
            PyObject *items = PyMapping_Items(value);
            if (items == NULL) goto done;
            Py_ssize_t count = PyList_GET_SIZE(items);
            result = _PyDict_NewPresized(count);
            if (result == NULL) {
                Py_DECREF(items);
                goto done;
            }
            for (Py_ssize_t index = 0; index < count; index++) {
                PyObject *pair = PyList_GET_ITEM(items, index);
                PyObject *key = PyTuple_GET_ITEM(pair, 0);
                PyObject *item = PyTuple_GET_ITEM(pair, 1);
                if (!PyUnicode_Check(key)) {
                    PyErr_Format(
                        PyExc_TypeError,
                        "%U: record keys must be strings, got '%s'",
                        where, Py_TYPE(key)->tp_name);
                    Py_CLEAR(result);
                    break;
                }
                PyObject *converted = cedar_convert_value(
                    item, entity_uid_type, mapping_type, where);
                if (converted == NULL ||
                    PyDict_SetItem(result, key, converted) < 0) {
                    Py_XDECREF(converted);
                    Py_CLEAR(result);
                    break;
                }
                Py_DECREF(converted);
            }
            Py_DECREF(items);
            goto done;
        }
    }

    if (exact_sequence || PyList_Check(value) || PyTuple_Check(value) ||
        PySet_Check(value) || PyFrozenSet_Check(value)) {
        PyObject *iterator = PyObject_GetIter(value);
        PyObject *seen = iterator != NULL ? PySet_New(NULL) : NULL;
        result = seen != NULL ? PyList_New(0) : NULL;
        if (result == NULL) {
            Py_XDECREF(seen);
            Py_XDECREF(iterator);
            goto done;
        }
        PyObject *item;
        while ((item = PyIter_Next(iterator)) != NULL) {
            PyObject *candidate = cedar_convert_value(
                item, entity_uid_type, mapping_type, where);
            Py_DECREF(item);
            if (candidate == NULL) {
                Py_CLEAR(result);
                break;
            }
            PyObject *key = NULL;
            if (cedar_dedupe_key(candidate, &key) < 0) {
                Py_DECREF(candidate);
                Py_CLEAR(result);
                break;
            }
            int duplicate = PySet_Contains(seen, key);
            if (duplicate == 0 && PySet_Add(seen, key) < 0) duplicate = -1;
            Py_DECREF(key);
            if (duplicate < 0 ||
                (duplicate == 0 && PyList_Append(result, candidate) < 0)) {
                Py_DECREF(candidate);
                Py_CLEAR(result);
                break;
            }
            Py_DECREF(candidate);
        }
        if (result != NULL && PyErr_Occurred()) Py_CLEAR(result);
        Py_DECREF(seen);
        Py_DECREF(iterator);
        goto done;
    }

    PyErr_Format(
        PyExc_TypeError,
        "%U: '%s' has no Cedar equivalent; use bool, int, str, EntityUid, "
        "a mapping, or a sequence",
        where, Py_TYPE(value)->tp_name);

done:
    Py_LeaveRecursiveCall();
    return result;
}

PyObject *
wreath_cedar_to_value(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *value, *entity_uid_type, *mapping_type, *where;
    if (!PyArg_ParseTuple(
            args, "OOOU:cedar_to_value", &value, &entity_uid_type,
            &mapping_type, &where)) return NULL;
    return cedar_convert_value(value, entity_uid_type, mapping_type, where);
}

/* Evaluate to a strict bool: 1 true, 0 false, -1 error. */
static int
cedar_eval_bool(cedar_ctx *ctx, PyObject *node, int depth, const char *what)
{
    PyObject *value = cedar_eval(ctx, node, depth + 1);
    if (value == NULL) {
        return -1;
    }
    if (!PyBool_Check(value)) {
        PyObject *message = PyUnicode_FromFormat(
            "%s requires a bool, got %s", what, cedar_type_name(value));
        Py_DECREF(value);
        cedar_fail(ctx, message);
        return -1;
    }
    int result = value == Py_True;
    Py_DECREF(value);
    return result;
}

/* Evaluate to an i64: 0 ok (result stored), -1 error. */
static int
cedar_eval_i64(cedar_ctx *ctx, PyObject *node, int depth, const char *what, int64_t *result)
{
    PyObject *value = cedar_eval(ctx, node, depth + 1);
    if (value == NULL) {
        return -1;
    }
    if (PyBool_Check(value) || !PyLong_Check(value)) {
        PyObject *message = PyUnicode_FromFormat(
            "%s requires a long, got %s", what, cedar_type_name(value));
        Py_DECREF(value);
        cedar_fail(ctx, message);
        return -1;
    }
    int overflow = 0;
    long long extracted = PyLong_AsLongLongAndOverflow(value, &overflow);
    Py_DECREF(value);
    if (extracted == -1 && PyErr_Occurred()) {
        return -1;
    }
    if (overflow != 0) {
        /* The compiler and boundary conversion bound every long to i64;
         * anything wider means a corrupted program, and calling it an
         * evaluation error keeps the policy loop's contract honest. */
        cedar_fail(ctx, PyUnicode_FromString("arithmetic overflowed i64"));
        return -1;
    }
    *result = (int64_t)extracted;
    return 0;
}

/* Reachability over the compact direct-parent graph. `target` is one uid when
 * `targets` is NULL; otherwise `targets` is a set/frozenset. The frontier and
 * visited set live for this evaluation only, so the native path owns no global
 * mutable cache and cycles cost one visit per entity. */
static int
cedar_ancestor_matches(cedar_ctx *ctx, PyObject *uid,
                       PyObject *target, PyObject *targets)
{
    PyObject *entry = PyDict_GetItemWithError(ctx->store, uid);
    PyObject *frontier = NULL, *seen = NULL;
    Py_ssize_t cursor = 0;
    int matched = 0;
    if (entry == NULL) return PyErr_Occurred() ? -1 : 0;
    frontier = PySequence_List(PyTuple_GET_ITEM(entry, 1));
    seen = PySet_New(NULL);
    if (frontier == NULL || seen == NULL) goto error;
    while (cursor < PyList_GET_SIZE(frontier)) {
        PyObject *parent = PyList_GET_ITEM(frontier, cursor++);
        int contains = targets != NULL
            ? PySet_Contains(targets, parent)
            : PyObject_RichCompareBool(target, parent, Py_EQ);
        if (contains < 0) goto error;
        if (contains) {
            matched = 1;
            break;
        }
        contains = PySet_Contains(seen, parent);
        if (contains < 0) goto error;
        if (contains) continue;
        if (PySet_Add(seen, parent) < 0) goto error;
        entry = PyDict_GetItemWithError(ctx->store, parent);
        if (entry == NULL) {
            if (PyErr_Occurred()) goto error;
            continue;
        }
        PyObject *parents = PyTuple_GET_ITEM(entry, 1);
        for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(parents); index++) {
            if (PyList_Append(frontier, PyTuple_GET_ITEM(parents, index)) < 0)
                goto error;
        }
    }
    Py_DECREF(seen);
    Py_DECREF(frontier);
    return matched;
error:
    Py_XDECREF(seen);
    Py_XDECREF(frontier);
    return -1;
}

/* Shared by OP_IN, OP_IS-with-ancestor: 1 in, 0 not, -1 error. */
static int
cedar_in(cedar_ctx *ctx, PyObject *left, PyObject *right)
{
    if (!cedar_is_uid(left)) {
        cedar_fail(ctx, PyUnicode_FromFormat(
            "'in' requires an entity, got %s", cedar_type_name(left)));
        return -1;
    }
    int right_is_list = PyList_Check(right);
    if (!right_is_list && !cedar_is_uid(right)) {
        cedar_fail(ctx, PyUnicode_FromFormat(
            "'in' requires an entity or a set of entities, got %s", cedar_type_name(right)));
        return -1;
    }
    Py_ssize_t count = right_is_list ? PyList_GET_SIZE(right) : 1;
    PyObject *targets = NULL;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *candidate = right_is_list ? PyList_GET_ITEM(right, i) : right;
        if (!cedar_is_uid(candidate)) {
            cedar_fail(ctx, PyUnicode_FromFormat(
                "'in' requires entities on the right, got %s", cedar_type_name(candidate)));
            Py_XDECREF(targets);
            return -1;
        }
        int equal = PyObject_RichCompareBool(candidate, left, Py_EQ);
        if (equal < 0) {
            Py_XDECREF(targets);
            return -1;
        }
        if (equal) {
            Py_XDECREF(targets);
            return 1;
        }
    }
    if (right_is_list) {
        targets = PySet_New(right);
        if (targets == NULL) return -1;
    }
    int matched = cedar_ancestor_matches(
        ctx, left, right_is_list ? NULL : right, targets);
    Py_XDECREF(targets);
    return matched;
}

/* like-pattern match against precompiled segments: 1/0/-1. */
static int
cedar_like(PyObject *value, PyObject *segments)
{
    Py_ssize_t parts = PyTuple_GET_SIZE(segments);
    Py_ssize_t length = PyUnicode_GET_LENGTH(value);
    if (parts == 1) {
        int equal = PyObject_RichCompareBool(value, PyTuple_GET_ITEM(segments, 0), Py_EQ);
        return equal;
    }
    PyObject *head = PyTuple_GET_ITEM(segments, 0);
    PyObject *tail = PyTuple_GET_ITEM(segments, parts - 1);
    Py_ssize_t start = PyUnicode_GET_LENGTH(head);
    Py_ssize_t limit = length - PyUnicode_GET_LENGTH(tail);
    int matches = PyUnicode_Tailmatch(value, head, 0, length, -1);
    if (matches <= 0) {
        return matches;
    }
    matches = PyUnicode_Tailmatch(value, tail, 0, length, 1);
    if (matches <= 0) {
        return matches;
    }
    if (start > limit) {
        return 0;
    }
    for (Py_ssize_t i = 1; i < parts - 1; i++) {
        PyObject *segment = PyTuple_GET_ITEM(segments, i);
        if (PyUnicode_GET_LENGTH(segment) == 0) {
            continue;
        }
        Py_ssize_t found = PyUnicode_Find(value, segment, start, limit, 1);
        if (found == -2) {
            return -1;
        }
        if (found < 0) {
            return 0;
        }
        start = found + PyUnicode_GET_LENGTH(segment);
    }
    return 1;
}

static PyObject *
cedar_eval_arith(cedar_ctx *ctx, PyObject *node, int depth)
{
    int64_t left, right;
    if (cedar_eval_i64(ctx, PyTuple_GET_ITEM(node, 2), depth, "arithmetic", &left) < 0 ||
        cedar_eval_i64(ctx, PyTuple_GET_ITEM(node, 3), depth, "arithmetic", &right) < 0) {
        return NULL;
    }
    long kind = cedar_program_int(PyTuple_GET_ITEM(node, 1));
    int64_t result;
    int overflowed;
    if (kind == 0) {
        overflowed = cedar_add_i64(left, right, &result);
    }
    else if (kind == 1) {
        overflowed = cedar_sub_i64(left, right, &result);
    }
    else {
        overflowed = cedar_mul_i64(left, right, &result);
    }
    if (overflowed) {
        return cedar_fail(ctx, PyUnicode_FromString("arithmetic overflowed i64"));
    }
    return PyLong_FromLongLong((long long)result);
}

static PyObject *
cedar_eval_getattr(cedar_ctx *ctx, PyObject *operand, PyObject *attribute)
{
    if (cedar_is_uid(operand)) {
        PyObject *entry = PyDict_GetItemWithError(ctx->store, operand);
        if (entry == NULL) {
            if (PyErr_Occurred()) {
                return NULL;
            }
            return cedar_fail(ctx, PyUnicode_FromFormat(
                "entity %R has no attributes in this request", operand));
        }
        PyObject *value = PyDict_GetItemWithError(PyTuple_GET_ITEM(entry, 0), attribute);
        if (value == NULL) {
            if (PyErr_Occurred()) {
                return NULL;
            }
            return cedar_fail(ctx, PyUnicode_FromFormat(
                "entity %R has no attribute %R", operand, attribute));
        }
        return Py_NewRef(value);
    }
    if (PyDict_Check(operand)) {
        PyObject *value = PyDict_GetItemWithError(operand, attribute);
        if (value == NULL) {
            if (PyErr_Occurred()) {
                return NULL;
            }
            return cedar_fail(ctx, PyUnicode_FromFormat(
                "record has no attribute %R", attribute));
        }
        return Py_NewRef(value);
    }
    return cedar_fail(ctx, PyUnicode_FromFormat(
        "attribute access requires an entity or record, got %s", cedar_type_name(operand)));
}

static PyObject *
cedar_eval_method(cedar_ctx *ctx, PyObject *node, int depth)
{
    PyObject *operand = cedar_eval(ctx, PyTuple_GET_ITEM(node, 2), depth + 1);
    if (operand == NULL) {
        return NULL;
    }
    if (!PyList_Check(operand)) {
        PyObject *message = PyUnicode_FromFormat(
            "set methods require a set, got %s", cedar_type_name(operand));
        Py_DECREF(operand);
        return cedar_fail(ctx, message);
    }
    long method = cedar_program_int(PyTuple_GET_ITEM(node, 1));
    if (method == 3) { /* isEmpty */
        PyObject *result = PyBool_FromLong(PyList_GET_SIZE(operand) == 0);
        Py_DECREF(operand);
        return result;
    }
    PyObject *argument = cedar_eval(ctx, PyTuple_GET_ITEM(node, 3), depth + 1);
    if (argument == NULL) {
        Py_DECREF(operand);
        return NULL;
    }
    int outcome = -2;
    if (method == 0) { /* contains */
        outcome = cedar_set_contains(ctx, operand, argument, depth);
    }
    else if (!PyList_Check(argument)) {
        cedar_fail(ctx, PyUnicode_FromString("containsAll/containsAny require a set argument"));
    }
    else if (method == 1) { /* containsAll */
        outcome = 1;
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(argument) && outcome == 1; i++) {
            outcome = cedar_set_contains(ctx, operand, PyList_GET_ITEM(argument, i), depth);
        }
    }
    else { /* containsAny */
        outcome = 0;
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(argument) && outcome == 0; i++) {
            outcome = cedar_set_contains(ctx, operand, PyList_GET_ITEM(argument, i), depth);
        }
    }
    Py_DECREF(operand);
    Py_DECREF(argument);
    if (outcome < 0 || outcome == -2) {
        return NULL;
    }
    return PyBool_FromLong(outcome);
}

static PyObject *
cedar_eval(cedar_ctx *ctx, PyObject *node, int depth)
{
    if (depth > CEDAR_MAX_DEPTH) {
        return cedar_fail(ctx, PyUnicode_FromString("expression is nested too deeply"));
    }
    long op = cedar_program_int(PyTuple_GET_ITEM(node, 0));
    switch (op) {
    case 0: /* CONST */
        return Py_NewRef(PyTuple_GET_ITEM(node, 1));
    case 1: /* VAR */
        return Py_NewRef(ctx->vars[cedar_program_int(PyTuple_GET_ITEM(node, 1))]);
    case 2: { /* AND */
        int left = cedar_eval_bool(ctx, PyTuple_GET_ITEM(node, 1), depth, "&&");
        if (left < 0) {
            return NULL;
        }
        if (!left) {
            Py_RETURN_FALSE;
        }
        int right = cedar_eval_bool(ctx, PyTuple_GET_ITEM(node, 2), depth, "&&");
        if (right < 0) {
            return NULL;
        }
        return PyBool_FromLong(right);
    }
    case 3: { /* OR */
        int left = cedar_eval_bool(ctx, PyTuple_GET_ITEM(node, 1), depth, "||");
        if (left < 0) {
            return NULL;
        }
        if (left) {
            Py_RETURN_TRUE;
        }
        int right = cedar_eval_bool(ctx, PyTuple_GET_ITEM(node, 2), depth, "||");
        if (right < 0) {
            return NULL;
        }
        return PyBool_FromLong(right);
    }
    case 4: { /* NOT */
        int value = cedar_eval_bool(ctx, PyTuple_GET_ITEM(node, 1), depth, "!");
        if (value < 0) {
            return NULL;
        }
        return PyBool_FromLong(!value);
    }
    case 5: /* ARITH */
        return cedar_eval_arith(ctx, node, depth);
    case 6: { /* CMP */
        int64_t left, right;
        if (cedar_eval_i64(ctx, PyTuple_GET_ITEM(node, 2), depth, "comparison", &left) < 0 ||
            cedar_eval_i64(ctx, PyTuple_GET_ITEM(node, 3), depth, "comparison", &right) < 0) {
            return NULL;
        }
        long kind = cedar_program_int(PyTuple_GET_ITEM(node, 1));
        int result;
        if (kind == 0) {
            result = left < right;
        }
        else if (kind == 1) {
            result = left <= right;
        }
        else if (kind == 2) {
            result = left > right;
        }
        else {
            result = left >= right;
        }
        return PyBool_FromLong(result);
    }
    case 7:   /* EQ */
    case 8: { /* NE */
        PyObject *left = cedar_eval(ctx, PyTuple_GET_ITEM(node, 1), depth + 1);
        if (left == NULL) {
            return NULL;
        }
        PyObject *right = cedar_eval(ctx, PyTuple_GET_ITEM(node, 2), depth + 1);
        if (right == NULL) {
            Py_DECREF(left);
            return NULL;
        }
        int equal = cedar_eq(ctx, left, right, 0);
        Py_DECREF(left);
        Py_DECREF(right);
        if (equal < 0) {
            return NULL;
        }
        return PyBool_FromLong(op == 7 ? equal : !equal);
    }
    case 9: { /* IN */
        PyObject *left = cedar_eval(ctx, PyTuple_GET_ITEM(node, 1), depth + 1);
        if (left == NULL) {
            return NULL;
        }
        if (!cedar_is_uid(left)) {
            PyObject *message = PyUnicode_FromFormat(
                "'in' requires an entity, got %s", cedar_type_name(left));
            Py_DECREF(left);
            return cedar_fail(ctx, message);
        }
        PyObject *right = cedar_eval(ctx, PyTuple_GET_ITEM(node, 2), depth + 1);
        if (right == NULL) {
            Py_DECREF(left);
            return NULL;
        }
        int result = cedar_in(ctx, left, right);
        Py_DECREF(left);
        Py_DECREF(right);
        if (result < 0) {
            return NULL;
        }
        return PyBool_FromLong(result);
    }
    case 10: { /* HAS */
        PyObject *operand = cedar_eval(ctx, PyTuple_GET_ITEM(node, 1), depth + 1);
        if (operand == NULL) {
            return NULL;
        }
        PyObject *attribute = PyTuple_GET_ITEM(node, 2);
        int present;
        if (cedar_is_uid(operand)) {
            PyObject *entry = PyDict_GetItemWithError(ctx->store, operand);
            if (entry == NULL && PyErr_Occurred()) {
                Py_DECREF(operand);
                return NULL;
            }
            present = entry != NULL &&
                      PyDict_Contains(PyTuple_GET_ITEM(entry, 0), attribute) == 1;
        }
        else if (PyDict_Check(operand)) {
            present = PyDict_Contains(operand, attribute);
        }
        else {
            PyObject *message = PyUnicode_FromFormat(
                "'has' requires an entity or record, got %s", cedar_type_name(operand));
            Py_DECREF(operand);
            return cedar_fail(ctx, message);
        }
        Py_DECREF(operand);
        if (present < 0) {
            return NULL;
        }
        return PyBool_FromLong(present);
    }
    case 11: { /* LIKE */
        PyObject *operand = cedar_eval(ctx, PyTuple_GET_ITEM(node, 1), depth + 1);
        if (operand == NULL) {
            return NULL;
        }
        if (!PyUnicode_Check(operand)) {
            PyObject *message = PyUnicode_FromFormat(
                "'like' requires a string, got %s", cedar_type_name(operand));
            Py_DECREF(operand);
            return cedar_fail(ctx, message);
        }
        int result = cedar_like(operand, PyTuple_GET_ITEM(node, 2));
        Py_DECREF(operand);
        if (result < 0) {
            return NULL;
        }
        return PyBool_FromLong(result);
    }
    case 12: { /* IS */
        PyObject *operand = cedar_eval(ctx, PyTuple_GET_ITEM(node, 1), depth + 1);
        if (operand == NULL) {
            return NULL;
        }
        if (!cedar_is_uid(operand)) {
            PyObject *message = PyUnicode_FromFormat(
                "'is' requires an entity, got %s", cedar_type_name(operand));
            Py_DECREF(operand);
            return cedar_fail(ctx, message);
        }
        int same = PyUnicode_Compare(
            PyTuple_GET_ITEM(operand, 0), PyTuple_GET_ITEM(node, 2));
        if (same != 0) {
            Py_DECREF(operand);
            if (PyErr_Occurred()) {
                return NULL;
            }
            Py_RETURN_FALSE;
        }
        PyObject *ancestor_node = PyTuple_GET_ITEM(node, 3);
        if (ancestor_node == Py_None) {
            Py_DECREF(operand);
            Py_RETURN_TRUE;
        }
        PyObject *ancestor = cedar_eval(ctx, ancestor_node, depth + 1);
        if (ancestor == NULL) {
            Py_DECREF(operand);
            return NULL;
        }
        int result = cedar_in(ctx, operand, ancestor);
        Py_DECREF(operand);
        Py_DECREF(ancestor);
        if (result < 0) {
            return NULL;
        }
        return PyBool_FromLong(result);
    }
    case 13: { /* IF */
        int condition = cedar_eval_bool(ctx, PyTuple_GET_ITEM(node, 1), depth, "if");
        if (condition < 0) {
            return NULL;
        }
        return cedar_eval(ctx, PyTuple_GET_ITEM(node, condition ? 2 : 3), depth + 1);
    }
    case 14: { /* SET */
        /* Every converted Cedar value has a tagged structural key.  Nested
         * sets and records therefore take the same linear expected-time path
         * as scalars instead of falling back to pairwise comparison. */
        PyObject *items = PyTuple_GET_ITEM(node, 1);
        PyObject *result = PyList_New(0);
        PyObject *seen = PySet_New(NULL);
        if (result == NULL || seen == NULL) {
            Py_XDECREF(result);
            Py_XDECREF(seen);
            return NULL;
        }
        for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(items); i++) {
            PyObject *item = cedar_eval(ctx, PyTuple_GET_ITEM(items, i), depth + 1);
            if (item == NULL) {
                goto set_error;
            }
            PyObject *key;
            if (cedar_dedupe_key(item, &key) < 0) {
                Py_DECREF(item);
                goto set_error;
            }
            int present = PySet_Contains(seen, key);
            if (present < 0 || (present == 0 && PySet_Add(seen, key) < 0)) {
                Py_DECREF(key);
                Py_DECREF(item);
                goto set_error;
            }
            Py_DECREF(key);
            if (present) {
                Py_DECREF(item);
                continue;
            }
            if (PyList_Append(result, item) < 0) {
                Py_DECREF(item);
                goto set_error;
            }
            Py_DECREF(item);
        }
        Py_DECREF(seen);
        return result;

    set_error:
        Py_DECREF(result);
        Py_DECREF(seen);
        return NULL;
    }
    case 15: { /* RECORD */
        PyObject *entries = PyTuple_GET_ITEM(node, 1);
        PyObject *result = PyDict_New();
        if (result == NULL) {
            return NULL;
        }
        for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(entries); i++) {
            PyObject *entry = PyTuple_GET_ITEM(entries, i);
            PyObject *value = cedar_eval(ctx, PyTuple_GET_ITEM(entry, 1), depth + 1);
            if (value == NULL ||
                PyDict_SetItem(result, PyTuple_GET_ITEM(entry, 0), value) < 0) {
                Py_XDECREF(value);
                Py_DECREF(result);
                return NULL;
            }
            Py_DECREF(value);
        }
        return result;
    }
    case 16: { /* GETATTR */
        PyObject *operand = cedar_eval(ctx, PyTuple_GET_ITEM(node, 1), depth + 1);
        if (operand == NULL) {
            return NULL;
        }
        PyObject *result = cedar_eval_getattr(ctx, operand, PyTuple_GET_ITEM(node, 2));
        Py_DECREF(operand);
        return result;
    }
    case 17: /* METHOD */
        return cedar_eval_method(ctx, node, depth);
    default:
        return cedar_fail(ctx, PyUnicode_FromFormat("unknown opcode %ld", op));
    }
}

/* Scope match: 1 matched, 0 not, -1 real error. Scopes never eval-error. */
static int
cedar_scope_matches(cedar_ctx *ctx, PyObject *scope, PyObject *uid)
{
    long kind = cedar_program_int(PyTuple_GET_ITEM(scope, 0));
    if (kind == 0) { /* any */
        return 1;
    }
    if (kind == 1) { /* == */
        return PyObject_RichCompareBool(uid, PyTuple_GET_ITEM(scope, 1), Py_EQ);
    }
    if (kind == 2) { /* in */
        PyObject *target = PyTuple_GET_ITEM(scope, 1);
        int equal = PyObject_RichCompareBool(uid, target, Py_EQ);
        if (equal != 0) {
            return equal;
        }
        return cedar_ancestor_matches(ctx, uid, target, NULL);
    }
    if (kind == 3) { /* in [..] */
        PyObject *targets = PyTuple_GET_ITEM(scope, 1);
        int member = PySet_Contains(targets, uid);
        if (member != 0) {
            return member;
        }
        return cedar_ancestor_matches(ctx, uid, NULL, targets);
    }
    /* is */
    int same = PyUnicode_Compare(PyTuple_GET_ITEM(uid, 0), PyTuple_GET_ITEM(scope, 1));
    if (PyErr_Occurred()) {
        return -1;
    }
    if (same != 0) {
        return 0;
    }
    PyObject *ancestor = PyTuple_GET_ITEM(scope, 2);
    if (ancestor == Py_None) {
        return 1;
    }
    int equal = PyObject_RichCompareBool(uid, ancestor, Py_EQ);
    if (equal != 0) {
        return equal;
    }
    return cedar_ancestor_matches(ctx, uid, ancestor, NULL);
}

/* Conditions for a policy whose scopes already matched. */
static int
cedar_policy_conditions(cedar_ctx *ctx, PyObject *policy)
{
    PyObject *conditions = PyTuple_GET_ITEM(policy, 5);
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(conditions); i++) {
        PyObject *condition = PyTuple_GET_ITEM(conditions, i);
        long unless = cedar_program_int(PyTuple_GET_ITEM(condition, 0));
        PyObject *value = cedar_eval(ctx, PyTuple_GET_ITEM(condition, 1), 0);
        if (value == NULL) {
            return PyErr_Occurred() ? -2 : -1;
        }
        if (!PyBool_Check(value)) {
            PyObject *message = PyUnicode_FromFormat(
                "condition evaluated to %s, not bool", cedar_type_name(value));
            Py_DECREF(value);
            cedar_fail(ctx, message);
            return message == NULL ? -2 : -1;
        }
        int truth = value == Py_True;
        Py_DECREF(value);
        if ((unless != 0) == truth) {
            return 0;
        }
    }
    return 1;
}

/* One policy: 1 satisfied, 0 not, -1 eval error (ctx->error), -2 real error. */
static int
cedar_policy_satisfied(cedar_ctx *ctx, PyObject *policy)
{
    static const int scope_slots[3] = {2, 3, 4};
    for (int i = 0; i < 3; i++) {
        int matched = cedar_scope_matches(
            ctx, PyTuple_GET_ITEM(policy, scope_slots[i]), ctx->vars[i]);
        if (matched < 0) return -2;
        if (!matched) return 0;
    }
    return cedar_policy_conditions(ctx, policy);
}

static int
cedar_policy_satisfied_resource(cedar_ctx *ctx, PyObject *policy)
{
    int matched = cedar_scope_matches(
        ctx, PyTuple_GET_ITEM(policy, 4), ctx->vars[2]);
    if (matched < 0) return -2;
    if (!matched) return 0;
    return cedar_policy_conditions(ctx, policy);
}

/* Compiled expressions are immutable tuples.  Constants are leaves even when
 * their value is itself a tuple (for example an entity uid), so this walk can
 * conservatively prove that a condition never reads vars[2] (`resource`). */
static int
cedar_program_reads_resource(PyObject *node, int depth)
{
    if (!PyTuple_Check(node) || PyTuple_GET_SIZE(node) == 0) return 0;
    if (depth >= 64) return 1;
    PyObject *opcode = PyTuple_GET_ITEM(node, 0);
    if (!PyLong_Check(opcode)) {
        for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(node); index++)
            if (cedar_program_reads_resource(
                    PyTuple_GET_ITEM(node, index), depth + 1)) return 1;
        return 0;
    }
    long op = cedar_program_int(opcode);
    if (op == 0) return 0; /* CONST */
    if (op == 1 && PyTuple_GET_SIZE(node) >= 2)
        return cedar_program_int(PyTuple_GET_ITEM(node, 1)) == 2;
    for (Py_ssize_t index = 1; index < PyTuple_GET_SIZE(node); index++)
        if (cedar_program_reads_resource(
                PyTuple_GET_ITEM(node, index), depth + 1)) return 1;
    return 0;
}

static int
cedar_policy_is_resource_independent(PyObject *policy)
{
    PyObject *resource_scope = PyTuple_GET_ITEM(policy, 4);
    if (cedar_program_int(PyTuple_GET_ITEM(resource_scope, 0)) != 0) return 0;
    PyObject *conditions = PyTuple_GET_ITEM(policy, 5);
    for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(conditions); index++) {
        PyObject *condition = PyTuple_GET_ITEM(conditions, index);
        if (cedar_program_reads_resource(PyTuple_GET_ITEM(condition, 1), 0))
            return 0;
    }
    return 1;
}

static PyObject *
cedar_authorize_one(PyObject *policies, PyObject *principal, PyObject *action,
                    PyObject *resource, PyObject *context, PyObject *store,
                    const unsigned char *shared_matches,
                    const signed char *shared_outcomes,
                    PyObject **matched_lines, PyObject **reason_cache)
{
    cedar_ctx ctx = {{principal, action, resource, context}, store, NULL};
    Py_ssize_t policy_count = PyTuple_GET_SIZE(policies);
    PyObject *diagnostics = PyTuple_New(policy_count);
    if (diagnostics == NULL) {
        return NULL;
    }
    Py_ssize_t diagnostic_count = 0;
    int permitted = 0;
    int forbidden = 0;
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(policies); i++) {
        PyObject *policy = PyTuple_GET_ITEM(policies, i);
        long forbid = cedar_program_int(PyTuple_GET_ITEM(policy, 0));
        PyObject *policy_id = PyTuple_GET_ITEM(policy, 1);
        const char *effect = forbid ? "forbid" : "permit";
        int outcome;
        if (shared_matches == NULL) outcome = cedar_policy_satisfied(&ctx, policy);
        else if (!shared_matches[i]) outcome = 0;
        else if (shared_outcomes[i] >= 0) outcome = shared_outcomes[i];
        else outcome = cedar_policy_satisfied_resource(&ctx, policy);
        if (outcome == -2) {
            goto error;
        }
        if (outcome == -1) {
            PyObject *line = PyUnicode_FromFormat(
                "%s %U skipped: %U", effect, policy_id, ctx.error);
            Py_CLEAR(ctx.error);
            if (line == NULL) goto error;
            PyTuple_SET_ITEM(diagnostics, diagnostic_count++, line);
            continue;
        }
        if (outcome == 1) {
            PyObject *line = matched_lines != NULL ? matched_lines[i] : NULL;
            if (line != NULL) line = Py_NewRef(line);
            else {
                line = PyUnicode_FromFormat("%s %U matched", effect, policy_id);
                if (line == NULL) goto error;
                if (matched_lines != NULL) matched_lines[i] = Py_NewRef(line);
            }
            PyTuple_SET_ITEM(diagnostics, diagnostic_count++, line);
            if (forbid) {
                forbidden = 1;
            }
            else {
                permitted = 1;
            }
        }
    }
    {
        if (diagnostic_count != policy_count &&
            _PyTuple_Resize(&diagnostics, diagnostic_count) < 0) return NULL;
        const char *reason = forbidden ? "explicit forbid"
                             : permitted ? "cedar permit"
                                         : "no permit policy matched";
        int reason_index = forbidden ? 2 : permitted ? 1 : 0;
        PyObject *reason_object = reason_cache != NULL
            ? reason_cache[reason_index] : NULL;
        if (reason_object != NULL) reason_object = Py_NewRef(reason_object);
        else {
            reason_object = PyUnicode_FromString(reason);
            if (reason_object != NULL && reason_cache != NULL)
                reason_cache[reason_index] = Py_NewRef(reason_object);
        }
        if (reason_object == NULL) {
            Py_DECREF(diagnostics);
            return NULL;
        }
        PyObject *allowed = (forbidden || !permitted) ? Py_False : Py_True;
        PyObject *result = PyTuple_New(3);
        if (result == NULL) {
            Py_DECREF(reason_object);
            Py_DECREF(diagnostics);
            return NULL;
        }
        PyTuple_SET_ITEM(result, 0, Py_NewRef(allowed));
        PyTuple_SET_ITEM(result, 1, reason_object);
        PyTuple_SET_ITEM(result, 2, diagnostics);
        return result;
    }
error:
    Py_XDECREF(ctx.error);
    Py_DECREF(diagnostics);
    return NULL;
}

static int
cedar_authorize_compact_one(
    PyObject *policies, PyObject *principal, PyObject *action,
    PyObject *resource, PyObject *context, PyObject *store,
    const unsigned char *shared_matches, const signed char *shared_outcomes,
    unsigned char *allowed, unsigned char *reason)
{
    cedar_ctx ctx = {{principal, action, resource, context}, store, NULL};
    int permitted = 0;
    int forbidden = 0;
    Py_ssize_t policy_count = PyTuple_GET_SIZE(policies);
    for (Py_ssize_t index = 0; index < policy_count; index++) {
        PyObject *policy = PyTuple_GET_ITEM(policies, index);
        int outcome;
        if (shared_matches == NULL)
            outcome = cedar_policy_satisfied(&ctx, policy);
        else if (!shared_matches[index]) outcome = 0;
        else if (shared_outcomes[index] >= 0)
            outcome = shared_outcomes[index];
        else outcome = cedar_policy_satisfied_resource(&ctx, policy);
        if (outcome == -2) {
            Py_XDECREF(ctx.error);
            return -1;
        }
        if (outcome == -1) {
            Py_CLEAR(ctx.error);
            continue;
        }
        if (outcome == 1) {
            if (cedar_program_int(PyTuple_GET_ITEM(policy, 0))) forbidden = 1;
            else permitted = 1;
        }
    }
    *allowed = (unsigned char)(!forbidden && permitted);
    *reason = (unsigned char)(forbidden ? 2 : permitted ? 1 : 0);
    return 0;
}

PyObject *
wreath_cedar_is_authorized(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *policies, *principal, *action, *resource, *context, *store;
    if (!PyArg_ParseTuple(args, "O!OOOO!O!:cedar_is_authorized",
                          &PyTuple_Type, &policies, &principal, &action, &resource,
                          &PyDict_Type, &context, &PyDict_Type, &store)) {
        return NULL;
    }
    return cedar_authorize_one(
        policies, principal, action, resource, context, store,
        NULL, NULL, NULL, NULL);
}

PyObject *
wreath_cedar_route_denial(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *policies, *principal, *action, *resource, *context, *store;
    if (!PyArg_ParseTuple(args, "O!OOOO!O!:cedar_route_denial",
                          &PyTuple_Type, &policies, &principal, &action, &resource,
                          &PyDict_Type, &context, &PyDict_Type, &store)) {
        return NULL;
    }
    unsigned char allowed, reason;
    if (cedar_authorize_compact_one(
            policies, principal, action, resource, context, store,
            NULL, NULL, &allowed, &reason) < 0) return NULL;
    if (allowed) Py_RETURN_NONE;
    static const char *reasons[] = {
        "no permit policy matched", "cedar permit", "explicit forbid",
    };
    if (reason > 2) {
        PyErr_SetString(PyExc_RuntimeError, "invalid native Cedar reason");
        return NULL;
    }
    return PyUnicode_FromString(reasons[reason]);
}

PyObject *
wreath_cedar_is_authorized_many(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *policies, *principal, *action, *resources, *context, *store;
    int stop_on_denied;
    if (!PyArg_ParseTuple(
            args, "O!OOO!O!O!p:cedar_is_authorized_many",
            &PyTuple_Type, &policies, &principal, &action,
            &PyTuple_Type, &resources, &PyDict_Type, &context,
            &PyDict_Type, &store, &stop_on_denied)) return NULL;

    Py_ssize_t policy_count = PyTuple_GET_SIZE(policies);
    unsigned char *shared_matches = policy_count != 0
        ? PyMem_Malloc((size_t)policy_count) : NULL;
    signed char *shared_outcomes = policy_count != 0
        ? PyMem_Malloc((size_t)policy_count) : NULL;
    PyObject **matched_lines = policy_count != 0
        ? PyMem_Calloc((size_t)policy_count, sizeof(*matched_lines)) : NULL;
    if (policy_count != 0 &&
        (shared_matches == NULL || shared_outcomes == NULL ||
         matched_lines == NULL)) {
        PyMem_Free(shared_matches);
        PyMem_Free(shared_outcomes);
        PyMem_Free(matched_lines);
        return PyErr_NoMemory();
    }
    PyObject *reason_cache[3] = {NULL, NULL, NULL};
    cedar_ctx shared = {{principal, action, Py_None, context}, store, NULL};
    for (Py_ssize_t index = 0; index < policy_count; index++) {
        PyObject *policy = PyTuple_GET_ITEM(policies, index);
        int principal_match = cedar_scope_matches(
            &shared, PyTuple_GET_ITEM(policy, 2), principal);
        int action_match = principal_match > 0 ? cedar_scope_matches(
            &shared, PyTuple_GET_ITEM(policy, 3), action) : principal_match;
        if (action_match < 0) {
            PyMem_Free(shared_matches);
            PyMem_Free(shared_outcomes);
            PyMem_Free(matched_lines);
            Py_XDECREF(shared.error);
            return NULL;
        }
        shared_matches[index] = action_match > 0;
        shared_outcomes[index] = -1;
        if (action_match > 0 && cedar_policy_is_resource_independent(policy)) {
            int outcome = cedar_policy_conditions(&shared, policy);
            if (outcome == -2) {
                PyMem_Free(shared_matches);
                PyMem_Free(shared_outcomes);
                PyMem_Free(matched_lines);
                Py_XDECREF(shared.error);
                return NULL;
            }
            if (outcome >= 0) shared_outcomes[index] = (signed char)outcome;
            else Py_CLEAR(shared.error);
        }
    }

    Py_ssize_t resource_count = PyTuple_GET_SIZE(resources);
    PyObject *results = PyTuple_New(resource_count);
    if (results == NULL) {
        PyMem_Free(shared_matches);
        PyMem_Free(shared_outcomes);
        PyMem_Free(matched_lines);
        return NULL;
    }
    Py_ssize_t result_count = 0;
    for (; result_count < resource_count; result_count++) {
        PyObject *result = cedar_authorize_one(
            policies, principal, action,
            PyTuple_GET_ITEM(resources, result_count), context, store,
            shared_matches, shared_outcomes, matched_lines, reason_cache);
        if (result == NULL) {
            Py_DECREF(results);
            PyMem_Free(shared_matches);
            PyMem_Free(shared_outcomes);
            for (Py_ssize_t index = 0; index < policy_count; index++)
                Py_XDECREF(matched_lines[index]);
            PyMem_Free(matched_lines);
            for (int index = 0; index < 3; index++)
                Py_XDECREF(reason_cache[index]);
            return NULL;
        }
        int allowed = PyTuple_GET_ITEM(result, 0) == Py_True;
        PyTuple_SET_ITEM(results, result_count, result);
        if (stop_on_denied && !allowed) {
            result_count++;
            break;
        }
    }
    PyMem_Free(shared_matches);
    PyMem_Free(shared_outcomes);
    for (Py_ssize_t index = 0; index < policy_count; index++)
        Py_XDECREF(matched_lines[index]);
    PyMem_Free(matched_lines);
    for (int index = 0; index < 3; index++)
        Py_XDECREF(reason_cache[index]);
    if (result_count != resource_count &&
        _PyTuple_Resize(&results, result_count) < 0) return NULL;
    return results;
}

PyObject *
wreath_cedar_is_authorized_many_native(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *policies, *principal, *action, *resources, *context, *store;
    int stop_on_denied;
    if (!PyArg_ParseTuple(
            args, "O!OOO!O!O!p:cedar_is_authorized_many_native",
            &PyTuple_Type, &policies, &principal, &action,
            &PyTuple_Type, &resources, &PyDict_Type, &context,
            &PyDict_Type, &store, &stop_on_denied)) return NULL;

    Py_ssize_t policy_count = PyTuple_GET_SIZE(policies);
    unsigned char *shared_matches = policy_count != 0
        ? PyMem_Malloc((size_t)policy_count) : NULL;
    signed char *shared_outcomes = policy_count != 0
        ? PyMem_Malloc((size_t)policy_count) : NULL;
    if (policy_count != 0 &&
        (shared_matches == NULL || shared_outcomes == NULL)) {
        PyMem_Free(shared_matches);
        PyMem_Free(shared_outcomes);
        return PyErr_NoMemory();
    }
    cedar_ctx shared = {{principal, action, Py_None, context}, store, NULL};
    for (Py_ssize_t index = 0; index < policy_count; index++) {
        PyObject *policy = PyTuple_GET_ITEM(policies, index);
        int principal_match = cedar_scope_matches(
            &shared, PyTuple_GET_ITEM(policy, 2), principal);
        int action_match = principal_match > 0 ? cedar_scope_matches(
            &shared, PyTuple_GET_ITEM(policy, 3), action) : principal_match;
        if (action_match < 0) goto error;
        shared_matches[index] = (unsigned char)(action_match > 0);
        shared_outcomes[index] = -1;
        if (action_match > 0 && cedar_policy_is_resource_independent(policy)) {
            int outcome = cedar_policy_conditions(&shared, policy);
            if (outcome == -2) goto error;
            if (outcome >= 0) shared_outcomes[index] = (signed char)outcome;
            else Py_CLEAR(shared.error);
        }
    }

    Py_ssize_t resource_count = PyTuple_GET_SIZE(resources);
    CedarDecisionBatch *batch = PyMem_Calloc(1, sizeof(*batch));
    if (batch == NULL) goto memory;
    if (resource_count != 0) {
        batch->allowed = PyMem_Malloc((size_t)resource_count);
        batch->reason = PyMem_Malloc((size_t)resource_count);
        if (batch->allowed == NULL || batch->reason == NULL) {
            PyMem_Free(batch->reason);
            PyMem_Free(batch->allowed);
            PyMem_Free(batch);
            goto memory;
        }
    }
    for (; batch->count < resource_count; batch->count++) {
        if (cedar_authorize_compact_one(
                policies, principal, action,
                PyTuple_GET_ITEM(resources, batch->count), context, store,
                shared_matches, shared_outcomes,
                &batch->allowed[batch->count], &batch->reason[batch->count]) < 0) {
            PyMem_Free(batch->reason);
            PyMem_Free(batch->allowed);
            PyMem_Free(batch);
            goto error;
        }
        if (stop_on_denied && !batch->allowed[batch->count]) {
            batch->count++;
            break;
        }
    }
    PyMem_Free(shared_outcomes);
    PyMem_Free(shared_matches);
    return PyCapsule_New(
        batch, CEDAR_DECISION_BATCH_CAPSULE, cedar_decision_batch_destroy);

memory:
    PyErr_NoMemory();
error:
    Py_XDECREF(shared.error);
    PyMem_Free(shared_outcomes);
    PyMem_Free(shared_matches);
    return NULL;
}
