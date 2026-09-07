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
#define CEDAR_PLAN_CAPSULE "wreath.cedar.plan"

typedef struct {
    Py_ssize_t count;
    unsigned char *allowed;
    unsigned char *reason;
} CedarDecisionBatch;

typedef struct CedarExpr CedarExpr;
struct CedarExpr {
    unsigned char opcode;
    signed char kind;
    PyObject *value;
    CedarExpr *left;
    CedarExpr *right;
    CedarExpr *third;
    CedarExpr **items;
    PyObject **names;
    Py_ssize_t count;
};

typedef struct {
    unsigned char kind;
    PyObject *value;
    PyObject *ancestor;
} CedarScope;

typedef struct {
    unsigned char unless;
    CedarExpr *expression;
} CedarCondition;

typedef struct {
    PyObject *policy;       /* borrowed from CedarPlan.policies */
    PyObject *exact_action; /* borrowed, or NULL */
    CedarScope scopes[3];
    CedarCondition *conditions;
    Py_ssize_t condition_count;
} CedarPlanPolicy;

typedef struct {
    PyObject *policies; /* owned; keeps every borrowed policy field alive */
    CedarPlanPolicy **all;
    Py_ssize_t policy_count;
    CedarPlanPolicy *ordered;
    CedarPlanPolicy *forbids;
    CedarPlanPolicy *permits;
    Py_ssize_t forbid_count;
    Py_ssize_t permit_count;
} CedarPlan;

static PyObject *cedar_eval_native(cedar_ctx *ctx, CedarExpr *node, int depth);

static void
cedar_expr_clear(CedarExpr *expression)
{
    if (expression == NULL) return;
    cedar_expr_clear(expression->left);
    cedar_expr_clear(expression->right);
    cedar_expr_clear(expression->third);
    for (Py_ssize_t index = 0; index < expression->count; index++)
        cedar_expr_clear(expression->items[index]);
    PyMem_Free(expression->names);
    PyMem_Free(expression->items);
    PyMem_Free(expression);
}

static CedarExpr *
cedar_expr_compile(PyObject *source, int depth)
{
    CedarExpr *expression;
    long opcode;
    if (depth > CEDAR_MAX_DEPTH || !PyTuple_CheckExact(source) ||
        PyTuple_GET_SIZE(source) == 0) {
        PyErr_SetString(PyExc_ValueError, "invalid compiled Cedar expression");
        return NULL;
    }
    opcode = PyLong_AsLong(PyTuple_GET_ITEM(source, 0));
    if (opcode < 0 || opcode > 17) {
        if (!PyErr_Occurred())
            PyErr_Format(PyExc_ValueError, "unknown Cedar opcode %ld", opcode);
        return NULL;
    }
    expression = PyMem_Calloc(1, sizeof(*expression));
    if (expression == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    expression->opcode = (unsigned char)opcode;
    switch (opcode) {
    case 0:
        expression->value = PyTuple_GET_ITEM(source, 1);
        break;
    case 1: {
        long variable = PyLong_AsLong(PyTuple_GET_ITEM(source, 1));
        if (variable == -1 && PyErr_Occurred()) goto error;
        if (variable < 0 || variable > 3) {
            PyErr_SetString(PyExc_ValueError, "invalid Cedar variable");
            goto error;
        }
        expression->kind = (signed char)variable;
        break;
    }
    case 2:
    case 3:
    case 7:
    case 8:
    case 9:
        expression->left = cedar_expr_compile(PyTuple_GET_ITEM(source, 1), depth + 1);
        expression->right = cedar_expr_compile(PyTuple_GET_ITEM(source, 2), depth + 1);
        if (expression->left == NULL || expression->right == NULL) goto error;
        break;
    case 4:
        expression->left = cedar_expr_compile(PyTuple_GET_ITEM(source, 1), depth + 1);
        if (expression->left == NULL) goto error;
        break;
    case 5:
    case 6: {
        long kind = PyLong_AsLong(PyTuple_GET_ITEM(source, 1));
        if (kind == -1 && PyErr_Occurred()) goto error;
        if (kind < 0 || kind > 3) {
            PyErr_SetString(PyExc_ValueError, "invalid Cedar operator");
            goto error;
        }
        expression->kind = (signed char)kind;
        expression->left = cedar_expr_compile(PyTuple_GET_ITEM(source, 2), depth + 1);
        expression->right = cedar_expr_compile(PyTuple_GET_ITEM(source, 3), depth + 1);
        if (expression->left == NULL || expression->right == NULL) goto error;
        break;
    }
    case 10:
    case 11:
    case 16:
        expression->left = cedar_expr_compile(PyTuple_GET_ITEM(source, 1), depth + 1);
        expression->value = PyTuple_GET_ITEM(source, 2);
        if (expression->left == NULL) goto error;
        break;
    case 12:
        expression->left = cedar_expr_compile(PyTuple_GET_ITEM(source, 1), depth + 1);
        expression->value = PyTuple_GET_ITEM(source, 2);
        if (expression->left == NULL) goto error;
        if (PyTuple_GET_ITEM(source, 3) != Py_None) {
            expression->right = cedar_expr_compile(PyTuple_GET_ITEM(source, 3),
                                                   depth + 1);
            if (expression->right == NULL) goto error;
        }
        break;
    case 13:
        expression->left = cedar_expr_compile(PyTuple_GET_ITEM(source, 1), depth + 1);
        expression->right = cedar_expr_compile(PyTuple_GET_ITEM(source, 2), depth + 1);
        expression->third = cedar_expr_compile(PyTuple_GET_ITEM(source, 3), depth + 1);
        if (expression->left == NULL || expression->right == NULL ||
            expression->third == NULL) goto error;
        break;
    case 14:
    case 15: {
        PyObject *items = PyTuple_GET_ITEM(source, 1);
        expression->count = PyTuple_GET_SIZE(items);
        if (expression->count != 0) {
            expression->items = PyMem_Calloc((size_t)expression->count,
                                              sizeof(*expression->items));
            if (opcode == 15)
                expression->names = PyMem_Calloc((size_t)expression->count,
                                                  sizeof(*expression->names));
            if (expression->items == NULL ||
                (opcode == 15 && expression->names == NULL)) {
                PyErr_NoMemory();
                goto error;
            }
        }
        for (Py_ssize_t index = 0; index < expression->count; index++) {
            PyObject *item = PyTuple_GET_ITEM(items, index);
            if (opcode == 15) {
                expression->names[index] = PyTuple_GET_ITEM(item, 0);
                item = PyTuple_GET_ITEM(item, 1);
            }
            expression->items[index] = cedar_expr_compile(item, depth + 1);
            if (expression->items[index] == NULL) goto error;
        }
        break;
    }
    case 17: {
        long method = PyLong_AsLong(PyTuple_GET_ITEM(source, 1));
        if (method == -1 && PyErr_Occurred()) goto error;
        if (method < 0 || method > 3) {
            PyErr_SetString(PyExc_ValueError, "invalid Cedar set method");
            goto error;
        }
        expression->kind = (signed char)method;
        expression->left = cedar_expr_compile(PyTuple_GET_ITEM(source, 2), depth + 1);
        if (expression->left == NULL) goto error;
        if (method != 3) {
            expression->right = cedar_expr_compile(PyTuple_GET_ITEM(source, 3),
                                                   depth + 1);
            if (expression->right == NULL) goto error;
        }
        break;
    }
    }
    return expression;
error:
    cedar_expr_clear(expression);
    return NULL;
}

static void
cedar_plan_policy_clear(CedarPlanPolicy *policy)
{
    for (Py_ssize_t index = 0; index < policy->condition_count; index++)
        cedar_expr_clear(policy->conditions[index].expression);
    PyMem_Free(policy->conditions);
    memset(policy, 0, sizeof(*policy));
}

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

static void
cedar_plan_destroy(PyObject *capsule)
{
    CedarPlan *plan = PyCapsule_GetPointer(capsule, CEDAR_PLAN_CAPSULE);
    if (plan == NULL) {
        PyErr_Clear();
        return;
    }
    for (Py_ssize_t index = 0; index < plan->policy_count; index++)
        cedar_plan_policy_clear(&plan->ordered[index]);
    PyMem_Free(plan->all);
    PyMem_Free(plan->ordered);
    Py_DECREF(plan->policies);
    PyMem_Free(plan);
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

static int
cedar_scope_compile(CedarScope *compiled, PyObject *source)
{
    long kind;
    if (!PyTuple_CheckExact(source) || PyTuple_GET_SIZE(source) == 0) {
        PyErr_SetString(PyExc_ValueError, "invalid compiled Cedar scope");
        return -1;
    }
    kind = cedar_program_int(PyTuple_GET_ITEM(source, 0));
    if (kind < 0 || kind > 4) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError,
                                               "invalid Cedar scope kind");
        return -1;
    }
    compiled->kind = (unsigned char)kind;
    if (kind == 1 || kind == 2 || kind == 3)
        compiled->value = PyTuple_GET_ITEM(source, 1);
    else if (kind == 4) {
        compiled->value = PyTuple_GET_ITEM(source, 1);
        compiled->ancestor = PyTuple_GET_ITEM(source, 2);
    }
    return 0;
}

static int
cedar_plan_policy_compile(CedarPlanPolicy *compiled, PyObject *policy)
{
    PyObject *conditions;
    compiled->policy = policy;
    for (int index = 0; index < 3; index++)
        if (cedar_scope_compile(&compiled->scopes[index],
                                PyTuple_GET_ITEM(policy, index + 2)) < 0)
            goto error;
    if (compiled->scopes[1].kind == 1)
        compiled->exact_action = compiled->scopes[1].value;
    conditions = PyTuple_GET_ITEM(policy, 5);
    compiled->condition_count = PyTuple_GET_SIZE(conditions);
    if (compiled->condition_count != 0) {
        compiled->conditions = PyMem_Calloc((size_t)compiled->condition_count,
                                             sizeof(*compiled->conditions));
        if (compiled->conditions == NULL) {
            PyErr_NoMemory();
            goto error;
        }
    }
    for (Py_ssize_t index = 0; index < compiled->condition_count; index++) {
        PyObject *condition = PyTuple_GET_ITEM(conditions, index);
        long unless = cedar_program_int(PyTuple_GET_ITEM(condition, 0));
        if (unless < 0 || unless > 1) {
            if (!PyErr_Occurred())
                PyErr_SetString(PyExc_ValueError,
                                "invalid Cedar condition kind");
            goto error;
        }
        compiled->conditions[index].unless = (unsigned char)unless;
        compiled->conditions[index].expression = cedar_expr_compile(
            PyTuple_GET_ITEM(condition, 1), 0);
        if (compiled->conditions[index].expression == NULL) goto error;
    }
    return 0;
error:
    cedar_plan_policy_clear(compiled);
    return -1;
}

PyObject *
wreath_cedar_compile_plan(PyObject *Py_UNUSED(self), PyObject *policies)
{
    if (!PyTuple_CheckExact(policies)) {
        PyErr_Format(
            PyExc_TypeError,
            "cedar plan policies must be a tuple, got %.200s",
            Py_TYPE(policies)->tp_name);
        return NULL;
    }
    Py_ssize_t policy_count = PyTuple_GET_SIZE(policies);
    Py_ssize_t forbid_count = 0;
    for (Py_ssize_t index = 0; index < policy_count; index++) {
        PyObject *policy = PyTuple_GET_ITEM(policies, index);
        if (!PyTuple_CheckExact(policy) || PyTuple_GET_SIZE(policy) < 6) {
            PyErr_Format(
                PyExc_ValueError,
                "cedar plan policy %zd must be a compiled six-field tuple",
                index);
            return NULL;
        }
        long effect = cedar_program_int(PyTuple_GET_ITEM(policy, 0));
        if ((effect == -1 && PyErr_Occurred()) || (effect != 0 && effect != 1)) {
            if (!PyErr_Occurred()) {
                PyErr_Format(
                    PyExc_ValueError,
                    "cedar plan policy %zd effect must be 0 (permit) or 1 (forbid)",
                    index);
            }
            return NULL;
        }
        forbid_count += effect != 0;
    }

    CedarPlan *plan = PyMem_Calloc(1, sizeof(*plan));
    if (plan == NULL) return PyErr_NoMemory();
    plan->forbid_count = forbid_count;
    plan->permit_count = policy_count - forbid_count;
    plan->policy_count = policy_count;
    if (policy_count != 0) {
        plan->all = PyMem_Malloc((size_t)policy_count * sizeof(*plan->all));
        plan->ordered = PyMem_Calloc((size_t)policy_count, sizeof(*plan->ordered));
    }
    if (policy_count != 0 && (plan->all == NULL || plan->ordered == NULL)) {
        PyMem_Free(plan->all);
        PyMem_Free(plan->ordered);
        PyMem_Free(plan);
        return PyErr_NoMemory();
    }
    if (policy_count != 0) {
        plan->forbids = plan->ordered;
        plan->permits = plan->ordered + forbid_count;
    }

    Py_ssize_t forbid_index = 0;
    Py_ssize_t permit_index = 0;
    for (Py_ssize_t index = 0; index < policy_count; index++) {
        PyObject *policy = PyTuple_GET_ITEM(policies, index);
        int forbid = cedar_program_int(PyTuple_GET_ITEM(policy, 0)) != 0;
        CedarPlanPolicy *compiled = forbid
            ? &plan->forbids[forbid_index++]
            : &plan->permits[permit_index++];
        if (cedar_plan_policy_compile(compiled, policy) < 0) goto error;
        plan->all[index] = compiled;
    }
    plan->policies = Py_NewRef(policies);
    PyObject *capsule = PyCapsule_New(plan, CEDAR_PLAN_CAPSULE, cedar_plan_destroy);
    if (capsule != NULL) return capsule;
    Py_DECREF(plan->policies);
error:
    for (Py_ssize_t index = 0; index < plan->policy_count; index++)
        cedar_plan_policy_clear(&plan->ordered[index]);
    PyMem_Free(plan->all);
    PyMem_Free(plan->ordered);
    PyMem_Free(plan);
    return NULL;
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
    if (PyList_Check(value) || PySet_Check(value) || PyFrozenSet_Check(value)) {
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

static int
cedar_is_set(PyObject *value)
{
    return PyList_Check(value) || PySet_Check(value) || PyFrozenSet_Check(value);
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
    if (cedar_is_set(a) && cedar_is_set(b)) {
        PyObject *left = NULL, *right = NULL;
        int equal;
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
    if (PyList_Check(set_list)) {
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(set_list); i++) {
            int equal = cedar_eq(ctx, value, PyList_GET_ITEM(set_list, i), depth);
            if (equal != 0) return equal;
        }
        return 0;
    }
    PyObject *iterator = PyObject_GetIter(set_list);
    if (iterator == NULL) return -1;
    PyObject *item;
    int equal = 0;
    while ((item = PyIter_Next(iterator)) != NULL) {
        equal = cedar_eq(ctx, value, item, depth);
        Py_DECREF(item);
        if (equal != 0) break;
    }
    Py_DECREF(iterator);
    if (equal == 0 && PyErr_Occurred()) return -1;
    return equal;
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
    else if (cedar_is_set(value)) {
        PyObject *members = PySet_New(NULL);
        PyObject *frozen = NULL;
        if (members == NULL) return -1;
        if (Py_EnterRecursiveCall(" while hashing a Cedar set")) {
            Py_DECREF(members);
            return -1;
        }
        if (PyList_Check(value)) {
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
        }
        else {
            PyObject *iterator = PyObject_GetIter(value);
            if (iterator == NULL) {
                Py_LeaveRecursiveCall();
                Py_DECREF(members);
                return -1;
            }
            PyObject *item;
            while ((item = PyIter_Next(iterator)) != NULL) {
                PyObject *member = NULL;
                int failed = cedar_dedupe_key(item, &member) < 0 ||
                             member == NULL || PySet_Add(members, member) < 0;
                Py_DECREF(item);
                Py_XDECREF(member);
                if (failed) break;
            }
            int failed = PyErr_Occurred() != NULL;
            Py_DECREF(iterator);
            if (failed) {
                Py_LeaveRecursiveCall();
                Py_DECREF(members);
                return -1;
            }
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

static int
cedar_eval_bool_native(cedar_ctx *ctx, CedarExpr *node, int depth,
                       const char *operation)
{
    PyObject *value = cedar_eval_native(ctx, node, depth + 1);
    int result;
    if (value == NULL) return -1;
    if (!PyBool_Check(value)) {
        PyObject *message = PyUnicode_FromFormat(
            "%s requires bool, got %s", operation, cedar_type_name(value));
        Py_DECREF(value);
        cedar_fail(ctx, message);
        return -1;
    }
    result = value == Py_True;
    Py_DECREF(value);
    return result;
}

static int
cedar_eval_i64_native(cedar_ctx *ctx, CedarExpr *node, int depth,
                      const char *operation, int64_t *result)
{
    PyObject *value = cedar_eval_native(ctx, node, depth + 1);
    long long converted;
    if (value == NULL) return -1;
    if (!PyLong_Check(value) || PyBool_Check(value)) {
        PyObject *message = PyUnicode_FromFormat(
            "%s requires long, got %s", operation, cedar_type_name(value));
        Py_DECREF(value);
        cedar_fail(ctx, message);
        return -1;
    }
    converted = PyLong_AsLongLong(value);
    Py_DECREF(value);
    if (converted == -1 && PyErr_Occurred()) return -1;
    *result = (int64_t)converted;
    return 0;
}

static PyObject *
cedar_eval_method_native(cedar_ctx *ctx, CedarExpr *node, int depth)
{
    PyObject *operand = cedar_eval_native(ctx, node->left, depth + 1);
    PyObject *argument;
    int outcome = -2;
    if (operand == NULL) return NULL;
    if (!cedar_is_set(operand)) {
        PyObject *message = PyUnicode_FromFormat(
            "set methods require a set, got %s", cedar_type_name(operand));
        Py_DECREF(operand);
        return cedar_fail(ctx, message);
    }
    if (node->kind == 3) {
        PyObject *result = PyBool_FromLong(PyObject_Size(operand) == 0);
        Py_DECREF(operand);
        return result;
    }
    argument = cedar_eval_native(ctx, node->right, depth + 1);
    if (argument == NULL) {
        Py_DECREF(operand);
        return NULL;
    }
    if (node->kind == 0) outcome = cedar_set_contains(ctx, operand, argument, depth);
    else if (!cedar_is_set(argument))
        cedar_fail(ctx, PyUnicode_FromString(
            "containsAll/containsAny require a set argument"));
    else if (PyList_Check(argument)) {
        outcome = node->kind == 1;
        for (Py_ssize_t index = 0;
             index < PyList_GET_SIZE(argument) && outcome == (node->kind == 1);
             index++)
            outcome = cedar_set_contains(
                ctx, operand, PyList_GET_ITEM(argument, index), depth);
    }
    else {
        PyObject *iterator = PyObject_GetIter(argument);
        if (iterator != NULL) {
            PyObject *item;
            outcome = node->kind == 1;
            while (outcome == (node->kind == 1) &&
                   (item = PyIter_Next(iterator)) != NULL) {
                outcome = cedar_set_contains(ctx, operand, item, depth);
                Py_DECREF(item);
            }
            Py_DECREF(iterator);
            if (outcome >= 0 && PyErr_Occurred()) outcome = -1;
        }
    }
    Py_DECREF(argument);
    Py_DECREF(operand);
    if (outcome < 0 || outcome == -2) return NULL;
    return PyBool_FromLong(outcome);
}

static PyObject *
cedar_eval_native(cedar_ctx *ctx, CedarExpr *node, int depth)
{
    if (depth > CEDAR_MAX_DEPTH)
        return cedar_fail(ctx,
                          PyUnicode_FromString("expression is nested too deeply"));
    switch (node->opcode) {
    case 0:
        return Py_NewRef(node->value);
    case 1:
        return Py_NewRef(ctx->vars[(int)node->kind]);
    case 2:
    case 3: {
        int left = cedar_eval_bool_native(
            ctx, node->left, depth, node->opcode == 2 ? "&&" : "||");
        int short_value = node->opcode == 2 ? 0 : 1;
        if (left < 0) return NULL;
        if (left == short_value) return PyBool_FromLong(left);
        int right = cedar_eval_bool_native(
            ctx, node->right, depth, node->opcode == 2 ? "&&" : "||");
        return right < 0 ? NULL : PyBool_FromLong(right);
    }
    case 4: {
        int value = cedar_eval_bool_native(ctx, node->left, depth, "!");
        return value < 0 ? NULL : PyBool_FromLong(!value);
    }
    case 5:
    case 6: {
        int64_t left, right;
        const char *operation = node->opcode == 5 ? "arithmetic" : "comparison";
        if (cedar_eval_i64_native(ctx, node->left, depth, operation, &left) < 0 ||
            cedar_eval_i64_native(ctx, node->right, depth, operation, &right) < 0)
            return NULL;
        if (node->opcode == 6) {
            int result = node->kind == 0 ? left < right :
                node->kind == 1 ? left <= right :
                node->kind == 2 ? left > right : left >= right;
            return PyBool_FromLong(result);
        }
        int64_t result;
        int overflowed = node->kind == 0 ? cedar_add_i64(left, right, &result) :
            node->kind == 1 ? cedar_sub_i64(left, right, &result) :
            cedar_mul_i64(left, right, &result);
        if (overflowed)
            return cedar_fail(ctx,
                              PyUnicode_FromString("arithmetic overflowed i64"));
        return PyLong_FromLongLong((long long)result);
    }
    case 7:
    case 8:
    case 9: {
        PyObject *left = cedar_eval_native(ctx, node->left, depth + 1);
        PyObject *right;
        int result;
        if (left == NULL) return NULL;
        right = cedar_eval_native(ctx, node->right, depth + 1);
        if (right == NULL) { Py_DECREF(left); return NULL; }
        result = node->opcode == 9 ? cedar_in(ctx, left, right) :
            cedar_eq(ctx, left, right, 0);
        Py_DECREF(right);
        Py_DECREF(left);
        if (result < 0) return NULL;
        return PyBool_FromLong(node->opcode == 8 ? !result : result);
    }
    case 10: {
        PyObject *operand = cedar_eval_native(ctx, node->left, depth + 1);
        int present;
        if (operand == NULL) return NULL;
        if (cedar_is_uid(operand)) {
            PyObject *entry = PyDict_GetItemWithError(ctx->store, operand);
            if (entry == NULL && PyErr_Occurred()) { Py_DECREF(operand); return NULL; }
            present = entry != NULL &&
                PyDict_Contains(PyTuple_GET_ITEM(entry, 0), node->value) == 1;
        }
        else if (PyDict_Check(operand)) present = PyDict_Contains(operand, node->value);
        else {
            PyObject *message = PyUnicode_FromFormat(
                "'has' requires an entity or record, got %s",
                cedar_type_name(operand));
            Py_DECREF(operand);
            return cedar_fail(ctx, message);
        }
        Py_DECREF(operand);
        return present < 0 ? NULL : PyBool_FromLong(present);
    }
    case 11: {
        PyObject *operand = cedar_eval_native(ctx, node->left, depth + 1);
        int result;
        if (operand == NULL) return NULL;
        if (!PyUnicode_Check(operand)) {
            PyObject *message = PyUnicode_FromFormat(
                "'like' requires a string, got %s", cedar_type_name(operand));
            Py_DECREF(operand);
            return cedar_fail(ctx, message);
        }
        result = cedar_like(operand, node->value);
        Py_DECREF(operand);
        return result < 0 ? NULL : PyBool_FromLong(result);
    }
    case 12: {
        PyObject *operand = cedar_eval_native(ctx, node->left, depth + 1);
        PyObject *ancestor;
        int same, result;
        if (operand == NULL) return NULL;
        if (!cedar_is_uid(operand)) {
            PyObject *message = PyUnicode_FromFormat(
                "'is' requires an entity, got %s", cedar_type_name(operand));
            Py_DECREF(operand);
            return cedar_fail(ctx, message);
        }
        same = PyUnicode_Compare(PyTuple_GET_ITEM(operand, 0), node->value);
        if (same != 0 || node->right == NULL) {
            Py_DECREF(operand);
            if (PyErr_Occurred()) return NULL;
            return PyBool_FromLong(same == 0);
        }
        ancestor = cedar_eval_native(ctx, node->right, depth + 1);
        if (ancestor == NULL) { Py_DECREF(operand); return NULL; }
        result = cedar_in(ctx, operand, ancestor);
        Py_DECREF(ancestor);
        Py_DECREF(operand);
        return result < 0 ? NULL : PyBool_FromLong(result);
    }
    case 13: {
        int condition = cedar_eval_bool_native(ctx, node->left, depth, "if");
        if (condition < 0) return NULL;
        return cedar_eval_native(ctx, condition ? node->right : node->third,
                                 depth + 1);
    }
    case 14: {
        PyObject *result = PyList_New(0);
        PyObject *seen = PySet_New(NULL);
        if (result == NULL || seen == NULL) {
            Py_XDECREF(seen); Py_XDECREF(result); return NULL;
        }
        for (Py_ssize_t index = 0; index < node->count; index++) {
            PyObject *item = cedar_eval_native(ctx, node->items[index], depth + 1);
            PyObject *key = NULL;
            int present;
            if (item == NULL || cedar_dedupe_key(item, &key) < 0) {
                Py_XDECREF(item); Py_XDECREF(key); goto set_error;
            }
            present = PySet_Contains(seen, key);
            if (present < 0 || (!present && PySet_Add(seen, key) < 0) ||
                (!present && PyList_Append(result, item) < 0)) {
                Py_DECREF(key); Py_DECREF(item); goto set_error;
            }
            Py_DECREF(key); Py_DECREF(item);
        }
        Py_DECREF(seen);
        return result;
set_error:
        Py_DECREF(seen); Py_DECREF(result); return NULL;
    }
    case 15: {
        PyObject *result = PyDict_New();
        if (result == NULL) return NULL;
        for (Py_ssize_t index = 0; index < node->count; index++) {
            PyObject *value = cedar_eval_native(ctx, node->items[index], depth + 1);
            if (value == NULL || PyDict_SetItem(result, node->names[index], value) < 0) {
                Py_XDECREF(value); Py_DECREF(result); return NULL;
            }
            Py_DECREF(value);
        }
        return result;
    }
    case 16: {
        PyObject *operand = cedar_eval_native(ctx, node->left, depth + 1);
        PyObject *result;
        if (operand == NULL) return NULL;
        result = cedar_eval_getattr(ctx, operand, node->value);
        Py_DECREF(operand);
        return result;
    }
    case 17:
        return cedar_eval_method_native(ctx, node, depth);
    default:
        return cedar_fail(ctx, PyUnicode_FromFormat(
            "unknown opcode %d", (int)node->opcode));
    }
}

/* Scope match: 1 matched, 0 not, -1 real error. Scopes never eval-error. */
static int
cedar_scope_matches_native(cedar_ctx *ctx, const CedarScope *scope,
                           PyObject *uid)
{
    if (scope->kind == 0) return 1;
    if (scope->kind == 1)
        return PyObject_RichCompareBool(uid, scope->value, Py_EQ);
    if (scope->kind == 2) {
        int equal = PyObject_RichCompareBool(uid, scope->value, Py_EQ);
        if (equal != 0) return equal;
        return cedar_ancestor_matches(ctx, uid, scope->value, NULL);
    }
    if (scope->kind == 3) {
        int member = PySet_Contains(scope->value, uid);
        if (member != 0) return member;
        return cedar_ancestor_matches(ctx, uid, NULL, scope->value);
    }
    int same = PyUnicode_Compare(PyTuple_GET_ITEM(uid, 0), scope->value);
    if (PyErr_Occurred()) return -1;
    if (same != 0) return 0;
    if (scope->ancestor == Py_None) return 1;
    int equal = PyObject_RichCompareBool(uid, scope->ancestor, Py_EQ);
    if (equal != 0) return equal;
    return cedar_ancestor_matches(ctx, uid, scope->ancestor, NULL);
}

static int
cedar_plan_policy_conditions(cedar_ctx *ctx,
                             const CedarPlanPolicy *policy)
{
    for (Py_ssize_t index = 0; index < policy->condition_count; index++) {
        const CedarCondition *condition = &policy->conditions[index];
        PyObject *value = cedar_eval_native(ctx, condition->expression, 0);
        if (value == NULL) return PyErr_Occurred() ? -2 : -1;
        if (!PyBool_Check(value)) {
            PyObject *message = PyUnicode_FromFormat(
                "condition evaluated to %s, not bool", cedar_type_name(value));
            Py_DECREF(value);
            cedar_fail(ctx, message);
            return message == NULL ? -2 : -1;
        }
        int truth = value == Py_True;
        Py_DECREF(value);
        if ((condition->unless != 0) == truth) return 0;
    }
    return 1;
}

/* One policy: 1 satisfied, 0 not, -1 eval error (ctx->error), -2 real error. */
static int
cedar_plan_policy_satisfied(cedar_ctx *ctx, const CedarPlanPolicy *compiled)
{
    if (compiled->exact_action != NULL) {
        int matched = PyObject_RichCompareBool(
            ctx->vars[1], compiled->exact_action, Py_EQ);
        if (matched <= 0) return matched;
    }
    else {
        int matched = cedar_scope_matches_native(
            ctx, &compiled->scopes[1], ctx->vars[1]);
        if (matched < 0) return -2;
        if (!matched) return 0;
    }
    int principal = cedar_scope_matches_native(
        ctx, &compiled->scopes[0], ctx->vars[0]);
    if (principal < 0) return -2;
    if (!principal) return 0;
    int resource = cedar_scope_matches_native(
        ctx, &compiled->scopes[2], ctx->vars[2]);
    if (resource < 0) return -2;
    if (!resource) return 0;
    return cedar_plan_policy_conditions(ctx, compiled);
}

static PyObject *
cedar_authorize_plan_diagnostic(const CedarPlan *plan,
                    PyObject *principal, PyObject *action,
                    PyObject *resource, PyObject *context, PyObject *store,
                    PyObject **matched_lines, PyObject **reason_cache)
{
    cedar_ctx ctx = {{principal, action, resource, context}, store, NULL};
    Py_ssize_t policy_count = plan->policy_count;
    PyObject *diagnostics = PyTuple_New(policy_count);
    if (diagnostics == NULL) {
        return NULL;
    }
    Py_ssize_t diagnostic_count = 0;
    int permitted = 0;
    int forbidden = 0;
    for (Py_ssize_t i = 0; i < policy_count; i++) {
        const CedarPlanPolicy *compiled = plan->all[i];
        PyObject *policy = compiled->policy;
        long forbid = cedar_program_int(PyTuple_GET_ITEM(policy, 0));
        PyObject *policy_id = PyTuple_GET_ITEM(policy, 1);
        const char *effect = forbid ? "forbid" : "permit";
        int outcome = cedar_plan_policy_satisfied(&ctx, compiled);
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

/* A decision does not need the diagnostic engine's complete matched-policy
 * inventory.  Cedar's effect algebra permits two safe early exits: the first
 * satisfied forbid fixes the answer to deny, and after every forbid has failed
 * the first satisfied permit fixes it to allow.  Evaluation errors remain
 * policy-local and are skipped exactly as in cedar_authorize_compact_one. */
static int
cedar_authorize_plan_one(
    const CedarPlan *plan, PyObject *principal, PyObject *action,
    PyObject *resource, PyObject *context, PyObject *store,
    unsigned char *allowed, unsigned char *reason)
{
    cedar_ctx ctx = {{principal, action, resource, context}, store, NULL};
    for (Py_ssize_t index = 0; index < plan->forbid_count; index++) {
        int outcome = cedar_plan_policy_satisfied(&ctx, &plan->forbids[index]);
        if (outcome == -2) {
            Py_XDECREF(ctx.error);
            return -1;
        }
        if (outcome == -1) {
            Py_CLEAR(ctx.error);
            continue;
        }
        if (outcome == 1) {
            *allowed = 0;
            *reason = 2;
            return 0;
        }
    }
    for (Py_ssize_t index = 0; index < plan->permit_count; index++) {
        int outcome = cedar_plan_policy_satisfied(&ctx, &plan->permits[index]);
        if (outcome == -2) {
            Py_XDECREF(ctx.error);
            return -1;
        }
        if (outcome == -1) {
            Py_CLEAR(ctx.error);
            continue;
        }
        if (outcome == 1) {
            *allowed = 1;
            *reason = 1;
            return 0;
        }
    }
    *allowed = 0;
    *reason = 0;
    return 0;
}

static int
cedar_prepared_value(PyObject *value, int depth)
{
    if (depth > CEDAR_MAX_DEPTH) return 0;
    if (PyBool_Check(value) || PyUnicode_Check(value)) return 1;
    if (PyLong_Check(value)) {
        int overflow = 0;
        (void)PyLong_AsLongLongAndOverflow(value, &overflow);
        if (PyErr_Occurred()) {
            PyErr_Clear();
            return 0;
        }
        return overflow == 0;
    }
    if (depth == 0 && PyDict_CheckExact(value)) {
        Py_ssize_t position = 0;
        PyObject *key, *item;
        while (PyDict_Next(value, &position, &key, &item)) {
            if (!PyUnicode_Check(key)) return 0;
            int prepared = cedar_prepared_value(item, depth + 1);
            if (prepared <= 0) return prepared;
        }
        return 1;
    }
    if (PyFrozenSet_CheckExact(value)) {
        PyObject *iterator = PyObject_GetIter(value);
        if (iterator == NULL) return -1;
        PyObject *item;
        int prepared = 1;
        while ((item = PyIter_Next(iterator)) != NULL) {
            prepared = cedar_prepared_value(item, depth + 1);
            Py_DECREF(item);
            if (prepared <= 0) break;
        }
        Py_DECREF(iterator);
        if (prepared > 0 && PyErr_Occurred()) return -1;
        return prepared;
    }
    return 0;
}

static PyObject *
cedar_plan_denial(
    const CedarPlan *plan, PyObject *principal, PyObject *action,
    PyObject *resource, PyObject *context, PyObject *store)
{
    unsigned char allowed, reason;
    if (cedar_authorize_plan_one(
            plan, principal, action, resource, context, store,
            &allowed, &reason) < 0) return NULL;
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
wreath_cedar_is_authorized(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *plan_object, *principal, *action, *resource, *context, *store;
    if (!PyArg_ParseTuple(args, "OOOOO!O!:cedar_is_authorized",
                          &plan_object, &principal, &action, &resource,
                          &PyDict_Type, &context, &PyDict_Type, &store)) {
        return NULL;
    }
    CedarPlan *plan = PyCapsule_GetPointer(plan_object, CEDAR_PLAN_CAPSULE);
    if (plan == NULL) return NULL;
    return cedar_authorize_plan_diagnostic(
        plan, principal, action, resource, context, store, NULL, NULL);
}

PyObject *
wreath_cedar_route_denial(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *plan_object, *principal, *action, *resource, *context, *store;
    if (!PyArg_ParseTuple(args, "OOOOO!O!:cedar_route_denial",
                          &plan_object, &principal, &action, &resource,
                          &PyDict_Type, &context, &PyDict_Type, &store)) {
        return NULL;
    }
    CedarPlan *plan = PyCapsule_GetPointer(plan_object, CEDAR_PLAN_CAPSULE);
    if (plan == NULL) return NULL;
    return cedar_plan_denial(plan, principal, action, resource, context, store);
}

PyObject *
wreath_cedar_route_denial_prepared(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *plan_object, *principal, *action, *resource, *context, *store;
    if (!PyArg_ParseTuple(args, "OOOOOO!:cedar_route_denial_prepared",
                          &plan_object, &principal, &action, &resource,
                          &context, &PyDict_Type, &store)) {
        return NULL;
    }
    CedarPlan *plan = PyCapsule_GetPointer(plan_object, CEDAR_PLAN_CAPSULE);
    if (plan == NULL) return NULL;
    int prepared = cedar_prepared_value(context, 0);
    if (prepared < 0) return NULL;
    if (!prepared) Py_RETURN_NOTIMPLEMENTED;
    return cedar_plan_denial(plan, principal, action, resource, context, store);
}

PyObject *
wreath_cedar_is_authorized_many(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *plan_object, *principal, *action, *resources, *context, *store;
    int stop_on_denied;
    if (!PyArg_ParseTuple(
            args, "OOOO!O!O!p:cedar_is_authorized_many",
            &plan_object, &principal, &action,
            &PyTuple_Type, &resources, &PyDict_Type, &context,
            &PyDict_Type, &store, &stop_on_denied)) return NULL;
    CedarPlan *plan = PyCapsule_GetPointer(plan_object, CEDAR_PLAN_CAPSULE);
    if (plan == NULL) return NULL;

    Py_ssize_t policy_count = plan->policy_count;
    PyObject **matched_lines = policy_count != 0
        ? PyMem_Calloc((size_t)policy_count, sizeof(*matched_lines)) : NULL;
    if (policy_count != 0 && matched_lines == NULL) return PyErr_NoMemory();
    PyObject *reason_cache[3] = {NULL, NULL, NULL};

    Py_ssize_t resource_count = PyTuple_GET_SIZE(resources);
    PyObject *results = PyTuple_New(resource_count);
    if (results == NULL) {
        PyMem_Free(matched_lines);
        return NULL;
    }
    Py_ssize_t result_count = 0;
    for (; result_count < resource_count; result_count++) {
        PyObject *result = cedar_authorize_plan_diagnostic(
            plan, principal, action,
            PyTuple_GET_ITEM(resources, result_count), context, store,
            matched_lines, reason_cache);
        if (result == NULL) {
            Py_DECREF(results);
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
    PyObject *plan_object, *principal, *action, *resources, *context, *store;
    int stop_on_denied;
    if (!PyArg_ParseTuple(
            args, "OOOO!O!O!p:cedar_is_authorized_many_native",
            &plan_object, &principal, &action,
            &PyTuple_Type, &resources, &PyDict_Type, &context,
            &PyDict_Type, &store, &stop_on_denied)) return NULL;
    CedarPlan *plan = PyCapsule_GetPointer(plan_object, CEDAR_PLAN_CAPSULE);
    if (plan == NULL) return NULL;

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
        if (cedar_authorize_plan_one(
                plan, principal, action,
                PyTuple_GET_ITEM(resources, batch->count), context, store,
                &batch->allowed[batch->count], &batch->reason[batch->count]) < 0) {
            PyMem_Free(batch->reason);
            PyMem_Free(batch->allowed);
            PyMem_Free(batch);
            return NULL;
        }
        if (stop_on_denied && !batch->allowed[batch->count]) {
            batch->count++;
            break;
        }
    }
    return PyCapsule_New(
        batch, CEDAR_DECISION_BATCH_CAPSULE, cedar_decision_batch_destroy);

memory:
    PyErr_NoMemory();
    return NULL;
}
