/* Bulk GraphQL result projection.  Python schedules policies and custom
 * resolvers; C owns repeated row projection and child-layout assembly. */
#include "wreathcore.h"

typedef enum {
    GQ_ATTR_NAME,
    GQ_ATTR_POLICY,
    GQ_ATTR_RESOLVER,
    GQ_ATTR_RELATIONSHIP,
    GQ_ATTR_COLUMN,
    GQ_ATTR_PYTHON_NAME,
    GQ_ATTR_ATTRIBUTE,
    GQ_ATTR_KEY,
    GQ_ATTR_ORM_GET_RELATION,
    GQ_ATTR_COUNT,
} GraphqlAttr;

static PyObject *graphql_attr_names[GQ_ATTR_COUNT];

int
wreath_graphql_ready(void)
{
    static const char *names[GQ_ATTR_COUNT] = {
        "name", "policy", "resolver", "relationship", "column",
        "python_name", "attribute", "key", "_orm_get_relation",
    };
    for (int index = 0; index < GQ_ATTR_COUNT; index++) {
        graphql_attr_names[index] = PyUnicode_InternFromString(names[index]);
        if (graphql_attr_names[index] == NULL) {
            while (index-- != 0) Py_CLEAR(graphql_attr_names[index]);
            return -1;
        }
    }
    return 0;
}

static inline PyObject *
graphql_getattr(PyObject *object, GraphqlAttr attribute)
{
    return PyObject_GetAttr(object, graphql_attr_names[attribute]);
}

typedef struct {
    PyObject *name;
    PyObject *resource;
    Py_hash_t hash;
} GraphqlPolicyEntry;

typedef struct {
    GraphqlPolicyEntry *entries;
    Py_ssize_t count;
    Py_ssize_t table_size;
    Py_ssize_t *slots;
} GraphqlPolicySchema;

typedef struct {
    PyObject *schema_capsule;
    signed char *decisions;
} GraphqlPolicyState;

typedef struct {
    PyObject *schema_capsule;
    Py_ssize_t root_index;
    PyObject *root_name;
    Py_ssize_t *field_indices;
    PyObject **field_names;
    Py_ssize_t field_count;
    Py_ssize_t *pending;
    Py_ssize_t pending_count;
} GraphqlPolicyPlan;

#define GRAPHQL_POLICY_SCHEMA_CAPSULE "wreath.graphql.policy_schema"
#define GRAPHQL_POLICY_STATE_CAPSULE "wreath.graphql.policy_state"
#define GRAPHQL_POLICY_PLAN_CAPSULE "wreath.graphql.policy_plan"

static GraphqlPolicySchema *
graphql_policy_schema_from(PyObject *capsule)
{
    return PyCapsule_GetPointer(capsule, GRAPHQL_POLICY_SCHEMA_CAPSULE);
}

static GraphqlPolicyState *
graphql_policy_state_from(PyObject *capsule)
{
    return PyCapsule_GetPointer(capsule, GRAPHQL_POLICY_STATE_CAPSULE);
}

static GraphqlPolicyPlan *
graphql_policy_plan_from(PyObject *capsule)
{
    return PyCapsule_GetPointer(capsule, GRAPHQL_POLICY_PLAN_CAPSULE);
}

static void
graphql_policy_schema_free(GraphqlPolicySchema *schema)
{
    if (schema == NULL) return;
    for (Py_ssize_t index = 0; index < schema->count; index++) {
        Py_XDECREF(schema->entries[index].name);
        Py_XDECREF(schema->entries[index].resource);
    }
    PyMem_Free(schema->entries);
    PyMem_Free(schema->slots);
    PyMem_Free(schema);
}

static void
graphql_policy_schema_destroy(PyObject *capsule)
{
    GraphqlPolicySchema *schema = graphql_policy_schema_from(capsule);
    if (schema == NULL) {
        PyErr_Clear();
        return;
    }
    graphql_policy_schema_free(schema);
}

static void
graphql_policy_state_destroy(PyObject *capsule)
{
    GraphqlPolicyState *state = graphql_policy_state_from(capsule);
    if (state == NULL) {
        PyErr_Clear();
        return;
    }
    Py_DECREF(state->schema_capsule);
    PyMem_Free(state->decisions);
    PyMem_Free(state);
}

static void
graphql_policy_plan_free(GraphqlPolicyPlan *plan)
{
    if (plan == NULL) return;
    Py_XDECREF(plan->schema_capsule);
    Py_XDECREF(plan->root_name);
    for (Py_ssize_t index = 0; index < plan->field_count; index++)
        Py_XDECREF(plan->field_names[index]);
    PyMem_Free(plan->field_names);
    PyMem_Free(plan->field_indices);
    PyMem_Free(plan->pending);
    PyMem_Free(plan);
}

static void
graphql_policy_plan_destroy(PyObject *capsule)
{
    GraphqlPolicyPlan *plan = graphql_policy_plan_from(capsule);
    if (plan == NULL) {
        PyErr_Clear();
        return;
    }
    graphql_policy_plan_free(plan);
}

static Py_ssize_t
graphql_policy_table_size(Py_ssize_t count)
{
    Py_ssize_t size = 8;
    Py_ssize_t target;
    if (count > PY_SSIZE_T_MAX / 2) return -1;
    target = count * 2;
    while (size < target) {
        if (size > PY_SSIZE_T_MAX / 2) return -1;
        size *= 2;
    }
    return size;
}

static Py_ssize_t
graphql_policy_find_hashed(GraphqlPolicySchema *schema, PyObject *name,
                           Py_hash_t hash)
{
    size_t mask = (size_t)schema->table_size - 1;
    size_t slot = (size_t)hash & mask;
    for (;;) {
        Py_ssize_t index = schema->slots[slot];
        if (index < 0) return -1;
        if (schema->entries[index].hash == hash) {
            int equal = PyObject_RichCompareBool(
                schema->entries[index].name, name, Py_EQ);
            if (equal < 0) return -2;
            if (equal) return index;
        }
        slot = (slot + 1) & mask;
    }
}

static Py_ssize_t
graphql_policy_find(GraphqlPolicySchema *schema, PyObject *name)
{
    Py_hash_t hash = PyObject_Hash(name);
    if (hash == -1) return -2;
    return graphql_policy_find_hashed(schema, name, hash);
}

static int
graphql_policy_insert(GraphqlPolicySchema *schema, Py_ssize_t index)
{
    size_t mask = (size_t)schema->table_size - 1;
    size_t slot = (size_t)schema->entries[index].hash & mask;
    while (schema->slots[slot] >= 0) slot = (slot + 1) & mask;
    schema->slots[slot] = index;
    return 0;
}

PyObject *
wreath_graphql_policy_schema(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *policies_object, *converter;
    PyObject *policies = NULL, *capsule = NULL;
    GraphqlPolicySchema *schema = NULL;
    if (!PyArg_UnpackTuple(args, "graphql_policy_schema", 2, 2,
                           &policies_object, &converter)) return NULL;
    if (!PyCallable_Check(converter)) {
        PyErr_SetString(PyExc_TypeError, "GraphQL policy converter must be callable");
        return NULL;
    }
    policies = PySequence_Fast(
        policies_object, "GraphQL policies must be a sequence");
    if (policies == NULL) return NULL;
    Py_ssize_t supplied = PySequence_Fast_GET_SIZE(policies);
    if ((size_t)supplied > SIZE_MAX / sizeof(*schema->entries)) {
        Py_DECREF(policies);
        return PyErr_NoMemory();
    }
    schema = PyMem_Calloc(1, sizeof(*schema));
    if (schema == NULL) goto memory;
    schema->table_size = graphql_policy_table_size(supplied);
    if (schema->table_size < 0) goto memory;
    schema->entries = PyMem_Calloc(
        (size_t)(supplied > 0 ? supplied : 1), sizeof(*schema->entries));
    schema->slots = PyMem_Malloc(
        (size_t)schema->table_size * sizeof(*schema->slots));
    if (schema->entries == NULL || schema->slots == NULL) goto memory;
    for (Py_ssize_t slot = 0; slot < schema->table_size; slot++)
        schema->slots[slot] = -1;
    for (Py_ssize_t supplied_index = 0; supplied_index < supplied;
         supplied_index++) {
        PyObject *name = PySequence_Fast_GET_ITEM(policies, supplied_index);
        Py_hash_t hash;
        Py_ssize_t found;
        if (!PyUnicode_Check(name)) {
            PyErr_SetString(PyExc_TypeError, "GraphQL policy names must be strings");
            goto error;
        }
        hash = PyObject_Hash(name);
        if (hash == -1) goto error;
        found = graphql_policy_find_hashed(schema, name, hash);
        if (found == -2) goto error;
        if (found >= 0) continue;
        schema->entries[schema->count].name = Py_NewRef(name);
        schema->entries[schema->count].resource = PyObject_CallOneArg(converter, name);
        schema->entries[schema->count].hash = hash;
        if (schema->entries[schema->count].resource == NULL) goto error;
        graphql_policy_insert(schema, schema->count);
        schema->count++;
    }
    capsule = PyCapsule_New(
        schema, GRAPHQL_POLICY_SCHEMA_CAPSULE, graphql_policy_schema_destroy);
    if (capsule == NULL) goto error;
    Py_DECREF(policies);
    return capsule;

memory:
    PyErr_NoMemory();
error:
    Py_XDECREF(policies);
    graphql_policy_schema_free(schema);
    return NULL;
}

PyObject *
wreath_graphql_policy_state(PyObject *Py_UNUSED(self), PyObject *schema_capsule)
{
    GraphqlPolicySchema *schema = graphql_policy_schema_from(schema_capsule);
    GraphqlPolicyState *state;
    PyObject *capsule;
    if (schema == NULL) return NULL;
    state = PyMem_Calloc(1, sizeof(*state));
    if (state == NULL) return PyErr_NoMemory();
    state->decisions = PyMem_Malloc(
        (size_t)(schema->count > 0 ? schema->count : 1));
    if (state->decisions == NULL) {
        PyMem_Free(state);
        return PyErr_NoMemory();
    }
    memset(state->decisions, -1, (size_t)schema->count);
    state->schema_capsule = Py_NewRef(schema_capsule);
    capsule = PyCapsule_New(
        state, GRAPHQL_POLICY_STATE_CAPSULE, graphql_policy_state_destroy);
    if (capsule == NULL) {
        Py_DECREF(state->schema_capsule);
        PyMem_Free(state->decisions);
        PyMem_Free(state);
    }
    return capsule;
}

static int
graphql_policy_same_schema(GraphqlPolicyState *state, PyObject *schema_capsule)
{
    if (state->schema_capsule == schema_capsule) return 1;
    PyErr_SetString(PyExc_ValueError, "GraphQL policy state belongs to another schema");
    return 0;
}

static int
graphql_policy_plan_add_pending(GraphqlPolicyPlan *plan,
                                GraphqlPolicyState *state, Py_ssize_t index,
                                unsigned char *seen)
{
    if (seen[index]) return 0;
    seen[index] = 1;
    if (state->decisions[index] < 0)
        plan->pending[plan->pending_count++] = index;
    return 0;
}

PyObject *
wreath_graphql_policy_prepare(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *schema_capsule, *state_capsule, *schema_fields;
    PyObject *fields_object, *root_policy, *root_name;
    PyObject *fields = NULL, *capsule = NULL;
    GraphqlPolicySchema *schema;
    GraphqlPolicyState *state;
    GraphqlPolicyPlan *plan = NULL;
    unsigned char *pending_seen = NULL, *field_seen = NULL;
    if (!PyArg_ParseTuple(args, "OOOOOO:graphql_policy_prepare",
                          &schema_capsule, &state_capsule, &schema_fields,
                          &fields_object, &root_policy, &root_name)) return NULL;
    schema = graphql_policy_schema_from(schema_capsule);
    state = graphql_policy_state_from(state_capsule);
    if (schema == NULL || state == NULL ||
        !graphql_policy_same_schema(state, schema_capsule)) return NULL;
    if (!PyDict_Check(schema_fields)) {
        PyErr_SetString(PyExc_TypeError, "GraphQL schema fields must be a dict");
        return NULL;
    }
    fields = PySequence_Fast(fields_object, "GraphQL fields must be a sequence");
    if (fields == NULL) return NULL;
    Py_ssize_t supplied = PySequence_Fast_GET_SIZE(fields);
    if (supplied == PY_SSIZE_T_MAX ||
        (size_t)supplied > SIZE_MAX / sizeof(*plan->field_indices) ||
        (size_t)supplied > SIZE_MAX / sizeof(*plan->field_names) ||
        (size_t)(supplied + 1) > SIZE_MAX / sizeof(*plan->pending)) {
        Py_DECREF(fields);
        return PyErr_NoMemory();
    }
    plan = PyMem_Calloc(1, sizeof(*plan));
    if (plan == NULL) goto memory;
    plan->root_index = -1;
    plan->schema_capsule = Py_NewRef(schema_capsule);
    plan->root_name = root_name == Py_None ? NULL : Py_NewRef(root_name);
    plan->field_indices = PyMem_Malloc(
        (size_t)(supplied > 0 ? supplied : 1) * sizeof(*plan->field_indices));
    plan->field_names = PyMem_Calloc(
        (size_t)(supplied > 0 ? supplied : 1), sizeof(*plan->field_names));
    plan->pending = PyMem_Malloc(
        (size_t)(supplied + 1) * sizeof(*plan->pending));
    pending_seen = PyMem_Calloc(
        (size_t)(schema->count > 0 ? schema->count : 1), 1);
    field_seen = PyMem_Calloc(
        (size_t)(schema->count > 0 ? schema->count : 1), 1);
    if (plan->field_indices == NULL || plan->field_names == NULL ||
        plan->pending == NULL || pending_seen == NULL ||
        field_seen == NULL) goto memory;
    if (root_policy != Py_None) {
        plan->root_index = graphql_policy_find(schema, root_policy);
        if (plan->root_index == -2) goto error;
        if (plan->root_index < 0) {
            PyErr_SetObject(PyExc_KeyError, root_policy);
            goto error;
        }
        graphql_policy_plan_add_pending(
            plan, state, plan->root_index, pending_seen);
    }
    for (Py_ssize_t supplied_index = 0; supplied_index < supplied;
         supplied_index++) {
        PyObject *field = PySequence_Fast_GET_ITEM(fields, supplied_index);
        PyObject *name = graphql_getattr(field, GQ_ATTR_NAME);
        PyObject *schema_field = NULL, *policy = NULL;
        Py_ssize_t policy_index;
        if (name == NULL) goto error;
        if (PyDict_GetItemRef(schema_fields, name, &schema_field) < 0) {
            Py_DECREF(name);
            goto error;
        }
        if (schema_field == NULL) {
            PyErr_SetObject(PyExc_KeyError, name);
            Py_DECREF(name);
            goto error;
        }
        policy = graphql_getattr(schema_field, GQ_ATTR_POLICY);
        Py_DECREF(schema_field);
        if (policy == NULL) {
            Py_DECREF(name);
            goto error;
        }
        policy_index = graphql_policy_find(schema, policy);
        Py_DECREF(policy);
        if (policy_index == -2) {
            Py_DECREF(name);
            goto error;
        }
        if (policy_index < 0) {
            PyErr_SetObject(PyExc_KeyError, name);
            Py_DECREF(name);
            goto error;
        }
        if (!field_seen[policy_index]) {
            plan->field_indices[plan->field_count] = policy_index;
            plan->field_names[plan->field_count] = name;
            plan->field_count++;
            field_seen[policy_index] = 1;
            graphql_policy_plan_add_pending(
                plan, state, policy_index, pending_seen);
        }
        else Py_DECREF(name);
    }
    PyMem_Free(field_seen);
    PyMem_Free(pending_seen);
    Py_DECREF(fields);
    capsule = PyCapsule_New(
        plan, GRAPHQL_POLICY_PLAN_CAPSULE, graphql_policy_plan_destroy);
    if (capsule == NULL) graphql_policy_plan_free(plan);
    return capsule;

memory:
    PyErr_NoMemory();
error:
    PyMem_Free(field_seen);
    PyMem_Free(pending_seen);
    Py_XDECREF(fields);
    graphql_policy_plan_free(plan);
    return NULL;
}

PyObject *
wreath_graphql_policy_resources(PyObject *Py_UNUSED(self), PyObject *plan_capsule)
{
    GraphqlPolicyPlan *plan = graphql_policy_plan_from(plan_capsule);
    GraphqlPolicySchema *schema;
    PyObject *resources;
    if (plan == NULL) return NULL;
    schema = graphql_policy_schema_from(plan->schema_capsule);
    if (schema == NULL) return NULL;
    resources = PyTuple_New(plan->pending_count);
    if (resources == NULL) return NULL;
    for (Py_ssize_t index = 0; index < plan->pending_count; index++)
        PyTuple_SET_ITEM(resources, index,
                         Py_NewRef(schema->entries[plan->pending[index]].resource));
    return resources;
}

static PyObject *
graphql_policy_path(GraphqlPolicyPlan *plan, Py_ssize_t policy_index)
{
    if (policy_index == plan->root_index && plan->root_name != NULL)
        return PyTuple_Pack(1, plan->root_name);
    for (Py_ssize_t index = 0; index < plan->field_count; index++) {
        if (plan->field_indices[index] == policy_index) {
            if (plan->root_name == NULL)
                return PyTuple_Pack(1, plan->field_names[index]);
            return PyTuple_Pack(2, plan->root_name, plan->field_names[index]);
        }
    }
    PyErr_SetString(PyExc_RuntimeError, "GraphQL policy plan lost a selected resource");
    return NULL;
}

PyObject *
wreath_graphql_policy_items(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *plan_capsule, *action, *requirement_type;
    GraphqlPolicyPlan *plan;
    GraphqlPolicySchema *schema;
    PyObject *items;
    if (!PyArg_UnpackTuple(args, "graphql_policy_items", 3, 3,
                           &plan_capsule, &action, &requirement_type)) return NULL;
    plan = graphql_policy_plan_from(plan_capsule);
    if (plan == NULL) return NULL;
    schema = graphql_policy_schema_from(plan->schema_capsule);
    if (schema == NULL) return NULL;
    items = PyTuple_New(plan->pending_count);
    if (items == NULL) return NULL;
    for (Py_ssize_t index = 0; index < plan->pending_count; index++) {
        Py_ssize_t policy_index = plan->pending[index];
        PyObject *requirement = PyObject_CallFunctionObjArgs(
            requirement_type, action, schema->entries[policy_index].resource, NULL);
        PyObject *path = NULL, *item = NULL;
        if (requirement == NULL) goto error;
        path = graphql_policy_path(plan, policy_index);
        if (path == NULL) {
            Py_DECREF(requirement);
            goto error;
        }
        item = wreath_tuple2_from_owned(requirement, path);
        if (item == NULL) goto error;
        PyTuple_SET_ITEM(items, index, item);
    }
    return items;
error:
    Py_DECREF(items);
    return NULL;
}

PyObject *
wreath_graphql_policy_apply(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *plan_capsule, *state_capsule, *decisions_object;
    PyObject *decisions = NULL;
    GraphqlPolicyPlan *plan;
    GraphqlPolicyState *state;
    int stop_on_denied;
    if (!PyArg_ParseTuple(args, "OOOp:graphql_policy_apply", &plan_capsule,
                          &state_capsule, &decisions_object,
                          &stop_on_denied)) return NULL;
    plan = graphql_policy_plan_from(plan_capsule);
    state = graphql_policy_state_from(state_capsule);
    if (plan == NULL || state == NULL ||
        !graphql_policy_same_schema(state, plan->schema_capsule)) return NULL;
    Py_ssize_t native_count = 0;
    const unsigned char *native_allowed = NULL;
    const unsigned char *native_reason = NULL;
    int native = wreath_cedar_decision_batch_read(
        decisions_object, &native_count, &native_allowed, &native_reason);
    if (native < 0) return NULL;
    if (native) {
        if (native_count > plan->pending_count) {
            PyErr_SetString(
                PyExc_ValueError, "more authorization decisions than resources");
            return NULL;
        }
        for (Py_ssize_t index = 0; index < native_count; index++) {
            int allowed = native_allowed[index] != 0;
            state->decisions[plan->pending[index]] = (signed char)allowed;
            if (!allowed && stop_on_denied) {
                static const char *reasons[] = {
                    "no permit policy matched", "cedar permit", "explicit forbid",
                };
                GraphqlPolicySchema *schema = graphql_policy_schema_from(
                    plan->schema_capsule);
                if (schema == NULL) return NULL;
                PyObject *reason = PyUnicode_FromString(reasons[native_reason[index]]);
                PyObject *path = reason != NULL
                    ? graphql_policy_path(plan, plan->pending[index]) : NULL;
                PyObject *denial = path != NULL ? PyTuple_Pack(
                    3, reason, path,
                    schema->entries[plan->pending[index]].name) : NULL;
                Py_XDECREF(path);
                Py_XDECREF(reason);
                return denial;
            }
        }
        if (native_count != plan->pending_count) {
            PyErr_SetString(
                PyExc_ValueError, "fewer authorization decisions than resources");
            return NULL;
        }
        Py_RETURN_NONE;
    }
    decisions = PySequence_Fast(
        decisions_object, "authorization decisions must be a sequence");
    if (decisions == NULL) return NULL;
    Py_ssize_t supplied = PySequence_Fast_GET_SIZE(decisions);
    if (supplied > plan->pending_count) {
        PyErr_SetString(PyExc_ValueError, "more authorization decisions than resources");
        Py_DECREF(decisions);
        return NULL;
    }
    for (Py_ssize_t index = 0; index < supplied; index++) {
        PyObject *decision = PySequence_Fast_GET_ITEM(decisions, index);
        PyObject *allowed_object = NULL;
        int found_allowed = PyObject_GetOptionalAttrString(
            decision, "allowed", &allowed_object);
        int allowed;
        if (found_allowed < 0) {
            Py_DECREF(decisions);
            return NULL;
        }
        if (!found_allowed) allowed = 0;
        else {
            allowed = PyObject_IsTrue(allowed_object);
            Py_DECREF(allowed_object);
            if (allowed < 0) {
                Py_DECREF(decisions);
                return NULL;
            }
        }
        state->decisions[plan->pending[index]] = (signed char)allowed;
        if (!allowed && stop_on_denied) {
            PyObject *reason = NULL;
            int found_reason = PyObject_GetOptionalAttrString(
                decision, "reason", &reason);
            GraphqlPolicySchema *schema;
            PyObject *path, *denial;
            if (found_reason < 0) {
                Py_DECREF(decisions);
                return NULL;
            }
            if (!found_reason) reason = Py_NewRef(Py_None);
            schema = graphql_policy_schema_from(plan->schema_capsule);
            if (schema == NULL) {
                Py_DECREF(reason);
                Py_DECREF(decisions);
                return NULL;
            }
            path = graphql_policy_path(plan, plan->pending[index]);
            if (path == NULL) {
                Py_DECREF(reason);
                Py_DECREF(decisions);
                return NULL;
            }
            denial = PyTuple_Pack(
                3, reason, path,
                schema->entries[plan->pending[index]].name);
            Py_DECREF(reason);
            Py_DECREF(path);
            Py_DECREF(decisions);
            return denial;
        }
    }
    if (supplied != plan->pending_count) {
        PyErr_SetString(PyExc_ValueError, "fewer authorization decisions than resources");
        Py_DECREF(decisions);
        return NULL;
    }
    Py_DECREF(decisions);
    Py_RETURN_NONE;
}

PyObject *
wreath_graphql_policy_result(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *plan_capsule, *state_capsule;
    GraphqlPolicyPlan *plan;
    GraphqlPolicyState *state;
    int root_allowed = 1, projection_allowed = 1;
    if (!PyArg_UnpackTuple(args, "graphql_policy_result", 2, 2,
                           &plan_capsule, &state_capsule)) return NULL;
    plan = graphql_policy_plan_from(plan_capsule);
    state = graphql_policy_state_from(state_capsule);
    if (plan == NULL || state == NULL ||
        !graphql_policy_same_schema(state, plan->schema_capsule)) return NULL;
    if (plan->root_index >= 0) {
        if (state->decisions[plan->root_index] < 0) goto missing;
        root_allowed = state->decisions[plan->root_index] != 0;
    }
    for (Py_ssize_t index = 0; index < plan->field_count; index++) {
        signed char decision = state->decisions[plan->field_indices[index]];
        if (decision < 0) goto missing;
        if (!decision) projection_allowed = 0;
    }
    return PyLong_FromLong(root_allowed | (projection_allowed << 1));
missing:
    PyErr_SetString(PyExc_RuntimeError, "GraphQL policy result is incomplete");
    return NULL;
}

static Py_ssize_t
graphql_policy_lookup_pair(PyObject *schema_capsule, PyObject *state_capsule,
                           PyObject *resource, GraphqlPolicyState **out_state)
{
    GraphqlPolicySchema *schema = graphql_policy_schema_from(schema_capsule);
    GraphqlPolicyState *state = graphql_policy_state_from(state_capsule);
    Py_ssize_t index;
    if (schema == NULL || state == NULL ||
        !graphql_policy_same_schema(state, schema_capsule)) return -2;
    index = graphql_policy_find(schema, resource);
    if (index == -1) PyErr_SetObject(PyExc_KeyError, resource);
    if (index < 0) return -2;
    *out_state = state;
    return index;
}

PyObject *
wreath_graphql_policy_cached(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *schema_capsule, *state_capsule, *resource;
    GraphqlPolicyState *state;
    Py_ssize_t index;
    if (!PyArg_UnpackTuple(args, "graphql_policy_cached", 3, 3,
                           &schema_capsule, &state_capsule, &resource)) return NULL;
    index = graphql_policy_lookup_pair(
        schema_capsule, state_capsule, resource, &state);
    if (index < 0) return NULL;
    return PyLong_FromLong(state->decisions[index]);
}

PyObject *
wreath_graphql_policy_store(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *schema_capsule, *state_capsule, *resource;
    GraphqlPolicyState *state;
    Py_ssize_t index;
    int allowed;
    if (!PyArg_ParseTuple(args, "OOOp:graphql_policy_store", &schema_capsule,
                          &state_capsule, &resource, &allowed)) return NULL;
    index = graphql_policy_lookup_pair(
        schema_capsule, state_capsule, resource, &state);
    if (index < 0) return NULL;
    state->decisions[index] = (signed char)allowed;
    Py_RETURN_NONE;
}

PyObject *
wreath_graphql_policy_resource(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *schema_capsule, *resource;
    GraphqlPolicySchema *schema;
    Py_ssize_t index;
    if (!PyArg_UnpackTuple(args, "graphql_policy_resource", 2, 2,
                           &schema_capsule, &resource)) return NULL;
    schema = graphql_policy_schema_from(schema_capsule);
    if (schema == NULL) return NULL;
    index = graphql_policy_find(schema, resource);
    if (index == -1) PyErr_SetObject(PyExc_KeyError, resource);
    if (index < 0) return NULL;
    return Py_NewRef(schema->entries[index].resource);
}

typedef struct {
    PyObject *key;
    PyObject **values;
    Py_hash_t hash;
} GraphqlResultField;

typedef struct {
    Py_ssize_t rows;
    Py_ssize_t count;
    Py_ssize_t capacity;
    GraphqlResultField *fields;
    Py_ssize_t table_size;
    Py_ssize_t *slots;
} GraphqlResults;

#define GRAPHQL_RESULTS_CAPSULE "wreath.graphql.results"

static void
graphql_result_values_clear(PyObject **values, Py_ssize_t rows)
{
    if (values == NULL) return;
    for (Py_ssize_t row = 0; row < rows; row++) Py_XDECREF(values[row]);
    PyMem_Free(values);
}

static void
graphql_results_free(GraphqlResults *results)
{
    if (results == NULL) return;
    for (Py_ssize_t field = 0; field < results->count; field++) {
        Py_XDECREF(results->fields[field].key);
        graphql_result_values_clear(
            results->fields[field].values, results->rows);
    }
    PyMem_Free(results->fields);
    PyMem_Free(results->slots);
    PyMem_Free(results);
}

static void
graphql_results_destroy(PyObject *capsule)
{
    GraphqlResults *results = PyCapsule_GetPointer(
        capsule, GRAPHQL_RESULTS_CAPSULE);
    if (results == NULL) {
        PyErr_Clear();
        return;
    }
    graphql_results_free(results);
}

static GraphqlResults *
graphql_results_from(PyObject *capsule)
{
    return PyCapsule_GetPointer(capsule, GRAPHQL_RESULTS_CAPSULE);
}

static PyObject **
graphql_result_values_new(Py_ssize_t rows)
{
    PyObject **values;
    if (rows == 0) return NULL;
    if ((size_t)rows > SIZE_MAX / sizeof(*values)) {
        PyErr_NoMemory();
        return NULL;
    }
    values = PyMem_Calloc((size_t)rows, sizeof(*values));
    if (values == NULL) PyErr_NoMemory();
    return values;
}

static int
graphql_results_table_grow(GraphqlResults *results)
{
    Py_ssize_t next_size = results->table_size == 0 ? 8 : results->table_size * 2;
    if (next_size < results->table_size ||
        (size_t)next_size > SIZE_MAX / sizeof(*results->slots)) {
        PyErr_NoMemory();
        return -1;
    }
    Py_ssize_t *next = PyMem_Malloc((size_t)next_size * sizeof(*next));
    if (next == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (Py_ssize_t slot = 0; slot < next_size; slot++) next[slot] = -1;
    for (Py_ssize_t index = 0; index < results->count; index++) {
        size_t slot = (size_t)results->fields[index].hash &
                      ((size_t)next_size - 1);
        while (next[slot] >= 0) slot = (slot + 1) & ((size_t)next_size - 1);
        next[slot] = index;
    }
    PyMem_Free(results->slots);
    results->slots = next;
    results->table_size = next_size;
    return 0;
}

static Py_ssize_t
graphql_results_find(GraphqlResults *results, PyObject *key, Py_hash_t hash,
                     size_t *empty_slot)
{
    size_t slot = (size_t)hash & ((size_t)results->table_size - 1);
    for (;;) {
        Py_ssize_t index = results->slots[slot];
        if (index < 0) {
            *empty_slot = slot;
            return -1;
        }
        if (results->fields[index].hash == hash) {
            int equal = results->fields[index].key == key ? 1 :
                PyObject_RichCompareBool(results->fields[index].key, key, Py_EQ);
            if (equal < 0) return -2;
            if (equal) return index;
        }
        slot = (slot + 1) & ((size_t)results->table_size - 1);
    }
}

/* Takes ownership of every value in `values`. Equal response keys overwrite
 * the earlier column, matching dict assignment without materializing a dict
 * for each row while asynchronous fields are still resolving. */
static int
graphql_results_store(GraphqlResults *results, PyObject *key, PyObject **values)
{
    Py_hash_t hash = PyObject_Hash(key);
    size_t empty_slot;
    if (hash == -1) return -1;
    Py_ssize_t found = graphql_results_find(results, key, hash, &empty_slot);
    if (found == -2) return -1;
    if (found >= 0) {
        graphql_result_values_clear(
            results->fields[found].values, results->rows);
        results->fields[found].values = values;
        return 0;
    }
    if ((results->count + 1) * 3 >= results->table_size * 2) {
        if (graphql_results_table_grow(results) < 0) return -1;
        found = graphql_results_find(results, key, hash, &empty_slot);
        if (found == -2) return -1;
    }
    if (results->count == results->capacity) {
        Py_ssize_t capacity = results->capacity == 0 ? 8 : results->capacity * 2;
        GraphqlResultField *grown;
        if (capacity < results->capacity ||
            (size_t)capacity > SIZE_MAX / sizeof(*grown)) {
            PyErr_NoMemory();
            return -1;
        }
        grown = PyMem_Realloc(
            results->fields, (size_t)capacity * sizeof(*grown));
        if (grown == NULL) {
            PyErr_NoMemory();
            return -1;
        }
        results->fields = grown;
        results->capacity = capacity;
    }
    results->fields[results->count].key = Py_NewRef(key);
    results->fields[results->count].values = values;
    results->fields[results->count].hash = hash;
    results->slots[empty_slot] = results->count;
    results->count++;
    return 0;
}

static int
graphql_results_same_size(GraphqlResults *results, PyObject *values,
                          Py_ssize_t *size)
{
    Py_ssize_t count = PySequence_Size(values);
    if (count < 0) return -1;
    if (results->rows != count) {
        PyErr_Format(
            PyExc_ValueError,
            "GraphQL projection lengths differ: %zd and %zd",
            results->rows, count);
        return -1;
    }
    *size = count;
    return 0;
}

PyObject *
wreath_graphql_new_results(PyObject *Py_UNUSED(self), PyObject *instances)
{
    Py_ssize_t size = PySequence_Size(instances);
    GraphqlResults *results;
    PyObject *capsule;
    if (size < 0) return NULL;
    results = PyMem_Calloc(1, sizeof(*results));
    if (results == NULL) return PyErr_NoMemory();
    results->rows = size;
    if (graphql_results_table_grow(results) < 0) {
        graphql_results_free(results);
        return NULL;
    }
    capsule = PyCapsule_New(
        results, GRAPHQL_RESULTS_CAPSULE, graphql_results_destroy);
    if (capsule == NULL) graphql_results_free(results);
    return capsule;
}

PyObject *
wreath_graphql_finish_results(PyObject *Py_UNUSED(self), PyObject *capsule)
{
    GraphqlResults *results = graphql_results_from(capsule);
    PyObject *rows;
    if (results == NULL) return NULL;
    rows = PyList_New(results->rows);
    if (rows == NULL) return NULL;
    for (Py_ssize_t row = 0; row < results->rows; row++) {
        PyObject *item = _PyDict_NewPresized(results->count);
        if (item == NULL) {
            Py_DECREF(rows);
            return NULL;
        }
        PyList_SET_ITEM(rows, row, item);
        for (Py_ssize_t field = 0; field < results->count; field++) {
            if (PyDict_SetItem(
                    item, results->fields[field].key,
                    results->fields[field].values[row]) < 0) {
                Py_DECREF(rows);
                return NULL;
            }
        }
    }
    return rows;
}

typedef struct {
    PyObject *key;
    PyObject *key_json;
    PyObject *attribute;
} GraphqlPlainField;

typedef struct {
    PyObject *instances;
    GraphqlPlainField *fields;
    Py_ssize_t field_count;
    int is_list;
} GraphqlProjection;

#define GRAPHQL_PROJECTION_CAPSULE "wreath.graphql.projection"

static void
graphql_plain_fields_clear(GraphqlPlainField *plan, Py_ssize_t count)
{
    if (plan == NULL) return;
    for (Py_ssize_t index = 0; index < count; index++) {
        Py_XDECREF(plan[index].key);
        Py_XDECREF(plan[index].key_json);
        Py_XDECREF(plan[index].attribute);
    }
    PyMem_Free(plan);
}

/* Compile the field list into a plan: 1 on success, 0 when a field declines
 * the fast path (a resolver, a relationship, or no attribute to read), -1 with
 * an exception set.
 *
 * `want_key_json` pre-renders each key's JSON form, which the writer path needs
 * and the dict path would only throw away. It is a parameter rather than a
 * second copy of the loop: this walk is thirty lines of paired refcounts and
 * five `goto`s, and `graphql_project_plain` used to carry its own transcription
 * of it -- so a leak fixed on one path stayed on the other. */
static int
graphql_plain_fields_compile(PyObject *schema_fields, PyObject *fields,
                             GraphqlPlainField **out, Py_ssize_t *count,
                             int want_key_json)
{
    GraphqlPlainField *plan = NULL;
    Py_ssize_t field_count = PySequence_Fast_GET_SIZE(fields);
    plan = PyMem_Calloc((size_t)(field_count > 0 ? field_count : 1), sizeof(*plan));
    if (plan == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (Py_ssize_t index = 0; index < field_count; index++) {
        PyObject *field = PySequence_Fast_GET_ITEM(fields, index);
        PyObject *name = graphql_getattr(field, GQ_ATTR_NAME);
        PyObject *schema_field = NULL;
        PyObject *resolver = NULL;
        PyObject *relationship = NULL;
        PyObject *column = NULL;
        if (name == NULL) goto error;
        if (PyDict_GetItemRef(schema_fields, name, &schema_field) < 0) {
            Py_DECREF(name);
            goto error;
        }
        Py_DECREF(name);
        if (schema_field == NULL) goto declined;
        resolver = graphql_getattr(schema_field, GQ_ATTR_RESOLVER);
        relationship = graphql_getattr(schema_field, GQ_ATTR_RELATIONSHIP);
        if (resolver == NULL || relationship == NULL) {
            Py_XDECREF(relationship);
            Py_XDECREF(resolver);
            Py_DECREF(schema_field);
            goto error;
        }
        if (resolver != Py_None || relationship != Py_None) {
            Py_DECREF(relationship);
            Py_DECREF(resolver);
            Py_DECREF(schema_field);
            goto declined;
        }
        Py_DECREF(relationship);
        Py_DECREF(resolver);
        column = graphql_getattr(schema_field, GQ_ATTR_COLUMN);
        if (column == NULL) {
            Py_DECREF(schema_field);
            goto error;
        }
        if (column != Py_None) {
            plan[index].attribute = graphql_getattr(column, GQ_ATTR_PYTHON_NAME);
        }
        else {
            plan[index].attribute = graphql_getattr(schema_field, GQ_ATTR_ATTRIBUTE);
        }
        Py_DECREF(column);
        Py_DECREF(schema_field);
        if (plan[index].attribute == NULL) goto error;
        if (plan[index].attribute == Py_None) goto declined;
        plan[index].key = graphql_getattr(field, GQ_ATTR_KEY);
        if (plan[index].key == NULL) goto error;
        if (want_key_json) {
            plan[index].key_json = wreath_json_dumps(NULL, plan[index].key);
            if (plan[index].key_json == NULL) goto error;
        }
    }
    *out = plan;
    *count = field_count;
    return 1;

declined:
    graphql_plain_fields_clear(plan, field_count);
    return 0;
error:
    graphql_plain_fields_clear(plan, field_count);
    return -1;
}

static void
graphql_projection_destroy(PyObject *capsule)
{
    GraphqlProjection *projection = PyCapsule_GetPointer(
        capsule, GRAPHQL_PROJECTION_CAPSULE);
    if (projection == NULL) {
        PyErr_Clear();
        return;
    }
    Py_DECREF(projection->instances);
    graphql_plain_fields_clear(projection->fields, projection->field_count);
    PyMem_Free(projection);
}

PyObject *
wreath_graphql_project_json(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *instances_object;
    PyObject *schema_fields;
    PyObject *fields_object;
    PyObject *instances = NULL;
    PyObject *fields = NULL;
    PyObject *capsule = NULL;
    GraphqlProjection *projection = NULL;
    int is_list;
    int compiled;
    if (!PyArg_ParseTuple(args, "OOOp:graphql_project_json", &instances_object,
                          &schema_fields, &fields_object, &is_list)) return NULL;
    if (!PyDict_Check(schema_fields)) {
        PyErr_SetString(PyExc_TypeError, "GraphQL schema fields must be a dict");
        return NULL;
    }
    instances = PySequence_Fast(instances_object, "instances must be a sequence");
    fields = PySequence_Fast(fields_object, "fields must be a sequence");
    if (instances == NULL || fields == NULL) goto error;
    projection = PyMem_Calloc(1, sizeof(*projection));
    if (projection == NULL) {
        PyErr_NoMemory();
        goto error;
    }
    compiled = graphql_plain_fields_compile(
        schema_fields, fields, &projection->fields, &projection->field_count, 1);
    if (compiled < 0) goto error;
    if (compiled == 0) {
        PyMem_Free(projection);
        Py_DECREF(fields);
        Py_DECREF(instances);
        Py_RETURN_NONE;
    }
    projection->instances = instances;
    projection->is_list = is_list;
    capsule = PyCapsule_New(
        projection, GRAPHQL_PROJECTION_CAPSULE, graphql_projection_destroy);
    if (capsule == NULL) goto error;
    Py_DECREF(fields);
    return capsule;

error:
    if (projection != NULL) {
        graphql_plain_fields_clear(projection->fields, projection->field_count);
        PyMem_Free(projection);
    }
    Py_XDECREF(fields);
    Py_XDECREF(instances);
    return NULL;
}

static int
graphql_write_row(WreathBytesWriter *writer, GraphqlProjection *projection,
                  PyObject *instance, int depth)
{
    if (wreath_writer_byte(writer, '{') < 0) return -1;
    for (Py_ssize_t field = 0; field < projection->field_count; field++) {
        PyObject *value = NULL;
        int found;
        if (field > 0 && wreath_writer_byte(writer, ',') < 0) return -1;
        if (wreath_writer_write(
                writer,
                PyBytes_AS_STRING(projection->fields[field].key_json),
                PyBytes_GET_SIZE(projection->fields[field].key_json)) < 0 ||
            wreath_writer_byte(writer, ':') < 0) return -1;
        found = PyObject_GetOptionalAttr(
            instance, projection->fields[field].attribute, &value);
        if (found < 0) return -1;
        if (!found) value = Py_NewRef(Py_None);
        if (wreath_json_write_value(writer, value, depth + 1) < 0) {
            Py_DECREF(value);
            return -1;
        }
        Py_DECREF(value);
    }
    return wreath_writer_byte(writer, '}');
}

int
wreath_graphql_write_projection(WreathBytesWriter *writer, PyObject *capsule,
                                int depth)
{
    GraphqlProjection *projection = PyCapsule_GetPointer(
        capsule, GRAPHQL_PROJECTION_CAPSULE);
    Py_ssize_t size;
    if (projection == NULL) return -1;
    size = PySequence_Fast_GET_SIZE(projection->instances);
    if (!projection->is_list) {
        if (size == 0) return wreath_writer_write(writer, "null", 4);
        return graphql_write_row(
            writer, projection, PySequence_Fast_GET_ITEM(projection->instances, 0),
            depth);
    }
    if (wreath_writer_byte(writer, '[') < 0) return -1;
    for (Py_ssize_t row = 0; row < size; row++) {
        if (row > 0 && wreath_writer_byte(writer, ',') < 0) return -1;
        if (graphql_write_row(
                writer, projection,
                PySequence_Fast_GET_ITEM(projection->instances, row), depth + 1) < 0)
            return -1;
    }
    return wreath_writer_byte(writer, ']');
}

PyObject *
wreath_graphql_project_plain(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *instances_object;
    PyObject *schema_fields;
    PyObject *fields_object;
    PyObject *instances = NULL;
    PyObject *fields = NULL;
    PyObject *results = NULL;
    GraphqlPlainField *plan = NULL;
    /* `compile` frees the plan and leaves `plan` NULL on both non-success
     * paths, so the labels below clear a null plan and a zero count. */
    Py_ssize_t field_count = 0;
    int compiled;
    if (!PyArg_ParseTuple(args, "OOO:graphql_project_plain", &instances_object,
                          &schema_fields, &fields_object)) return NULL;
    if (!PyDict_Check(schema_fields)) {
        PyErr_SetString(PyExc_TypeError, "GraphQL schema fields must be a dict");
        return NULL;
    }
    instances = PySequence_Fast(instances_object, "instances must be a sequence");
    fields = PySequence_Fast(fields_object, "fields must be a sequence");
    if (instances == NULL || fields == NULL) goto error;
    compiled = graphql_plain_fields_compile(
        schema_fields, fields, &plan, &field_count, 0);
    if (compiled < 0) goto error;
    if (compiled == 0) goto declined;
    results = PyList_New(PySequence_Fast_GET_SIZE(instances));
    if (results == NULL) goto error;
    for (Py_ssize_t row = 0; row < PySequence_Fast_GET_SIZE(instances); row++) {
        PyObject *result = _PyDict_NewPresized(field_count);
        PyObject *instance = PySequence_Fast_GET_ITEM(instances, row);
        if (result == NULL) goto error;
        PyList_SET_ITEM(results, row, result);
        for (Py_ssize_t field = 0; field < field_count; field++) {
            PyObject *value = NULL;
            int found = PyObject_GetOptionalAttr(
                instance, plan[field].attribute, &value);
            if (found < 0) goto error;
            if (!found) value = Py_NewRef(Py_None);
            if (PyDict_SetItem(result, plan[field].key, value) < 0) {
                Py_DECREF(value);
                goto error;
            }
            Py_DECREF(value);
        }
    }
    graphql_plain_fields_clear(plan, field_count);
    Py_DECREF(fields);
    Py_DECREF(instances);
    return results;

declined:
    graphql_plain_fields_clear(plan, field_count);
    Py_DECREF(fields);
    Py_DECREF(instances);
    Py_RETURN_NONE;
error:
    graphql_plain_fields_clear(plan, field_count);
    Py_XDECREF(results);
    Py_XDECREF(fields);
    Py_XDECREF(instances);
    return NULL;
}

PyObject *
wreath_graphql_project_constant(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *capsule, *key, *value;
    GraphqlResults *results;
    PyObject **values;
    if (!PyArg_UnpackTuple(args, "graphql_project_constant", 3, 3,
                           &capsule, &key, &value)) return NULL;
    results = graphql_results_from(capsule);
    if (results == NULL) return NULL;
    values = graphql_result_values_new(results->rows);
    if (values == NULL && results->rows != 0) return NULL;
    for (Py_ssize_t row = 0; row < results->rows; row++)
        values[row] = Py_NewRef(value);
    if (graphql_results_store(results, key, values) < 0) {
        graphql_result_values_clear(values, results->rows);
        return NULL;
    }
    Py_RETURN_NONE;
}

PyObject *
wreath_graphql_project_attribute(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *capsule, *instances, *key, *attribute;
    PyObject *objects = NULL;
    PyObject **values = NULL;
    GraphqlResults *results;
    Py_ssize_t size;
    if (!PyArg_UnpackTuple(args, "graphql_project_attribute", 4, 4,
                           &capsule, &instances, &key, &attribute)) return NULL;
    results = graphql_results_from(capsule);
    if (results == NULL ||
        graphql_results_same_size(results, instances, &size) < 0) return NULL;
    objects = PySequence_Fast(instances, "instances must be a sequence");
    values = graphql_result_values_new(size);
    if (objects == NULL || (values == NULL && size != 0)) goto error;
    for (Py_ssize_t index = 0; index < size; index++) {
        PyObject *value = NULL;
        int found = PyObject_GetOptionalAttr(
            PySequence_Fast_GET_ITEM(objects, index), attribute, &value);
        if (found < 0) goto error;
        if (!found) value = Py_NewRef(Py_None);
        values[index] = value;
    }
    if (graphql_results_store(results, key, values) < 0) goto error;
    Py_DECREF(objects);
    Py_RETURN_NONE;
error:
    graphql_result_values_clear(values, size);
    Py_XDECREF(objects);
    return NULL;
}

PyObject *
wreath_graphql_project_values(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *capsule, *key, *values_object;
    PyObject *items = NULL;
    PyObject **values = NULL;
    GraphqlResults *results;
    Py_ssize_t size;
    if (!PyArg_UnpackTuple(args, "graphql_project_values", 3, 3,
                           &capsule, &key, &values_object)) return NULL;
    results = graphql_results_from(capsule);
    if (results == NULL ||
        graphql_results_same_size(results, values_object, &size) < 0) return NULL;
    items = PySequence_Fast(values_object, "values must be a sequence");
    values = graphql_result_values_new(size);
    if (items == NULL || (values == NULL && size != 0)) goto error;
    for (Py_ssize_t index = 0; index < size; index++)
        values[index] = Py_NewRef(PySequence_Fast_GET_ITEM(items, index));
    if (graphql_results_store(results, key, values) < 0) goto error;
    Py_DECREF(items);
    Py_RETURN_NONE;
error:
    graphql_result_values_clear(values, size);
    Py_XDECREF(items);
    return NULL;
}

static PyObject *
flatten(PyObject *values, int is_list)
{
    PyObject *sequence = PySequence_Fast(values, "values must be a sequence");
    PyObject *flat = NULL, *layout = NULL, *answer = NULL;
    PyObject **children = NULL;
    Py_ssize_t count, flat_count = 0, flat_at = 0;
    if (sequence == NULL) return NULL;
    count = PySequence_Fast_GET_SIZE(sequence);
    layout = PyList_New(count);
    if (layout == NULL) goto error;
    if (is_list) {
        if ((size_t)count > SIZE_MAX / sizeof(*children)) {
            PyErr_NoMemory();
            goto error;
        }
        children = PyMem_Calloc(
            (size_t)(count == 0 ? 1 : count), sizeof(*children));
        if (children == NULL) {
            PyErr_NoMemory();
            goto error;
        }
        for (Py_ssize_t index = 0; index < count; index++) {
            PyObject *value = PySequence_Fast_GET_ITEM(sequence, index);
            children[index] = value == Py_None ? PyTuple_New(0) :
                PySequence_Fast(
                    value, "a list GraphQL field must return an iterable");
            if (children[index] == NULL) goto error;
            Py_ssize_t width = PySequence_Fast_GET_SIZE(children[index]);
            if (width > PY_SSIZE_T_MAX - flat_count) {
                PyErr_NoMemory();
                goto error;
            }
            flat_count += width;
        }
    }
    else {
        for (Py_ssize_t index = 0; index < count; index++)
            flat_count += PySequence_Fast_GET_ITEM(sequence, index) != Py_None;
    }
    flat = PyList_New(flat_count);
    if (flat == NULL) goto error;
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *value = PySequence_Fast_GET_ITEM(sequence, index);
        PyObject *slot = NULL;
        if (is_list) {
            Py_ssize_t start = flat_at;
            Py_ssize_t width = PySequence_Fast_GET_SIZE(children[index]);
            for (Py_ssize_t item = 0; item < width; item++) {
                PyList_SET_ITEM(
                    flat, flat_at++,
                    Py_NewRef(PySequence_Fast_GET_ITEM(children[index], item)));
            }
            slot = Py_BuildValue("nn", start, width);
        }
        else if (value == Py_None) slot = Py_NewRef(Py_None);
        else {
            slot = PyLong_FromSsize_t(flat_at);
            if (slot != NULL)
                PyList_SET_ITEM(flat, flat_at++, Py_NewRef(value));
        }
        if (slot == NULL) goto error;
        PyList_SET_ITEM(layout, index, slot);
    }
    answer = wreath_tuple2_from_owned(flat, layout);
    flat = NULL;
    layout = NULL;
error:
    if (children != NULL) {
        for (Py_ssize_t index = 0; index < count; index++)
            Py_XDECREF(children[index]);
        PyMem_Free(children);
    }
    Py_XDECREF(layout);
    Py_XDECREF(flat);
    Py_DECREF(sequence);
    return answer;
}

PyObject *
wreath_graphql_flatten_values(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *values;
    int is_list;
    if (!PyArg_ParseTuple(args, "Op:graphql_flatten_values", &values, &is_list))
        return NULL;
    return flatten(values, is_list);
}

PyObject *
wreath_graphql_flatten_relationship(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *instances, *objects = NULL, *values = NULL, *name = NULL;
    PyObject *argument = NULL, *answer;
    Py_ssize_t relation_index;
    int is_list;
    if (!PyArg_ParseTuple(args, "Onp:graphql_flatten_relationship",
                          &instances, &relation_index, &is_list)) return NULL;
    objects = PySequence_Fast(instances, "instances must be a sequence");
    if (objects == NULL) return NULL;
    values = PyList_New(PySequence_Fast_GET_SIZE(objects));
    name = Py_NewRef(graphql_attr_names[GQ_ATTR_ORM_GET_RELATION]);
    argument = PyLong_FromSsize_t(relation_index);
    if (values == NULL || name == NULL || argument == NULL) goto error;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(objects); index++) {
        PyObject *value = PyObject_CallMethodOneArg(
            PySequence_Fast_GET_ITEM(objects, index), name, argument);
        if (value == NULL) goto error;
        PyList_SET_ITEM(values, index, value);
    }
    answer = flatten(values, is_list);
    Py_DECREF(argument);
    Py_DECREF(name);
    Py_DECREF(values);
    Py_DECREF(objects);
    return answer;
error:
    Py_XDECREF(argument);
    Py_XDECREF(name);
    Py_XDECREF(values);
    Py_DECREF(objects);
    return NULL;
}

static PyObject *
layout_value(PyObject *projected, PyObject *slot, int is_list)
{
    if (is_list) {
        Py_ssize_t start, width;
        if (!PyArg_ParseTuple(slot, "nn", &start, &width)) return NULL;
        return PySequence_GetSlice(projected, start, start + width);
    }
    if (slot == Py_None) return Py_NewRef(Py_None);
    Py_ssize_t index = PyLong_AsSsize_t(slot);
    if (index == -1 && PyErr_Occurred()) return NULL;
    return PySequence_GetItem(projected, index);
}

PyObject *
wreath_graphql_restore_layout(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *capsule, *key, *projected, *layout;
    PyObject *slots = NULL;
    PyObject **values = NULL;
    GraphqlResults *results;
    Py_ssize_t size;
    int is_list;
    if (!PyArg_ParseTuple(args, "OOOOp:graphql_restore_layout",
                          &capsule, &key, &projected, &layout, &is_list)) return NULL;
    results = graphql_results_from(capsule);
    if (results == NULL ||
        graphql_results_same_size(results, layout, &size) < 0) return NULL;
    slots = PySequence_Fast(layout, "layout must be a sequence");
    values = graphql_result_values_new(size);
    if (slots == NULL || (values == NULL && size != 0)) goto error;
    for (Py_ssize_t index = 0; index < size; index++) {
        values[index] = layout_value(
            projected, PySequence_Fast_GET_ITEM(slots, index), is_list);
        if (values[index] == NULL) goto error;
    }
    if (graphql_results_store(results, key, values) < 0) goto error;
    Py_DECREF(slots);
    Py_RETURN_NONE;
error:
    graphql_result_values_clear(values, size);
    Py_XDECREF(slots);
    return NULL;
}

PyObject *
wreath_graphql_restore_values(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *projected, *layout, *slots = NULL, *out = NULL;
    int is_list;
    if (!PyArg_ParseTuple(args, "OOp:graphql_restore_values",
                          &projected, &layout, &is_list)) return NULL;
    slots = PySequence_Fast(layout, "layout must be a sequence");
    if (slots == NULL) return NULL;
    out = PyList_New(PySequence_Fast_GET_SIZE(slots));
    if (out == NULL) goto error;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(slots); index++) {
        PyObject *value = layout_value(
            projected, PySequence_Fast_GET_ITEM(slots, index), is_list);
        if (value == NULL) goto error;
        PyList_SET_ITEM(out, index, value);
    }
    Py_DECREF(slots);
    return out;
error:
    Py_XDECREF(out);
    Py_DECREF(slots);
    return NULL;
}
