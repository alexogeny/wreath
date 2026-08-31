/* Bulk ORM relationship keying and attachment.
 *
 * SQL execution and model-cell hydration are owned by the PostgreSQL extension.
 * These helpers own the remaining repeated Python-object traversal between the
 * hydrated child batch and its parent objects. All method-name objects and
 * temporary maps belong to one call; there is no native global state. */
#include "wreathcore.h"
#include "model_api.h"

static PyObject *
model_key(PyObject *instance, PyObject *indices, const WreathModelAPI *models)
{
    Py_ssize_t count = PyTuple_GET_SIZE(indices);
    PyObject *key = PyTuple_New(count);
    if (key == NULL) return NULL;
    for (Py_ssize_t index = 0; index < count; index++) {
        Py_ssize_t cell = PyLong_AsSsize_t(PyTuple_GET_ITEM(indices, index));
        int answer;
        if (cell == -1 && PyErr_Occurred()) goto error;
        answer = models->is_loaded(instance, cell);
        if (answer < 0) goto error;
        if (!answer) {
            Py_DECREF(key);
            return Py_NewRef(Py_None);
        }
        answer = models->is_null(instance, cell);
        if (answer < 0) goto error;
        if (answer) {
            Py_DECREF(key);
            return Py_NewRef(Py_None);
        }
        PyObject *value = models->get(instance, cell);
        if (value == NULL) goto error;
        PyTuple_SET_ITEM(key, index, value);
    }
    return key;
error:
    Py_DECREF(key);
    return NULL;
}

PyObject *
wreath_orm_relationship_keys(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *parents_object, *indices, *models_object;
    PyObject *parents = NULL, *groups = NULL;
    const WreathModelAPI *models;
    Py_ssize_t relation_index;
    int many;
    if (!PyArg_ParseTuple(args, "OOnpO:orm_relationship_keys", &parents_object,
                          &indices, &relation_index, &many, &models_object)) return NULL;
    if (!PyTuple_Check(indices)) {
        PyErr_SetString(PyExc_TypeError, "relationship column indices must be a tuple");
        return NULL;
    }
    models = wreath_model_api(models_object);
    if (models == NULL) return NULL;
    parents = PySequence_Fast(parents_object, "parents must be a sequence");
    groups = PyDict_New();
    if (parents == NULL || groups == NULL) goto error;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(parents); index++) {
        PyObject *parent = PySequence_Fast_GET_ITEM(parents, index);
        PyObject *key = model_key(parent, indices, models);
        PyObject *initial = many ? PyList_New(0) : Py_NewRef(Py_None);
        if (key == NULL || initial == NULL) {
            Py_XDECREF(key);
            Py_XDECREF(initial);
            goto error;
        }
        int status = models->set_relation(parent, relation_index, initial);
        Py_DECREF(initial);
        if (status < 0) { Py_DECREF(key); goto error; }
        if (key != Py_None) {
            PyObject *bucket = NULL;
            if (PyDict_GetItemRef(groups, key, &bucket) < 0) {
                Py_DECREF(key);
                goto error;
            }
            if (bucket == NULL) {
                bucket = PyList_New(0);
                if (bucket == NULL || PyDict_SetItem(groups, key, bucket) < 0) {
                    Py_XDECREF(bucket);
                    Py_DECREF(key);
                    goto error;
                }
            }
            if (PyList_Append(bucket, parent) < 0) {
                Py_DECREF(bucket);
                Py_DECREF(key);
                goto error;
            }
            Py_DECREF(bucket);
        }
        Py_DECREF(key);
    }
    Py_DECREF(parents);
    return groups;
error:
    Py_XDECREF(groups); Py_XDECREF(parents);
    return NULL;
}

PyObject *
wreath_orm_attach_relationships(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *groups, *children_object, *indices, *models_object;
    PyObject *children = NULL;
    const WreathModelAPI *models;
    Py_ssize_t relation_index;
    int many;
    if (!PyArg_ParseTuple(args, "OOOnpO:orm_attach_relationships", &groups,
                          &children_object, &indices, &relation_index, &many,
                          &models_object)) return NULL;
    if (!PyDict_Check(groups) || !PyTuple_Check(indices)) {
        PyErr_SetString(PyExc_TypeError, "relationship groups and indices are invalid");
        return NULL;
    }
    models = wreath_model_api(models_object);
    if (models == NULL) return NULL;
    children = PySequence_Fast(children_object, "children must be a sequence");
    if (children == NULL) goto error;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(children); index++) {
        PyObject *child = PySequence_Fast_GET_ITEM(children, index);
        PyObject *key = model_key(child, indices, models);
        PyObject *parents = NULL;
        if (key == NULL) goto error;
        if (key != Py_None && PyDict_GetItemRef(groups, key, &parents) < 0) {
            Py_DECREF(key);
            goto error;
        }
        Py_DECREF(key);
        if (parents == NULL) continue;
        for (Py_ssize_t parent_index = 0;
             parent_index < PyList_GET_SIZE(parents); parent_index++) {
            PyObject *parent = PyList_GET_ITEM(parents, parent_index);
            if (many) {
                PyObject *values = models->get_relation(parent, relation_index);
                if (values == NULL) { Py_DECREF(parents); goto error; }
                if (PyList_Append(values, child) < 0) {
                    Py_DECREF(values);
                    Py_DECREF(parents);
                    goto error;
                }
                Py_DECREF(values);
            }
            else if (models->set_relation(parent, relation_index, child) < 0) {
                Py_DECREF(parents);
                goto error;
            }
        }
        Py_DECREF(parents);
    }
    Py_DECREF(children);
    Py_RETURN_NONE;
error:
    Py_XDECREF(children);
    return NULL;
}

typedef struct {
    Py_ssize_t row_index;
    PyObject *decoder;
} HydrateKey;

typedef struct {
    Py_ssize_t row_index;
    Py_ssize_t cell;
    PyObject *decoder;
} HydrateCell;

typedef struct {
    PyObject *spec;
    PyObject *model_type;
    HydrateKey *keys;
    Py_ssize_t key_count;
    HydrateCell *cells;
    Py_ssize_t cell_count;
} HydratePlan;

typedef struct JoinPlan JoinPlan;
struct JoinPlan {
    HydratePlan hydrate;
    Py_ssize_t offset;
    Py_ssize_t relation;
    JoinPlan *nested;
    Py_ssize_t nested_count;
};

typedef struct {
    HydratePlan root;
    JoinPlan *joins;
    Py_ssize_t join_count;
} CompiledHydratePlan;

#define ORM_HYDRATE_PLAN_CAPSULE "wreath.orm.hydrate-plan"

typedef struct {
    PyObject *session;
    PyObject *identity;
    const WreathModelAPI *models;
} HydrateContext;

static void
hydrate_plan_clear(HydratePlan *plan)
{
    for (Py_ssize_t index = 0; index < plan->key_count; index++)
        Py_XDECREF(plan->keys[index].decoder);
    for (Py_ssize_t index = 0; index < plan->cell_count; index++) {
        Py_XDECREF(plan->cells[index].decoder);
    }
    PyMem_Free(plan->keys);
    PyMem_Free(plan->cells);
    Py_XDECREF(plan->model_type);
    Py_XDECREF(plan->spec);
    memset(plan, 0, sizeof(*plan));
}

static int
plan_pair(PyObject *object, Py_ssize_t *number, PyObject **decoder,
          const char *kind)
{
    PyObject *pair = PySequence_Fast(object,
                                    "hydration plan entries must be pairs");
    PyObject *value;
    if (pair == NULL) return -1;
    if (PySequence_Fast_GET_SIZE(pair) != 2) {
        PyErr_Format(PyExc_TypeError,
                     "hydration %s entry must contain (index, decoder)", kind);
        Py_DECREF(pair);
        return -1;
    }
    *number = PyLong_AsSsize_t(PySequence_Fast_GET_ITEM(pair, 0));
    if (*number == -1 && PyErr_Occurred()) {
        Py_DECREF(pair);
        return -1;
    }
    value = PySequence_Fast_GET_ITEM(pair, 1);
    *decoder = value == Py_None ? NULL : Py_NewRef(value);
    Py_DECREF(pair);
    return 0;
}

static int
hydrate_plan_init(HydratePlan *result, PyObject *spec, PyObject *plan)
{
    PyObject *keys_object = NULL, *cells_object = NULL;
    PyObject *keys = NULL, *cells = NULL;
    memset(result, 0, sizeof(*result));
    result->spec = Py_NewRef(spec);
    result->model_type = PyObject_GetAttrString(spec, "model_type");
    keys_object = PyObject_GetAttrString(plan, "key");
    cells_object = PyObject_GetAttrString(plan, "cells");
    if (result->model_type == NULL || keys_object == NULL || cells_object == NULL)
        goto error;
    keys = PySequence_Fast(keys_object, "hydration plan key must be a sequence");
    cells = PySequence_Fast(cells_object, "hydration plan cells must be a sequence");
    if (keys == NULL || cells == NULL) goto error;
    result->key_count = PySequence_Fast_GET_SIZE(keys);
    result->cell_count = PySequence_Fast_GET_SIZE(cells);
    if (result->key_count != 0) {
        result->keys = PyMem_Calloc((size_t)result->key_count,
                                    sizeof(*result->keys));
        if (result->keys == NULL) { PyErr_NoMemory(); goto error; }
    }
    if (result->cell_count != 0) {
        result->cells = PyMem_Calloc((size_t)result->cell_count,
                                     sizeof(*result->cells));
        if (result->cells == NULL) { PyErr_NoMemory(); goto error; }
    }
    for (Py_ssize_t index = 0; index < result->key_count; index++) {
        if (plan_pair(PySequence_Fast_GET_ITEM(keys, index),
                      &result->keys[index].row_index,
                      &result->keys[index].decoder, "key") < 0) goto error;
    }
    for (Py_ssize_t index = 0; index < result->cell_count; index++) {
        Py_ssize_t cell;
        result->cells[index].row_index = index;
        if (plan_pair(PySequence_Fast_GET_ITEM(cells, index), &cell,
                      &result->cells[index].decoder, "cell") < 0) goto error;
        result->cells[index].cell = cell;
    }
    Py_DECREF(cells); Py_DECREF(keys);
    Py_DECREF(cells_object); Py_DECREF(keys_object);
    return 0;
error:
    Py_XDECREF(cells); Py_XDECREF(keys);
    Py_XDECREF(cells_object); Py_XDECREF(keys_object);
    hydrate_plan_clear(result);
    return -1;
}

static void
join_plans_clear(JoinPlan *plans, Py_ssize_t count)
{
    for (Py_ssize_t index = 0; index < count; index++) {
        join_plans_clear(plans[index].nested, plans[index].nested_count);
        hydrate_plan_clear(&plans[index].hydrate);
        PyMem_Free(plans[index].nested);
    }
}

static int
join_plans_init(PyObject *cursors_object, JoinPlan **plans_out,
                Py_ssize_t *count_out)
{
    PyObject *cursors = NULL;
    JoinPlan *plans = NULL;
    Py_ssize_t count = 0;
    cursors = PySequence_Fast(cursors_object, "joined cursors must be a sequence");
    if (cursors == NULL) return -1;
    count = PySequence_Fast_GET_SIZE(cursors);
    if (count != 0) {
        plans = PyMem_Calloc((size_t)count, sizeof(*plans));
        if (plans == NULL) { PyErr_NoMemory(); goto error; }
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *cursor = PySequence_Fast_GET_ITEM(cursors, index);
        PyObject *step = PyObject_GetAttrString(cursor, "step");
        PyObject *row_plan = PyObject_GetAttrString(cursor, "plan");
        PyObject *nested = PyObject_GetAttrString(cursor, "nested");
        PyObject *relationship = NULL, *target = NULL, *offset = NULL;
        if (step == NULL || row_plan == NULL || nested == NULL) goto item_error;
        relationship = PyObject_GetAttrString(step, "relationship");
        offset = PyObject_GetAttrString(step, "offset");
        if (relationship == NULL || offset == NULL) goto item_error;
        target = PyObject_GetAttrString(relationship, "target");
        {
            PyObject *relation = PyObject_GetAttrString(relationship, "index");
            if (target == NULL || relation == NULL) {
                Py_XDECREF(relation);
                goto item_error;
            }
            Py_ssize_t relation_index = PyLong_AsSsize_t(relation);
            if (relation_index == -1 && PyErr_Occurred()) {
                Py_DECREF(relation);
                goto item_error;
            }
            Py_DECREF(relation);
            plans[index].relation = relation_index;
        }
        plans[index].offset = PyLong_AsSsize_t(offset);
        if (plans[index].offset == -1 && PyErr_Occurred()) goto item_error;
        if (hydrate_plan_init(&plans[index].hydrate, target, row_plan) < 0)
            goto item_error;
        if (join_plans_init(nested, &plans[index].nested,
                            &plans[index].nested_count) < 0) goto item_error;
        Py_DECREF(target); Py_DECREF(offset); Py_DECREF(relationship);
        Py_DECREF(nested); Py_DECREF(row_plan); Py_DECREF(step);
        continue;
item_error:
        Py_XDECREF(target); Py_XDECREF(offset); Py_XDECREF(relationship);
        Py_XDECREF(nested); Py_XDECREF(row_plan); Py_XDECREF(step);
        join_plans_clear(plans, count);
        PyMem_Free(plans);
        Py_DECREF(cursors);
        return -1;
    }
    Py_DECREF(cursors);
    *plans_out = plans;
    *count_out = count;
    return 0;
error:
    PyMem_Free(plans);
    Py_DECREF(cursors);
    return -1;
}

static void
compiled_hydrate_plan_clear(CompiledHydratePlan *plan)
{
    if (plan == NULL) return;
    join_plans_clear(plan->joins, plan->join_count);
    PyMem_Free(plan->joins);
    hydrate_plan_clear(&plan->root);
    PyMem_Free(plan);
}

static void
compiled_hydrate_plan_destructor(PyObject *capsule)
{
    CompiledHydratePlan *plan = PyCapsule_GetPointer(
        capsule, ORM_HYDRATE_PLAN_CAPSULE);
    if (plan != NULL) compiled_hydrate_plan_clear(plan);
    else PyErr_Clear();
}

PyObject *
wreath_orm_compile_hydrate_plan(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *spec, *row_plan, *cursors;
    CompiledHydratePlan *plan;
    PyObject *capsule;
    if (!PyArg_ParseTuple(args, "OOO:orm_compile_hydrate_plan", &spec,
                          &row_plan, &cursors)) return NULL;
    plan = PyMem_Calloc(1, sizeof(*plan));
    if (plan == NULL) return PyErr_NoMemory();
    if (hydrate_plan_init(&plan->root, spec, row_plan) < 0 ||
        join_plans_init(cursors, &plan->joins, &plan->join_count) < 0) {
        compiled_hydrate_plan_clear(plan);
        return NULL;
    }
    capsule = PyCapsule_New(plan, ORM_HYDRATE_PLAN_CAPSULE,
                            compiled_hydrate_plan_destructor);
    if (capsule == NULL) {
        compiled_hydrate_plan_clear(plan);
        return NULL;
    }
    return capsule;
}

static PyObject *
hydrate_one(HydrateContext *context, HydratePlan *plan, PyObject *row,
            Py_ssize_t offset)
{
    PyObject *key = NULL, *identity = NULL, *instance = NULL;
    Py_ssize_t row_size = PySequence_Fast_GET_SIZE(row);
    key = PyTuple_New(plan->key_count);
    if (key == NULL) return NULL;
    for (Py_ssize_t index = 0; index < plan->key_count; index++) {
        HydrateKey *part = &plan->keys[index];
        Py_ssize_t position = offset + part->row_index;
        PyObject *value;
        if (position < 0 || position >= row_size) {
            PyErr_SetString(PyExc_IndexError, "hydration key index is outside the row");
            goto error;
        }
        value = PySequence_Fast_GET_ITEM(row, position);
        if (value == Py_None) {
            Py_DECREF(key);
            return Py_NewRef(Py_None);
        }
        value = part->decoder == NULL
            ? Py_NewRef(value) : PyObject_CallOneArg(part->decoder, value);
        if (value == NULL) goto error;
        PyTuple_SET_ITEM(key, index, value);
    }
    identity = PyTuple_Pack(2, plan->spec, key);
    Py_DECREF(key); key = NULL;
    if (identity == NULL) return NULL;
    if (PyDict_GetItemRef(context->identity, identity, &instance) < 0) goto error;
    if (instance == NULL) {
        instance = context->models->alloc((PyTypeObject *)plan->model_type);
        if (instance == NULL) goto error;
        if (context->models->make_persistent(instance, context->session) < 0 ||
            PyDict_SetItem(context->identity, identity, instance) < 0) goto error;
    }
    Py_DECREF(identity); identity = NULL;
    for (Py_ssize_t index = 0; index < plan->cell_count; index++) {
        HydrateCell *cell = &plan->cells[index];
        Py_ssize_t position = offset + cell->row_index;
        PyObject *value;
        int dirty = context->models->is_dirty(instance, cell->cell);
        if (dirty < 0) goto error;
        if (dirty) continue;
        if (position < 0 || position >= row_size) {
            PyErr_SetString(PyExc_IndexError, "hydration cell index is outside the row");
            goto error;
        }
        value = PySequence_Fast_GET_ITEM(row, position);
        value = cell->decoder == NULL
            ? Py_NewRef(value) : PyObject_CallOneArg(cell->decoder, value);
        if (value == NULL) goto error;
        if (context->models->set_loaded(instance, cell->cell, value) < 0) {
            Py_DECREF(value);
            goto error;
        }
        Py_DECREF(value);
    }
    return instance;
error:
    Py_XDECREF(instance); Py_XDECREF(identity); Py_XDECREF(key);
    return NULL;
}

static int
assemble_plans(HydrateContext *context, JoinPlan *plans, Py_ssize_t count,
               PyObject *parent, PyObject *row)
{
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *child = hydrate_one(context, &plans[index].hydrate, row,
                                      plans[index].offset);
        if (child == NULL) return -1;
        if (context->models->set_relation(
                parent, plans[index].relation, child) < 0) {
            Py_DECREF(child);
            return -1;
        }
        if (child != Py_None &&
            assemble_plans(context, plans[index].nested,
                           plans[index].nested_count, child, row) < 0) {
            Py_DECREF(child);
            return -1;
        }
        Py_DECREF(child);
    }
    return 0;
}

static int
hydrate_context_init(HydrateContext *context, PyObject *session, PyObject *models)
{
    memset(context, 0, sizeof(*context));
    context->session = Py_NewRef(session);
    context->identity = PyObject_GetAttrString(session, "_identity");
    context->models = wreath_model_api(models);
    if (context->identity == NULL || !PyDict_Check(context->identity) ||
        context->models == NULL) {
        if (context->identity != NULL && !PyDict_Check(context->identity))
            PyErr_SetString(PyExc_TypeError, "session _identity must be a dict");
        return -1;
    }
    return 0;
}

static void
hydrate_context_clear(HydrateContext *context)
{
    Py_XDECREF(context->identity);
    Py_XDECREF(context->session);
}

PyObject *
wreath_orm_hydrate_records(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *session, *plan_object, *rows_object, *models;
    PyObject *rows = NULL, *objects = NULL;
    PyObject **unique = NULL, **seen = NULL;
    CompiledHydratePlan *plan;
    Py_ssize_t unique_count = 0, seen_capacity = 8;
    HydrateContext context = {0};
    if (!PyArg_ParseTuple(args, "OOOO:orm_hydrate_records", &session,
                          &plan_object, &rows_object, &models)) return NULL;
    plan = PyCapsule_GetPointer(plan_object, ORM_HYDRATE_PLAN_CAPSULE);
    if (plan == NULL) return NULL;
    rows = PySequence_Fast(rows_object, "rows must be a sequence");
    if (rows == NULL) goto error;
    {
        Py_ssize_t row_count = PySequence_Fast_GET_SIZE(rows);
        if (row_count > (Py_ssize_t)(SIZE_MAX / sizeof(*unique))) {
            PyErr_NoMemory();
            goto error;
        }
        while (seen_capacity < row_count) {
            if (seen_capacity > PY_SSIZE_T_MAX / 2) {
                PyErr_NoMemory();
                goto error;
            }
            seen_capacity *= 2;
        }
        unique = row_count == 0 ? NULL : PyMem_Malloc(
            (size_t)row_count * sizeof(*unique));
        seen = PyMem_Calloc((size_t)seen_capacity, sizeof(*seen));
    }
    if ((PySequence_Fast_GET_SIZE(rows) != 0 && unique == NULL) || seen == NULL ||
        hydrate_context_init(&context, session, models) < 0) goto error;
    for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(rows); index++) {
        PyObject *row = PySequence_Fast(
            PySequence_Fast_GET_ITEM(rows, index), "database row must be a sequence");
        PyObject *root;
        uintptr_t hash;
        Py_ssize_t slot;
        int is_new = 0;
        if (row == NULL) goto error;
        root = hydrate_one(&context, &plan->root, row, 0);
        if (root == NULL) { Py_DECREF(row); goto error; }
        if (root == Py_None) { Py_DECREF(root); Py_DECREF(row); continue; }
        hash = (uintptr_t)root >> 4;
        hash ^= hash >> 17;
        hash *= (uintptr_t)UINT64_C(0xff51afd7ed558ccd);
        hash ^= hash >> 17;
        slot = (Py_ssize_t)(hash & (uintptr_t)(seen_capacity - 1));
        while (seen[slot] != NULL && seen[slot] != root)
            slot = (slot + 1) & (seen_capacity - 1);
        if (seen[slot] == NULL) {
            seen[slot] = root;
            unique[unique_count++] = root;
            is_new = 1;
        }
        if (assemble_plans(&context, plan->joins, plan->join_count, root, row) < 0) {
            if (!is_new) Py_DECREF(root);
            Py_DECREF(row);
            goto error;
        }
        if (!is_new) Py_DECREF(root);
        Py_DECREF(row);
    }
    objects = PyList_New(unique_count);
    if (objects == NULL) goto error;
    for (Py_ssize_t index = 0; index < unique_count; index++) {
        PyList_SET_ITEM(objects, index, unique[index]);
        unique[index] = NULL;
    }
    PyMem_Free(seen); PyMem_Free(unique);
    hydrate_context_clear(&context);
    Py_DECREF(rows);
    return objects;
error:
    for (Py_ssize_t index = 0; index < unique_count; index++)
        Py_XDECREF(unique == NULL ? NULL : unique[index]);
    PyMem_Free(seen); PyMem_Free(unique);
    hydrate_context_clear(&context);
    Py_XDECREF(objects); Py_XDECREF(rows);
    return NULL;
}
