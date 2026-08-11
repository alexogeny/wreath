/* Incremental gRPC message deframing.
 *
 * One native object owns one stream buffer.  A feed parses every complete
 * five-byte-prefixed message in place and compacts the retained tail once,
 * after the loop, so coalescing many messages into one HTTP/2 DATA frame does
 * not bounce through Python or front-delete the buffer once per message.
 *
 * Compression policy and Wreath's exception vocabulary stay in the facade:
 * the object stores the exact exception/status objects supplied at
 * construction, and calls the supplied decoder only for a flagged message.
 * There is no module-global type or mutable cache; the heap type is owned by
 * the module and every stream owns all of its mutable state.
 */
#include "wreathcore.h"

#include <stdint.h>
#include <string.h>

#define WREATH_GRPC_PREFIX 5

typedef struct {
    PyObject_HEAD
    WreathByteBuffer buffer;
    PyObject *decoder;
    PyObject *error_type;
    PyObject *resource_status;
    PyObject *internal_status;
    Py_ssize_t maximum;
    int accepts_compressed;
} WreathGrpcUnframer;

static int
grpc_raise(WreathGrpcUnframer *self, PyObject *status, const char *message)
{
    PyObject *text = PyUnicode_FromString(message);
    PyObject *error;
    if (text == NULL) return -1;
    error = PyObject_CallFunctionObjArgs(self->error_type, status, text, NULL);
    Py_DECREF(text);
    if (error == NULL) return -1;
    PyErr_SetObject(self->error_type, error);
    Py_DECREF(error);
    return -1;
}

static int
grpc_raise_length(WreathGrpcUnframer *self, uint32_t length)
{
    PyObject *text = PyUnicode_FromFormat(
        "message of %u bytes exceeds the %zd-byte limit", length, self->maximum);
    PyObject *error;
    if (text == NULL) return -1;
    error = PyObject_CallFunctionObjArgs(
        self->error_type, self->resource_status, text, NULL);
    Py_DECREF(text);
    if (error == NULL) return -1;
    PyErr_SetObject(self->error_type, error);
    Py_DECREF(error);
    return -1;
}

static int
grpc_raise_flag(WreathGrpcUnframer *self, unsigned int flag)
{
    PyObject *text = PyUnicode_FromFormat(
        "compressed flag must be 0 or 1, not %u", flag);
    PyObject *error;
    if (text == NULL) return -1;
    error = PyObject_CallFunctionObjArgs(
        self->error_type, self->internal_status, text, NULL);
    Py_DECREF(text);
    if (error == NULL) return -1;
    PyErr_SetObject(self->error_type, error);
    Py_DECREF(error);
    return -1;
}

static int
grpc_init(PyObject *object, PyObject *args, PyObject *kwargs)
{
    WreathGrpcUnframer *self = (WreathGrpcUnframer *)object;
    PyObject *encoding;
    PyObject *decoder;
    PyObject *error_type;
    PyObject *resource_status;
    PyObject *internal_status;
    static char *keywords[] = {
        "max_message_bytes", "encoding", "decoder", "error_type",
        "resource_status", "internal_status", NULL,
    };

    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "nOOOOO:GrpcUnframer", keywords,
            &self->maximum, &encoding, &decoder, &error_type,
            &resource_status, &internal_status)) {
        return -1;
    }
    if (self->maximum < 0) {
        PyErr_SetString(PyExc_ValueError, "max_message_bytes must be non-negative");
        return -1;
    }
    if (!PyUnicode_Check(encoding)) {
        PyErr_SetString(PyExc_TypeError, "encoding must be str");
        return -1;
    }
    int identity = PyUnicode_CompareWithASCIIString(encoding, "identity");
    if (identity < 0 && PyErr_Occurred()) return -1;
    int gzip = PyUnicode_CompareWithASCIIString(encoding, "gzip");
    if (gzip < 0 && PyErr_Occurred()) return -1;
    if (identity != 0 && gzip != 0) {
        PyErr_SetString(PyExc_ValueError, "encoding must be 'identity' or 'gzip'");
        return -1;
    }
    if (gzip == 0 && !PyCallable_Check(decoder)) {
        PyErr_SetString(PyExc_TypeError, "gzip decoder must be callable");
        return -1;
    }
    if (!PyCallable_Check(error_type)) {
        PyErr_SetString(PyExc_TypeError, "error_type must be callable");
        return -1;
    }
    wreath_buffer_init(&self->buffer);
    self->decoder = Py_NewRef(decoder);
    self->error_type = Py_NewRef(error_type);
    self->resource_status = Py_NewRef(resource_status);
    self->internal_status = Py_NewRef(internal_status);
    self->accepts_compressed = gzip == 0;
    return 0;
}

static int
grpc_traverse(PyObject *object, visitproc visit, void *arg)
{
    WreathGrpcUnframer *self = (WreathGrpcUnframer *)object;
    Py_VISIT(self->decoder);
    Py_VISIT(self->error_type);
    Py_VISIT(self->resource_status);
    Py_VISIT(self->internal_status);
    return 0;
}

static int
grpc_clear(PyObject *object)
{
    WreathGrpcUnframer *self = (WreathGrpcUnframer *)object;
    wreath_buffer_clear(&self->buffer);
    Py_CLEAR(self->decoder);
    Py_CLEAR(self->error_type);
    Py_CLEAR(self->resource_status);
    Py_CLEAR(self->internal_status);
    return 0;
}

static void
grpc_dealloc(PyObject *object)
{
    PyObject_GC_UnTrack(object);
    grpc_clear(object);
    Py_TYPE(object)->tp_free(object);
}

static PyObject *
grpc_feed(PyObject *object, PyObject *chunk)
{
    WreathGrpcUnframer *self = (WreathGrpcUnframer *)object;
    Py_buffer view;
    PyObject *out = NULL;
    Py_ssize_t total;
    Py_ssize_t cursor = 0;
    Py_ssize_t complete = 0;
    Py_ssize_t output_index = 0;

    if (PyObject_GetBuffer(chunk, &view, PyBUF_SIMPLE) < 0) return NULL;
    if (wreath_buffer_append(&self->buffer, view.buf, view.len) < 0) {
        PyBuffer_Release(&view);
        return NULL;
    }
    PyBuffer_Release(&view);
    total = wreath_buffer_size(&self->buffer);

    /* Count complete frames without touching their payloads, then allocate the
     * public result at its exact size. The second pass performs validation and
     * decoding; five header bytes per frame cost less than growing and mutating
     * a Python list for every message. */
    while (total - cursor >= WREATH_GRPC_PREFIX) {
        const unsigned char *raw =
            (const unsigned char *)wreath_buffer_data(&self->buffer) + cursor;
        uint32_t length = ((uint32_t)raw[1] << 24) | ((uint32_t)raw[2] << 16)
                        | ((uint32_t)raw[3] << 8) | (uint32_t)raw[4];
        if ((uint64_t)length > (uint64_t)PY_SSIZE_T_MAX) break;
        if ((uint64_t)(total - cursor - WREATH_GRPC_PREFIX) < (uint64_t)length)
            break;
        complete++;
        cursor += WREATH_GRPC_PREFIX + (Py_ssize_t)length;
    }
    cursor = 0;
    out = PyList_New(complete);
    if (out == NULL) return NULL;
    while (total - cursor >= WREATH_GRPC_PREFIX) {
        const unsigned char *raw =
            (const unsigned char *)wreath_buffer_data(&self->buffer) + cursor;
        unsigned int compressed = raw[0];
        uint32_t length = ((uint32_t)raw[1] << 24) | ((uint32_t)raw[2] << 16)
                        | ((uint32_t)raw[3] << 8) | (uint32_t)raw[4];
        PyObject *payload;

        if ((uint64_t)length > (uint64_t)self->maximum) {
            Py_DECREF(out);
            grpc_raise_length(self, length);
            return NULL;
        }
        if (compressed > 1) {
            Py_DECREF(out);
            grpc_raise_flag(self, compressed);
            return NULL;
        }
        if (compressed && !self->accepts_compressed) {
            Py_DECREF(out);
            grpc_raise(
                self, self->internal_status,
                "a message is flagged compressed but this call declared "
                "grpc-encoding: identity");
            return NULL;
        }
        if ((uint64_t)(total - cursor - WREATH_GRPC_PREFIX) < (uint64_t)length) break;

        payload = PyBytes_FromStringAndSize(
            (const char *)raw + WREATH_GRPC_PREFIX, (Py_ssize_t)length);
        if (payload == NULL) {
            Py_DECREF(out);
            return NULL;
        }
        if (compressed) {
            PyObject *decoded = PyObject_CallOneArg(self->decoder, payload);
            Py_DECREF(payload);
            payload = decoded;
            if (payload == NULL) {
                Py_DECREF(out);
                return NULL;
            }
        }
        PyList_SET_ITEM(out, output_index++, payload);
        cursor += WREATH_GRPC_PREFIX + (Py_ssize_t)length;
    }

    if (cursor != 0) wreath_buffer_consume(&self->buffer, cursor);
    return out;
}

static PyObject *
grpc_finish(PyObject *object, PyObject *Py_UNUSED(ignored))
{
    WreathGrpcUnframer *self = (WreathGrpcUnframer *)object;
    Py_ssize_t retained = wreath_buffer_size(&self->buffer);
    if (retained != 0) {
        PyObject *text = PyUnicode_FromFormat(
            "stream ended mid-message with %zd bytes buffered", retained);
        PyObject *error;
        if (text == NULL) return NULL;
        error = PyObject_CallFunctionObjArgs(
            self->error_type, self->internal_status, text, NULL);
        Py_DECREF(text);
        if (error == NULL) return NULL;
        PyErr_SetObject(self->error_type, error);
        Py_DECREF(error);
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyMethodDef grpc_methods[] = {
    {"feed", grpc_feed, METH_O, "feed(chunk) -> list[bytes]"},
    {"finish", grpc_finish, METH_NOARGS, "finish() -> None"},
    {NULL, NULL, 0, NULL},
};

static PyType_Slot grpc_slots[] = {
    {Py_tp_init, grpc_init},
    {Py_tp_dealloc, grpc_dealloc},
    {Py_tp_traverse, grpc_traverse},
    {Py_tp_clear, grpc_clear},
    {Py_tp_methods, grpc_methods},
    {Py_tp_new, PyType_GenericNew},
    {0, NULL},
};

static PyType_Spec grpc_spec = {
    .name = "wreath._native._core.GrpcUnframer",
    .basicsize = sizeof(WreathGrpcUnframer),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_IMMUTABLETYPE,
    .slots = grpc_slots,
};

int
wreath_register_grpc(PyObject *module)
{
    PyObject *type = PyType_FromSpec(&grpc_spec);
    if (type == NULL) return -1;
    if (PyModule_AddObject(module, "GrpcUnframer", type) < 0) {
        Py_DECREF(type);
        return -1;
    }
    return 0;
}
