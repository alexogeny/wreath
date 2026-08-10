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
    PyObject *store;    /* uid -> (attrs dict, ancestors frozenset) (borrowed) */
    PyObject *error;    /* owned evaluation-error message, or NULL */
} cedar_ctx;

static PyObject *cedar_eval(cedar_ctx *ctx, PyObject *node, int depth);

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
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(a); i++) {
            int found = 0;
            for (Py_ssize_t j = 0; j < PyList_GET_SIZE(b) && !found; j++) {
                found = cedar_eq(ctx, PyList_GET_ITEM(a, i), PyList_GET_ITEM(b, j), depth + 1);
                if (found < 0) {
                    return -1;
                }
            }
            if (!found) {
                return 0;
            }
        }
        for (Py_ssize_t j = 0; j < PyList_GET_SIZE(b); j++) {
            int found = 0;
            for (Py_ssize_t i = 0; i < PyList_GET_SIZE(a) && !found; i++) {
                found = cedar_eq(ctx, PyList_GET_ITEM(b, j), PyList_GET_ITEM(a, i), depth + 1);
                if (found < 0) {
                    return -1;
                }
            }
            if (!found) {
                return 0;
            }
        }
        return 1;
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
 * Returns 0 on success with *key set to an owned reference, or to NULL when the
 * value needs structural comparison instead. Returns -1 with a Python exception
 * set on a real failure; unlike the evaluation helpers in this file, this one
 * never uses the NULL-without-exception convention.
 *
 * The tag keeps kinds apart because `cedar_eq` does: `True` and `1` are not
 * equal in Cedar's model, but Python compares them equal and hashes them alike,
 * so an untagged key would silently merge them. Mirrors `_dedupe_key` in
 * `_auth/cedar_engine.py`. */
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
    else {
        return 0;                /* records and nested sets: structural */
    }
    *key = Py_BuildValue("(sO)", tag, value);
    return *key == NULL ? -1 : 0;
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

static PyObject *
cedar_ancestors(cedar_ctx *ctx, PyObject *uid)
{
    /* Borrowed frozenset of ancestors, or NULL when the uid has no entry
     * (which is not an error: an undeclared entity has no ancestors). */
    PyObject *entry = PyDict_GetItemWithError(ctx->store, uid);
    if (entry == NULL) {
        return NULL;
    }
    return PyTuple_GET_ITEM(entry, 1);
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
    PyObject *ancestors = cedar_ancestors(ctx, left);
    if (ancestors == NULL && PyErr_Occurred()) {
        return -1;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *candidate = right_is_list ? PyList_GET_ITEM(right, i) : right;
        if (!cedar_is_uid(candidate)) {
            cedar_fail(ctx, PyUnicode_FromFormat(
                "'in' requires entities on the right, got %s", cedar_type_name(candidate)));
            return -1;
        }
        int equal = PyObject_RichCompareBool(candidate, left, Py_EQ);
        if (equal < 0) {
            return -1;
        }
        if (equal) {
            return 1;
        }
        if (ancestors != NULL) {
            int member = PySet_Contains(ancestors, candidate);
            if (member < 0) {
                return -1;
            }
            if (member) {
                return 1;
            }
        }
    }
    return 0;
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
        overflowed = __builtin_add_overflow(left, right, &result);
    }
    else if (kind == 1) {
        overflowed = __builtin_sub_overflow(left, right, &result);
    }
    else {
        overflowed = __builtin_mul_overflow(left, right, &result);
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
        /* Deduplicated by hash where the member allows it. Scanning the kept
         * list per candidate is O(n**2), and a set literal is re-evaluated on
         * every authorization, so a policy holding a few hundred entries (an
         * allowlist, a tenant list) paid that per request. Only records and
         * nested sets, which cannot be hashed, still compare structurally --
         * and only against each other. */
        PyObject *items = PyTuple_GET_ITEM(node, 1);
        PyObject *result = PyList_New(0);
        PyObject *seen = PySet_New(NULL);
        PyObject *structural = PyList_New(0);
        if (result == NULL || seen == NULL || structural == NULL) {
            Py_XDECREF(result);
            Py_XDECREF(seen);
            Py_XDECREF(structural);
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
            if (key != NULL) {
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
            }
            else {
                int present = cedar_set_contains(ctx, structural, item, 0);
                if (present < 0 ||
                    (present == 0 && PyList_Append(structural, item) < 0)) {
                    Py_DECREF(item);
                    goto set_error;
                }
                if (present) {
                    Py_DECREF(item);
                    continue;
                }
            }
            if (PyList_Append(result, item) < 0) {
                Py_DECREF(item);
                goto set_error;
            }
            Py_DECREF(item);
        }
        Py_DECREF(seen);
        Py_DECREF(structural);
        return result;

    set_error:
        Py_DECREF(result);
        Py_DECREF(seen);
        Py_DECREF(structural);
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
    PyObject *ancestors;
    if (kind == 2) { /* in */
        PyObject *target = PyTuple_GET_ITEM(scope, 1);
        int equal = PyObject_RichCompareBool(uid, target, Py_EQ);
        if (equal != 0) {
            return equal;
        }
        ancestors = cedar_ancestors(ctx, uid);
        if (ancestors == NULL) {
            return PyErr_Occurred() ? -1 : 0;
        }
        return PySet_Contains(ancestors, target);
    }
    if (kind == 3) { /* in [..] */
        PyObject *targets = PyTuple_GET_ITEM(scope, 1);
        int member = PySet_Contains(targets, uid);
        if (member != 0) {
            return member;
        }
        ancestors = cedar_ancestors(ctx, uid);
        if (ancestors == NULL) {
            return PyErr_Occurred() ? -1 : 0;
        }
        PyObject *overlap = PyNumber_And(targets, ancestors);
        if (overlap == NULL) {
            return -1;
        }
        int matched = PySet_GET_SIZE(overlap) > 0;
        Py_DECREF(overlap);
        return matched;
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
    ancestors = cedar_ancestors(ctx, uid);
    if (ancestors == NULL) {
        return PyErr_Occurred() ? -1 : 0;
    }
    return PySet_Contains(ancestors, ancestor);
}

/* One policy: 1 satisfied, 0 not, -1 eval error (ctx->error), -2 real error. */
static int
cedar_policy_satisfied(cedar_ctx *ctx, PyObject *policy)
{
    static const int scope_slots[3] = {2, 3, 4};
    for (int i = 0; i < 3; i++) {
        int matched = cedar_scope_matches(
            ctx, PyTuple_GET_ITEM(policy, scope_slots[i]), ctx->vars[i]);
        if (matched < 0) {
            return -2;
        }
        if (!matched) {
            return 0;
        }
    }
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

PyObject *
wreath_cedar_is_authorized(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *policies, *principal, *action, *resource, *context, *store;
    if (!PyArg_ParseTuple(args, "O!OOOO!O!:cedar_is_authorized",
                          &PyTuple_Type, &policies, &principal, &action, &resource,
                          &PyDict_Type, &context, &PyDict_Type, &store)) {
        return NULL;
    }
    cedar_ctx ctx = {{principal, action, resource, context}, store, NULL};
    PyObject *diagnostics = PyList_New(0);
    if (diagnostics == NULL) {
        return NULL;
    }
    int permitted = 0;
    int forbidden = 0;
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(policies); i++) {
        PyObject *policy = PyTuple_GET_ITEM(policies, i);
        long forbid = cedar_program_int(PyTuple_GET_ITEM(policy, 0));
        PyObject *policy_id = PyTuple_GET_ITEM(policy, 1);
        const char *effect = forbid ? "forbid" : "permit";
        int outcome = cedar_policy_satisfied(&ctx, policy);
        if (outcome == -2) {
            goto error;
        }
        if (outcome == -1) {
            PyObject *line = PyUnicode_FromFormat(
                "%s %U skipped: %U", effect, policy_id, ctx.error);
            Py_CLEAR(ctx.error);
            if (line == NULL || PyList_Append(diagnostics, line) < 0) {
                Py_XDECREF(line);
                goto error;
            }
            Py_DECREF(line);
            continue;
        }
        if (outcome == 1) {
            PyObject *line = PyUnicode_FromFormat("%s %U matched", effect, policy_id);
            if (line == NULL || PyList_Append(diagnostics, line) < 0) {
                Py_XDECREF(line);
                goto error;
            }
            Py_DECREF(line);
            if (forbid) {
                forbidden = 1;
            }
            else {
                permitted = 1;
            }
        }
    }
    {
        const char *reason = forbidden ? "explicit forbid"
                             : permitted ? "cedar permit"
                                         : "no permit policy matched";
        PyObject *diagnostics_tuple = PyList_AsTuple(diagnostics);
        Py_DECREF(diagnostics);
        if (diagnostics_tuple == NULL) {
            return NULL;
        }
        PyObject *reason_object = PyUnicode_FromString(reason);
        if (reason_object == NULL) {
            Py_DECREF(diagnostics_tuple);
            return NULL;
        }
        PyObject *allowed = (forbidden || !permitted) ? Py_False : Py_True;
        PyObject *result = PyTuple_Pack(3, allowed, reason_object, diagnostics_tuple);
        Py_DECREF(reason_object);
        Py_DECREF(diagnostics_tuple);
        return result;
    }
error:
    Py_XDECREF(ctx.error);
    Py_DECREF(diagnostics);
    return NULL;
}
