#include "slab.h"

#include <string.h>

PyTypeObject *WreathPgSlabType = NULL;
static PyTypeObject *chained_payload_type = NULL;
static PyTypeObject *slab_window_type = NULL;

typedef struct {
    PyObject_HEAD
    PyObject *owner;
    char *data;
    Py_ssize_t length;
    int readonly;
} WreathPgSlabWindow;

static int
window_getbuffer(PyObject *object, Py_buffer *view, int flags)
{
    WreathPgSlabWindow *window = (WreathPgSlabWindow *)object;
    return PyBuffer_FillInfo(
        view, object, window->data, window->length, window->readonly, flags
    );
}

static void
window_dealloc(WreathPgSlabWindow *self)
{
    Py_CLEAR(self->owner);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyType_Slot window_slots[] = {
    {Py_tp_dealloc, window_dealloc},
    {Py_bf_getbuffer, window_getbuffer},
    {0, NULL},
};

static PyType_Spec window_spec = {
    .name = "wreath._native._postgres._SlabWindow",
    .basicsize = sizeof(WreathPgSlabWindow),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_DISALLOW_INSTANTIATION,
    .slots = window_slots,
};

typedef struct {
    PyObject_HEAD
    PyObject *owners;
    char *data;
    Py_ssize_t length;
} WreathPgChainedPayload;

static int
chained_getbuffer(PyObject *object, Py_buffer *view, int flags)
{
    WreathPgChainedPayload *payload = (WreathPgChainedPayload *)object;
    return PyBuffer_FillInfo(
        view, object, payload->data, payload->length, 1, flags
    );
}

static int
chained_traverse(WreathPgChainedPayload *self, visitproc visit, void *arg)
{
    Py_VISIT(self->owners);
    return 0;
}

static int
chained_clear(WreathPgChainedPayload *self)
{
    Py_CLEAR(self->owners);
    return 0;
}

static void
chained_dealloc(WreathPgChainedPayload *self)
{
    PyObject_GC_UnTrack(self);
    chained_clear(self);
    PyMem_Free(self->data);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyType_Slot chained_slots[] = {
    {Py_tp_dealloc, chained_dealloc},
    {Py_tp_traverse, chained_traverse},
    {Py_tp_clear, chained_clear},
    {Py_bf_getbuffer, chained_getbuffer},
    {0, NULL},
};

static PyType_Spec chained_spec = {
    .name = "wreath._native._postgres._ChainedPayload",
    .basicsize = sizeof(WreathPgChainedPayload),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = chained_slots,
};

static int
slab_getbuffer(PyObject *object, Py_buffer *view, int flags)
{
    WreathPgSlab *slab = (WreathPgSlab *)object;
    return PyBuffer_FillInfo(
        view, object, slab->data, WREATH_PG_SLAB_SIZE, 0, flags
    );
}

static PyType_Slot slab_slots[] = {
    {Py_bf_getbuffer, slab_getbuffer},
    {0, NULL},
};

static PyType_Spec slab_spec = {
    .name = "wreath._native._postgres._ReceiveSlab",
    .basicsize = sizeof(WreathPgSlab),
    .flags = Py_TPFLAGS_DEFAULT,
    .slots = slab_slots,
};

WreathPgSlab *
wreath_pg_slab_new(void)
{
    WreathPgSlab *slab = (WreathPgSlab *)WreathPgSlabType->tp_alloc(WreathPgSlabType, 0);
    if (slab != NULL) {
        slab->read_position = 0;
        slab->write_position = 0;
    }
    return slab;
}

static PyObject *
slab_slice(WreathPgSlab *slab, Py_ssize_t start, Py_ssize_t stop, int readonly)
{
    WreathPgSlabWindow *window;
    PyObject *view;
    window = (WreathPgSlabWindow *)slab_window_type->tp_alloc(slab_window_type, 0);
    if (window == NULL) return NULL;
    window->owner = Py_NewRef((PyObject *)slab);
    window->data = (char *)slab->data + start;
    window->length = stop - start;
    window->readonly = readonly;
    view = PyMemoryView_FromObject((PyObject *)window);
    Py_DECREF(window);
    return view;
}

PyObject *
wreath_pg_slab_writable_view(WreathPgSlab *slab)
{
    return slab_slice(slab, slab->write_position, WREATH_PG_SLAB_SIZE, 0);
}

PyObject *
wreath_pg_slab_view(WreathPgSlab *slab, Py_ssize_t start, Py_ssize_t length)
{
    if (start < 0 || length < 0 || start > WREATH_PG_SLAB_SIZE - length) {
        PyErr_SetString(PyExc_ValueError, "invalid receive slab slice");
        return NULL;
    }
    return slab_slice(slab, start, start + length, 1);
}

PyObject *
wreath_pg_chained_payload(PyObject *owners, const char *data, Py_ssize_t length)
{
    WreathPgChainedPayload *payload;
    PyObject *view;
    payload = (WreathPgChainedPayload *)chained_payload_type->tp_alloc(
        chained_payload_type, 0
    );
    if (payload == NULL) return NULL;
    payload->data = PyMem_Malloc((size_t)length);
    if (payload->data == NULL && length > 0) {
        Py_DECREF(payload);
        return PyErr_NoMemory();
    }
    if (length > 0) memcpy(payload->data, data, (size_t)length);
    payload->length = length;
    payload->owners = Py_NewRef(owners);
    view = PyMemoryView_FromObject((PyObject *)payload);
    Py_DECREF(payload);
    return view;
}

int
wreath_pg_slab_init(PyObject *module)
{
    int result;
    WreathPgSlabType = (PyTypeObject *)PyType_FromSpec(&slab_spec);
    if (WreathPgSlabType == NULL) return -1;
    chained_payload_type = (PyTypeObject *)PyType_FromSpec(&chained_spec);
    if (chained_payload_type == NULL) return -1;
    slab_window_type = (PyTypeObject *)PyType_FromSpec(&window_spec);
    if (slab_window_type == NULL) return -1;
    result = PyModule_AddObjectRef(module, "_ReceiveSlab", (PyObject *)WreathPgSlabType);
    if (result == 0) {
        result = PyModule_AddObjectRef(
            module, "_ChainedPayload", (PyObject *)chained_payload_type
        );
    }
    if (result == 0) {
        Py_DECREF(WreathPgSlabType);
        Py_DECREF(chained_payload_type);
    }
    return result;
}
