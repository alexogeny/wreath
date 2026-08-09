#include "record.h"

#include <stddef.h>

PyTypeObject *WreathPgRecordType = NULL;
PyTypeObject *WreathPgRecordBatchType = NULL;
static Py_ssize_t record_allocations = 0;
static Py_ssize_t record_storage_allocations = 0;

/* Result row with the decoded values stored inline after the header, so a
   row costs a single allocation instead of a Record plus a values tuple. */
typedef struct {
    PyObject_VAR_HEAD
    PyObject *names;
    PyObject *index;
    PyObject *values[1];
} WreathPgRecord;

typedef struct {
    PyObject_HEAD
    PyObject **objects;  /* arbitrary rows, or lazily materialized Records */
    PyObject **cells;    /* batch-owned decoded values, row-major */
    PyObject *names;
    PyObject *index;
    Py_ssize_t size;
    Py_ssize_t capacity;
    Py_ssize_t columns;
} WreathPgRecordBatch;

#define RECORD_BASICSIZE ((Py_ssize_t)offsetof(WreathPgRecord, values))

/* A Fortunes response creates and destroys twelve same-width Records. Their
 * values are request-owned, but the variable-sized GC shells are not. Keep a
 * deliberately small mixed-width cache so decode reuses that storage without
 * retaining any names, indexes, or row values. Free-threaded workers share the
 * extension in one process, hence TLS only for that ABI; the ordinary build's
 * GIL makes a static cache cheaper and sufficient. */
#define RECORD_FREELIST_CAP 64
#ifdef Py_GIL_DISABLED
static _Thread_local WreathPgRecord *record_freelist[RECORD_FREELIST_CAP];
static _Thread_local int record_freelist_len = 0;
#else
static WreathPgRecord *record_freelist[RECORD_FREELIST_CAP];
static int record_freelist_len = 0;
#endif

static WreathPgRecord *
record_allocate(PyTypeObject *type, Py_ssize_t count)
{
    if (type == WreathPgRecordType) {
        for (int i = record_freelist_len - 1; i >= 0; i--) {
            WreathPgRecord *record = record_freelist[i];
            if (Py_SIZE(record) != count) {
                continue;
            }
            record_freelist[i] = record_freelist[--record_freelist_len];
            _Py_NewReference((PyObject *)record);
            if (!PyObject_GC_IsTracked((PyObject *)record)) {
                PyObject_GC_Track(record);
            }
            return record;
        }
    }
    WreathPgRecord *record = (WreathPgRecord *)type->tp_alloc(type, count);
    if (record != NULL) {
        record_storage_allocations++;
    }
    return record;
}

static int
record_traverse(WreathPgRecord *self, visitproc visit, void *arg)
{
    Py_VISIT(self->names);
    Py_VISIT(self->index);
    for (Py_ssize_t i = 0; i < Py_SIZE(self); i++) Py_VISIT(self->values[i]);
    return 0;
}

static int
record_clear(WreathPgRecord *self)
{
    Py_CLEAR(self->names);
    Py_CLEAR(self->index);
    for (Py_ssize_t i = 0; i < Py_SIZE(self); i++) Py_CLEAR(self->values[i]);
    return 0;
}

static void
record_dealloc(WreathPgRecord *self)
{
    PyObject_GC_UnTrack(self);
    record_clear(self);
    if (Py_TYPE(self) == WreathPgRecordType) {
        if (record_freelist_len == RECORD_FREELIST_CAP) {
            /* A large result of one width must not permanently starve every
             * later projection. Retain the newest shell and evict one empty
             * predecessor; the cache remains strictly bounded. */
            WreathPgRecord *victim =
                record_freelist[--record_freelist_len];
            PyObject_GC_Del(victim);
        }
        record_freelist[record_freelist_len++] = self;
    } else {
        Py_TYPE(self)->tp_free((PyObject *)self);
    }
}

static PyObject *
record_values_tuple(WreathPgRecord *self)
{
    Py_ssize_t count = Py_SIZE(self);
    PyObject *values = PyTuple_New(count);
    if (values == NULL) return NULL;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *value = self->values[i];
        if (value == NULL) {
            Py_DECREF(values);
            PyErr_SetString(PyExc_SystemError, "Record value is unset");
            return NULL;
        }
        PyTuple_SET_ITEM(values, i, Py_NewRef(value));
    }
    return values;
}

static PyObject *
record_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    PyObject *names;
    PyObject *values;
    WreathPgRecord *self;
    PyObject *index;
    Py_ssize_t count;
    static char *keywords[] = {"names", "values", NULL};

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "OO:Record", keywords,
                                     &names, &values)) {
        return NULL;
    }
    if (!PyTuple_Check(names) || !PyTuple_Check(values) ||
        PyTuple_GET_SIZE(names) != PyTuple_GET_SIZE(values)) {
        PyErr_SetString(PyExc_TypeError, "Record requires equal-length name and value tuples");
        return NULL;
    }
    index = PyDict_New();
    if (index == NULL) return NULL;
    count = PyTuple_GET_SIZE(names);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *position = PyLong_FromSsize_t(i);
        if (position == NULL ||
            PyDict_SetItem(index, PyTuple_GET_ITEM(names, i), position) < 0) {
            Py_XDECREF(position);
            Py_DECREF(index);
            return NULL;
        }
        Py_DECREF(position);
    }
    self = record_allocate(type, count);
    if (self == NULL) {
        Py_DECREF(index);
        return NULL;
    }
    self->names = Py_NewRef(names);
    self->index = index;
    for (Py_ssize_t i = 0; i < count; i++) {
        self->values[i] = Py_NewRef(PyTuple_GET_ITEM(values, i));
    }
    record_allocations++;
    return (PyObject *)self;
}

PyObject *
wreath_pg_record_alloc(PyObject *names, PyObject *index, Py_ssize_t count)
{
    WreathPgRecord *self;
    self = record_allocate(WreathPgRecordType, count);
    if (self == NULL) return NULL;
    self->names = Py_NewRef(names);
    self->index = Py_NewRef(index);
    record_allocations++;
    return (PyObject *)self;
}

void
wreath_pg_record_set_value(PyObject *record, Py_ssize_t position, PyObject *value)
{
    ((WreathPgRecord *)record)->values[position] = value;
}

PyObject *
wreath_pg_record_create(PyObject *names, PyObject *index, PyObject *values)
{
    WreathPgRecord *self;
    Py_ssize_t count;
    if (!PyTuple_Check(names) || !PyDict_Check(index) || !PyTuple_Check(values)) {
        PyErr_SetString(PyExc_TypeError, "invalid native Record storage");
        return NULL;
    }
    count = PyTuple_GET_SIZE(values);
    self = (WreathPgRecord *)wreath_pg_record_alloc(names, index, count);
    if (self == NULL) return NULL;
    for (Py_ssize_t i = 0; i < count; i++) {
        self->values[i] = Py_NewRef(PyTuple_GET_ITEM(values, i));
    }
    return (PyObject *)self;
}

static Py_ssize_t
record_length(WreathPgRecord *self)
{
    return Py_SIZE(self);
}

static PyObject *
record_item(WreathPgRecord *self, Py_ssize_t index)
{
    if (index < 0 || index >= Py_SIZE(self)) {
        PyErr_SetString(PyExc_IndexError, "Record index out of range");
        return NULL;
    }
    return Py_NewRef(self->values[index]);
}

static PyObject *
record_subscript(WreathPgRecord *self, PyObject *key)
{
    if (PyUnicode_Check(key)) {
        PyObject *position = PyDict_GetItemWithError(self->index, key);
        Py_ssize_t index;
        if (position == NULL) {
            if (!PyErr_Occurred()) PyErr_SetObject(PyExc_KeyError, key);
            return NULL;
        }
        index = PyLong_AsSsize_t(position);
        if (index == -1 && PyErr_Occurred()) return NULL;
        if (index < 0 || index >= Py_SIZE(self)) {
            PyErr_SetObject(PyExc_KeyError, key);
            return NULL;
        }
        return Py_NewRef(self->values[index]);
    }
    if (PyIndex_Check(key)) {
        Py_ssize_t index = PyNumber_AsSsize_t(key, PyExc_IndexError);
        if (index == -1 && PyErr_Occurred()) return NULL;
        if (index < 0) index += Py_SIZE(self);
        return record_item(self, index);
    }
    {
        PyObject *values = record_values_tuple(self);
        PyObject *result;
        if (values == NULL) return NULL;
        result = PyObject_GetItem(values, key);
        Py_DECREF(values);
        return result;
    }
}

static int
record_get_borrowed(PyObject *object, PyObject *key,
                    PyObject **cached_names, Py_ssize_t *cached_position,
                    PyObject **value)
{
    if (Py_TYPE(object) != WreathPgRecordType) {
        return 0;
    }
    WreathPgRecord *self = (WreathPgRecord *)object;
    Py_ssize_t position = *cached_position;
    if (*cached_names != self->names || position < 0 ||
        position >= Py_SIZE(self)) {
        PyObject *position_object = PyDict_GetItemWithError(self->index, key);
        if (position_object == NULL) {
            return PyErr_Occurred() ? -1 : 2;
        }
        position = PyLong_AsSsize_t(position_object);
        if (position == -1 && PyErr_Occurred()) {
            return -1;
        }
        if (position < 0 || position >= Py_SIZE(self)) {
            PyErr_SetObject(PyExc_KeyError, key);
            return -1;
        }
        Py_XSETREF(*cached_names, Py_NewRef(self->names));
        *cached_position = position;
    }
    *value = self->values[position];
    return 1;
}

static int
batch_reserve(WreathPgRecordBatch *self, Py_ssize_t needed)
{
    if (needed <= self->capacity) return 0;
    Py_ssize_t capacity = self->capacity > 0 ? self->capacity : 16;
    while (capacity < needed) {
        if (capacity > PY_SSIZE_T_MAX / 2) {
            PyErr_NoMemory();
            return -1;
        }
        capacity *= 2;
    }
    PyObject **objects = PyMem_Realloc(
        self->objects, (size_t)capacity * sizeof(PyObject *));
    if (objects == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    memset(objects + self->capacity, 0,
           (size_t)(capacity - self->capacity) * sizeof(PyObject *));
    self->objects = objects;
    if (self->columns > 0) {
        PyObject **cells = PyMem_Realloc(
            self->cells,
            (size_t)capacity * (size_t)self->columns * sizeof(PyObject *));
        if (cells == NULL) {
            PyErr_NoMemory();
            return -1;
        }
        memset(cells + self->capacity * self->columns, 0,
               (size_t)(capacity - self->capacity) *
                   (size_t)self->columns * sizeof(PyObject *));
        self->cells = cells;
    }
    self->capacity = capacity;
    return 0;
}

int
wreath_pg_record_batch_append(PyObject *batch, PyObject *value)
{
    WreathPgRecordBatch *self = (WreathPgRecordBatch *)batch;
    if (Py_TYPE(batch) != WreathPgRecordBatchType) {
        PyErr_SetString(PyExc_TypeError, "destination is not an exact RecordBatch");
        return -1;
    }
    if (batch_reserve(self, self->size + 1) < 0) return -1;
    self->objects[self->size++] = Py_NewRef(value);
    return 0;
}

int
wreath_pg_record_batch_check(PyObject *batch)
{
    return Py_TYPE(batch) == WreathPgRecordBatchType;
}

Py_ssize_t
wreath_pg_record_batch_size(PyObject *batch)
{
    return ((WreathPgRecordBatch *)batch)->size;
}

void
wreath_pg_record_batch_truncate(PyObject *batch, Py_ssize_t size)
{
    WreathPgRecordBatch *self = (WreathPgRecordBatch *)batch;
    while (self->size > size) {
        self->size--;
        Py_CLEAR(self->objects[self->size]);
        for (Py_ssize_t column = 0; column < self->columns; column++) {
            Py_CLEAR(self->cells[self->size * self->columns + column]);
        }
    }
}

int
wreath_pg_record_batch_prepare(PyObject *batch, PyObject *names,
                               PyObject *index, Py_ssize_t columns,
                               Py_ssize_t rows, Py_ssize_t *start)
{
    WreathPgRecordBatch *self = (WreathPgRecordBatch *)batch;
    if (!wreath_pg_record_batch_check(batch) || columns < 0 || rows < 0) {
        PyErr_SetString(PyExc_ValueError, "invalid RecordBatch decode shape");
        return -1;
    }
    if (self->columns == 0 && self->size == 0) {
        Py_XSETREF(self->names, Py_NewRef(names));
        Py_XSETREF(self->index, Py_NewRef(index));
        self->columns = columns;
    } else if (self->names != names || self->index != index ||
               self->columns != columns) {
        PyErr_SetString(PyExc_ValueError, "RecordBatch result shape changed");
        return -1;
    }
    if (batch_reserve(self, self->size + rows) < 0) return -1;
    *start = self->size;
    return 0;
}

void
wreath_pg_record_batch_set_value(PyObject *batch, Py_ssize_t row,
                                 Py_ssize_t column, PyObject *value)
{
    WreathPgRecordBatch *self = (WreathPgRecordBatch *)batch;
    self->cells[row * self->columns + column] = value;
}

void
wreath_pg_record_batch_commit(PyObject *batch, Py_ssize_t size)
{
    ((WreathPgRecordBatch *)batch)->size = size;
}

static int
batch_traverse(WreathPgRecordBatch *self, visitproc visit, void *arg)
{
    Py_VISIT(self->names);
    Py_VISIT(self->index);
    for (Py_ssize_t i = 0; i < self->size; i++) {
        Py_VISIT(self->objects[i]);
        for (Py_ssize_t column = 0; column < self->columns; column++) {
            Py_VISIT(self->cells[i * self->columns + column]);
        }
    }
    return 0;
}

static int
batch_clear(WreathPgRecordBatch *self)
{
    wreath_pg_record_batch_truncate((PyObject *)self, 0);
    Py_CLEAR(self->names);
    Py_CLEAR(self->index);
    PyMem_Free(self->objects);
    PyMem_Free(self->cells);
    self->objects = NULL;
    self->cells = NULL;
    self->columns = 0;
    self->capacity = 0;
    return 0;
}

static void
batch_dealloc(WreathPgRecordBatch *self)
{
    PyObject_GC_UnTrack(self);
    batch_clear(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
batch_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    PyObject *iterable = NULL;
    static char *keywords[] = {"iterable", NULL};
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "|O:RecordBatch", keywords, &iterable)) return NULL;
    WreathPgRecordBatch *self =
        (WreathPgRecordBatch *)type->tp_alloc(type, 0);
    if (self == NULL) return NULL;
    self->objects = NULL;
    self->cells = NULL;
    self->names = NULL;
    self->index = NULL;
    self->size = 0;
    self->capacity = 0;
    self->columns = 0;
    if (iterable != NULL) {
        PyObject *iterator = PyObject_GetIter(iterable);
        if (iterator == NULL) {
            Py_DECREF(self);
            return NULL;
        }
        PyObject *item;
        while ((item = PyIter_Next(iterator)) != NULL) {
            int result = wreath_pg_record_batch_append((PyObject *)self, item);
            Py_DECREF(item);
            if (result < 0) {
                Py_DECREF(iterator);
                Py_DECREF(self);
                return NULL;
            }
        }
        Py_DECREF(iterator);
        if (PyErr_Occurred()) {
            Py_DECREF(self);
            return NULL;
        }
    }
    return (PyObject *)self;
}

PyObject *
wreath_pg_record_batch_new(void)
{
    WreathPgRecordBatch *self =
        (WreathPgRecordBatch *)WreathPgRecordBatchType->tp_alloc(
            WreathPgRecordBatchType, 0);
    if (self == NULL) return NULL;
    self->objects = NULL;
    self->cells = NULL;
    self->names = NULL;
    self->index = NULL;
    self->size = 0;
    self->capacity = 0;
    self->columns = 0;
    return (PyObject *)self;
}

static Py_ssize_t
batch_length(WreathPgRecordBatch *self)
{
    return self->size;
}

static PyObject *
batch_item(WreathPgRecordBatch *self, Py_ssize_t position)
{
    if (position < 0 || position >= self->size) {
        PyErr_SetString(PyExc_IndexError, "RecordBatch index out of range");
        return NULL;
    }
    if (self->objects[position] != NULL) {
        return Py_NewRef(self->objects[position]);
    }
    if (self->columns <= 0 || self->names == NULL || self->index == NULL) {
        PyErr_SetString(PyExc_SystemError, "RecordBatch row is unset");
        return NULL;
    }
    PyObject *record = wreath_pg_record_alloc(
        self->names, self->index, self->columns);
    if (record == NULL) return NULL;
    for (Py_ssize_t column = 0; column < self->columns; column++) {
        PyObject *value = self->cells[position * self->columns + column];
        if (value == NULL) {
            Py_DECREF(record);
            PyErr_SetString(PyExc_SystemError, "RecordBatch cell is unset");
            return NULL;
        }
        wreath_pg_record_set_value(record, column, Py_NewRef(value));
    }
    self->objects[position] = record;
    return Py_NewRef(record);
}

static PyObject *
batch_append_method(WreathPgRecordBatch *self, PyObject *value)
{
    if (wreath_pg_record_batch_append((PyObject *)self, value) < 0) return NULL;
    Py_RETURN_NONE;
}

typedef struct {
    Py_ssize_t row;
    PyObject *key;   /* owned */
} BatchSortEntry;

static PyObject *
batch_sort_key(WreathPgRecordBatch *self, Py_ssize_t row, PyObject *column)
{
    PyObject *item = self->objects[row];
    if (item == NULL) {
        PyObject *position_object = PyDict_GetItemWithError(self->index, column);
        if (position_object == NULL) {
            if (!PyErr_Occurred()) PyErr_SetObject(PyExc_KeyError, column);
            return NULL;
        }
        Py_ssize_t position = PyLong_AsSsize_t(position_object);
        if (position == -1 && PyErr_Occurred()) return NULL;
        if (position < 0 || position >= self->columns) {
            PyErr_SetObject(PyExc_KeyError, column);
            return NULL;
        }
        return Py_NewRef(self->cells[row * self->columns + position]);
    }
    if (Py_TYPE(item) == WreathPgRecordType) {
        WreathPgRecord *record = (WreathPgRecord *)item;
        PyObject *position_object = PyDict_GetItemWithError(record->index, column);
        if (position_object == NULL) {
            if (!PyErr_Occurred()) PyErr_SetObject(PyExc_KeyError, column);
            return NULL;
        }
        Py_ssize_t position = PyLong_AsSsize_t(position_object);
        if (position == -1 && PyErr_Occurred()) return NULL;
        if (position < 0 || position >= Py_SIZE(record)) {
            PyErr_SetObject(PyExc_KeyError, column);
            return NULL;
        }
        return Py_NewRef(record->values[position]);
    }
    return PyObject_GetItem(item, column);
}

static PyObject *
batch_sort_by(WreathPgRecordBatch *self, PyObject *column)
{
    Py_ssize_t count = self->size;
    if (count < 2) Py_RETURN_NONE;
    BatchSortEntry *entries = PyMem_Calloc((size_t)count, sizeof(BatchSortEntry));
    BatchSortEntry *scratch = PyMem_Malloc((size_t)count * sizeof(BatchSortEntry));
    PyObject **owned_keys = PyMem_Calloc((size_t)count, sizeof(PyObject *));
    if (entries == NULL || scratch == NULL || owned_keys == NULL) {
        PyMem_Free(entries);
        PyMem_Free(scratch);
        PyMem_Free(owned_keys);
        return PyErr_NoMemory();
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        entries[i].row = i;
        entries[i].key = batch_sort_key(self, i, column);
        if (entries[i].key == NULL) goto error;
        owned_keys[i] = entries[i].key;
    }
    BatchSortEntry *source = entries;
    BatchSortEntry *dest = scratch;
    for (Py_ssize_t width = 1; width < count;
         width = width > count / 2 ? count : width * 2) {
        for (Py_ssize_t begin = 0; begin < count; begin += width * 2) {
            Py_ssize_t left = begin;
            Py_ssize_t middle = begin + width < count ? begin + width : count;
            Py_ssize_t right = middle;
            Py_ssize_t end = middle + width < count ? middle + width : count;
            Py_ssize_t output = begin;
            while (left < middle && right < end) {
                int take_right = PyObject_RichCompareBool(
                    source[right].key, source[left].key, Py_LT);
                if (take_right < 0) goto error;
                dest[output++] = source[take_right ? right++ : left++];
            }
            while (left < middle) dest[output++] = source[left++];
            while (right < end) dest[output++] = source[right++];
        }
        BatchSortEntry *swap = source;
        source = dest;
        dest = swap;
    }
    PyObject **sorted_objects = PyMem_Malloc(
        (size_t)count * sizeof(PyObject *));
    PyObject **sorted_cells = self->columns > 0 ? PyMem_Malloc(
        (size_t)count * (size_t)self->columns * sizeof(PyObject *)) : NULL;
    if (sorted_objects == NULL || (self->columns > 0 && sorted_cells == NULL)) {
        PyMem_Free(sorted_objects);
        PyMem_Free(sorted_cells);
        PyErr_NoMemory();
        goto error;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        Py_ssize_t row = source[i].row;
        sorted_objects[i] = self->objects[row];
        if (self->columns > 0) {
            memcpy(sorted_cells + i * self->columns,
                   self->cells + row * self->columns,
                   (size_t)self->columns * sizeof(PyObject *));
        }
    }
    memcpy(self->objects, sorted_objects, (size_t)count * sizeof(PyObject *));
    if (self->columns > 0) {
        memcpy(self->cells, sorted_cells,
               (size_t)count * (size_t)self->columns * sizeof(PyObject *));
    }
    PyMem_Free(sorted_objects);
    PyMem_Free(sorted_cells);
    for (Py_ssize_t i = 0; i < count; i++) Py_DECREF(owned_keys[i]);
    PyMem_Free(entries);
    PyMem_Free(scratch);
    PyMem_Free(owned_keys);
    Py_RETURN_NONE;

error:
    for (Py_ssize_t i = 0; i < count; i++) Py_XDECREF(owned_keys[i]);
    PyMem_Free(entries);
    PyMem_Free(scratch);
    PyMem_Free(owned_keys);
    return NULL;
}

static PyMethodDef batch_methods[] = {
    {"append", (PyCFunction)batch_append_method, METH_O, NULL},
    {"sort_by", (PyCFunction)batch_sort_by, METH_O, NULL},
    {NULL, NULL, 0, NULL}
};

static PyType_Slot batch_slots[] = {
    {Py_tp_new, batch_new},
    {Py_tp_dealloc, batch_dealloc},
    {Py_tp_traverse, batch_traverse},
    {Py_tp_clear, batch_clear},
    {Py_tp_methods, batch_methods},
    {Py_sq_length, batch_length},
    {Py_sq_item, batch_item},
    {0, NULL},
};

static PyType_Spec batch_spec = {
    .name = "wreath._native._postgres.RecordBatch",
    .basicsize = sizeof(WreathPgRecordBatch),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = batch_slots,
};

static int
record_capi_batch_check(PyObject *batch)
{
    return Py_TYPE(batch) == WreathPgRecordBatchType;
}

static Py_ssize_t
record_capi_batch_size(PyObject *batch)
{
    return ((WreathPgRecordBatch *)batch)->size;
}

static PyObject *
record_capi_batch_get_borrowed(PyObject *batch, Py_ssize_t position)
{
    WreathPgRecordBatch *self = (WreathPgRecordBatch *)batch;
    if (self->objects[position] == NULL) {
        PyObject *record = batch_item(self, position);
        if (record == NULL) return NULL;
        Py_DECREF(record);
    }
    return self->objects[position];
}

static int
record_capi_batch_get_value(PyObject *batch, Py_ssize_t row, PyObject *key,
                            PyObject **cached_names,
                            Py_ssize_t *cached_position, PyObject **value)
{
    WreathPgRecordBatch *self = (WreathPgRecordBatch *)batch;
    if (self->objects[row] != NULL) {
        return record_get_borrowed(
            self->objects[row], key, cached_names, cached_position, value);
    }
    Py_ssize_t position = *cached_position;
    if (*cached_names != self->names || position < 0 ||
        position >= self->columns) {
        PyObject *position_object = PyDict_GetItemWithError(self->index, key);
        if (position_object == NULL) return PyErr_Occurred() ? -1 : 2;
        position = PyLong_AsSsize_t(position_object);
        if (position == -1 && PyErr_Occurred()) return -1;
        if (position < 0 || position >= self->columns) return 2;
        Py_XSETREF(*cached_names, Py_NewRef(self->names));
        *cached_position = position;
    }
    *value = self->cells[row * self->columns + position];
    return 1;
}

static WreathRecordCAPI record_capi = {
    .version = WREATH_RECORD_CAPI_VERSION,
    .get_borrowed = record_get_borrowed,
    .batch_check = record_capi_batch_check,
    .batch_size = record_capi_batch_size,
    .batch_get_borrowed = record_capi_batch_get_borrowed,
    .batch_get_value = record_capi_batch_get_value,
};

static PyObject *
record_repr(WreathPgRecord *self)
{
    PyObject *values = record_values_tuple(self);
    PyObject *result;
    if (values == NULL) return NULL;
    result = PyUnicode_FromFormat("Record(%R)", values);
    Py_DECREF(values);
    return result;
}

static PyType_Slot record_slots[] = {
    {Py_tp_new, record_new},
    {Py_tp_dealloc, record_dealloc},
    {Py_tp_traverse, record_traverse},
    {Py_tp_clear, record_clear},
    {Py_tp_repr, record_repr},
    {Py_sq_length, record_length},
    {Py_sq_item, record_item},
    {Py_mp_length, record_length},
    {Py_mp_subscript, record_subscript},
    {0, NULL},
};

static PyType_Spec record_spec = {
    .name = "wreath._native._postgres.Record",
    .basicsize = (int)RECORD_BASICSIZE,
    .itemsize = (int)sizeof(PyObject *),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = record_slots,
};

static PyObject *
record_allocation_count(PyObject *module, PyObject *unused)
{
    (void)module;
    (void)unused;
    return PyLong_FromSsize_t(record_allocations);
}

static PyObject *
record_storage_allocation_count(PyObject *module, PyObject *unused)
{
    (void)module;
    (void)unused;
    return PyLong_FromSsize_t(record_storage_allocations);
}

static PyMethodDef record_methods[] = {
    {"_record_allocation_count", record_allocation_count, METH_NOARGS, NULL},
    {"_record_storage_allocation_count", record_storage_allocation_count,
     METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL}
};

int
wreath_pg_record_init(PyObject *module)
{
    PyObject *capsule;
    WreathPgRecordType = (PyTypeObject *)PyType_FromSpec(&record_spec);
    if (WreathPgRecordType == NULL) return -1;
    WreathPgRecordBatchType = (PyTypeObject *)PyType_FromSpec(&batch_spec);
    if (WreathPgRecordBatchType == NULL) return -1;
    if (PyModule_AddObjectRef(module, "Record", (PyObject *)WreathPgRecordType) < 0 ||
        PyModule_AddObjectRef(module, "RecordBatch", (PyObject *)WreathPgRecordBatchType) < 0 ||
        PyModule_AddFunctions(module, record_methods) < 0) return -1;
    capsule = PyCapsule_New(&record_capi, WREATH_RECORD_CAPI_NAME, NULL);
    if (capsule == NULL ||
        PyModule_AddObject(module, "_RECORD_C_API", capsule) < 0) {
        Py_XDECREF(capsule);
        return -1;
    }
    Py_DECREF(WreathPgRecordType);
    Py_DECREF(WreathPgRecordBatchType);
    return 0;
}

void
wreath_pg_record_fini(void)
{
    while (record_freelist_len > 0) {
        WreathPgRecord *record = record_freelist[--record_freelist_len];
        PyObject_GC_Del(record);
    }
}
