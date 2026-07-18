#include "tape.h"

#include <limits.h>
#include <string.h>

PyTypeObject *WreathPgFieldTapeType = NULL;

static uint16_t
read_u16(const unsigned char *data)
{
    return (uint16_t)(((uint16_t)data[0] << 8) | data[1]);
}

static uint32_t
read_u32(const unsigned char *data)
{
    return ((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) |
           ((uint32_t)data[2] << 8) | data[3];
}

/* Move live refs to the physical front. Callers must not hold a ref pointer
 * across this. */
static void
compact_refs(WreathPgFieldTape *self)
{
    if (self->ref_head == 0) return;
    if (self->ref_count > 0) {
        memmove(self->refs, self->refs + self->ref_head,
                (size_t)self->ref_count * sizeof(WreathPgFieldRef));
    }
    self->ref_head = 0;
}

/* Drop the consumed owner prefix, rebasing every surviving ref's stored owner
 * index once for the move. This is the only place stored indexes shift. */
static int
compact_owners(WreathPgFieldTape *self)
{
    Py_ssize_t drop = self->owner_head;
    if (drop <= 0) return 0;
    if (PyList_SetSlice(self->owners, 0, drop, NULL) < 0) return -1;
    for (Py_ssize_t i = 0; i < self->ref_count; i++) {
        self->refs[self->ref_head + i].slab_index -= (uint32_t)drop;
    }
    self->owner_head = 0;
    return 0;
}

static int
reserve_refs(WreathPgFieldTape *self, Py_ssize_t additional)
{
    Py_ssize_t required;
    Py_ssize_t capacity;
    WreathPgFieldRef *resized;
    if (additional < 0 || self->ref_count > PY_SSIZE_T_MAX - additional ||
        self->ref_head > PY_SSIZE_T_MAX - (self->ref_count + additional)) {
        PyErr_NoMemory();
        return -1;
    }
    required = self->ref_head + self->ref_count + additional;
    if (required <= self->ref_capacity) return 0;
    /* Reuse the consumed prefix before growing: a tape that is appended to and
     * drained repeatedly would otherwise grow without bound. */
    if (self->ref_head > 0) {
        compact_refs(self);
        required = self->ref_count + additional;
        if (required <= self->ref_capacity) return 0;
    }
    capacity = self->ref_capacity > 0 ? self->ref_capacity : 1024;
    while (capacity < required) {
        if (capacity > PY_SSIZE_T_MAX / 2) {
            capacity = required;
            break;
        }
        capacity *= 2;
    }
    if (capacity > PY_SSIZE_T_MAX / (Py_ssize_t)sizeof(WreathPgFieldRef)) {
        PyErr_NoMemory();
        return -1;
    }
    resized = PyMem_Realloc(
        self->refs, (size_t)capacity * sizeof(WreathPgFieldRef)
    );
    if (resized == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->refs = resized;
    self->ref_capacity = capacity;
    return 0;
}

static PyObject *
tape_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    unsigned int columns;
    WreathPgFieldTape *self;
    static char *keywords[] = {"column_count", NULL};
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "I:_FieldTape", keywords, &columns)) return NULL;
    if (columns == 0 || columns > UINT16_MAX) {
        PyErr_SetString(PyExc_ValueError, "field tape column count is out of range");
        return NULL;
    }
    self = (WreathPgFieldTape *)type->tp_alloc(type, 0);
    if (self == NULL) return NULL;
    self->source_columns = columns;
    self->stored_columns = columns;
    self->owners = PyList_New(0);
    if (self->owners == NULL) {
        Py_DECREF(self);
        return NULL;
    }
    return (PyObject *)self;
}

static int
tape_traverse(WreathPgFieldTape *self, visitproc visit, void *arg)
{
    Py_VISIT(self->owners);
    return 0;
}

static int
tape_clear_refs(WreathPgFieldTape *self)
{
    self->row_count = 0;
    self->ref_count = 0;
    self->ref_head = 0;
    self->owner_head = 0;
    if (self->owners != NULL && PyList_SetSlice(
            self->owners, 0, PyList_GET_SIZE(self->owners), NULL) < 0) return -1;
    return 0;
}

static int
tape_clear(WreathPgFieldTape *self)
{
    Py_CLEAR(self->owners);
    return 0;
}

static void
tape_dealloc(WreathPgFieldTape *self)
{
    PyObject_GC_UnTrack(self);
    tape_clear(self);
    PyMem_Free(self->refs);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

int
wreath_pg_tape_append_raw(
    WreathPgFieldTape *self, PyObject *owner, const unsigned char *data,
    Py_ssize_t length, Py_ssize_t base_offset, unsigned int selected
)
{
    Py_ssize_t offset = 2;
    Py_ssize_t initial_ref_count = self->ref_count;
    Py_ssize_t owner_count;
    uint16_t columns;
    uint32_t owner_index;
    int owner_is_last;

    if (selected == 0 || selected > self->source_columns) {
        PyErr_SetString(PyExc_ValueError, "selected field count is out of range");
        return -1;
    }
    if (self->row_count > 0 && selected != self->stored_columns) {
        PyErr_SetString(PyExc_ValueError, "field tape selection changed within a batch");
        return -1;
    }
    if (length < 2) {
        PyErr_SetString(PyExc_ValueError, "truncated DataRow");
        return -1;
    }
    if (base_offset < 0 || base_offset > (Py_ssize_t)UINT32_MAX - length) {
        PyErr_SetString(PyExc_ValueError, "DataRow offset is out of range");
        return -1;
    }
    columns = read_u16(data);
    if (columns != self->source_columns) {
        PyErr_SetString(PyExc_ValueError, "DataRow column count does not match tape");
        return -1;
    }
    owner_count = PyList_GET_SIZE(self->owners);
    if (owner_count >= UINT32_MAX || reserve_refs(self, selected) < 0) return -1;
    /* Consecutive rows parsed from the same receive slab share one owner
       entry, so a multi-row result retains each slab exactly once. */
    owner_is_last = owner_count > 0 &&
        PyList_GET_ITEM(self->owners, owner_count - 1) == owner;
    owner_index = (uint32_t)(owner_is_last ? owner_count - 1 : owner_count);
    for (uint32_t column = 0; column < columns; column++) {
        int32_t field_length;
        if (offset > length - 4) {
            PyErr_SetString(PyExc_ValueError, "truncated DataRow field length");
            goto error;
        }
        field_length = (int32_t)read_u32(data + offset);
        offset += 4;
        if (field_length < -1 ||
            (field_length >= 0 && offset > length - field_length)) {
            PyErr_SetString(PyExc_ValueError, "invalid DataRow field length");
            goto error;
        }
        if (column < selected) {
            WreathPgFieldRef *field = &self->refs[self->ref_head + self->ref_count++];
            field->slab_index = owner_index;
            field->offset = (uint32_t)(base_offset + offset);
            field->length = field_length;
        }
        if (field_length >= 0) offset += field_length;
    }
    if (offset != length ||
        (!owner_is_last && PyList_Append(self->owners, owner) < 0)) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "invalid DataRow length");
        goto error;
    }
    self->stored_columns = selected;
    self->row_count++;
    return 0;

error:
    self->ref_count = initial_ref_count;
    return -1;
}

int
wreath_pg_tape_append_payload(
    WreathPgFieldTape *self, PyObject *payload, unsigned int selected
)
{
    Py_buffer view;
    int result;

    if (PyObject_GetBuffer(payload, &view, PyBUF_CONTIG_RO) < 0) return -1;
    result = wreath_pg_tape_append_raw(
        self, payload, (const unsigned char *)view.buf, view.len, 0, selected
    );
    PyBuffer_Release(&view);
    return result;
}

static PyObject *
tape_append(WreathPgFieldTape *self, PyObject *args)
{
    PyObject *payload;
    unsigned int selected;
    if (!PyArg_ParseTuple(args, "OI:append", &payload, &selected)) return NULL;
    if (wreath_pg_tape_append_payload(self, payload, selected) < 0) return NULL;
    Py_RETURN_NONE;
}

int
wreath_pg_tape_consume(WreathPgFieldTape *self, Py_ssize_t rows)
{
    Py_ssize_t refs;
    Py_ssize_t owner_size;
    if (rows < 0 || rows > self->row_count) {
        PyErr_SetString(PyExc_ValueError, "invalid field tape consume count");
        return -1;
    }
    if (self->stored_columns > 0 &&
        rows > PY_SSIZE_T_MAX / (Py_ssize_t)self->stored_columns) {
        PyErr_NoMemory();
        return -1;
    }
    refs = rows * (Py_ssize_t)self->stored_columns;

    /* Advance the cursors: no shifting, no per-ref rebase. */
    self->ref_head += refs;
    self->ref_count -= refs;
    self->row_count -= rows;

    if (self->row_count == 0) {
        /* Drained: release every owner and reset both cursors so the next
         * batch starts from a clean, reusable tape. */
        self->ref_head = 0;
        self->ref_count = 0;
        self->owner_head = 0;
        return PyList_SetSlice(self->owners, 0, PyList_GET_SIZE(self->owners), NULL);
    }

    /* Owner indexes are monotonic, so the first surviving ref names the first
     * owner that must be retained; everything before it is unreferenced. */
    self->owner_head = (Py_ssize_t)self->refs[self->ref_head].slab_index;

    /* Reclaim physical space only occasionally, so the amortized cost of
     * consuming a row stays constant. */
    if (self->ref_head >= 1024 && self->ref_head >= self->ref_count) {
        compact_refs(self);
    }
    owner_size = PyList_GET_SIZE(self->owners);
    if (self->owner_head >= 64 && self->owner_head * 2 >= owner_size) {
        return compact_owners(self);
    }
    return 0;
}

static PyObject *
tape_clear_method(WreathPgFieldTape *self, PyObject *unused)
{
    (void)unused;
    if (tape_clear_refs(self) < 0) return NULL;
    Py_RETURN_NONE;
}

static PyObject *
tape_row_count(WreathPgFieldTape *self, void *closure)
{
    (void)closure;
    return PyLong_FromSsize_t(self->row_count);
}

static PyObject *
tape_owner_count(WreathPgFieldTape *self, void *closure)
{
    (void)closure;
    /* The live window: consumed owners may still be physically present until
       the next compaction, but they are not part of the tape's contents. */
    return PyLong_FromSsize_t(PyList_GET_SIZE(self->owners) - self->owner_head);
}

static PyObject *
tape_field_count(WreathPgFieldTape *self, void *closure)
{
    (void)closure;
    return PyLong_FromSsize_t(self->ref_count);
}

static PyMethodDef tape_methods[] = {
    {"append", (PyCFunction)tape_append, METH_VARARGS, NULL},
    {"clear", (PyCFunction)tape_clear_method, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL}
};

static PyGetSetDef tape_getset[] = {
    {"row_count", (getter)tape_row_count, NULL, NULL, NULL},
    {"owner_count", (getter)tape_owner_count, NULL, NULL, NULL},
    {"stored_field_count", (getter)tape_field_count, NULL, NULL, NULL},
    {NULL, NULL, NULL, NULL, NULL}
};

static PyType_Slot tape_slots[] = {
    {Py_tp_new, tape_new},
    {Py_tp_dealloc, tape_dealloc},
    {Py_tp_traverse, tape_traverse},
    {Py_tp_clear, tape_clear},
    {Py_tp_methods, tape_methods},
    {Py_tp_getset, tape_getset},
    {0, NULL}
};

static PyType_Spec tape_spec = {
    .name = "wreath._native._postgres._FieldTape",
    .basicsize = sizeof(WreathPgFieldTape),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = tape_slots
};

int
wreath_pg_tape_init(PyObject *module)
{
    int result;
    WreathPgFieldTapeType = (PyTypeObject *)PyType_FromSpec(&tape_spec);
    if (WreathPgFieldTapeType == NULL) return -1;
    result = PyModule_AddObjectRef(module, "_FieldTape", (PyObject *)WreathPgFieldTapeType);
    if (result == 0) Py_DECREF(WreathPgFieldTapeType);
    return result;
}
