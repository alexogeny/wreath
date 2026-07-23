/* Incremental HTTP/1 response protocol used by the outbound client.
 *
 * The protocol owns its receive buffer and emits one parsed response head at a
 * time.  Keeping framing state in C avoids rescanning previously received bytes;
 * body framing remains with the Python connection until it migrates here.
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

/* ======================================================================== */
/* Http1ClientStream: the transport-facing native stream for the client.    */
/*                                                                          */
/* Replaces asyncio streams: a C-owned receive buffer with StreamReader-    */
/* shaped awaitable reads (readuntil/readexactly/read, asyncio semantics    */
/* for IncompleteReadError/LimitOverrunError/at_eof), writer backpressure   */
/* (_drain/_wait_closed), and the stream-fusion C API so metal transports   */
/* deliver wire bytes with no Python calling convention per read. The       */
/* Python connection keeps all framing logic; only byte transport moves.    */
/* ======================================================================== */

#define CS_READ_NONE 0
#define CS_READ_UNTIL 1
#define CS_READ_EXACTLY 2
#define CS_READ_SOME 3
#define CS_SEP_MAX 16
#define CS_OFFER_SIZE 65536

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
} WreathClientStream;

static PyTypeObject *client_stream_type = NULL;
static PyObject *cs_incomplete_read_error = NULL;  /* asyncio classes */
static PyObject *cs_limit_overrun_error = NULL;
static PyObject *cs_str_done = NULL;
static PyObject *cs_str_set_result = NULL;
static PyObject *cs_str_set_exception = NULL;

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
cs_clear_pending(WreathClientStream *self)
{
    self->pending_kind = CS_READ_NONE;
    self->pending_sep_len = 0;
    self->pending_count = 0;
    self->pending_scan = 0;
    Py_CLEAR(self->pending_future);
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
        for (Py_ssize_t i = *scan; i + seplen <= avail; i++) {
            if (base[i] == sep[0] &&
                memcmp(base + i, sep, (size_t)seplen) == 0) {
                isep = i;
                break;
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
    PyObject *ready = cs_take_ready(
        self, self->pending_kind, self->pending_sep, self->pending_sep_len,
        self->pending_count, &scan, &would_block);
    if (would_block) {
        self->pending_scan = scan;
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
    Py_DECREF(loop);
    if (create_future == NULL) {
        return NULL;
    }
    WreathClientStream *self = (WreathClientStream *)type->tp_alloc(type, 0);
    if (self == NULL) {
        Py_DECREF(create_future);
        return NULL;
    }
    self->create_future = create_future;
    self->limit = limit;
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
    Py_VISIT(self->drain_waiter);
    Py_VISIT(self->closed_future);
    Py_VISIT(self->offer_obj);
    Py_VISIT(self->pending_future);
    return 0;
}

static int
cs_clear(PyObject *op)
{
    WreathClientStream *self = (WreathClientStream *)op;
    Py_CLEAR(self->exc);
    Py_CLEAR(self->transport);
    Py_CLEAR(self->create_future);
    Py_CLEAR(self->drain_waiter);
    Py_CLEAR(self->closed_future);
    Py_CLEAR(self->offer_obj);
    Py_CLEAR(self->pending_future);
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
    if (base == NULL || cs_incomplete_read_error == NULL ||
        cs_limit_overrun_error == NULL || cs_str_done == NULL ||
        cs_str_set_result == NULL || cs_str_set_exception == NULL) {
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
