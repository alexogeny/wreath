/* Incremental HTTP/1 response protocol used by the outbound client.
 *
 * The protocol owns its receive buffer and emits one parsed response head at a
 * time.  Keeping framing state in C avoids rescanning previously received bytes;
 * body framing remains with the Python connection until it migrates here.
 */
#include "wreathcore.h"

typedef struct {
    PyObject_HEAD
    PyObject *buffer;
    Py_ssize_t scan;
    Py_ssize_t max_header_bytes;
} WreathHttpClientProtocol;

static PyObject *
client_protocol_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    WreathHttpClientProtocol *self;
    Py_ssize_t limit = 65536;
    static char *names[] = {"max_header_bytes", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|n", names, &limit)) return NULL;
    if (limit <= 0) {
        PyErr_SetString(PyExc_ValueError, "max_header_bytes must be positive");
        return NULL;
    }
    self = (WreathHttpClientProtocol *)type->tp_alloc(type, 0);
    if (self == NULL) return NULL;
    self->buffer = PyByteArray_FromStringAndSize(NULL, 0);
    self->scan = 0;
    self->max_header_bytes = limit;
    if (self->buffer == NULL) {
        Py_DECREF(self);
        return NULL;
    }
    return (PyObject *)self;
}

static void
client_protocol_dealloc(PyObject *op)
{
    WreathHttpClientProtocol *self = (WreathHttpClientProtocol *)op;
    Py_XDECREF(self->buffer);
    Py_TYPE(op)->tp_free(op);
}

static PyObject *
client_protocol_feed(WreathHttpClientProtocol *self, PyObject *chunk)
{
    Py_buffer view;
    Py_ssize_t old_size;
    Py_ssize_t size;
    char *data;
    Py_ssize_t end = -1;
    PyObject *head;
    PyObject *parsed;
    if (PyObject_GetBuffer(chunk, &view, PyBUF_SIMPLE) < 0) return NULL;
    old_size = PyByteArray_GET_SIZE(self->buffer);
    if (view.len > PY_SSIZE_T_MAX - old_size) {
        PyBuffer_Release(&view);
        return PyErr_NoMemory();
    }
    if (PyByteArray_Resize(self->buffer, old_size + view.len) < 0) {
        PyBuffer_Release(&view);
        return NULL;
    }
    memcpy(PyByteArray_AS_STRING(self->buffer) + old_size, view.buf, view.len);
    PyBuffer_Release(&view);
    data = PyByteArray_AS_STRING(self->buffer);
    size = PyByteArray_GET_SIZE(self->buffer);
    if (self->scan > 3) self->scan -= 3;
    for (Py_ssize_t i = self->scan; i + 3 < size; i++) {
        if (data[i] == '\r' && data[i + 1] == '\n' &&
            data[i + 2] == '\r' && data[i + 3] == '\n') {
            end = i + 4;
            break;
        }
    }
    if (end < 0) {
        if (size > self->max_header_bytes) {
            PyErr_SetString(PyExc_ValueError, "response headers exceed configured limit");
            return NULL;
        }
        self->scan = size;
        Py_RETURN_NONE;
    }
    if (end > self->max_header_bytes) {
        PyErr_SetString(PyExc_ValueError, "response headers exceed configured limit");
        return NULL;
    }
    head = PyBytes_FromStringAndSize(data, end);
    if (head == NULL) return NULL;
    parsed = wreath_http_parse_response(NULL, head);
    Py_DECREF(head);
    if (parsed == NULL) return NULL;
    memmove(data, data + end, size - end);
    if (PyByteArray_Resize(self->buffer, size - end) < 0) {
        Py_DECREF(parsed);
        return NULL;
    }
    self->scan = 0;
    return parsed;
}

static PyObject *
client_protocol_pending(WreathHttpClientProtocol *self, void *closure)
{
    (void)closure;
    return PyBytes_FromStringAndSize(
        PyByteArray_AS_STRING(self->buffer), PyByteArray_GET_SIZE(self->buffer)
    );
}

static PyMethodDef client_protocol_methods[] = {
    {"feed_data", (PyCFunction)client_protocol_feed, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef client_protocol_getset[] = {
    {"pending", (getter)client_protocol_pending, NULL, NULL, NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyType_Slot client_protocol_slots[] = {
    {Py_tp_new, client_protocol_new},
    {Py_tp_dealloc, client_protocol_dealloc},
    {Py_tp_methods, client_protocol_methods},
    {Py_tp_getset, client_protocol_getset},
    {0, NULL},
};

static PyType_Spec client_protocol_spec = {
    .name = "wreath._native._client.Http1ClientProtocol",
    .basicsize = sizeof(WreathHttpClientProtocol),
    .flags = Py_TPFLAGS_DEFAULT,
    .slots = client_protocol_slots,
};

int
wreath_register_http_client_protocol(PyObject *module)
{
    PyObject *type = PyType_FromSpec(&client_protocol_spec);
    if (type == NULL) return -1;
    if (PyModule_AddObject(module, "Http1ClientProtocol", type) < 0) {
        Py_DECREF(type);
        return -1;
    }
    return 0;
}
