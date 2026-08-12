/* Incremental HTTP/1 response protocol used by the outbound client.
 *
 * The protocol owns its receive buffer and emits one complete response at a
 * time. Framing state lives with the request that created it: Python sees the
 * final ClientResponse only after the wire transaction is complete.
 *
 * Http1ClientStream (below) is the transport-facing side: a C-owned stream
 * reader/protocol replacing asyncio streams for client connections, and the
 * third implementer of the stream-fusion C API.
 */
#include "wreathcore.h"
#include "wreath_stream.h"

#include <string.h>

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
    /* The same search the server runs in `http.c`, by the same route. This was
     * the last place either side of the wire hand-rolled the terminator scan.
     * Measured on `feed_data` with a >=0.1% A/A floor: 7% faster on a 4-header
     * response, 12% on 12 headers, 17% on 96 -- in both the one-chunk and the
     * 256-byte-chunk feeds. */
    if (size - self->scan >= 4) {
        const uint8_t *found = wreath_memmem(
            (const uint8_t *)data + self->scan, size - self->scan,
            (const uint8_t *)"\r\n\r\n", 4);
        if (found != NULL) {
            end = (Py_ssize_t)(found - (const uint8_t *)data) + 4;
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

/* ======================================================================== */
/* Http1ClientStream: the transport-facing native stream for the client.    */
/*                                                                          */
/* Replaces asyncio streams: a C-owned receive buffer with StreamReader-    */
/* shaped awaitable reads (readuntil/readexactly/read, asyncio semantics    */
/* for IncompleteReadError/LimitOverrunError/at_eof), writer backpressure   */
/* (_drain/_wait_closed), and the stream-fusion C API so metal transports   */
/* deliver wire bytes with no Python calling convention per read. Buffered  */
/* response framing and phase deadlines remain in this request-owned object */
/* until a complete ClientResponse crosses back to Python.                  */
/* ======================================================================== */

#define CS_READ_NONE 0
#define CS_READ_UNTIL 1
#define CS_READ_EXACTLY 2
#define CS_READ_SOME 3
#define CS_READ_RESPONSE 4
#define CS_SEP_MAX 16
#define CS_OFFER_SIZE 65536

#define CS_RESPONSE_HEAD 0
#define CS_RESPONSE_LENGTH 1
#define CS_RESPONSE_CHUNK_SIZE 2
#define CS_RESPONSE_CHUNK_DATA 3
#define CS_RESPONSE_TRAILERS 4
#define CS_RESPONSE_CLOSE 5

typedef struct {
    PyObject_HEAD
    /* C-owned receive buffer with a consumed-prefix head offset. */
    char *data;
    Py_ssize_t len;
    Py_ssize_t head;
    Py_ssize_t cap;
    Py_ssize_t limit;           /* readuntil overrun limit */
    int eof;
    int connected;
    int offer_live;             /* fused offer outstanding: no realloc/move */
    int write_paused;
    PyObject *exc;              /* stored connection exception, or NULL */
    PyObject *transport;
    PyObject *create_future;    /* bound loop.create_future */
    PyObject *call_later;       /* bound loop.call_later */
    PyObject *drain_waiter;
    PyObject *closed_future;
    PyObject *offer_obj;        /* bytearray for the Python get_buffer path */
    /* At most one pending read. */
    int pending_kind;
    char pending_sep[CS_SEP_MAX];
    Py_ssize_t pending_sep_len;
    Py_ssize_t pending_count;
    Py_ssize_t pending_scan;    /* readuntil progress, relative to head */
    PyObject *pending_future;
    /* A buffered response is one operation-owned state machine. None of this
     * survives the read that created it, so free-threaded interpreters never
     * share framing or timeout state through the extension module. */
    PyObject *response_method;
    PyObject *response_type;
    PyObject *response_protocol_error;
    PyObject *response_too_large;
    PyObject *response_timeout_error;
    PyObject *response_head;
    PyObject *response_body;
    PyObject *response_header_timeout;
    PyObject *response_body_timeout;
    PyObject *response_timer;
    Py_ssize_t response_max_header;
    Py_ssize_t response_max_body;
    Py_ssize_t response_length;
    Py_ssize_t response_trailers;
    int response_phase;
    int response_timer_phase;
    int response_framed;
} WreathClientStream;

static PyTypeObject *client_stream_type = NULL;
static PyObject *cs_incomplete_read_error = NULL;  /* asyncio classes */
static PyObject *cs_limit_overrun_error = NULL;
static PyObject *cs_str_done = NULL;
static PyObject *cs_str_set_result = NULL;
static PyObject *cs_str_set_exception = NULL;
static PyObject *cs_str_cancel = NULL;

static Py_ssize_t
cs_avail(WreathClientStream *self)
{
    return self->len - self->head;
}

/* Grow the buffer so `extra` bytes fit after len, compacting the consumed
 * prefix first. Never called while a fused offer is live (the offer pins the
 * tail address). */
static int
cs_reserve(WreathClientStream *self, Py_ssize_t extra)
{
    if (self->cap - self->len >= extra) {
        return 0;
    }
    if (self->head > 0) {
        memmove(self->data, self->data + self->head,
                (size_t)(self->len - self->head));
        self->len -= self->head;
        self->head = 0;
        if (self->cap - self->len >= extra) {
            return 0;
        }
    }
    Py_ssize_t newcap = self->cap ? self->cap : 16384;
    while (newcap - self->len < extra) {
        if (newcap > PY_SSIZE_T_MAX / 2) {
            PyErr_NoMemory();
            return -1;
        }
        newcap *= 2;
    }
    char *grown = PyMem_Realloc(self->data, (size_t)newcap);
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->data = grown;
    self->cap = newcap;
    return 0;
}

/* Take `count` bytes off the front as bytes; reclaim the prefix lazily. */
static PyObject *
cs_consume(WreathClientStream *self, Py_ssize_t count)
{
    PyObject *chunk = PyBytes_FromStringAndSize(
        self->data + self->head, count);
    if (chunk == NULL) {
        return NULL;
    }
    self->head += count;
    if (self->head == self->len) {
        self->head = 0;
        self->len = 0;
    } else if (!self->offer_live && self->head > 65536 &&
               self->head * 2 >= self->len) {
        memmove(self->data, self->data + self->head,
                (size_t)(self->len - self->head));
        self->len -= self->head;
        self->head = 0;
    }
    return chunk;
}

static int
cs_future_done(PyObject *future)
{
    PyObject *done = PyObject_CallMethodNoArgs(future, cs_str_done);
    if (done == NULL) {
        return -1;
    }
    int result = PyObject_IsTrue(done);
    Py_DECREF(done);
    return result;
}

static int
cs_resolve(PyObject *future, PyObject *value, int is_exception)
{
    PyObject *result = PyObject_CallMethodOneArg(
        future, is_exception ? cs_str_set_exception : cs_str_set_result,
        value);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    return 0;
}

static void
cs_cancel_response_timer(WreathClientStream *self)
{
    if (self->response_timer != NULL) {
        PyObject *cancelled = PyObject_CallMethodNoArgs(
            self->response_timer, cs_str_cancel);
        if (cancelled == NULL) {
            /* Timer cancellation is best-effort cleanup. A cancelled or
             * already-fired asyncio handle is harmless; never replace the
             * response result with a cleanup error. */
            PyErr_Clear();
        } else {
            Py_DECREF(cancelled);
        }
        Py_CLEAR(self->response_timer);
    }
    self->response_timer_phase = -1;
}

static void
cs_clear_response(WreathClientStream *self)
{
    cs_cancel_response_timer(self);
    Py_CLEAR(self->response_method);
    Py_CLEAR(self->response_type);
    Py_CLEAR(self->response_protocol_error);
    Py_CLEAR(self->response_too_large);
    Py_CLEAR(self->response_timeout_error);
    Py_CLEAR(self->response_head);
    Py_CLEAR(self->response_body);
    Py_CLEAR(self->response_header_timeout);
    Py_CLEAR(self->response_body_timeout);
    self->response_max_header = 0;
    self->response_max_body = 0;
    self->response_length = 0;
    self->response_trailers = 0;
    self->response_phase = CS_RESPONSE_HEAD;
    self->response_framed = 0;
}

static void
cs_clear_pending(WreathClientStream *self)
{
    if (self->pending_kind == CS_READ_RESPONSE) {
        cs_clear_response(self);
    }
    self->pending_kind = CS_READ_NONE;
    self->pending_sep_len = 0;
    self->pending_count = 0;
    self->pending_scan = 0;
    Py_CLEAR(self->pending_future);
}

static int
cs_raise_response(PyObject *type, const char *message)
{
    PyErr_SetString(type != NULL ? type : PyExc_ValueError, message);
    return -1;
}

static int
cs_response_consume(WreathClientStream *self, Py_ssize_t count)
{
    if (count < 0 || count > cs_avail(self)) {
        PyErr_SetString(PyExc_RuntimeError, "invalid response buffer consumption");
        return -1;
    }
    self->head += count;
    if (self->head == self->len) {
        self->head = 0;
        self->len = 0;
    }
    return 0;
}

static Py_ssize_t
cs_find_crlf(WreathClientStream *self, Py_ssize_t start, const char *needle,
             Py_ssize_t needle_size)
{
    Py_ssize_t available = cs_avail(self);
    if (start < 0 || start > available || available - start < needle_size) {
        return -1;
    }
    const uint8_t *base = (const uint8_t *)self->data + self->head;
    const uint8_t *found = wreath_memmem(
        base + start, available - start,
        (const uint8_t *)needle, needle_size);
    return found == NULL ? -1 : (Py_ssize_t)(found - base);
}

static int
cs_response_append(WreathClientStream *self, const char *data, Py_ssize_t size)
{
    Py_ssize_t old_size = PyByteArray_GET_SIZE(self->response_body);
    if (size < 0 || old_size > self->response_max_body - size) {
        return cs_raise_response(
            self->response_too_large,
            "response body exceeds configured limit");
    }
    if (PyByteArray_Resize(self->response_body, old_size + size) < 0) return -1;
    memcpy(PyByteArray_AS_STRING(self->response_body) + old_size, data,
           (size_t)size);
    return 0;
}

static PyObject *
cs_finish_response(WreathClientStream *self, PyObject *body)
{
    PyObject *minor = PyTuple_GET_ITEM(self->response_head, 0);
    PyObject *status = PyTuple_GET_ITEM(self->response_head, 1);
    PyObject *reason = PyTuple_GET_ITEM(self->response_head, 2);
    PyObject *headers_list = PyTuple_GET_ITEM(self->response_head, 3);
    PyObject *headers = PySequence_Tuple(headers_list);
    PyObject *version = NULL;
    PyObject *response = NULL;
    PyObject *framed = NULL;
    PyObject *keep_args = NULL;
    PyObject *reusable = NULL;
    PyObject *result = NULL;
    long minor_value;

    if (headers == NULL) goto done;
    minor_value = PyLong_AsLong(minor);
    if (minor_value == -1 && PyErr_Occurred()) goto done;
    version = PyUnicode_FromFormat("1.%ld", minor_value);
    if (version == NULL) goto done;
    response = PyObject_CallFunctionObjArgs(
        self->response_type, status, headers, body, version, reason, NULL);
    if (response == NULL) goto done;
    framed = PyBool_FromLong(self->response_framed);
    keep_args = framed != NULL ? PyTuple_Pack(3, minor, headers_list, framed) : NULL;
    if (keep_args == NULL) goto done;
    reusable = wreath_http_response_keeps_alive(NULL, keep_args);
    if (reusable == NULL) goto done;
    result = PyTuple_Pack(2, response, reusable);

done:
    Py_XDECREF(headers);
    Py_XDECREF(version);
    Py_XDECREF(response);
    Py_XDECREF(framed);
    Py_XDECREF(keep_args);
    Py_XDECREF(reusable);
    return result;
}

/* Advance one complete buffered response. The return convention mirrors
 * cs_take_ready: a new result when complete, NULL plus would_block when more
 * wire bytes are required, or NULL with an exception on refusal. */
static PyObject *
cs_take_response(WreathClientStream *self, int *would_block)
{
    *would_block = 0;
    for (;;) {
        Py_ssize_t available = cs_avail(self);
        if (self->response_phase == CS_RESPONSE_HEAD) {
            Py_ssize_t end = cs_find_crlf(self, 0, "\r\n\r\n", 4);
            if (end < 0) {
                if (available > self->response_max_header) {
                    cs_raise_response(
                        self->response_protocol_error,
                        "response headers exceed configured limit");
                    return NULL;
                }
                if (self->eof) {
                    cs_raise_response(
                        self->response_protocol_error,
                        "upstream closed before response headers completed");
                    return NULL;
                }
                *would_block = 1;
                return NULL;
            }
            end += 4;
            if (end > self->response_max_header) {
                cs_raise_response(
                    self->response_protocol_error,
                    "response headers exceed configured limit");
                return NULL;
            }
            PyObject *head = PyBytes_FromStringAndSize(
                self->data + self->head, end);
            PyObject *parsed = head != NULL
                ? wreath_http_parse_response(NULL, head) : NULL;
            Py_XDECREF(head);
            if (parsed == NULL) return NULL;
            if (parsed == Py_None) {
                Py_DECREF(parsed);
                cs_raise_response(
                    self->response_protocol_error,
                    "complete response head was not parsed");
                return NULL;
            }
            long status = PyLong_AsLong(PyTuple_GET_ITEM(parsed, 1));
            if (status == -1 && PyErr_Occurred()) {
                Py_DECREF(parsed);
                return NULL;
            }
            if (cs_response_consume(self, end) < 0) {
                Py_DECREF(parsed);
                return NULL;
            }
            if (status == 101) {
                Py_DECREF(parsed);
                cs_raise_response(
                    self->response_protocol_error,
                    "protocol switching is not supported");
                return NULL;
            }
            if (status < 200) {
                Py_DECREF(parsed);
                continue;
            }
            self->response_head = parsed;
            PyObject *frame_args = PyTuple_Pack(
                3, self->response_method, PyTuple_GET_ITEM(parsed, 1),
                PyTuple_GET_ITEM(parsed, 3));
            PyObject *framing = frame_args != NULL
                ? wreath_http_response_framing(NULL, frame_args) : NULL;
            Py_XDECREF(frame_args);
            if (framing == NULL) return NULL;
            PyObject *mode = PyTuple_GET_ITEM(framing, 0);
            PyObject *length = PyTuple_GET_ITEM(framing, 1);
            int is_none = PyUnicode_CompareWithASCIIString(mode, "none") == 0;
            int is_chunked = !is_none &&
                PyUnicode_CompareWithASCIIString(mode, "chunked") == 0;
            int is_length = !is_none && !is_chunked &&
                PyUnicode_CompareWithASCIIString(mode, "length") == 0;
            if (PyErr_Occurred()) {
                Py_DECREF(framing);
                return NULL;
            }
            if (is_none) {
                self->response_framed = 1;
                Py_DECREF(framing);
                if (cs_avail(self) != 0) {
                    cs_raise_response(
                        self->response_protocol_error,
                        "unsolicited bytes follow the framed response body");
                    return NULL;
                }
                PyObject *empty = PyBytes_FromStringAndSize(NULL, 0);
                PyObject *result = empty != NULL
                    ? cs_finish_response(self, empty) : NULL;
                Py_XDECREF(empty);
                return result;
            }
            if (is_length) {
                self->response_length = PyLong_AsSsize_t(length);
                if (self->response_length == -1 && PyErr_Occurred()) {
                    Py_DECREF(framing);
                    return NULL;
                }
                if (self->response_length > self->response_max_body) {
                    Py_DECREF(framing);
                    cs_raise_response(
                        self->response_too_large,
                        "response body exceeds configured limit");
                    return NULL;
                }
                self->response_phase = CS_RESPONSE_LENGTH;
                self->response_framed = 1;
            } else if (is_chunked) {
                self->response_phase = CS_RESPONSE_CHUNK_SIZE;
                self->response_framed = 1;
                self->response_body = PyByteArray_FromStringAndSize(NULL, 0);
                if (self->response_body == NULL) {
                    Py_DECREF(framing);
                    return NULL;
                }
            } else {
                self->response_phase = CS_RESPONSE_CLOSE;
                self->response_framed = 0;
            }
            Py_DECREF(framing);
            continue;
        }
        if (self->response_phase == CS_RESPONSE_LENGTH) {
            if (available < self->response_length) {
                if (self->eof) {
                    cs_raise_response(
                        self->response_protocol_error,
                        "upstream closed before response body completed");
                    return NULL;
                }
                *would_block = 1;
                return NULL;
            }
            if (available > self->response_length) {
                cs_raise_response(
                    self->response_protocol_error,
                    "unsolicited bytes follow the framed response body");
                return NULL;
            }
            PyObject *body = cs_consume(self, self->response_length);
            PyObject *result = body != NULL ? cs_finish_response(self, body) : NULL;
            Py_XDECREF(body);
            return result;
        }
        if (self->response_phase == CS_RESPONSE_CLOSE) {
            if (available > self->response_max_body) {
                cs_raise_response(
                    self->response_too_large,
                    "response body exceeds configured limit");
                return NULL;
            }
            if (!self->eof) {
                *would_block = 1;
                return NULL;
            }
            PyObject *body = cs_consume(self, available);
            PyObject *result = body != NULL ? cs_finish_response(self, body) : NULL;
            Py_XDECREF(body);
            return result;
        }
        if (self->response_phase == CS_RESPONSE_CHUNK_SIZE) {
            Py_ssize_t line_end = cs_find_crlf(self, 0, "\r\n", 2);
            if (line_end < 0) {
                if (available > 1024) {
                    cs_raise_response(
                        self->response_protocol_error,
                        "response chunk line exceeds limit");
                    return NULL;
                }
                if (self->eof) {
                    cs_raise_response(
                        self->response_protocol_error,
                        "upstream closed during response chunk");
                    return NULL;
                }
                *would_block = 1;
                return NULL;
            }
            if (line_end > 1022) {
                cs_raise_response(
                    self->response_protocol_error,
                    "response chunk line exceeds limit");
                return NULL;
            }
            const unsigned char *line =
                (const unsigned char *)self->data + self->head;
            Py_ssize_t digits = 0;
            size_t size = 0;
            while (digits < line_end && line[digits] != ';') {
                unsigned char c = line[digits];
                unsigned value;
                if (c >= '0' && c <= '9') value = c - '0';
                else if (c >= 'a' && c <= 'f') value = c - 'a' + 10;
                else if (c >= 'A' && c <= 'F') value = c - 'A' + 10;
                else {
                    cs_raise_response(
                        self->response_protocol_error,
                        "invalid response chunk size");
                    return NULL;
                }
                if (size > (size_t)PY_SSIZE_T_MAX / 16U) {
                    cs_raise_response(
                        self->response_too_large,
                        "response body exceeds configured limit");
                    return NULL;
                }
                size = size * 16U + value;
                digits++;
            }
            if (digits == 0) {
                cs_raise_response(
                    self->response_protocol_error,
                    "invalid response chunk size");
                return NULL;
            }
            if (cs_response_consume(self, line_end + 2) < 0) return NULL;
            if (size == 0) {
                self->response_phase = CS_RESPONSE_TRAILERS;
                continue;
            }
            if ((Py_ssize_t)size > self->response_max_body -
                    PyByteArray_GET_SIZE(self->response_body)) {
                cs_raise_response(
                    self->response_too_large,
                    "response body exceeds configured limit");
                return NULL;
            }
            self->response_length = (Py_ssize_t)size;
            self->response_phase = CS_RESPONSE_CHUNK_DATA;
            continue;
        }
        if (self->response_phase == CS_RESPONSE_CHUNK_DATA) {
            if (available < self->response_length + 2) {
                if (self->eof) {
                    cs_raise_response(
                        self->response_protocol_error,
                        "upstream closed during response chunk");
                    return NULL;
                }
                *would_block = 1;
                return NULL;
            }
            const char *data = self->data + self->head;
            if (data[self->response_length] != '\r' ||
                data[self->response_length + 1] != '\n') {
                cs_raise_response(
                    self->response_protocol_error,
                    "malformed response chunk terminator");
                return NULL;
            }
            if (cs_response_append(self, data, self->response_length) < 0 ||
                cs_response_consume(self, self->response_length + 2) < 0) {
                return NULL;
            }
            self->response_phase = CS_RESPONSE_CHUNK_SIZE;
            continue;
        }
        /* Trailers are validated by the same response-header parser as an
         * ordinary head. They are deliberately not exposed in ClientResponse,
         * matching the public client contract. */
        Py_ssize_t line_end = cs_find_crlf(self, 0, "\r\n", 2);
        if (line_end < 0) {
            if (self->response_trailers + available > self->response_max_header) {
                cs_raise_response(
                    self->response_protocol_error,
                    "response trailers exceed configured limit");
                return NULL;
            }
            if (self->eof) {
                cs_raise_response(
                    self->response_protocol_error,
                    "upstream closed during response trailers");
                return NULL;
            }
            *would_block = 1;
            return NULL;
        }
        self->response_trailers += line_end + 2;
        if (self->response_trailers > self->response_max_header) {
            cs_raise_response(
                self->response_protocol_error,
                "response trailers exceed configured limit");
            return NULL;
        }
        if (line_end == 0) {
            if (cs_response_consume(self, 2) < 0) return NULL;
            if (cs_avail(self) != 0) {
                cs_raise_response(
                    self->response_protocol_error,
                    "unsolicited bytes follow the framed response body");
                return NULL;
            }
            PyObject *body = PyBytes_FromStringAndSize(
                PyByteArray_AS_STRING(self->response_body),
                PyByteArray_GET_SIZE(self->response_body));
            PyObject *result = body != NULL ? cs_finish_response(self, body) : NULL;
            Py_XDECREF(body);
            return result;
        }
        static const char synthetic_head[] = "HTTP/1.1 200 OK\r\n";
        Py_ssize_t synthetic_size =
            (Py_ssize_t)(sizeof(synthetic_head) - 1) + line_end + 4;
        PyObject *synthetic = PyBytes_FromStringAndSize(NULL, synthetic_size);
        if (synthetic != NULL) {
            char *write = PyBytes_AS_STRING(synthetic);
            memcpy(write, synthetic_head, sizeof(synthetic_head) - 1);
            memcpy(write + sizeof(synthetic_head) - 1,
                   self->data + self->head, (size_t)line_end);
            memcpy(write + sizeof(synthetic_head) - 1 + line_end, "\r\n\r\n", 4);
        }
        PyObject *valid = synthetic != NULL
            ? wreath_http_parse_response(NULL, synthetic) : NULL;
        Py_XDECREF(synthetic);
        if (valid == NULL) return NULL;
        if (valid == Py_None) {
            Py_DECREF(valid);
            cs_raise_response(
                self->response_protocol_error,
                "malformed response trailer");
            return NULL;
        }
        Py_DECREF(valid);
        if (cs_response_consume(self, line_end + 2) < 0) return NULL;
        /* The public reader's body timeout is per trailer read, not one total
         * deadline for an arbitrary trailer block. Re-arm after each complete
         * line while keeping the state wholly inside this transaction. */
        self->response_timer_phase = -1;
    }
}

static void
cs_raise_limit_overrun(const char *message, Py_ssize_t consumed)
{
    PyObject *exc = PyObject_CallFunction(
        cs_limit_overrun_error, "sn", message, consumed);
    if (exc != NULL) {
        PyErr_SetRaisedException(exc);
    }
}

/* Consumes the `partial` reference. */
static void
cs_raise_incomplete(PyObject *partial, Py_ssize_t expected)
{
    PyObject *exc = expected < 0
        ? PyObject_CallFunction(
              cs_incomplete_read_error, "OO", partial, Py_None)
        : PyObject_CallFunction(
              cs_incomplete_read_error, "On", partial, expected);
    Py_DECREF(partial);
    if (exc != NULL) {
        PyErr_SetRaisedException(exc);
    }
}

/* Produce a read's result from buffered data / EOF, or report that it must
 * wait. Returns a new reference on success; NULL with the exception set on a
 * stream error; NULL with *would_block set (no exception) when more bytes are
 * needed. `scan` carries readuntil progress (relative to head) in and out. */
static PyObject *
cs_take_ready(WreathClientStream *self, int kind, const char *sep,
              Py_ssize_t seplen, Py_ssize_t count, Py_ssize_t *scan,
              int *would_block)
{
    *would_block = 0;
    Py_ssize_t avail = cs_avail(self);
    if (kind == CS_READ_UNTIL) {
        const char *base = self->data + self->head;
        Py_ssize_t isep = -1;
        /* `wreath_memmem` rather than a first-byte test plus `memcmp`, which is
         * what this was. The separators are `\r\n\r\n` for a response head and
         * `\r\n` for chunk framing, both of which it sends to the vector scan.
         *
         * This is the largest win of the three in this file, because the cost
         * here is the scan itself rather than a buffer copy around it: with a
         * 2.4% A/A floor, 8% faster over 477 bytes, 21% over 1,257 and 39% over
         * 3,753. The shape is the tell -- the time barely grows with the
         * response now (1,470 ns -> 1,561 ns across that range) where it used
         * to climb with every byte (1,542 ns -> 2,543 ns). */
        if (avail - *scan >= seplen) {
            const uint8_t *found = wreath_memmem(
                (const uint8_t *)base + *scan, avail - *scan,
                (const uint8_t *)sep, seplen);
            if (found != NULL) {
                isep = (Py_ssize_t)(found - (const uint8_t *)base);
            }
        }
        if (isep >= 0) {
            Py_ssize_t total = isep + seplen;
            if (total > self->limit) {
                cs_raise_limit_overrun(
                    "Separator is found, but chunk is longer than limit",
                    isep);
                return NULL;
            }
            return cs_consume(self, total);
        }
        if (avail > self->limit) {
            cs_raise_limit_overrun(
                "Separator is not found, and chunk exceed the limit", avail);
            return NULL;
        }
        if (self->eof) {
            PyObject *partial = cs_consume(self, avail);
            if (partial == NULL) {
                return NULL;
            }
            cs_raise_incomplete(partial, -1);
            return NULL;
        }
        *scan = avail >= seplen - 1 ? avail - (seplen - 1) : 0;
        *would_block = 1;
        return NULL;
    }
    if (kind == CS_READ_EXACTLY) {
        if (avail >= count) {
            return cs_consume(self, count);
        }
        if (self->eof) {
            PyObject *partial = cs_consume(self, avail);
            if (partial == NULL) {
                return NULL;
            }
            cs_raise_incomplete(partial, count);
            return NULL;
        }
        *would_block = 1;
        return NULL;
    }
    /* CS_READ_SOME */
    if (count >= 0) {
        if (count == 0) {
            return PyBytes_FromStringAndSize(NULL, 0);
        }
        if (avail > 0) {
            return cs_consume(self, avail < count ? avail : count);
        }
        if (self->eof) {
            return PyBytes_FromStringAndSize(NULL, 0);
        }
        *would_block = 1;
        return NULL;
    }
    if (self->eof) {  /* read(-1): everything until EOF */
        return cs_consume(self, avail);
    }
    *would_block = 1;
    return NULL;
}

/* Attempt to complete the pending read from buffered data / EOF / a stored
 * connection error. Called after every data arrival and on EOF. */
static int
cs_try_satisfy(WreathClientStream *self)
{
    if (self->pending_kind == CS_READ_NONE) {
        return 0;
    }
    int done = cs_future_done(self->pending_future);
    if (done < 0) {
        return -1;
    }
    if (done) {
        /* Cancelled (e.g. wait_for timeout): drop the request, keep bytes. */
        cs_clear_pending(self);
        return 0;
    }
    if (self->exc != NULL) {
        int status = cs_resolve(self->pending_future, self->exc, 1);
        cs_clear_pending(self);
        return status;
    }
    int would_block = 0;
    Py_ssize_t scan = self->pending_scan;
    PyObject *ready = self->pending_kind == CS_READ_RESPONSE
        ? cs_take_response(self, &would_block)
        : cs_take_ready(
              self, self->pending_kind, self->pending_sep, self->pending_sep_len,
              self->pending_count, &scan, &would_block);
    if (would_block) {
        self->pending_scan = scan;
        if (self->pending_kind == CS_READ_RESPONSE &&
            self->response_timer_phase != self->response_phase) {
            cs_cancel_response_timer(self);
            PyObject *callback = PyObject_GetAttrString(
                (PyObject *)self, "_response_timeout");
            PyObject *delay = self->response_phase == CS_RESPONSE_HEAD
                ? self->response_header_timeout : self->response_body_timeout;
            self->response_timer = callback != NULL
                ? PyObject_CallFunctionObjArgs(
                      self->call_later, delay, callback, NULL)
                : NULL;
            Py_XDECREF(callback);
            if (self->response_timer == NULL) return -1;
            self->response_timer_phase = self->response_phase;
        }
        return 0;
    }
    int status;
    if (ready == NULL) {
        PyObject *exc = PyErr_GetRaisedException();
        if (exc == NULL) {
            return -1;
        }
        status = cs_resolve(self->pending_future, exc, 1);
        Py_DECREF(exc);
    } else {
        status = cs_resolve(self->pending_future, ready, 0);
        Py_DECREF(ready);
    }
    cs_clear_pending(self);
    return status;
}

static PyObject *
cs_response_timeout(WreathClientStream *self, PyObject *Py_UNUSED(ignored))
{
    if (self->pending_kind != CS_READ_RESPONSE || self->pending_future == NULL) {
        Py_RETURN_NONE;
    }
    int done = cs_future_done(self->pending_future);
    if (done < 0) return NULL;
    if (done) {
        cs_clear_pending(self);
        Py_RETURN_NONE;
    }
    const char *message = self->response_phase == CS_RESPONSE_HEAD
        ? "timed out reading response headers"
        : "timed out reading response body";
    PyObject *exc = PyObject_CallFunction(
        self->response_timeout_error, "s", message);
    if (exc == NULL) return NULL;
    int status = cs_resolve(self->pending_future, exc, 1);
    Py_DECREF(exc);
    cs_clear_pending(self);
    if (status < 0) return NULL;
    Py_RETURN_NONE;
}

/* Begin a read. When buffered bytes can satisfy it, the result is returned
 * SYNCHRONOUSLY as bytes -- no future, no await machinery, and the caller
 * (http_client._timed) skips asyncio.wait_for's Timeout setup entirely.
 * Only a read that must wait allocates a loop future. */
static PyObject *
cs_begin_read(WreathClientStream *self, int kind, const char *sep,
              Py_ssize_t seplen, Py_ssize_t count)
{
    if (self->pending_kind != CS_READ_NONE) {
        int done = cs_future_done(self->pending_future);
        if (done < 0) {
            return NULL;
        }
        if (!done) {
            PyErr_SetString(PyExc_RuntimeError,
                            "a stream read is already awaited elsewhere");
            return NULL;
        }
        cs_clear_pending(self);
    }
    if (self->exc != NULL) {
        PyErr_SetRaisedException(Py_NewRef(self->exc));
        return NULL;
    }
    int would_block = 0;
    Py_ssize_t scan = 0;
    PyObject *ready = cs_take_ready(
        self, kind, sep, seplen, count, &scan, &would_block);
    if (!would_block) {
        return ready;  /* bytes, or NULL with the stream exception set */
    }
    PyObject *future = PyObject_CallNoArgs(self->create_future);
    if (future == NULL) {
        return NULL;
    }
    self->pending_kind = kind;
    if (seplen > 0) {
        memcpy(self->pending_sep, sep, (size_t)seplen);
    }
    self->pending_sep_len = seplen;
    self->pending_count = count;
    self->pending_scan = scan;
    self->pending_future = Py_NewRef(future);
    return future;
}

static PyObject *
cs_read_response(WreathClientStream *self, PyObject *args)
{
    PyObject *method;
    PyObject *response_type;
    PyObject *protocol_error;
    PyObject *too_large;
    PyObject *timeout_error;
    PyObject *header_timeout;
    PyObject *body_timeout;
    Py_ssize_t max_header;
    Py_ssize_t max_body;
    if (!PyArg_ParseTuple(
            args, "OnnOOOOOO:read_response", &method, &max_header, &max_body,
            &response_type, &protocol_error, &too_large, &timeout_error,
            &header_timeout, &body_timeout)) {
        return NULL;
    }
    if (!PyUnicode_Check(method)) {
        PyErr_SetString(PyExc_TypeError, "response method must be str");
        return NULL;
    }
    if (max_header <= 0 || max_body < 0) {
        PyErr_SetString(PyExc_ValueError, "response limits are invalid");
        return NULL;
    }
    if (self->pending_kind != CS_READ_NONE) {
        int done = cs_future_done(self->pending_future);
        if (done < 0) return NULL;
        if (!done) {
            PyErr_SetString(PyExc_RuntimeError,
                            "a stream read is already awaited elsewhere");
            return NULL;
        }
        cs_clear_pending(self);
    }
    if (self->exc != NULL) {
        PyErr_SetRaisedException(Py_NewRef(self->exc));
        return NULL;
    }
    self->pending_kind = CS_READ_RESPONSE;
    self->response_method = Py_NewRef(method);
    self->response_type = Py_NewRef(response_type);
    self->response_protocol_error = Py_NewRef(protocol_error);
    self->response_too_large = Py_NewRef(too_large);
    self->response_timeout_error = Py_NewRef(timeout_error);
    self->response_header_timeout = Py_NewRef(header_timeout);
    self->response_body_timeout = Py_NewRef(body_timeout);
    self->response_max_header = max_header;
    self->response_max_body = max_body;
    self->response_phase = CS_RESPONSE_HEAD;
    self->response_timer_phase = -1;

    int would_block = 0;
    PyObject *ready = cs_take_response(self, &would_block);
    if (!would_block) {
        cs_clear_pending(self);
        return ready;
    }
    PyObject *future = PyObject_CallNoArgs(self->create_future);
    if (future == NULL) {
        cs_clear_pending(self);
        return NULL;
    }
    self->pending_future = Py_NewRef(future);
    /* Schedule the phase deadline through the same path used after a head
     * transition, keeping the timer attached to this response operation. */
    if (cs_try_satisfy(self) < 0) {
        Py_DECREF(future);
        cs_clear_pending(self);
        return NULL;
    }
    return future;
}

/* --- reader API --------------------------------------------------------- */

static PyObject *
cs_readuntil(WreathClientStream *self, PyObject *separator)
{
    Py_buffer view;
    if (PyObject_GetBuffer(separator, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    if (view.len < 1 || view.len > CS_SEP_MAX) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError,
                        "separator must be 1 to 16 bytes long");
        return NULL;
    }
    PyObject *future = cs_begin_read(
        self, CS_READ_UNTIL, view.buf, view.len, 0);
    PyBuffer_Release(&view);
    return future;
}

static PyObject *
cs_readexactly(WreathClientStream *self, PyObject *count_obj)
{
    Py_ssize_t count = PyLong_AsSsize_t(count_obj);
    if (count == -1 && PyErr_Occurred()) {
        return NULL;
    }
    if (count < 0) {
        PyErr_SetString(PyExc_ValueError, "readexactly size can not be less than zero");
        return NULL;
    }
    return cs_begin_read(self, CS_READ_EXACTLY, NULL, 0, count);
}

static PyObject *
cs_read(WreathClientStream *self, PyObject *count_obj)
{
    Py_ssize_t count = PyLong_AsSsize_t(count_obj);
    if (count == -1 && PyErr_Occurred()) {
        return NULL;
    }
    return cs_begin_read(self, CS_READ_SOME, NULL, 0, count);
}

static PyObject *
cs_at_eof(WreathClientStream *self, PyObject *Py_UNUSED(ignored))
{
    return PyBool_FromLong(self->eof && cs_avail(self) == 0);
}

static PyObject *
cs_buffer_get(PyObject *op, void *Py_UNUSED(closure))
{
    WreathClientStream *self = (WreathClientStream *)op;
    return PyBytes_FromStringAndSize(
        self->data + self->head, cs_avail(self));
}

/* --- protocol surface (Python path: stock loops and TLS) ---------------- */

static PyObject *
cs_connection_made(WreathClientStream *self, PyObject *transport)
{
    Py_XSETREF(self->transport, Py_NewRef(transport));
    self->connected = 1;
    if (self->closed_future == NULL) {
        self->closed_future = PyObject_CallNoArgs(self->create_future);
        if (self->closed_future == NULL) {
            return NULL;
        }
    }
    Py_RETURN_NONE;
}

static PyObject *
cs_connection_lost(WreathClientStream *self, PyObject *exc)
{
    self->connected = 0;
    self->write_paused = 0;
    self->eof = 1;
    if (exc != Py_None && self->exc == NULL) {
        self->exc = Py_NewRef(exc);
    }
    if (self->pending_kind != CS_READ_NONE && cs_try_satisfy(self) < 0) {
        return NULL;
    }
    if (self->drain_waiter != NULL) {
        int done = cs_future_done(self->drain_waiter);
        if (done < 0) {
            return NULL;
        }
        if (!done) {
            int status = self->exc != NULL
                ? cs_resolve(self->drain_waiter, self->exc, 1)
                : cs_resolve(self->drain_waiter, Py_None, 0);
            if (status < 0) {
                return NULL;
            }
        }
        Py_CLEAR(self->drain_waiter);
    }
    if (self->closed_future != NULL) {
        int done = cs_future_done(self->closed_future);
        if (done < 0) {
            return NULL;
        }
        if (!done && cs_resolve(self->closed_future, Py_None, 0) < 0) {
            return NULL;
        }
    }
    Py_CLEAR(self->transport);
    Py_RETURN_NONE;
}

static PyObject *
cs_eof_received(WreathClientStream *self, PyObject *Py_UNUSED(ignored))
{
    self->eof = 1;
    if (cs_try_satisfy(self) < 0) {
        return NULL;
    }
    Py_RETURN_TRUE;  /* keep the transport open, as asyncio streams do */
}

static PyObject *
cs_pause_writing(WreathClientStream *self, PyObject *Py_UNUSED(ignored))
{
    self->write_paused = 1;
    Py_RETURN_NONE;
}

static PyObject *
cs_resume_writing(WreathClientStream *self, PyObject *Py_UNUSED(ignored))
{
    self->write_paused = 0;
    if (self->drain_waiter != NULL) {
        int done = cs_future_done(self->drain_waiter);
        if (done < 0) {
            return NULL;
        }
        if (!done && cs_resolve(self->drain_waiter, Py_None, 0) < 0) {
            return NULL;
        }
        Py_CLEAR(self->drain_waiter);
    }
    Py_RETURN_NONE;
}

static PyObject *
cs_get_buffer(WreathClientStream *self, PyObject *Py_UNUSED(sizehint))
{
    if (self->offer_obj == NULL) {
        self->offer_obj = PyByteArray_FromStringAndSize(NULL, CS_OFFER_SIZE);
        if (self->offer_obj == NULL) {
            return NULL;
        }
    }
    return PyMemoryView_FromObject(self->offer_obj);
}

static PyObject *
cs_buffer_updated(WreathClientStream *self, PyObject *count_obj)
{
    Py_ssize_t count = PyLong_AsSsize_t(count_obj);
    if (count == -1 && PyErr_Occurred()) {
        return NULL;
    }
    if (self->offer_obj == NULL || count < 0 ||
        count > PyByteArray_GET_SIZE(self->offer_obj)) {
        PyErr_SetString(PyExc_ValueError, "invalid buffered receive count");
        return NULL;
    }
    if (count > 0) {
        if (cs_reserve(self, count) < 0) {
            return NULL;
        }
        memcpy(self->data + self->len,
               PyByteArray_AS_STRING(self->offer_obj), (size_t)count);
        self->len += count;
        if (cs_try_satisfy(self) < 0) {
            return NULL;
        }
    }
    Py_RETURN_NONE;
}

/* --- writer support ----------------------------------------------------- */

static PyObject *
cs_drain(WreathClientStream *self, PyObject *Py_UNUSED(ignored))
{
    if (!self->connected) {
        if (self->exc != NULL) {
            PyErr_SetRaisedException(Py_NewRef(self->exc));
        } else {
            PyErr_SetString(PyExc_ConnectionResetError, "Connection lost");
        }
        return NULL;
    }
    if (!self->write_paused) {
        /* Nothing to wait for: None tells the writer wrapper to skip the
         * await entirely -- no future allocation per request. */
        Py_RETURN_NONE;
    }
    if (self->drain_waiter == NULL) {
        self->drain_waiter = PyObject_CallNoArgs(self->create_future);
        if (self->drain_waiter == NULL) {
            return NULL;
        }
    }
    return Py_NewRef(self->drain_waiter);
}

static PyObject *
cs_wait_closed(WreathClientStream *self, PyObject *Py_UNUSED(ignored))
{
    if (self->closed_future == NULL) {
        /* Never connected: nothing to wait for. */
        PyObject *future = PyObject_CallNoArgs(self->create_future);
        if (future == NULL) {
            return NULL;
        }
        if (cs_resolve(future, Py_None, 0) < 0) {
            Py_DECREF(future);
            return NULL;
        }
        return future;
    }
    return Py_NewRef(self->closed_future);
}

/* --- stream-fusion C API ------------------------------------------------ */

static int
cs_stream_check(PyObject *protocol)
{
    return client_stream_type != NULL &&
           PyObject_TypeCheck(protocol, client_stream_type);
}

static int
cs_stream_acquire(PyObject *protocol, char **buffer, Py_ssize_t *capacity)
{
    if (!cs_stream_check(protocol)) {
        PyErr_SetString(PyExc_TypeError, "expected Http1ClientStream");
        return -1;
    }
    WreathClientStream *self = (WreathClientStream *)protocol;
    if (self->offer_live) {
        PyErr_SetString(PyExc_RuntimeError,
                        "acquire_read_buffer() while a read offer is live");
        return -1;
    }
    if (cs_reserve(self, 16384) < 0) {
        return -1;
    }
    self->offer_live = 1;
    *buffer = self->data + self->len;
    *capacity = self->cap - self->len;
    return 0;
}

static int
cs_stream_commit(PyObject *protocol, Py_ssize_t nbytes)
{
    if (!cs_stream_check(protocol)) {
        PyErr_SetString(PyExc_TypeError, "expected Http1ClientStream");
        return -1;
    }
    WreathClientStream *self = (WreathClientStream *)protocol;
    if (!self->offer_live) {
        PyErr_SetString(PyExc_RuntimeError,
                        "commit_read() without a live read offer");
        return -1;
    }
    self->offer_live = 0;
    if (nbytes < 0 || nbytes > self->cap - self->len) {
        PyErr_SetString(PyExc_ValueError, "invalid fused receive count");
        return -1;
    }
    self->len += nbytes;
    if (nbytes == 0) {
        return 0;
    }
    return cs_try_satisfy(self);
}

static int
cs_stream_feed(PyObject *protocol, const char *data, Py_ssize_t size)
{
    if (!cs_stream_check(protocol)) {
        PyErr_SetString(PyExc_TypeError, "expected Http1ClientStream");
        return -1;
    }
    if (size < 0) {
        PyErr_SetString(PyExc_ValueError, "negative external read size");
        return -1;
    }
    WreathClientStream *self = (WreathClientStream *)protocol;
    if (self->offer_live) {
        PyErr_SetString(PyExc_RuntimeError,
                        "external read while a read offer is live");
        return -1;
    }
    if (size == 0) {
        return 0;
    }
    if (cs_reserve(self, size) < 0) {
        return -1;
    }
    memcpy(self->data + self->len, data, (size_t)size);
    self->len += size;
    return cs_try_satisfy(self);
}

static const WreathStreamCAPI cs_stream_capi = {
    WREATH_STREAM_CAPI_VERSION,
    cs_stream_check,
    cs_stream_acquire,
    cs_stream_commit,
    cs_stream_feed,
};

/* --- lifecycle ---------------------------------------------------------- */

static PyObject *
cs_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    Py_ssize_t limit = 65536;
    static char *names[] = {"limit", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|n", names, &limit)) {
        return NULL;
    }
    if (limit <= 0) {
        PyErr_SetString(PyExc_ValueError, "limit must be positive");
        return NULL;
    }
    PyObject *asyncio_module = PyImport_ImportModule("asyncio");
    if (asyncio_module == NULL) {
        return NULL;
    }
    PyObject *loop = PyObject_CallMethod(
        asyncio_module, "get_running_loop", NULL);
    Py_DECREF(asyncio_module);
    if (loop == NULL) {
        return NULL;
    }
    PyObject *create_future = PyObject_GetAttrString(loop, "create_future");
    PyObject *call_later = PyObject_GetAttrString(loop, "call_later");
    Py_DECREF(loop);
    if (create_future == NULL || call_later == NULL) {
        Py_XDECREF(create_future);
        Py_XDECREF(call_later);
        return NULL;
    }
    WreathClientStream *self = (WreathClientStream *)type->tp_alloc(type, 0);
    if (self == NULL) {
        Py_DECREF(create_future);
        Py_DECREF(call_later);
        return NULL;
    }
    self->create_future = create_future;
    self->call_later = call_later;
    self->limit = limit;
    self->response_timer_phase = -1;
    return (PyObject *)self;
}

static int
cs_traverse(PyObject *op, visitproc visit, void *arg)
{
    WreathClientStream *self = (WreathClientStream *)op;
    Py_VISIT(Py_TYPE(op));
    Py_VISIT(self->exc);
    Py_VISIT(self->transport);
    Py_VISIT(self->create_future);
    Py_VISIT(self->call_later);
    Py_VISIT(self->drain_waiter);
    Py_VISIT(self->closed_future);
    Py_VISIT(self->offer_obj);
    Py_VISIT(self->pending_future);
    Py_VISIT(self->response_method);
    Py_VISIT(self->response_type);
    Py_VISIT(self->response_protocol_error);
    Py_VISIT(self->response_too_large);
    Py_VISIT(self->response_timeout_error);
    Py_VISIT(self->response_head);
    Py_VISIT(self->response_body);
    Py_VISIT(self->response_header_timeout);
    Py_VISIT(self->response_body_timeout);
    Py_VISIT(self->response_timer);
    return 0;
}

static int
cs_clear(PyObject *op)
{
    WreathClientStream *self = (WreathClientStream *)op;
    Py_CLEAR(self->exc);
    Py_CLEAR(self->transport);
    Py_CLEAR(self->create_future);
    Py_CLEAR(self->call_later);
    Py_CLEAR(self->drain_waiter);
    Py_CLEAR(self->closed_future);
    Py_CLEAR(self->offer_obj);
    Py_CLEAR(self->pending_future);
    cs_clear_response(self);
    return 0;
}

static void
cs_dealloc(PyObject *op)
{
    WreathClientStream *self = (WreathClientStream *)op;
    PyTypeObject *type = Py_TYPE(op);
    PyObject_GC_UnTrack(op);
    cs_clear(op);
    PyMem_Free(self->data);
    self->data = NULL;
    type->tp_free(op);
    Py_DECREF(type);
}

static PyMethodDef cs_methods[] = {
    {"connection_made", (PyCFunction)cs_connection_made, METH_O, NULL},
    {"connection_lost", (PyCFunction)cs_connection_lost, METH_O, NULL},
    {"eof_received", (PyCFunction)cs_eof_received, METH_NOARGS, NULL},
    {"pause_writing", (PyCFunction)cs_pause_writing, METH_NOARGS, NULL},
    {"resume_writing", (PyCFunction)cs_resume_writing, METH_NOARGS, NULL},
    {"get_buffer", (PyCFunction)cs_get_buffer, METH_O, NULL},
    {"buffer_updated", (PyCFunction)cs_buffer_updated, METH_O, NULL},
    {"readuntil", (PyCFunction)cs_readuntil, METH_O, NULL},
    {"readexactly", (PyCFunction)cs_readexactly, METH_O, NULL},
    {"read", (PyCFunction)cs_read, METH_O, NULL},
    {"read_response", (PyCFunction)cs_read_response, METH_VARARGS, NULL},
    {"_response_timeout", (PyCFunction)cs_response_timeout, METH_NOARGS, NULL},
    {"at_eof", (PyCFunction)cs_at_eof, METH_NOARGS, NULL},
    {"_drain", (PyCFunction)cs_drain, METH_NOARGS, NULL},
    {"_wait_closed", (PyCFunction)cs_wait_closed, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef cs_getset[] = {
    {"_buffer", cs_buffer_get, NULL,
     "unconsumed buffered bytes (framing-overrun detection)", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyType_Slot cs_slots[] = {
    {Py_tp_new, cs_new},
    {Py_tp_dealloc, cs_dealloc},
    {Py_tp_traverse, cs_traverse},
    {Py_tp_clear, cs_clear},
    {Py_tp_methods, cs_methods},
    {Py_tp_getset, cs_getset},
    {0, NULL},
};

static PyType_Spec cs_spec = {
    .name = "wreath._native._client.Http1ClientStream",
    .basicsize = sizeof(WreathClientStream),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = cs_slots,
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
    /* The transport-facing stream must be an asyncio.BufferedProtocol so
     * every transport (stock loops, TLS) selects buffered delivery. */
    /* native-lint: allow NC004 -- one-time module initialization */
    PyObject *asyncio_module = PyImport_ImportModule("asyncio");
    if (asyncio_module == NULL) return -1;
    PyObject *base = PyObject_GetAttrString(asyncio_module, "BufferedProtocol");
    cs_incomplete_read_error = PyObject_GetAttrString(
        asyncio_module, "IncompleteReadError");
    cs_limit_overrun_error = PyObject_GetAttrString(
        asyncio_module, "LimitOverrunError");
    Py_DECREF(asyncio_module);
    cs_str_done = PyUnicode_InternFromString("done");
    cs_str_set_result = PyUnicode_InternFromString("set_result");
    cs_str_set_exception = PyUnicode_InternFromString("set_exception");
    cs_str_cancel = PyUnicode_InternFromString("cancel");
    if (base == NULL || cs_incomplete_read_error == NULL ||
        cs_limit_overrun_error == NULL || cs_str_done == NULL ||
        cs_str_set_result == NULL || cs_str_set_exception == NULL ||
        cs_str_cancel == NULL) {
        Py_XDECREF(base);
        return -1;
    }
    PyObject *bases = PyTuple_Pack(1, base);
    Py_DECREF(base);
    if (bases == NULL) return -1;
    client_stream_type = (PyTypeObject *)PyType_FromSpecWithBases(
        &cs_spec, bases);
    Py_DECREF(bases);
    if (client_stream_type == NULL) return -1;
    if (PyModule_AddObjectRef(
            module, "Http1ClientStream", (PyObject *)client_stream_type) < 0) {
        return -1;
    }
    PyObject *capsule = PyCapsule_New(
        (void *)&cs_stream_capi,
        "wreath._native._client._STREAM_C_API", NULL);
    if (capsule == NULL ||
        PyModule_AddObject(module, "_STREAM_C_API", capsule) < 0) {
        Py_XDECREF(capsule);
        return -1;
    }
    return 0;
}
