#include "record.h"

#include <stddef.h>

PyTypeObject *WreathPgRecordType = NULL;
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
    WreathPgRecordType = (PyTypeObject *)PyType_FromSpec(&record_spec);
    if (WreathPgRecordType == NULL) return -1;
    if (PyModule_AddObjectRef(module, "Record", (PyObject *)WreathPgRecordType) < 0 ||
        PyModule_AddFunctions(module, record_methods) < 0) return -1;
    Py_DECREF(WreathPgRecordType);
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
