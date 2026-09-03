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
#include <stdatomic.h>
#include <structmember.h>

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
    int over_ssl;              /* cached once from transport extra info */
    int offer_live;             /* fused offer outstanding: no realloc/move */
    int write_paused;
    PyObject *exc;              /* stored connection exception, or NULL */
    PyObject *transport;
    const WreathTransportCAPI *transport_capi;
    PyObject *loop;
    PyObject *create_future;    /* bound loop.create_future */
    PyObject *call_soon;        /* bound metal/asyncio ready enqueue */
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
    PyObject *response_request_timeout_error;
    PyObject *response_headers;
    PyObject *response_reason;
    PyObject *response_body;
    PyObject *response_header_timeout;
    PyObject *response_body_timeout;
    PyObject *response_timer;
    PyObject *response_timeout_callback;
    PyObject *response_layout_type;
    PyObject *response_version_10;
    PyObject *response_version_11;
    Py_ssize_t response_status_offset;
    Py_ssize_t response_headers_offset;
    Py_ssize_t response_body_offset;
    Py_ssize_t response_version_offset;
    Py_ssize_t response_reason_offset;
    Py_ssize_t response_max_header;
    Py_ssize_t response_max_body;
    Py_ssize_t response_length;
    Py_ssize_t response_trailers;
    int response_minor;
    int response_status;
    int response_reusable;
    int response_layout_fast;
    int response_direct_completion;
    int response_total_enabled;
    int response_timer_is_total;
    double response_total_deadline;
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

static int fast_resolve_offset(
    PyObject *type, const char *name, Py_ssize_t *offset);

typedef struct {
    Py_ssize_t active;
    Py_ssize_t authority_bytes;
    Py_ssize_t base_path;
    Py_ssize_t condition;
    Py_ssize_t idle;
    Py_ssize_t limits;
    Py_ssize_t native_counters;
    Py_ssize_t open;
    Py_ssize_t request_plans;
    Py_ssize_t request_last_plan;
    Py_ssize_t started;
    Py_ssize_t timeout;
    Py_ssize_t waiters;
} ClientOffsets;

typedef struct {
    Py_ssize_t reader;
} ConnectionOffsets;

typedef struct {
    Py_ssize_t max_keepalive_connections;
    Py_ssize_t max_request_header_bytes;
    Py_ssize_t max_response_header_bytes;
    Py_ssize_t max_response_bytes;
} LimitOffsets;

typedef struct {
    Py_ssize_t response_body;
    Py_ssize_t response_headers;
    Py_ssize_t total;
} TimeoutOffsets;

static ClientOffsets fast_client_off;
static ConnectionOffsets fast_connection_off;
static LimitOffsets fast_limit_off;
static TimeoutOffsets fast_timeout_off;
static PyTypeObject *fast_client_type = NULL;
static PyTypeObject *fast_connection_type = NULL;
static PyTypeObject *fast_limit_type = NULL;
static PyTypeObject *fast_timeout_type = NULL;

#define FAST_SLOT(obj, offset) (*(PyObject **)((char *)(obj) + (offset)))

#define HTTP_CLIENT_COUNTERS_CAPSULE "wreath.http_client.counters"

typedef struct {
    _Atomic uint64_t requests;
    _Atomic uint64_t reused;
} HttpClientCounters;

static void
http_client_counters_destroy(PyObject *capsule)
{
    void *pointer = PyCapsule_GetPointer(
        capsule, HTTP_CLIENT_COUNTERS_CAPSULE);
    if (pointer == NULL) {
        PyErr_Clear();
        return;
    }
    PyMem_Free(pointer);
}

PyObject *
wreath_http_client_counters_new(PyObject *Py_UNUSED(self),
                                PyObject *Py_UNUSED(ignored))
{
    HttpClientCounters *counters = PyMem_Calloc(1, sizeof(*counters));
    if (counters == NULL) return PyErr_NoMemory();
    PyObject *capsule = PyCapsule_New(
        counters, HTTP_CLIENT_COUNTERS_CAPSULE,
        http_client_counters_destroy);
    if (capsule == NULL) PyMem_Free(counters);
    return capsule;
}

PyObject *
wreath_http_client_counters_snapshot(PyObject *Py_UNUSED(self),
                                     PyObject *capsule)
{
    HttpClientCounters *counters = PyCapsule_GetPointer(
        capsule, HTTP_CLIENT_COUNTERS_CAPSULE);
    if (counters == NULL) return NULL;
    PyObject *requests = PyLong_FromUnsignedLongLong(atomic_load_explicit(
        &counters->requests, memory_order_relaxed));
    PyObject *reused = PyLong_FromUnsignedLongLong(atomic_load_explicit(
        &counters->reused, memory_order_relaxed));
    if (requests == NULL || reused == NULL) {
        Py_XDECREF(requests);
        Py_XDECREF(reused);
        return NULL;
    }
    PyObject *result = PyTuple_Pack(2, requests, reused);
    Py_DECREF(requests);
    Py_DECREF(reused);
    return result;
}

static HttpClientCounters *
fast_client_counters(PyObject *client)
{
    return PyCapsule_GetPointer(
        FAST_SLOT(client, fast_client_off.native_counters),
        HTTP_CLIENT_COUNTERS_CAPSULE);
}

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

static double
cs_monotonic(void)
{
    PyTime_t now;
    if (PyTime_Monotonic(&now) < 0) {
        return -1.0;
    }
    return PyTime_AsSecondsDouble(now);
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
    Py_CLEAR(self->response_request_timeout_error);
    Py_CLEAR(self->response_headers);
    Py_CLEAR(self->response_reason);
    Py_CLEAR(self->response_body);
    Py_CLEAR(self->response_header_timeout);
    Py_CLEAR(self->response_body_timeout);
    self->response_max_header = 0;
    self->response_max_body = 0;
    self->response_length = 0;
    self->response_trailers = 0;
    self->response_minor = 0;
    self->response_status = 0;
    self->response_phase = CS_RESPONSE_HEAD;
    self->response_framed = 0;
    self->response_total_enabled = 0;
    self->response_timer_is_total = 0;
    self->response_total_deadline = 0.0;
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
    PyObject *status = PyLong_FromLong(self->response_status);
    PyObject *version = self->response_minor == 0
        ? self->response_version_10 : self->response_version_11;
    PyObject *response = NULL;
    PyObject *result = NULL;

    if (status == NULL) goto done;
    if (self->response_layout_fast) {
        PyTypeObject *type = (PyTypeObject *)self->response_type;
        response = type->tp_alloc(type, 0);
        if (response != NULL) {
            FAST_SLOT(response, self->response_status_offset) = Py_NewRef(status);
            FAST_SLOT(response, self->response_headers_offset) =
                Py_NewRef(self->response_headers);
            FAST_SLOT(response, self->response_body_offset) = Py_NewRef(body);
            FAST_SLOT(response, self->response_version_offset) = Py_NewRef(version);
            FAST_SLOT(response, self->response_reason_offset) =
                Py_NewRef(self->response_reason);
        }
    } else {
        response = PyObject_CallFunctionObjArgs(
            self->response_type, status, self->response_headers, body, version,
            self->response_reason, NULL);
    }
    if (response == NULL) goto done;
    result = self->response_direct_completion
        ? Py_NewRef(response)
        : PyTuple_Pack(
            2, response, self->response_reusable ? Py_True : Py_False);

done:
    Py_XDECREF(status);
    Py_XDECREF(response);
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
            WreathHttpResponseHead parsed;
            int parsed_status = wreath_http_parse_response_parts(
                (const uint8_t *)self->data + self->head, end,
                self->response_method, &parsed);
            if (parsed_status < 0) return NULL;
            if (parsed_status == 0) {
                cs_raise_response(
                    self->response_protocol_error,
                    "complete response head was not parsed");
                return NULL;
            }
            if (cs_response_consume(self, parsed.consumed) < 0) {
                wreath_http_response_head_clear(&parsed);
                return NULL;
            }
            if (parsed.status == 101) {
                wreath_http_response_head_clear(&parsed);
                cs_raise_response(
                    self->response_protocol_error,
                    "protocol switching is not supported");
                return NULL;
            }
            if (parsed.status < 200) {
                wreath_http_response_head_clear(&parsed);
                continue;
            }
            self->response_headers = parsed.headers;
            self->response_reason = parsed.reason;
            parsed.headers = NULL;
            parsed.reason = NULL;
            self->response_minor = parsed.minor;
            self->response_status = parsed.status;
            self->response_reusable = parsed.reusable;
            if (parsed.framing == 0) {
                self->response_framed = 1;
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
            if (parsed.framing == 2) {
                self->response_length = parsed.content_length;
                if (self->response_length > self->response_max_body) {
                    cs_raise_response(
                        self->response_too_large,
                        "response body exceeds configured limit");
                    return NULL;
                }
                self->response_phase = CS_RESPONSE_LENGTH;
                self->response_framed = 1;
            } else if (parsed.framing == 1) {
                self->response_phase = CS_RESPONSE_CHUNK_SIZE;
                self->response_framed = 1;
                self->response_body = PyByteArray_FromStringAndSize(NULL, 0);
                if (self->response_body == NULL) {
                    return NULL;
                }
            } else {
                self->response_phase = CS_RESPONSE_CLOSE;
                self->response_framed = 0;
            }
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
            PyObject *delay = self->response_phase == CS_RESPONSE_HEAD
                ? self->response_header_timeout : self->response_body_timeout;
            PyObject *scheduled_delay = Py_NewRef(delay);
            self->response_timer_is_total = 0;
            if (scheduled_delay != NULL && self->response_total_enabled) {
                double remaining = self->response_total_deadline - cs_monotonic();
                double phase = PyFloat_AsDouble(delay);
                if (remaining < 0.0 && PyErr_Occurred()) {
                    Py_CLEAR(scheduled_delay);
                } else if (phase == -1.0 && PyErr_Occurred()) {
                    Py_CLEAR(scheduled_delay);
                } else if (remaining <= phase) {
                    Py_SETREF(
                        scheduled_delay,
                        PyFloat_FromDouble(remaining > 0.0 ? remaining : 0.0));
                    self->response_timer_is_total = 1;
                }
            }
            self->response_timer = scheduled_delay != NULL
                ? PyObject_CallFunctionObjArgs(
                      self->call_later, scheduled_delay,
                      self->response_timeout_callback, NULL)
                : NULL;
            Py_XDECREF(scheduled_delay);
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
    PyObject *error_type = self->response_timeout_error;
    if (self->response_timer_is_total) {
        message = "outbound request exceeded total timeout";
        error_type = self->response_request_timeout_error;
    }
    PyObject *exc = PyObject_CallFunction(
        error_type, "s", message);
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
cs_start_response(WreathClientStream *self, PyObject *method,
                  Py_ssize_t max_header, Py_ssize_t max_body,
                  PyObject *response_type, PyObject *protocol_error,
                  PyObject *too_large, PyObject *timeout_error,
                  PyObject *header_timeout, PyObject *body_timeout,
                  PyObject *completion, PyObject *request_timeout,
                  PyObject *total_timeout)
{
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
    if (self->response_layout_type != response_type) {
        Py_CLEAR(self->response_layout_type);
        self->response_layout_fast = 0;
        if (PyType_Check(response_type) &&
            fast_resolve_offset(
                response_type, "status", &self->response_status_offset) == 0 &&
            fast_resolve_offset(
                response_type, "headers", &self->response_headers_offset) == 0 &&
            fast_resolve_offset(
                response_type, "body", &self->response_body_offset) == 0 &&
            fast_resolve_offset(
                response_type, "http_version", &self->response_version_offset) == 0 &&
            fast_resolve_offset(
                response_type, "reason", &self->response_reason_offset) == 0) {
            self->response_layout_type = Py_NewRef(response_type);
            self->response_layout_fast = 1;
        } else {
            /* A caller-supplied response class remains supported through its
             * constructor.  Only the exact slotted boundary earns direct fill. */
            PyErr_Clear();
        }
    }
    self->pending_kind = CS_READ_RESPONSE;
    self->response_direct_completion = completion != NULL;
    self->response_method = Py_NewRef(method);
    self->response_type = Py_NewRef(response_type);
    self->response_protocol_error = Py_NewRef(protocol_error);
    self->response_too_large = Py_NewRef(too_large);
    self->response_timeout_error = Py_NewRef(timeout_error);
    self->response_request_timeout_error = Py_NewRef(request_timeout);
    self->response_header_timeout = Py_NewRef(header_timeout);
    self->response_body_timeout = Py_NewRef(body_timeout);
    self->response_max_header = max_header;
    self->response_max_body = max_body;
    self->response_phase = CS_RESPONSE_HEAD;
    self->response_timer_phase = -1;
    if (total_timeout != Py_None) {
        double total = PyFloat_AsDouble(total_timeout);
        if (total == -1.0 && PyErr_Occurred()) {
            cs_clear_pending(self);
            return NULL;
        }
        double now = cs_monotonic();
        if (now < 0.0 && PyErr_Occurred()) {
            cs_clear_pending(self);
            return NULL;
        }
        self->response_total_enabled = 1;
        self->response_total_deadline = now + total;
    }

    int would_block = 0;
    PyObject *ready = cs_take_response(self, &would_block);
    if (!would_block) {
        cs_clear_pending(self);
        return ready;
    }
    PyObject *future = completion != NULL
        ? Py_NewRef(completion) : PyObject_CallNoArgs(self->create_future);
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
    return cs_start_response(
        self, method, max_header, max_body, response_type, protocol_error,
        too_large, timeout_error, header_timeout, body_timeout, NULL,
        Py_None, Py_None);
}

/* ----------------------------------------------------------------------- */
/* Steady-state buffered request transaction.                              */

typedef struct {
    PyObject_HEAD
    PyObject *client;
    PyObject *connection;
    PyObject *stream;
    PyObject *loop;
    PyObject *result;
    PyObject *error;
    PyObject *callback;
    PyObject *callback_context;
    PyObject *callbacks;
    PyObject *blocking;
    PyObject *transport_error;
    PyObject *request_timeout;
    PyObject *total_timer;
    int done;
    int cancelled;
    int yielded;
    int released;
} ClientRequestAwait;

static PyTypeObject *client_request_await_type = NULL;
static PyObject *client_cancelled_error = NULL;
static PyObject *client_str_close = NULL;
static PyObject *client_str_context = NULL;
static PyObject *client_str_is_closing = NULL;
static PyObject *client_str_write = NULL;
static PyObject *client_str_upper = NULL;
static PyObject *client_context_names = NULL;

static int
fast_counter_add(PyObject *owner, Py_ssize_t offset, Py_ssize_t delta)
{
    PyObject *current = FAST_SLOT(owner, offset);
    Py_ssize_t value = PyLong_AsSsize_t(current);
    if (value == -1 && PyErr_Occurred()) return -1;
    PyObject *updated = PyLong_FromSsize_t(value + delta);
    if (updated == NULL) return -1;
    FAST_SLOT(owner, offset) = updated;
    Py_DECREF(current);
    return 0;
}

static int
fast_wake_condition(PyObject *condition, int wake_all)
{
    PyObject *waiters = PyObject_GetAttrString(condition, "_waiters");
    PyObject *iterator = waiters != NULL ? PyObject_GetIter(waiters) : NULL;
    Py_XDECREF(waiters);
    if (iterator == NULL) return -1;
    PyObject *waiter;
    while ((waiter = PyIter_Next(iterator)) != NULL) {
        int done = cs_future_done(waiter);
        if (done < 0) {
            Py_DECREF(waiter);
            Py_DECREF(iterator);
            return -1;
        }
        if (!done && cs_resolve(waiter, Py_None, 0) < 0) {
            Py_DECREF(waiter);
            Py_DECREF(iterator);
            return -1;
        }
        Py_DECREF(waiter);
        if (!done && !wake_all) break;
    }
    Py_DECREF(iterator);
    return PyErr_Occurred() ? -1 : 0;
}

static int
fast_release_connection(ClientRequestAwait *self, int reusable)
{
    if (self->released) return 0;
    self->released = 1;
    PyObject *client = self->client;
    PyObject *connection = self->connection;
    PyObject *active = FAST_SLOT(client, fast_client_off.active);
    PyObject *idle = FAST_SLOT(client, fast_client_off.idle);
    PyObject *limits = FAST_SLOT(client, fast_client_off.limits);
    PyObject *started = FAST_SLOT(client, fast_client_off.started);
    PyObject *waiters_obj = FAST_SLOT(client, fast_client_off.waiters);
    Py_ssize_t waiters = PyLong_AsSsize_t(waiters_obj);
    if (waiters == -1 && PyErr_Occurred()) return -1;
    if (PySet_Discard(active, connection) < 0) return -1;

    WreathClientStream *stream = (WreathClientStream *)self->stream;
    Py_ssize_t max_idle = PyLong_AsSsize_t(
        FAST_SLOT(limits, fast_limit_off.max_keepalive_connections));
    if (max_idle == -1 && PyErr_Occurred()) return -1;
    int keep = reusable && started == Py_True && waiters == 0 &&
        stream->connected && !stream->eof &&
        PyList_GET_SIZE(idle) < max_idle;
    if (keep) {
        if (PyList_Append(idle, connection) < 0) return -1;
    } else {
        if (fast_counter_add(client, fast_client_off.open, -1) < 0) return -1;
        if (stream->transport != NULL) {
            PyObject *closed = PyObject_CallMethodNoArgs(
                stream->transport, client_str_close);
            if (closed == NULL) return -1;
            Py_DECREF(closed);
        }
    }
    if (waiters > 0 || started != Py_True) {
        if (fast_wake_condition(
                FAST_SLOT(client, fast_client_off.condition),
                started != Py_True) < 0) return -1;
    }
    return 0;
}

static int
client_request_schedule_one(ClientRequestAwait *self, PyObject *callback,
                            PyObject *context)
{
    WreathClientStream *stream = (WreathClientStream *)self->stream;
    PyObject *values[3] = {callback, (PyObject *)self, context};
    PyObject *scheduled = PyObject_Vectorcall(
        stream->call_soon, values, 2,
        context == Py_None ? NULL : client_context_names);
    if (scheduled == NULL) return -1;
    Py_DECREF(scheduled);
    return 0;
}

static int
client_request_schedule_callbacks(ClientRequestAwait *self)
{
    if (self->callback != NULL) {
        if (client_request_schedule_one(
                self, self->callback, self->callback_context) < 0) return -1;
        Py_CLEAR(self->callback);
        Py_CLEAR(self->callback_context);
    }
    if (self->callbacks == NULL) return 0;
    Py_ssize_t count = PyList_GET_SIZE(self->callbacks);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *entry = PyList_GET_ITEM(self->callbacks, i);
        if (client_request_schedule_one(
                self, PyTuple_GET_ITEM(entry, 0),
                PyTuple_GET_ITEM(entry, 1)) < 0) return -1;
    }
    return PyList_SetSlice(self->callbacks, 0, count, NULL);
}

static int
client_request_store_error(ClientRequestAwait *self, PyObject *error)
{
    PyObject *stored = error;
    if (self->transport_error != NULL &&
        (PyErr_GivenExceptionMatches(error, PyExc_ConnectionError) ||
         PyErr_GivenExceptionMatches(error, PyExc_OSError))) {
        stored = PyObject_CallFunction(
            self->transport_error, "s", "connection failed during HTTP exchange");
        if (stored == NULL) return -1;
        PyException_SetCause(stored, Py_NewRef(error));
    } else {
        Py_INCREF(stored);
    }
    Py_XSETREF(self->error, stored);
    return 0;
}

static void
client_request_cancel_total(ClientRequestAwait *self)
{
    if (self->total_timer == NULL) return;
    PyObject *cancelled = PyObject_CallMethodNoArgs(
        self->total_timer, cs_str_cancel);
    if (cancelled == NULL) PyErr_Clear();
    else Py_DECREF(cancelled);
    Py_CLEAR(self->total_timer);
}

static void
client_request_abort_response(ClientRequestAwait *self)
{
    WreathClientStream *stream = (WreathClientStream *)self->stream;
    if (stream != NULL && stream->pending_future == (PyObject *)self) {
        cs_clear_pending(stream);
    }
}

static PyObject *
client_request_set_result(ClientRequestAwait *self, PyObject *value)
{
    if (self->done) {
        PyErr_SetString(PyExc_RuntimeError, "request result is already set");
        return NULL;
    }
    PyObject *response;
    int reusable;
    if (PyTuple_CheckExact(value) && PyTuple_GET_SIZE(value) == 2) {
        response = PyTuple_GET_ITEM(value, 0);
        reusable = PyObject_IsTrue(PyTuple_GET_ITEM(value, 1));
    } else {
        WreathClientStream *stream = (WreathClientStream *)self->stream;
        response = value;
        reusable = stream->response_reusable;
    }
    if (reusable < 0 || fast_release_connection(self, reusable) < 0) return NULL;
    client_request_cancel_total(self);
    self->result = Py_NewRef(response);
    self->done = 1;
    if (client_request_schedule_callbacks(self) < 0) return NULL;
    Py_RETURN_NONE;
}

static PyObject *
client_request_set_exception(ClientRequestAwait *self, PyObject *error)
{
    if (self->done) {
        PyErr_SetString(PyExc_RuntimeError, "request result is already set");
        return NULL;
    }
    PyObject *instance;
    if (PyExceptionClass_Check(error)) {
        instance = PyObject_CallNoArgs(error);
        if (instance == NULL) return NULL;
    } else if (PyExceptionInstance_Check(error)) {
        instance = Py_NewRef(error);
    } else {
        PyErr_SetString(PyExc_TypeError, "exception must derive from BaseException");
        return NULL;
    }
    if (fast_release_connection(self, 0) < 0) {
        Py_DECREF(instance);
        return NULL;
    }
    client_request_cancel_total(self);
    if (client_request_store_error(self, instance) < 0) {
        Py_DECREF(instance);
        return NULL;
    }
    Py_DECREF(instance);
    self->done = 1;
    if (client_request_schedule_callbacks(self) < 0) return NULL;
    Py_RETURN_NONE;
}

static PyObject *
client_request_done(ClientRequestAwait *self, PyObject *unused)
{
    (void)unused;
    return PyBool_FromLong(self->done);
}

static PyObject *
client_request_cancelled(ClientRequestAwait *self, PyObject *unused)
{
    (void)unused;
    return PyBool_FromLong(self->cancelled);
}

static PyObject *
client_request_get_loop(ClientRequestAwait *self, PyObject *unused)
{
    (void)unused;
    return Py_NewRef(self->loop);
}

static PyObject *
client_request_result(ClientRequestAwait *self, PyObject *unused)
{
    (void)unused;
    if (!self->done) {
        PyErr_SetString(PyExc_RuntimeError, "request result is not ready");
        return NULL;
    }
    if (self->error != NULL) {
        PyErr_SetRaisedException(Py_NewRef(self->error));
        return NULL;
    }
    return Py_XNewRef(self->result != NULL ? self->result : Py_None);
}

static PyObject *
client_request_exception(ClientRequestAwait *self, PyObject *unused)
{
    (void)unused;
    if (!self->done) {
        PyErr_SetString(PyExc_RuntimeError, "request result is not ready");
        return NULL;
    }
    if (self->cancelled && self->error != NULL) {
        PyErr_SetRaisedException(Py_NewRef(self->error));
        return NULL;
    }
    return Py_XNewRef(self->error != NULL ? self->error : Py_None);
}

static PyObject *
client_request_add_done_callback(
    ClientRequestAwait *self, PyObject *const *args, Py_ssize_t nargs,
    PyObject *kwnames)
{
    if (nargs != 1) {
        PyErr_SetString(PyExc_TypeError, "add_done_callback expects one callback");
        return NULL;
    }
    PyObject *context = NULL;
    if (kwnames != NULL && PyTuple_GET_SIZE(kwnames) != 0) {
        if (PyTuple_GET_SIZE(kwnames) != 1 ||
            PyUnicode_CompareWithASCIIString(
                PyTuple_GET_ITEM(kwnames, 0), "context") != 0) {
            PyErr_SetString(PyExc_TypeError, "unexpected callback keyword");
            return NULL;
        }
        context = args[nargs];
    }
    if (context == NULL || context == Py_None) {
        context = PyContext_CopyCurrent();
        if (context == NULL) return NULL;
    } else {
        Py_INCREF(context);
    }
    if (self->done) {
        int status = client_request_schedule_one(self, args[0], context);
        Py_DECREF(context);
        if (status < 0) return NULL;
    } else if (self->callback == NULL) {
        self->callback = Py_NewRef(args[0]);
        self->callback_context = context;
    } else {
        if (self->callbacks == NULL) {
            self->callbacks = PyList_New(0);
            if (self->callbacks == NULL) {
                Py_DECREF(context);
                return NULL;
            }
        }
        PyObject *entry = PyTuple_Pack(2, args[0], context);
        Py_DECREF(context);
        if (entry == NULL) return NULL;
        int status = PyList_Append(self->callbacks, entry);
        Py_DECREF(entry);
        if (status < 0) return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
client_request_remove_done_callback(ClientRequestAwait *self, PyObject *callback)
{
    Py_ssize_t removed = 0;
    if (self->callback != NULL) {
        int same = PyObject_RichCompareBool(self->callback, callback, Py_EQ);
        if (same < 0) return NULL;
        if (same) {
            Py_CLEAR(self->callback);
            Py_CLEAR(self->callback_context);
            removed++;
        }
    }
    if (self->callbacks != NULL) {
        for (Py_ssize_t i = PyList_GET_SIZE(self->callbacks); i > 0; i--) {
            PyObject *entry = PyList_GET_ITEM(self->callbacks, i - 1);
            int same = PyObject_RichCompareBool(
                PyTuple_GET_ITEM(entry, 0), callback, Py_EQ);
            if (same < 0) return NULL;
            if (same && PySequence_DelItem(self->callbacks, i - 1) < 0)
                return NULL;
            removed += same;
        }
    }
    return PyLong_FromSsize_t(removed);
}

static PyObject *
client_request_cancel(
    ClientRequestAwait *self, PyObject *const *args, Py_ssize_t nargs,
    PyObject *kwnames)
{
    PyObject *message = Py_None;
    Py_ssize_t nkw = kwnames == NULL ? 0 : PyTuple_GET_SIZE(kwnames);
    if (nargs + nkw > 1) {
        PyErr_SetString(PyExc_TypeError, "cancel accepts at most one message");
        return NULL;
    }
    if (nargs == 1) message = args[0];
    if (nkw == 1) {
        if (PyUnicode_CompareWithASCIIString(
                PyTuple_GET_ITEM(kwnames, 0), "msg") != 0) {
            PyErr_SetString(PyExc_TypeError, "unexpected cancel keyword");
            return NULL;
        }
        message = args[nargs];
    }
    if (self->done) Py_RETURN_FALSE;
    PyObject *error = message == Py_None
        ? PyObject_CallNoArgs(client_cancelled_error)
        : PyObject_CallOneArg(client_cancelled_error, message);
    if (error == NULL) return NULL;
    client_request_abort_response(self);
    if (fast_release_connection(self, 0) < 0) {
        Py_DECREF(error);
        return NULL;
    }
    self->error = error;
    self->done = 1;
    self->cancelled = 1;
    client_request_cancel_total(self);
    if (client_request_schedule_callbacks(self) < 0) return NULL;
    Py_RETURN_TRUE;
}

static PyObject *
client_request_await(ClientRequestAwait *self)
{
    return Py_NewRef((PyObject *)self);
}

static PyObject *
client_request_iternext(ClientRequestAwait *self)
{
    if (!self->done) {
        Py_SETREF(self->blocking, Py_NewRef(Py_True));
        self->yielded = 1;
        return Py_NewRef((PyObject *)self);
    }
    if (self->error != NULL) {
        PyErr_SetRaisedException(Py_NewRef(self->error));
        return NULL;
    }
    PyObject *value = self->result != NULL ? self->result : Py_None;
    PyObject *stop = PyObject_CallOneArg(PyExc_StopIteration, value);
    if (stop == NULL) return NULL;
    PyErr_SetRaisedException(stop);
    return NULL;
}

static PyObject *
client_request_send(ClientRequestAwait *self, PyObject *value)
{
    (void)value;
    return client_request_iternext(self);
}

static PyObject *
client_request_throw(ClientRequestAwait *self, PyObject *args)
{
    if (self->done) return client_request_iternext(self);
    Py_ssize_t count = PyTuple_GET_SIZE(args);
    if (count < 1 || count > 3) {
        PyErr_SetString(PyExc_TypeError, "throw expected 1 to 3 arguments");
        return NULL;
    }
    PyObject *kind = PyTuple_GET_ITEM(args, 0);
    PyObject *error;
    if (PyExceptionInstance_Check(kind)) {
        if (count != 1) {
            PyErr_SetString(PyExc_TypeError, "instance exception takes no value");
            return NULL;
        }
        error = Py_NewRef(kind);
    } else if (PyExceptionClass_Check(kind)) {
        PyObject *value = count >= 2 ? PyTuple_GET_ITEM(args, 1) : NULL;
        error = value == NULL || value == Py_None
            ? PyObject_CallNoArgs(kind) : PyObject_CallOneArg(kind, value);
        if (error == NULL) return NULL;
    } else {
        PyErr_SetString(PyExc_TypeError, "exceptions must derive from BaseException");
        return NULL;
    }
    if (count == 3 && PyTuple_GET_ITEM(args, 2) != Py_None &&
        PyException_SetTraceback(error, PyTuple_GET_ITEM(args, 2)) < 0) {
        Py_DECREF(error);
        return NULL;
    }
    client_request_abort_response(self);
    if (fast_release_connection(self, 0) < 0) {
        Py_DECREF(error);
        return NULL;
    }
    self->error = error;
    self->done = 1;
    self->cancelled = PyErr_GivenExceptionMatches(error, client_cancelled_error);
    client_request_cancel_total(self);
    PyErr_SetRaisedException(Py_NewRef(error));
    return NULL;
}

static PyObject *
client_request_close(ClientRequestAwait *self, PyObject *unused)
{
    (void)unused;
    if (!self->done) {
        client_request_abort_response(self);
        if (fast_release_connection(self, 0) < 0) return NULL;
        self->done = 1;
    }
    client_request_cancel_total(self);
    Py_RETURN_NONE;
}

static PyObject *
client_request_total_timeout(ClientRequestAwait *self, PyObject *unused)
{
    (void)unused;
    if (self->done) Py_RETURN_NONE;
    PyObject *error = PyObject_CallFunction(
        self->request_timeout, "s", "outbound request exceeded total timeout");
    if (error == NULL) return NULL;
    client_request_abort_response(self);
    if (fast_release_connection(self, 0) < 0) {
        Py_DECREF(error);
        return NULL;
    }
    self->error = error;
    self->done = 1;
    Py_CLEAR(self->total_timer);
    if (client_request_schedule_callbacks(self) < 0) return NULL;
    Py_RETURN_NONE;
}

static int
client_request_traverse(ClientRequestAwait *self, visitproc visit, void *arg)
{
    Py_VISIT(Py_TYPE(self));
    Py_VISIT(self->client);
    Py_VISIT(self->connection);
    Py_VISIT(self->stream);
    Py_VISIT(self->loop);
    Py_VISIT(self->result);
    Py_VISIT(self->error);
    Py_VISIT(self->callback);
    Py_VISIT(self->callback_context);
    Py_VISIT(self->callbacks);
    Py_VISIT(self->blocking);
    Py_VISIT(self->transport_error);
    Py_VISIT(self->request_timeout);
    Py_VISIT(self->total_timer);
    return 0;
}

static void
client_request_dealloc(ClientRequestAwait *self)
{
    PyTypeObject *type = Py_TYPE(self);
    PyObject_GC_UnTrack(self);
    if (!self->released && self->client != NULL && self->connection != NULL) {
        if (fast_release_connection(self, 0) < 0) PyErr_Clear();
    }
    Py_CLEAR(self->client);
    Py_CLEAR(self->connection);
    Py_CLEAR(self->stream);
    Py_CLEAR(self->loop);
    Py_CLEAR(self->result);
    Py_CLEAR(self->error);
    Py_CLEAR(self->callback);
    Py_CLEAR(self->callback_context);
    Py_CLEAR(self->callbacks);
    Py_CLEAR(self->blocking);
    Py_CLEAR(self->transport_error);
    Py_CLEAR(self->request_timeout);
    Py_CLEAR(self->total_timer);
    type->tp_free((PyObject *)self);
    Py_DECREF(type);
}

static PyMethodDef client_request_methods[] = {
    {"send", (PyCFunction)client_request_send, METH_O, NULL},
    {"throw", (PyCFunction)client_request_throw, METH_VARARGS, NULL},
    {"close", (PyCFunction)client_request_close, METH_NOARGS, NULL},
    {"done", (PyCFunction)client_request_done, METH_NOARGS, NULL},
    {"cancelled", (PyCFunction)client_request_cancelled, METH_NOARGS, NULL},
    {"get_loop", (PyCFunction)client_request_get_loop, METH_NOARGS, NULL},
    {"result", (PyCFunction)client_request_result, METH_NOARGS, NULL},
    {"exception", (PyCFunction)client_request_exception, METH_NOARGS, NULL},
    {"set_result", (PyCFunction)client_request_set_result, METH_O, NULL},
    {"set_exception", (PyCFunction)client_request_set_exception, METH_O, NULL},
    {"add_done_callback",
     (PyCFunction)(void (*)(void))client_request_add_done_callback,
     METH_FASTCALL | METH_KEYWORDS, NULL},
    {"remove_done_callback", (PyCFunction)client_request_remove_done_callback,
     METH_O, NULL},
    {"cancel", (PyCFunction)(void (*)(void))client_request_cancel,
     METH_FASTCALL | METH_KEYWORDS, NULL},
    {"_total_timeout", (PyCFunction)client_request_total_timeout,
     METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyMemberDef client_request_members[] = {
    {"_asyncio_future_blocking", T_OBJECT,
     offsetof(ClientRequestAwait, blocking), 0, NULL},
    {NULL, 0, 0, 0, NULL},
};

static PyType_Slot client_request_slots[] = {
    {Py_tp_dealloc, client_request_dealloc},
    {Py_tp_traverse, client_request_traverse},
    {Py_am_await, client_request_await},
    {Py_tp_iter, PyObject_SelfIter},
    {Py_tp_iternext, client_request_iternext},
    {Py_tp_methods, client_request_methods},
    {Py_tp_members, client_request_members},
    {0, NULL},
};

static PyType_Spec client_request_spec = {
    .name = "wreath._native._client._RequestAwait",
    .basicsize = sizeof(ClientRequestAwait),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = client_request_slots,
};

static int
fast_resolve_offset(PyObject *type, const char *name, Py_ssize_t *offset)
{
    if (!PyType_Check(type)) {
        PyErr_SetString(PyExc_TypeError,
                        "fast HTTP client configuration expects types");
        return -1;
    }
    PyObject *dict = ((PyTypeObject *)type)->tp_dict;
    PyObject *descriptor = dict == NULL
        ? NULL : PyDict_GetItemString(dict, name);
    if (descriptor == NULL ||
        !PyObject_TypeCheck(descriptor, &PyMemberDescr_Type)) {
        PyErr_Format(
            PyExc_RuntimeError,
            "wreath._native._client: %s has no __slots__ member %s; "
            "the client accelerator must be rebuilt",
            ((PyTypeObject *)type)->tp_name, name);
        return -1;
    }
    *offset = ((PyMemberDescrObject *)descriptor)->d_member->offset;
    return 0;
}

PyObject *
wreath_http_client_configure_fast_path(PyObject *self, PyObject *args)
{
    (void)self;
    PyObject *client_type;
    PyObject *connection_type;
    PyObject *limit_type;
    PyObject *timeout_type;
    if (!PyArg_ParseTuple(
            args, "OOOO:_configure_fast_path", &client_type,
            &connection_type, &limit_type, &timeout_type)) return NULL;
    if (fast_resolve_offset(client_type, "_active", &fast_client_off.active) < 0 ||
        fast_resolve_offset(
            client_type, "_authority_bytes", &fast_client_off.authority_bytes) < 0 ||
        fast_resolve_offset(
            client_type, "_base_path", &fast_client_off.base_path) < 0 ||
        fast_resolve_offset(
            client_type, "_condition", &fast_client_off.condition) < 0 ||
        fast_resolve_offset(client_type, "_idle", &fast_client_off.idle) < 0 ||
        fast_resolve_offset(client_type, "_limits", &fast_client_off.limits) < 0 ||
        fast_resolve_offset(
            client_type, "_native_counters",
            &fast_client_off.native_counters) < 0 ||
        fast_resolve_offset(client_type, "_open", &fast_client_off.open) < 0 ||
        fast_resolve_offset(
            client_type, "_request_plans", &fast_client_off.request_plans) < 0 ||
        fast_resolve_offset(
            client_type, "_request_last_plan",
            &fast_client_off.request_last_plan) < 0 ||
        fast_resolve_offset(client_type, "_started", &fast_client_off.started) < 0 ||
        fast_resolve_offset(client_type, "_timeout", &fast_client_off.timeout) < 0 ||
        fast_resolve_offset(client_type, "_waiters", &fast_client_off.waiters) < 0 ||
        fast_resolve_offset(
            connection_type, "reader", &fast_connection_off.reader) < 0 ||
        fast_resolve_offset(
            limit_type, "max_keepalive_connections",
            &fast_limit_off.max_keepalive_connections) < 0 ||
        fast_resolve_offset(
            limit_type, "max_response_header_bytes",
            &fast_limit_off.max_response_header_bytes) < 0 ||
        fast_resolve_offset(
            limit_type, "max_request_header_bytes",
            &fast_limit_off.max_request_header_bytes) < 0 ||
        fast_resolve_offset(
            limit_type, "max_response_bytes",
            &fast_limit_off.max_response_bytes) < 0 ||
        fast_resolve_offset(
            timeout_type, "response_body", &fast_timeout_off.response_body) < 0 ||
        fast_resolve_offset(
            timeout_type, "response_headers",
            &fast_timeout_off.response_headers) < 0 ||
        fast_resolve_offset(
            timeout_type, "total", &fast_timeout_off.total) < 0) return NULL;
    Py_XSETREF(fast_client_type, (PyTypeObject *)Py_NewRef(client_type));
    Py_XSETREF(
        fast_connection_type, (PyTypeObject *)Py_NewRef(connection_type));
    Py_XSETREF(fast_limit_type, (PyTypeObject *)Py_NewRef(limit_type));
    Py_XSETREF(fast_timeout_type, (PyTypeObject *)Py_NewRef(timeout_type));
    Py_RETURN_NONE;
}

static int
fast_stream_is_open(WreathClientStream *stream)
{
    if (!stream->connected || stream->eof || stream->write_paused ||
        stream->transport == NULL) return 0;
    if (stream->transport_capi != NULL)
        return !stream->transport_capi->is_closing(stream->transport);
    PyObject *closing = PyObject_CallMethodNoArgs(
        stream->transport, client_str_is_closing);
    if (closing == NULL) return -1;
    int open = !PyObject_IsTrue(closing);
    Py_DECREF(closing);
    return open;
}

static ClientRequestAwait *
client_request_new(PyObject *client, PyObject *connection,
                   WreathClientStream *stream, PyObject *transport_error,
                   PyObject *request_timeout, PyObject *total)
{
    ClientRequestAwait *awaitable =
        (ClientRequestAwait *)client_request_await_type->tp_alloc(
            client_request_await_type, 0);
    if (awaitable == NULL) return NULL;
    awaitable->client = Py_NewRef(client);
    awaitable->connection = Py_NewRef(connection);
    awaitable->stream = Py_NewRef((PyObject *)stream);
    awaitable->loop = Py_NewRef(stream->loop);
    awaitable->result = NULL;
    awaitable->error = NULL;
    awaitable->callback = NULL;
    awaitable->callback_context = NULL;
    awaitable->callbacks = NULL;
    awaitable->blocking = Py_NewRef(Py_None);
    awaitable->transport_error = Py_NewRef(transport_error);
    awaitable->request_timeout = Py_NewRef(request_timeout);
    awaitable->total_timer = NULL;
    awaitable->done = 0;
    awaitable->cancelled = 0;
    awaitable->yielded = 0;
    awaitable->released = 0;
    (void)total;  /* the response state machine owns the one fused deadline */
    return awaitable;
}

static PyObject *
http_client_request_once_parts(
    PyObject *client, PyObject *method, PyObject *request,
    PyObject *response_type, PyObject *protocol_error, PyObject *too_large,
    PyObject *timeout_error, PyObject *transport_error,
    PyObject *request_timeout, PyObject *total)
{
    if (fast_client_type == NULL || client_request_await_type == NULL) {
        PyErr_SetString(PyExc_RuntimeError,
                        "native HTTP client fast path is not configured");
        return NULL;
    }
    if (!Py_IS_TYPE(client, fast_client_type) || !PyBytes_CheckExact(request)) {
        Py_RETURN_NONE;
    }
    PyObject *idle = FAST_SLOT(client, fast_client_off.idle);
    PyObject *active = FAST_SLOT(client, fast_client_off.active);
    PyObject *limits = FAST_SLOT(client, fast_client_off.limits);
    PyObject *timeouts = FAST_SLOT(client, fast_client_off.timeout);
    PyObject *waiters_obj = FAST_SLOT(client, fast_client_off.waiters);
    if (FAST_SLOT(client, fast_client_off.started) != Py_True ||
        !PyList_CheckExact(idle) || !PySet_CheckExact(active) ||
        !Py_IS_TYPE(limits, fast_limit_type) ||
        !Py_IS_TYPE(timeouts, fast_timeout_type)) Py_RETURN_NONE;
    Py_ssize_t waiters = PyLong_AsSsize_t(waiters_obj);
    if (waiters == -1 && PyErr_Occurred()) return NULL;
    if (waiters != 0) Py_RETURN_NONE;

    PyObject *connection = NULL;
    WreathClientStream *stream = NULL;
    while (PyList_GET_SIZE(idle) > 0) {
        Py_ssize_t index = PyList_GET_SIZE(idle) - 1;
        connection = Py_NewRef(PyList_GET_ITEM(idle, index));
        if (PyList_SetSlice(idle, index, index + 1, NULL) < 0) {
            Py_DECREF(connection);
            return NULL;
        }
        if (Py_IS_TYPE(connection, fast_connection_type)) {
            PyObject *reader = FAST_SLOT(
                connection, fast_connection_off.reader);
            if (Py_IS_TYPE(reader, client_stream_type)) {
                stream = (WreathClientStream *)reader;
                int open = fast_stream_is_open(stream);
                if (open < 0) {
                    Py_DECREF(connection);
                    return NULL;
                }
                if (open) break;
            }
        }
        if (stream != NULL && stream->transport != NULL) {
            PyObject *closed = PyObject_CallMethodNoArgs(
                stream->transport, client_str_close);
            if (closed == NULL) {
                Py_DECREF(connection);
                return NULL;
            }
            Py_DECREF(closed);
        }
        if (fast_counter_add(client, fast_client_off.open, -1) < 0) {
            Py_DECREF(connection);
            return NULL;
        }
        Py_CLEAR(connection);
        stream = NULL;
    }
    if (connection == NULL) Py_RETURN_NONE;
    HttpClientCounters *counters = fast_client_counters(client);
    if (counters == NULL) {
        Py_DECREF(connection);
        return NULL;
    }
    if (PySet_Add(active, connection) < 0) {
        Py_DECREF(connection);
        return NULL;
    }
    atomic_fetch_add_explicit(&counters->reused, 1, memory_order_relaxed);
    ClientRequestAwait *awaitable = client_request_new(
        client, connection, stream, transport_error, request_timeout, total);
    Py_DECREF(connection);
    if (awaitable == NULL) return NULL;

    if (stream->transport_capi != NULL) {
        if (stream->transport_capi->write(stream->transport, request) < 0)
            goto request_error;
    } else {
        PyObject *written = PyObject_CallMethodOneArg(
            stream->transport, client_str_write, request);
        if (written == NULL) goto request_error;
        Py_DECREF(written);
    }
    atomic_fetch_add_explicit(&counters->requests, 1, memory_order_relaxed);
    Py_ssize_t max_header = PyLong_AsSsize_t(
        FAST_SLOT(limits, fast_limit_off.max_response_header_bytes));
    Py_ssize_t max_body = PyLong_AsSsize_t(
        FAST_SLOT(limits, fast_limit_off.max_response_bytes));
    if ((max_header == -1 || max_body == -1) && PyErr_Occurred())
        goto request_error;
    PyObject *response = cs_start_response(
        stream, method, max_header, max_body, response_type, protocol_error,
        too_large, timeout_error,
        FAST_SLOT(timeouts, fast_timeout_off.response_headers),
        FAST_SLOT(timeouts, fast_timeout_off.response_body),
        (PyObject *)awaitable, request_timeout, total);
    if (response == NULL) goto request_error;
    if (response != (PyObject *)awaitable) {
        PyObject *set = client_request_set_result(awaitable, response);
        Py_DECREF(response);
        if (set == NULL) goto request_error;
        Py_DECREF(set);
    } else {
        Py_DECREF(response);
    }
    return (PyObject *)awaitable;

request_error:
    if (!awaitable->released && fast_release_connection(awaitable, 0) < 0) {
        PyErr_Clear();
    }
    Py_DECREF(awaitable);
    return NULL;
}

PyObject *
wreath_http_client_request_once(PyObject *self, PyObject *args)
{
    (void)self;
    PyObject *client;
    PyObject *method;
    PyObject *request;
    PyObject *response_type;
    PyObject *protocol_error;
    PyObject *too_large;
    PyObject *timeout_error;
    PyObject *transport_error;
    if (!PyArg_ParseTuple(
            args, "OOOOOOOO:_request_once", &client, &method, &request,
            &response_type, &protocol_error, &too_large, &timeout_error,
            &transport_error)) return NULL;
    return http_client_request_once_parts(
        client, method, request, response_type, protocol_error, too_large,
        timeout_error, transport_error, Py_None, Py_None);
}

PyObject *
wreath_http_client_request_default(PyObject *self, PyObject *args)
{
    (void)self;
    PyObject *client;
    PyObject *method;
    PyObject *target;
    PyObject *response_type;
    PyObject *protocol_error;
    PyObject *too_large;
    PyObject *response_timeout;
    PyObject *transport_error;
    PyObject *client_error;
    PyObject *request_timeout;
    PyObject *headers;
    PyObject *body;
    if (!PyArg_ParseTuple(
            args, "OOOOOOOOOOOO:_request_default", &client, &method, &target,
            &response_type, &protocol_error, &too_large, &response_timeout,
            &transport_error, &client_error, &request_timeout, &headers,
            &body)) return NULL;
    if (fast_client_type == NULL || !Py_IS_TYPE(client, fast_client_type) ||
        !PyUnicode_Check(method) || !PyUnicode_Check(target)) Py_RETURN_NONE;
    if (PyUnicode_GET_LENGTH(target) == 0 ||
        PyUnicode_ReadChar(target, 0) != '/' ||
        (PyUnicode_GET_LENGTH(target) > 1 &&
         PyUnicode_ReadChar(target, 1) == '/')) {
        PyErr_SetString(PyExc_ValueError,
                        "request target must be origin-relative");
        return NULL;
    }
    PyObject *plans = FAST_SLOT(client, fast_client_off.request_plans);
    if (!PyDict_CheckExact(plans)) Py_RETURN_NONE;
    PyObject *last = FAST_SLOT(client, fast_client_off.request_last_plan);
    PyObject *key = NULL;
    PyObject *plan = NULL;
    PyObject *method_upper;
    PyObject *request;
    PyObject *payload = NULL;
    int prepared = headers != Py_GetConstantBorrowed(Py_CONSTANT_EMPTY_TUPLE) ||
        body != Py_GetConstantBorrowed(Py_CONSTANT_EMPTY_BYTES);
    if (prepared) {
        method_upper = PyObject_CallMethodNoArgs(method, client_str_upper);
        if (method_upper == NULL) return NULL;
        PyObject *base = FAST_SLOT(client, fast_client_off.base_path);
        PyObject *combined = PyUnicode_GET_LENGTH(base) == 0
            ? Py_NewRef(target) : PyUnicode_Concat(base, target);
        if (combined == NULL) {
            Py_DECREF(method_upper);
            return NULL;
        }
        PyObject *target_bytes = PyUnicode_AsASCIIString(combined);
        Py_DECREF(combined);
        if (target_bytes == NULL) {
            Py_DECREF(method_upper);
            PyErr_Clear();
            PyErr_SetString(PyExc_ValueError,
                            "request target must be ASCII/percent-encoded");
            return NULL;
        }
        payload = PyBytes_FromObject(body);
        PyObject *header_tuple = payload == NULL ? NULL : PySequence_Tuple(headers);
        if (header_tuple == NULL || payload == NULL) {
            Py_XDECREF(header_tuple);
            Py_XDECREF(payload);
            Py_DECREF(target_bytes);
            Py_DECREF(method_upper);
            return NULL;
        }
        PyObject *serialize_args = PyTuple_Pack(
            5, method_upper, target_bytes,
            FAST_SLOT(client, fast_client_off.authority_bytes), header_tuple,
            payload);
        Py_DECREF(header_tuple);
        Py_DECREF(target_bytes);
        if (serialize_args == NULL) {
            Py_DECREF(payload);
            Py_DECREF(method_upper);
            return NULL;
        }
        request = wreath_http_serialize_request(NULL, serialize_args);
        Py_DECREF(serialize_args);
        if (request == NULL) {
            Py_DECREF(payload);
            Py_DECREF(method_upper);
            return NULL;
        }
        goto request_prepared;
    }
    if (PyTuple_CheckExact(last) && PyTuple_GET_SIZE(last) == 4 &&
        PyTuple_GET_ITEM(last, 0) == method &&
        PyTuple_GET_ITEM(last, 1) == target) {
        method_upper = Py_NewRef(PyTuple_GET_ITEM(last, 2));
        request = Py_NewRef(PyTuple_GET_ITEM(last, 3));
    } else {
        key = PyTuple_Pack(2, method, target);
        if (key == NULL) return NULL;
        plan = PyDict_GetItemWithError(plans, key);
    }
    if (key != NULL && plan != NULL) {
        if (!PyTuple_CheckExact(plan) || PyTuple_GET_SIZE(plan) != 2 ||
            !PyUnicode_CheckExact(PyTuple_GET_ITEM(plan, 0)) ||
            !PyBytes_CheckExact(PyTuple_GET_ITEM(plan, 1))) {
            Py_DECREF(key);
            PyErr_SetString(PyExc_RuntimeError,
                            "native HTTP request plan is invalid");
            return NULL;
        }
        method_upper = Py_NewRef(PyTuple_GET_ITEM(plan, 0));
        request = Py_NewRef(PyTuple_GET_ITEM(plan, 1));
    } else if (key != NULL && PyErr_Occurred()) {
        Py_DECREF(key);
        return NULL;
    } else if (key != NULL) {
        method_upper = PyObject_CallMethodNoArgs(method, client_str_upper);
        if (method_upper == NULL) {
            Py_DECREF(key);
            return NULL;
        }
        PyObject *base = FAST_SLOT(client, fast_client_off.base_path);
        PyObject *combined = PyUnicode_GET_LENGTH(base) == 0
            ? Py_NewRef(target) : PyUnicode_Concat(base, target);
        if (combined == NULL) {
            Py_DECREF(key);
            Py_DECREF(method_upper);
            return NULL;
        }
        PyObject *target_bytes = PyUnicode_AsASCIIString(combined);
        Py_DECREF(combined);
        if (target_bytes == NULL) {
            Py_DECREF(key);
            Py_DECREF(method_upper);
            PyErr_Clear();
            PyErr_SetString(PyExc_ValueError,
                            "request target must be ASCII/percent-encoded");
            return NULL;
        }
        PyObject *serialize_args = PyTuple_Pack(
            5, method_upper, target_bytes,
            FAST_SLOT(client, fast_client_off.authority_bytes),
            Py_GetConstantBorrowed(Py_CONSTANT_EMPTY_TUPLE),
            Py_GetConstantBorrowed(Py_CONSTANT_EMPTY_BYTES));
        if (serialize_args == NULL) {
            Py_DECREF(key);
            Py_DECREF(method_upper);
            Py_DECREF(target_bytes);
            return NULL;
        }
        request = wreath_http_serialize_request(NULL, serialize_args);
        Py_DECREF(serialize_args);
        Py_DECREF(target_bytes);
        if (request == NULL) {
            Py_DECREF(key);
            Py_DECREF(method_upper);
            return NULL;
        }
        /* A plan cache belongs to one explicitly-owned client and is bounded:
         * dynamic targets beyond the first 128 still use the fused operation,
         * but cannot turn it into process-lifetime request retention. */
        if (PyDict_GET_SIZE(plans) < 128) {
            plan = PyTuple_Pack(2, method_upper, request);
            if (plan == NULL || PyDict_SetItem(plans, key, plan) < 0) {
                Py_XDECREF(plan);
                Py_DECREF(request);
                Py_DECREF(key);
                Py_DECREF(method_upper);
                return NULL;
            }
            Py_DECREF(plan);
        }
    }
    if (key != NULL) {
        PyObject *retained = PyTuple_Pack(
            4, method, target, method_upper, request);
        if (retained == NULL) {
            Py_DECREF(key);
            Py_DECREF(method_upper);
            Py_DECREF(request);
            return NULL;
        }
        Py_SETREF(
            FAST_SLOT(client, fast_client_off.request_last_plan), retained);
        Py_DECREF(key);
    }
request_prepared:
    PyObject *limits = FAST_SLOT(client, fast_client_off.limits);
    Py_ssize_t request_limit = PyLong_AsSsize_t(
        FAST_SLOT(limits, fast_limit_off.max_request_header_bytes));
    if (request_limit == -1 && PyErr_Occurred()) {
        Py_XDECREF(payload);
        Py_DECREF(method_upper);
        Py_DECREF(request);
        return NULL;
    }
    Py_ssize_t payload_size = payload == NULL ? 0 : PyBytes_GET_SIZE(payload);
    if (PyBytes_GET_SIZE(request) - payload_size > request_limit) {
        PyObject *error = PyObject_CallFunction(
            client_error, "s", "request headers exceed configured limit");
        Py_XDECREF(payload);
        Py_DECREF(method_upper);
        Py_DECREF(request);
        if (error == NULL) return NULL;
        PyErr_SetRaisedException(error);
        return NULL;
    }
    PyObject *total = FAST_SLOT(
        FAST_SLOT(client, fast_client_off.timeout), fast_timeout_off.total);
    PyObject *awaited = http_client_request_once_parts(
        client, method_upper, request, response_type, protocol_error,
        too_large, response_timeout, transport_error, request_timeout, total);
    Py_XDECREF(payload);
    Py_DECREF(method_upper);
    Py_DECREF(request);
    return awaited;
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
cs_has_buffered_data(WreathClientStream *self, PyObject *Py_UNUSED(ignored))
{
    return PyBool_FromLong(cs_avail(self) != 0);
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
    PyObject *sslcontext = PyObject_CallMethod(
        transport, "get_extra_info", "s", "sslcontext");
    if (sslcontext == NULL) {
        return NULL;
    }
    self->over_ssl = sslcontext != Py_None;
    Py_DECREF(sslcontext);
    Py_XSETREF(self->transport, Py_NewRef(transport));
    const WreathTransportCAPI *capi = wreath_transport_capi_resolve();
    self->transport_capi =
        capi != NULL && capi->check(transport) ? capi : NULL;
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
    /* asyncio cannot honour a request to keep an SSL transport open after
     * close_notify and warns whenever a protocol asks it to.  Match
     * StreamReaderProtocol: plaintext may remain half-open; TLS may not. */
    return PyBool_FromLong(!self->over_ssl);
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
    PyObject *call_soon = PyObject_GetAttrString(loop, "call_soon");
    PyObject *call_later = PyObject_GetAttrString(loop, "call_later");
    if (create_future == NULL || call_soon == NULL || call_later == NULL) {
        Py_XDECREF(create_future);
        Py_XDECREF(call_soon);
        Py_XDECREF(call_later);
        Py_DECREF(loop);
        return NULL;
    }
    WreathClientStream *self = (WreathClientStream *)type->tp_alloc(type, 0);
    if (self == NULL) {
        Py_DECREF(create_future);
        Py_DECREF(call_soon);
        Py_DECREF(call_later);
        Py_DECREF(loop);
        return NULL;
    }
    self->loop = loop;
    self->create_future = create_future;
    self->call_soon = call_soon;
    self->call_later = call_later;
    self->response_version_10 = PyUnicode_FromString("1.0");
    self->response_version_11 = PyUnicode_FromString("1.1");
    self->response_timeout_callback = PyObject_GetAttrString(
        (PyObject *)self, "_response_timeout");
    if (self->response_version_10 == NULL || self->response_version_11 == NULL ||
        self->response_timeout_callback == NULL) {
        Py_DECREF(self);
        return NULL;
    }
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
    Py_VISIT(self->loop);
    Py_VISIT(self->create_future);
    Py_VISIT(self->call_soon);
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
    Py_VISIT(self->response_request_timeout_error);
    Py_VISIT(self->response_headers);
    Py_VISIT(self->response_reason);
    Py_VISIT(self->response_body);
    Py_VISIT(self->response_header_timeout);
    Py_VISIT(self->response_body_timeout);
    Py_VISIT(self->response_timer);
    Py_VISIT(self->response_layout_type);
    Py_VISIT(self->response_version_10);
    Py_VISIT(self->response_version_11);
    Py_VISIT(self->response_timeout_callback);
    return 0;
}

static int
cs_clear(PyObject *op)
{
    WreathClientStream *self = (WreathClientStream *)op;
    Py_CLEAR(self->exc);
    Py_CLEAR(self->transport);
    Py_CLEAR(self->loop);
    Py_CLEAR(self->create_future);
    Py_CLEAR(self->call_soon);
    Py_CLEAR(self->call_later);
    Py_CLEAR(self->drain_waiter);
    Py_CLEAR(self->closed_future);
    Py_CLEAR(self->offer_obj);
    Py_CLEAR(self->pending_future);
    cs_clear_response(self);
    Py_CLEAR(self->response_layout_type);
    Py_CLEAR(self->response_version_10);
    Py_CLEAR(self->response_version_11);
    Py_CLEAR(self->response_timeout_callback);
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
    {"_has_buffered_data", (PyCFunction)cs_has_buffered_data, METH_NOARGS, NULL},
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
    client_cancelled_error = PyObject_GetAttrString(
        asyncio_module, "CancelledError");
    cs_incomplete_read_error = PyObject_GetAttrString(
        asyncio_module, "IncompleteReadError");
    cs_limit_overrun_error = PyObject_GetAttrString(
        asyncio_module, "LimitOverrunError");
    Py_DECREF(asyncio_module);
    cs_str_done = PyUnicode_InternFromString("done");
    cs_str_set_result = PyUnicode_InternFromString("set_result");
    cs_str_set_exception = PyUnicode_InternFromString("set_exception");
    cs_str_cancel = PyUnicode_InternFromString("cancel");
    client_str_close = PyUnicode_InternFromString("close");
    client_str_context = PyUnicode_InternFromString("context");
    client_str_is_closing = PyUnicode_InternFromString("is_closing");
    client_str_write = PyUnicode_InternFromString("write");
    client_str_upper = PyUnicode_InternFromString("upper");
    client_context_names = client_str_context != NULL
        ? PyTuple_Pack(1, client_str_context) : NULL;
    if (base == NULL || cs_incomplete_read_error == NULL ||
        cs_limit_overrun_error == NULL || cs_str_done == NULL ||
        cs_str_set_result == NULL || cs_str_set_exception == NULL ||
        cs_str_cancel == NULL || client_cancelled_error == NULL ||
        client_str_close == NULL ||
        client_str_context == NULL || client_str_is_closing == NULL ||
        client_str_write == NULL || client_str_upper == NULL ||
        client_context_names == NULL) {
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
    client_request_await_type = (PyTypeObject *)PyType_FromSpec(
        &client_request_spec);
    if (client_request_await_type == NULL) return -1;
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
