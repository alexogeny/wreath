#include "header_block.h"

#include <string.h>

typedef struct {
    Py_ssize_t name_offset;
    Py_ssize_t name_size;
    Py_ssize_t value_offset;
    Py_ssize_t value_size;
} WreathHeaderSpan;

typedef struct {
    PyObject_HEAD
    PyObject *raw;
    PyObject *materialized;
    WreathHeaderSpan *spans;
    PyObject **names;
    PyObject **values;
    Py_ssize_t count;
    Py_ssize_t capacity;
    unsigned char object_mode;
} WreathHeaderBlock;

static PyTypeObject WreathHeaderBlockType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._HeaderBlock",
    .tp_basicsize = sizeof(WreathHeaderBlock),
    .tp_flags = Py_TPFLAGS_DEFAULT,
};
static int header_block_ready = 0;
static Py_ssize_t header_block_allocations = 0;

#define HEADER_BLOCK_FREELIST_CAP 64
#ifdef Py_GIL_DISABLED
static _Thread_local WreathHeaderBlock *
    header_block_freelist[HEADER_BLOCK_FREELIST_CAP];
static _Thread_local int header_block_freelist_len = 0;
static _Thread_local int header_block_freelist_enabled = 0;
#else
static WreathHeaderBlock *header_block_freelist[HEADER_BLOCK_FREELIST_CAP];
static int header_block_freelist_len = 0;
static int header_block_freelist_enabled = 0;
#endif

static void
header_block_free_storage(WreathHeaderBlock *self)
{
    PyMem_Free(self->spans);
    PyMem_Free(self->names);
    PyMem_Free(self->values);
    PyObject_Free(self);
}

static void
header_block_dealloc(WreathHeaderBlock *self)
{
    for (Py_ssize_t i = 0; i < self->count; i++) {
        Py_XDECREF(self->names[i]);
        Py_XDECREF(self->values[i]);
        self->names[i] = NULL;
        self->values[i] = NULL;
    }
    Py_XDECREF(self->raw);
    Py_XDECREF(self->materialized);
    self->raw = NULL;
    self->materialized = NULL;
    self->count = 0;
    if (header_block_freelist_enabled && self->names != NULL &&
        self->values != NULL && (self->object_mode || self->spans != NULL) &&
        header_block_freelist_len < HEADER_BLOCK_FREELIST_CAP) {
        header_block_freelist[header_block_freelist_len++] = self;
    } else {
        header_block_free_storage(self);
    }
}

void
wreath_header_block_freelist_enable(void)
{
    header_block_freelist_enabled = 1;
}

void
wreath_header_block_freelist_fini(void)
{
    header_block_freelist_enabled = 0;
    while (header_block_freelist_len > 0) {
        header_block_free_storage(
            header_block_freelist[--header_block_freelist_len]);
    }
}

Py_ssize_t
wreath_header_block_storage_allocations(void)
{
    return header_block_allocations;
}

static int
ensure_type(void)
{
    if (header_block_ready) return 0;
    WreathHeaderBlockType.tp_dealloc = (destructor)header_block_dealloc;
    if (PyType_Ready(&WreathHeaderBlockType) < 0) return -1;
    header_block_ready = 1;
    return 0;
}

static WreathHeaderBlock *
new_block(Py_ssize_t capacity, int object_mode)
{
    if (ensure_type() < 0) return NULL;
    if (capacity < 1) capacity = 1;
    WreathHeaderBlock *self = NULL;
    if (header_block_freelist_enabled) {
        for (int i = header_block_freelist_len - 1; i >= 0; i--) {
            WreathHeaderBlock *candidate = header_block_freelist[i];
            if (candidate->object_mode != (unsigned char)object_mode ||
                candidate->capacity < capacity) continue;
            header_block_freelist[i] =
                header_block_freelist[--header_block_freelist_len];
            self = candidate;
            _Py_NewReference((PyObject *)self);
            break;
        }
    }
    if (self != NULL) return self;
    self = PyObject_New(WreathHeaderBlock, &WreathHeaderBlockType);
    if (self == NULL) return NULL;
    header_block_allocations++;
    self->raw = NULL;
    self->materialized = NULL;
    self->spans = NULL;
    self->names = NULL;
    self->values = NULL;
    self->count = 0;
    self->capacity = capacity;
    self->object_mode = (unsigned char)object_mode;
    self->names = PyMem_Calloc((size_t)capacity, sizeof(PyObject *));
    self->values = PyMem_Calloc((size_t)capacity, sizeof(PyObject *));
    if (self->names == NULL || self->values == NULL) goto memory_error;
    if (!object_mode) {
        self->spans = PyMem_Malloc((size_t)capacity * sizeof(WreathHeaderSpan));
        if (self->spans == NULL) goto memory_error;
    }
    return self;

memory_error:
    PyErr_NoMemory();
    Py_DECREF(self);
    return NULL;
}

PyObject *
wreath_header_block_new_raw(const uint8_t *data, Py_ssize_t size)
{
    WreathHeaderBlock *self = new_block(16, 0);
    if (self == NULL) return NULL;
    self->raw = PyBytes_FromStringAndSize((const char *)data, size);
    if (self->raw == NULL) {
        Py_DECREF(self);
        return NULL;
    }
    return (PyObject *)self;
}

PyObject *
wreath_header_block_new_objects(Py_ssize_t capacity)
{
    return (PyObject *)new_block(capacity, 1);
}

char *
wreath_header_block_raw_data(PyObject *block)
{
    WreathHeaderBlock *self = (WreathHeaderBlock *)block;
    if (!wreath_headers_is_block(block) || self->object_mode || self->raw == NULL) {
        PyErr_SetString(PyExc_TypeError, "expected a raw header block");
        return NULL;
    }
    return PyBytes_AS_STRING(self->raw);
}

static int
grow(WreathHeaderBlock *self)
{
    Py_ssize_t next = self->capacity <= PY_SSIZE_T_MAX / 2
        ? self->capacity * 2 : PY_SSIZE_T_MAX;
    if (next <= self->capacity) {
        PyErr_NoMemory();
        return -1;
    }
    PyObject **names = PyMem_Realloc(
        self->names, (size_t)next * sizeof(PyObject *));
    if (names == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->names = names;
    PyObject **values = PyMem_Realloc(
        self->values, (size_t)next * sizeof(PyObject *));
    if (values == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->values = values;
    memset(self->names + self->capacity, 0,
           (size_t)(next - self->capacity) * sizeof(PyObject *));
    memset(self->values + self->capacity, 0,
           (size_t)(next - self->capacity) * sizeof(PyObject *));
    if (!self->object_mode) {
        WreathHeaderSpan *spans = PyMem_Realloc(
            self->spans, (size_t)next * sizeof(WreathHeaderSpan));
        if (spans == NULL) {
            PyErr_NoMemory();
            return -1;
        }
        self->spans = spans;
    }
    self->capacity = next;
    return 0;
}

int
wreath_header_block_append_span(
    PyObject *block, Py_ssize_t name_offset, Py_ssize_t name_size,
    Py_ssize_t value_offset, Py_ssize_t value_size)
{
    WreathHeaderBlock *self = (WreathHeaderBlock *)block;
    if (!wreath_headers_is_block(block) || self->object_mode) {
        PyErr_SetString(PyExc_TypeError, "expected a raw header block");
        return -1;
    }
    if (self->count == self->capacity && grow(self) < 0) return -1;
    self->spans[self->count++] = (WreathHeaderSpan){
        name_offset, name_size, value_offset, value_size};
    return 0;
}

int
wreath_header_block_append_objects(
    PyObject *block, PyObject *name, PyObject *value)
{
    WreathHeaderBlock *self = (WreathHeaderBlock *)block;
    if (!wreath_headers_is_block(block) || !self->object_mode) {
        PyErr_SetString(PyExc_TypeError, "expected an object header block");
        return -1;
    }
    if (self->count == self->capacity && grow(self) < 0) return -1;
    self->names[self->count] = Py_NewRef(name);
    self->values[self->count] = Py_NewRef(value);
    self->count++;
    return 0;
}

int
wreath_headers_is_block(PyObject *headers)
{
    return header_block_ready && headers != NULL &&
        Py_TYPE(headers) == &WreathHeaderBlockType;
}

Py_ssize_t
wreath_headers_count(PyObject *headers)
{
    if (wreath_headers_is_block(headers)) {
        WreathHeaderBlock *self = (WreathHeaderBlock *)headers;
        if (self->materialized != NULL) return PyList_GET_SIZE(self->materialized);
        return self->count;
    }
    return PyList_Check(headers) ? PyList_GET_SIZE(headers) : PySequence_Size(headers);
}

int
wreath_headers_view(
    PyObject *headers, Py_ssize_t index,
    const char **name, Py_ssize_t *name_size,
    const char **value, Py_ssize_t *value_size)
{
    PyObject *pair;
    PyObject *name_obj;
    PyObject *value_obj;
    if (wreath_headers_is_block(headers)) {
        WreathHeaderBlock *self = (WreathHeaderBlock *)headers;
        if (index < 0 || index >= wreath_headers_count(headers)) {
            PyErr_SetString(PyExc_IndexError, "header index out of range");
            return -1;
        }
        if (self->materialized == NULL) {
            if (self->object_mode) {
                name_obj = self->names[index];
                value_obj = self->values[index];
                *name = PyBytes_AS_STRING(name_obj);
                *name_size = PyBytes_GET_SIZE(name_obj);
                *value = PyBytes_AS_STRING(value_obj);
                *value_size = PyBytes_GET_SIZE(value_obj);
            }
            else {
                WreathHeaderSpan *span = &self->spans[index];
                const char *raw = PyBytes_AS_STRING(self->raw);
                *name = raw + span->name_offset;
                *name_size = span->name_size;
                if (self->values[index] != NULL) {
                    *value = PyBytes_AS_STRING(self->values[index]);
                    *value_size = PyBytes_GET_SIZE(self->values[index]);
                }
                else {
                    *value = raw + span->value_offset;
                    *value_size = span->value_size;
                }
            }
            return 0;
        }
        headers = self->materialized;
    }
    if (!PyList_Check(headers) || index < 0 || index >= PyList_GET_SIZE(headers)) {
        PyErr_SetString(PyExc_TypeError, "headers must be a list of byte pairs");
        return -1;
    }
    pair = PyList_GET_ITEM(headers, index);
    if (!PyTuple_Check(pair) || PyTuple_GET_SIZE(pair) != 2) {
        PyErr_SetString(PyExc_TypeError, "header must be a pair");
        return -1;
    }
    name_obj = PyTuple_GET_ITEM(pair, 0);
    value_obj = PyTuple_GET_ITEM(pair, 1);
    if (!PyBytes_Check(name_obj) || !PyBytes_Check(value_obj)) {
        PyErr_SetString(PyExc_TypeError, "header pair must contain bytes");
        return -1;
    }
    *name = PyBytes_AS_STRING(name_obj);
    *name_size = PyBytes_GET_SIZE(name_obj);
    *value = PyBytes_AS_STRING(value_obj);
    *value_size = PyBytes_GET_SIZE(value_obj);
    return 0;
}

static inline PyObject *
wreath_headers_item_object(PyObject *headers, Py_ssize_t index, int value)
{
    if (wreath_headers_is_block(headers)) {
        WreathHeaderBlock *self = (WreathHeaderBlock *)headers;
        if (self->materialized == NULL && self->object_mode &&
            index >= 0 && index < self->count) {
            return Py_NewRef(value ? self->values[index] : self->names[index]);
        }
    }
    PyObject *list = wreath_headers_materialize(headers);
    if (list == NULL) return NULL;
    if (index < 0 || index >= PyList_GET_SIZE(list)) {
        Py_DECREF(list);
        PyErr_SetString(PyExc_IndexError, "header index out of range");
        return NULL;
    }
    PyObject *result = Py_NewRef(
        PyTuple_GET_ITEM(PyList_GET_ITEM(list, index), value));
    Py_DECREF(list);
    return result;
}

PyObject *
wreath_headers_name_object(PyObject *headers, Py_ssize_t index)
{
    return wreath_headers_item_object(headers, index, 0);
}

PyObject *
wreath_headers_value_object(PyObject *headers, Py_ssize_t index)
{
    return wreath_headers_item_object(headers, index, 1);
}

PyObject *
wreath_headers_value_borrowed(PyObject *headers, Py_ssize_t index)
{
    if (wreath_headers_is_block(headers)) {
        WreathHeaderBlock *self = (WreathHeaderBlock *)headers;
        if (index < 0 || index >= self->count) {
            PyErr_SetString(PyExc_IndexError, "header index out of range");
            return NULL;
        }
        if (self->materialized != NULL) {
            return PyTuple_GET_ITEM(PyList_GET_ITEM(self->materialized, index), 1);
        }
        if (self->values[index] == NULL) {
            if (self->object_mode) return NULL;
            const char *raw = PyBytes_AS_STRING(self->raw);
            WreathHeaderSpan *span = &self->spans[index];
            self->values[index] = PyBytes_FromStringAndSize(
                raw + span->value_offset, span->value_size);
            if (self->values[index] == NULL) return NULL;
        }
        return self->values[index];
    }
    if (!PyList_Check(headers) || index < 0 || index >= PyList_GET_SIZE(headers)) {
        PyErr_SetString(PyExc_TypeError, "headers must be a list of byte pairs");
        return NULL;
    }
    return PyTuple_GET_ITEM(PyList_GET_ITEM(headers, index), 1);
}

PyObject *
wreath_headers_materialize(PyObject *headers)
{
    if (!wreath_headers_is_block(headers)) return Py_NewRef(headers);
    WreathHeaderBlock *self = (WreathHeaderBlock *)headers;
    if (self->materialized != NULL) return Py_NewRef(self->materialized);
    PyObject *list = PyList_New(self->count);
    if (list == NULL) return NULL;
    for (Py_ssize_t i = 0; i < self->count; i++) {
        PyObject *name;
        PyObject *value;
        if (self->object_mode) {
            name = Py_NewRef(self->names[i]);
            value = Py_NewRef(self->values[i]);
        }
        else {
            const char *raw = PyBytes_AS_STRING(self->raw);
            WreathHeaderSpan *span = &self->spans[i];
            name = self->names[i] != NULL
                ? Py_NewRef(self->names[i])
                : PyBytes_FromStringAndSize(raw + span->name_offset, span->name_size);
            value = self->values[i] != NULL
                ? Py_NewRef(self->values[i])
                : PyBytes_FromStringAndSize(raw + span->value_offset, span->value_size);
        }
        PyObject *pair = NULL;
        if (name != NULL && value != NULL) {
            pair = PyTuple_New(2);
            if (pair != NULL) {
                PyTuple_SET_ITEM(pair, 0, name);
                PyTuple_SET_ITEM(pair, 1, value);
                name = NULL;
                value = NULL;
            }
        }
        Py_XDECREF(name);
        Py_XDECREF(value);
        if (pair == NULL) {
            Py_DECREF(list);
            return NULL;
        }
        PyList_SET_ITEM(list, i, pair);
    }
    self->materialized = Py_NewRef(list);
    return list;
}

int
wreath_headers_set_first(PyObject *headers, PyObject *name, PyObject *value)
{
    if (!PyBytes_Check(name) || !PyBytes_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "header names and values must be bytes");
        return -1;
    }
    PyObject *list = headers;
    if (wreath_headers_is_block(headers)) {
        WreathHeaderBlock *self = (WreathHeaderBlock *)headers;
        if (self->materialized == NULL) {
            const char *wanted = PyBytes_AS_STRING(name);
            Py_ssize_t wanted_size = PyBytes_GET_SIZE(name);
            for (Py_ssize_t index = 0; index < self->count; index++) {
                const char *candidate;
                const char *ignored_value;
                Py_ssize_t candidate_size;
                Py_ssize_t ignored_value_size;
                if (wreath_headers_view(
                        headers, index, &candidate, &candidate_size,
                        &ignored_value, &ignored_value_size) < 0) return -1;
                if (candidate_size == wanted_size &&
                    memcmp(candidate, wanted, (size_t)wanted_size) == 0) {
                    Py_XSETREF(self->values[index], Py_NewRef(value));
                    return 0;
                }
            }
            if (self->object_mode) {
                return wreath_header_block_append_objects(headers, name, value);
            }
            list = wreath_headers_materialize(headers);
            if (list == NULL) return -1;
        }
        else {
            list = Py_NewRef(self->materialized);
        }
    }
    else {
        if (!PyList_Check(headers)) {
            PyErr_SetString(PyExc_TypeError, "headers must be a list of byte pairs");
            return -1;
        }
        Py_INCREF(list);
    }
    Py_ssize_t count = PyList_GET_SIZE(list);
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *pair = PyList_GET_ITEM(list, index);
        if (!PyTuple_Check(pair) || PyTuple_GET_SIZE(pair) != 2 ||
            !PyBytes_Check(PyTuple_GET_ITEM(pair, 0)) ||
            !PyBytes_Check(PyTuple_GET_ITEM(pair, 1))) {
            Py_DECREF(list);
            PyErr_SetString(PyExc_TypeError, "header must be a pair of bytes");
            return -1;
        }
        int equal = PyObject_RichCompareBool(PyTuple_GET_ITEM(pair, 0), name, Py_EQ);
        if (equal < 0) {
            Py_DECREF(list);
            return -1;
        }
        if (equal == 1) {
            PyObject *replacement = PyTuple_Pack(2, name, value);
            if (replacement == NULL) {
                Py_DECREF(list);
                return -1;
            }
            int rc = PyList_SetItem(list, index, replacement);
            Py_DECREF(list);
            return rc;
        }
    }
    PyObject *pair = PyTuple_Pack(2, name, value);
    int rc = pair == NULL ? -1 : PyList_Append(list, pair);
    Py_XDECREF(pair);
    Py_DECREF(list);
    return rc;
}
