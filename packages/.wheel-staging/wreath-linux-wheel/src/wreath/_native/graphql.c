/* Bulk GraphQL result projection.  Python schedules policies and custom
 * resolvers; C owns repeated row projection and child-layout assembly. */
#include "wreathcore.h"

typedef struct {
    PyObject *key;
    PyObject **values;
} GraphqlResultField;

typedef struct {
    Py_ssize_t rows;
    Py_ssize_t count;
    Py_ssize_t capacity;
    GraphqlResultField *fields;
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

/* Takes ownership of every value in `values`. Equal response keys overwrite
 * the earlier column, matching dict assignment without materializing a dict
 * for each row while asynchronous fields are still resolving. */
static int
graphql_results_store(GraphqlResults *results, PyObject *key, PyObject **values)
{
    for (Py_ssize_t field = 0; field < results->count; field++) {
        int equal = PyObject_RichCompareBool(
            results->fields[field].key, key, Py_EQ);
        if (equal < 0) return -1;
        if (equal) {
            graphql_result_values_clear(
                results->fields[field].values, results->rows);
            results->fields[field].values = values;
            return 0;
        }
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

static int
graphql_plain_fields_compile(PyObject *schema_fields, PyObject *fields,
                             GraphqlPlainField **out, Py_ssize_t *count)
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
        PyObject *name = PyObject_GetAttrString(field, "name");
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
        resolver = PyObject_GetAttrString(schema_field, "resolver");
        relationship = PyObject_GetAttrString(schema_field, "relationship");
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
        column = PyObject_GetAttrString(schema_field, "column");
        if (column == NULL) {
            Py_DECREF(schema_field);
            goto error;
        }
        if (column != Py_None) {
            plan[index].attribute = PyObject_GetAttrString(column, "python_name");
        }
        else {
            plan[index].attribute = PyObject_GetAttrString(schema_field, "attribute");
        }
        Py_DECREF(column);
        Py_DECREF(schema_field);
        if (plan[index].attribute == NULL) goto error;
        if (plan[index].attribute == Py_None) goto declined;
        plan[index].key = PyObject_GetAttrString(field, "key");
        if (plan[index].key == NULL) goto error;
        plan[index].key_json = wreath_json_dumps(NULL, plan[index].key);
        if (plan[index].key_json == NULL) goto error;
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
        schema_fields, fields, &projection->fields, &projection->field_count);
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
    Py_ssize_t field_count;
    if (!PyArg_ParseTuple(args, "OOO:graphql_project_plain", &instances_object,
                          &schema_fields, &fields_object)) return NULL;
    if (!PyDict_Check(schema_fields)) {
        PyErr_SetString(PyExc_TypeError, "GraphQL schema fields must be a dict");
        return NULL;
    }
    instances = PySequence_Fast(instances_object, "instances must be a sequence");
    fields = PySequence_Fast(fields_object, "fields must be a sequence");
    if (instances == NULL || fields == NULL) goto error;
    field_count = PySequence_Fast_GET_SIZE(fields);
    plan = PyMem_Calloc((size_t)(field_count > 0 ? field_count : 1), sizeof(*plan));
    if (plan == NULL) {
        PyErr_NoMemory();
        goto error;
    }
    for (Py_ssize_t index = 0; index < field_count; index++) {
        PyObject *field = PySequence_Fast_GET_ITEM(fields, index);
        PyObject *name = PyObject_GetAttrString(field, "name");
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
        resolver = PyObject_GetAttrString(schema_field, "resolver");
        relationship = PyObject_GetAttrString(schema_field, "relationship");
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
        column = PyObject_GetAttrString(schema_field, "column");
        if (column == NULL) {
            Py_DECREF(schema_field);
            goto error;
        }
        if (column != Py_None) {
            plan[index].attribute = PyObject_GetAttrString(column, "python_name");
        }
        else {
            plan[index].attribute = PyObject_GetAttrString(schema_field, "attribute");
        }
        Py_DECREF(column);
        Py_DECREF(schema_field);
        if (plan[index].attribute == NULL) goto error;
        if (plan[index].attribute == Py_None) goto declined;
        plan[index].key = PyObject_GetAttrString(field, "key");
        if (plan[index].key == NULL) goto error;
    }
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
    answer = PyTuple_Pack(2, flat, layout);
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
    name = PyUnicode_InternFromString("_orm_get_relation");
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
