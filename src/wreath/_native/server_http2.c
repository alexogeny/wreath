/* HTTP/2 (RFC 9113) connection protocol for Wreath.
 *
 * One native connection object (Http2Protocol) owns a native stream table of
 * Http2Stream objects keyed by stream id. Frame parsing, HPACK, flow control,
 * and response framing stay in C; the ASGI application is entered exactly once
 * per request stream (one create_task per stream).
 *
 * Layout of this file:
 *   - constants + small helpers
 *   - Http2Stream: per-stream ASGI plumbing (_receive/_send/_done)
 *   - Http2Protocol: framing, settings, flow control, error handling
 *   - type specs
 */
#include "server.h"

/* --- frame types / flags / settings --------------------------------------- */
enum {
    FRAME_DATA = 0x0, FRAME_HEADERS = 0x1, FRAME_PRIORITY = 0x2,
    FRAME_RST_STREAM = 0x3, FRAME_SETTINGS = 0x4, FRAME_PUSH_PROMISE = 0x5,
    FRAME_PING = 0x6, FRAME_GOAWAY = 0x7, FRAME_WINDOW_UPDATE = 0x8,
    FRAME_CONTINUATION = 0x9, FRAME_PRIORITY_UPDATE = 0x10
};
enum {
    FLAG_END_STREAM = 0x1, FLAG_ACK = 0x1, FLAG_END_HEADERS = 0x4,
    FLAG_PADDED = 0x8, FLAG_PRIORITY = 0x20
};
enum {
    SET_HEADER_TABLE_SIZE = 0x1, SET_ENABLE_PUSH = 0x2,
    SET_MAX_CONCURRENT_STREAMS = 0x3, SET_INITIAL_WINDOW_SIZE = 0x4,
    SET_MAX_FRAME_SIZE = 0x5, SET_MAX_HEADER_LIST_SIZE = 0x6
};
/* stream states (RFC 9113 s5.1) */
enum {
    S_IDLE = 0, S_OPEN, S_HALF_CLOSED_REMOTE, S_HALF_CLOSED_LOCAL, S_CLOSED
};

#define H2_PREFACE "PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
#define H2_PREFACE_LEN 24
#define DEFAULT_MAX_FRAME 16384
#define MAX_ALLOWED_FRAME 16777215
#define WINDOW_MAX 0x7FFFFFFF

/* ======================================================================== */
/* Http2Stream                                                              */
/* ======================================================================== */

#define H2_BODY_COALESCE_MAX (16 * 1024)

/* Frame-flood defence: a peer that keeps sending frames which make no request
 * progress (empty DATA, control frames, or unknown extension frames) is
 * spending our CPU for nothing. We count consecutive unproductive frames and
 * GOAWAY(ENHANCE_YOUR_CALM) past this budget; any productive frame (a new
 * request, real body bytes, or response DATA framed toward the peer) resets it,
 * so ordinary interleaved control traffic never trips. */
#define H2_MAX_UNPRODUCTIVE_FRAMES 100

/* Outbound DATA scheduling is deliberately bounded per activation. Persistent
 * deficits make renewed connection credit fair across active streams; the byte
 * and stream budgets keep one response burst from monopolizing the loop. */
#define H2_DRR_QUANTUM (16 * 1024)
#define H2_SCHED_MAX_STREAMS 64
#define H2_SCHED_MAX_BYTES (256 * 1024)

typedef struct {
    PyObject *body;             /* bytearray, mutable only while queued */
    Py_ssize_t flow_bytes;      /* DATA frame bytes represented by this chunk */
} H2BodyChunk;

typedef struct {
    PyObject_HEAD
    PyObject *protocol;        /* owned; the Http2Protocol (borrowed logically) */
    uint32_t id;
    int state;

    PyObject *scope;           /* owned; ASGI scope */
    PyObject *task;            /* owned; ASGI Task */
    PyObject *receive_callable;/* bound _receive */
    PyObject *send_callable;   /* bound _send */
    PyObject *done_callable;   /* bound _done */
    PyObject *timeout_handle;  /* request_timeout TimerHandle */

    /* request body plumbing: C-owned queue of coalesced chunks */
    H2BodyChunk *body_chunks;
    Py_ssize_t body_cap;
    Py_ssize_t body_len;
    Py_ssize_t body_head;
    PyObject *receive_waiter;  /* Future or NULL */
    int request_ended;         /* END_STREAM seen for the request */
    int disconnected;          /* reset/closed: deliver http.disconnect */
    Py_ssize_t content_length; /* declared content-length, or -1 */
    Py_ssize_t body_received;
    Py_ssize_t body_frames;    /* non-empty or padded DATA frames accepted */

    /* response state */
    int response_started;
    int response_ended;
    int trailers_expected;

    /* flow control */
    int64_t send_window;       /* peer receive window for this stream */
    int64_t applied_initial_window; /* peer setting reflected in send_window */
    int64_t recv_window;       /* our advertised receive window */
    int64_t recv_consumed_pending;
    int64_t recv_credit_threshold;
    /* One outstanding ASGI body message. The application's own immutable bytes
     * are retained and framed from `pending_offset` onward; they are never
     * copied into a staging buffer, and `await send()` completes only once the
     * whole message has been framed. */
    PyObject *pending_body;    /* bytes retained while flow-control blocked */
    Py_ssize_t pending_offset; /* next unsent payload byte */
    PyObject *send_waiter;     /* Future returned by _send, or NULL */
    int pending_end_stream;
    int in_conn_blocked;       /* queued in protocol->conn_blocked */
    int64_t send_deficit;      /* persistent DRR payload credit */
    uint8_t urgency;           /* RFC 9218 urgency, 0 (highest) through 7 */
    uint8_t incremental;       /* RFC 9218 incremental delivery preference */

    /* First-class HTTP policy is stream-owned: concurrent h2 requests must
     * never share ingress/egress state. */
    WreathPolicyState policy_state;

    /* Native Flight Recorder per-stream context (one request). */
    wreath_nfr_context nfr_ctx;
    int nfr_active;
    int nfr_status;            /* captured from http.response.start */
    uint64_t nfr_bytes_out;    /* response DATA payload framed for this stream */
} Http2Stream;

/* forward */
static PyTypeObject *Http2StreamType;   /* set at module init via spec */
static int h2_flush(PyObject *proto);
static int h2_write_frame(PyObject *proto, int type, int flags, uint32_t sid,
                          const uint8_t *payload, Py_ssize_t len);
static int h2_stream_error(PyObject *proto, uint32_t sid, int code);
static int h2_flush_stream_pending(PyObject *proto, Http2Stream *st);
static int h2_schedule_pending(PyObject *proto);
static void h2_maybe_close_stream(PyObject *proto, Http2Stream *st);
static void h2_abort_pending_send(Http2Stream *st);

/* ======================================================================== */
/* Http2Protocol                                                            */
/* ======================================================================== */

typedef struct {
    PyObject_HEAD
    PyObject *app;
    PyObject *native_app;       /* bound Wreath._wreath_http, or NULL */
    WreathPolicyProgram policy;
    PyObject *loop;
    PyObject *registry;
    PyObject *config;
    PyObject *default_response_headers;
    PyObject *transport;
    PyObject *transport_write_fn;

    PyObject *loop_create_future;
    PyObject *loop_create_task;
    PyObject *scope_type;
    PyObject *scope_asgi;
    PyObject *scope_http_version;
    PyObject *scope_scheme;
    PyObject *scope_root_path;

    PyObject *streams;         /* dict {int stream_id: Http2Stream} */
    PyObject *pending_apps;    /* streams activated after the current input batch */
    PyObject *pending_priorities; /* bounded PRIORITY_UPDATE values for idle streams */
    PyObject *conn_blocked;    /* cursor queue of streams with pending DATA */
    Py_ssize_t conn_blocked_head;
    PyObject *stream_window_blocked; /* pending streams with no stream credit */
    PyObject *scheduler_callable; /* bound _run_write_scheduler */
    int scheduler_scheduled;
    int rfc9218_enabled;       /* metal-only urgency/incremental policy */
    uint32_t nonincremental_stream; /* current same-urgency sequential response */
    PyObject *out;             /* bytearray: pending outbound bytes */

    WreathHpackTable hpack;       /* inbound header decode table */

    /* input buffer + cursor */
    char *buf;
    Py_ssize_t buf_len;
    Py_ssize_t buf_cap;
    Py_ssize_t cursor;
    Py_ssize_t read_offer_offset;
    Py_ssize_t read_offer_size;

    int preface_seen;          /* bytes of client preface matched */
    int got_first_settings;    /* client SETTINGS received */
    int closing;               /* graceful drain: GOAWAY sent, still readable */
    int fatal;                 /* hard stop: connection error, stop reading */
    int goaway_sent;
    int accepting;
    int app_batch_active;      /* native one-shot replies flush after activation */

    /* CONTINUATION assembly */
    uint32_t header_stream;    /* stream id mid-header-block, 0 if none */
    int header_end_stream;     /* END_STREAM flag on the HEADERS frame */
    PyObject *header_block;    /* bytearray of accumulated header block */

    uint32_t last_stream_id;   /* highest client stream id processed */
    int active_requests;       /* streams not yet closed */
    int idle_frames;           /* consecutive frames making no request progress */

    /* settings / limits */
    int64_t conn_send_window;  /* our sending window toward the peer */
    int64_t conn_recv_window;  /* our receive window */
    int64_t conn_consumed_pending;
    int64_t conn_credit_threshold;
    int64_t peer_initial_window;
    int64_t our_initial_window;
    Py_ssize_t peer_max_frame;
    Py_ssize_t our_max_frame;
    Py_ssize_t max_concurrent;
    Py_ssize_t max_header_list;
    Py_ssize_t max_header_count;
    Py_ssize_t max_body_bytes;
    Py_ssize_t max_body_chunks;
    double request_timeout;
    Py_ssize_t hpack_max;

    int write_paused;

    /* Native Flight Recorder: borrowed worker (or NULL) and this connection's id. */
    wreath_nfr_worker *nfr_worker;
    uint64_t nfr_connection_id;
} Http2Protocol;

static PyTypeObject *Http2ProtocolType;

/* --- small helpers -------------------------------------------------------- */

static PyObject *
h2_make_future(Http2Protocol *self)
{
    return PyObject_CallNoArgs(self->loop_create_future);
}

static int
h2_future_set_result(PyObject *future, PyObject *result)
{
    /* The waiter may already be resolved or cancelled (e.g. the task was torn
     * down); setting its result again would raise InvalidStateError. */
    PyObject *done = PyObject_CallMethod(future, "done", NULL);
    if (done == NULL) {
        return -1;
    }
    int is_done = PyObject_IsTrue(done);
    Py_DECREF(done);
    if (is_done) {
        return 0;
    }
    /* Parentheses keep a tuple-valued body slot as one positional argument. */
    PyObject *r = PyObject_CallMethod(future, "set_result", "(O)", result);
    if (r == NULL) {
        return -1;
    }
    Py_DECREF(r);
    return 0;
}

/* --- native request-body queue ------------------------------------------ */
static Py_ssize_t
body_queue_len(Http2Stream *self)
{
    return self->body_len - self->body_head;
}

static void
body_queue_clear(Http2Stream *self)
{
    for (Py_ssize_t i = self->body_head; i < self->body_len; i++) {
        Py_XDECREF(self->body_chunks[i].body);
    }
    PyMem_Free(self->body_chunks);
    self->body_chunks = NULL;
    self->body_cap = self->body_len = self->body_head = 0;
}

static int
body_queue_coalesce(Http2Stream *self, const uint8_t *data, Py_ssize_t len,
                    Py_ssize_t flow_bytes)
{
    if (self->body_len > self->body_head) {
        H2BodyChunk *tail = &self->body_chunks[self->body_len - 1];
        Py_ssize_t old_size = PyByteArray_GET_SIZE(tail->body);
        if (len <= H2_BODY_COALESCE_MAX - old_size) {
            /* native-lint: allow NC004 -- growth is capped at one 16 KiB chunk */
            if (PyByteArray_Resize(tail->body, old_size + len) < 0) return -1;
            if (len > 0) {
                memcpy(PyByteArray_AS_STRING(tail->body) + old_size, data, (size_t)len);
            }
            tail->flow_bytes += flow_bytes;
            return 0;
        }
    }
    if (self->body_len == self->body_cap) {
        if (self->body_head > 0) {
            Py_ssize_t live = body_queue_len(self);
            memmove(self->body_chunks, self->body_chunks + self->body_head,
                    (size_t)live * sizeof(H2BodyChunk));
            self->body_len = live;
            self->body_head = 0;
        }
        if (self->body_len == self->body_cap) {
            Py_ssize_t cap = self->body_cap ? self->body_cap * 2 : 16;
            H2BodyChunk *grown = PyMem_Realloc(
                self->body_chunks, (size_t)cap * sizeof(H2BodyChunk));
            if (grown == NULL) return PyErr_NoMemory(), -1;
            self->body_chunks = grown;
            self->body_cap = cap;
        }
    }
    PyObject *body = PyByteArray_FromStringAndSize((const char *)data, len);
    if (body == NULL) return -1;
    H2BodyChunk *chunk = &self->body_chunks[self->body_len++];
    chunk->body = body;
    chunk->flow_bytes = flow_bytes;
    return 0;
}

static PyObject *
body_queue_pop(Http2Stream *self, Py_ssize_t *flow_bytes)
{
    H2BodyChunk *chunk = &self->body_chunks[self->body_head];
    PyObject *body = PyBytes_FromStringAndSize(
        PyByteArray_AS_STRING(chunk->body), PyByteArray_GET_SIZE(chunk->body));
    if (body == NULL) return NULL;
    *flow_bytes = chunk->flow_bytes;
    Py_CLEAR(chunk->body);
    chunk->flow_bytes = 0;
    self->body_head++;
    if (self->body_head == self->body_len) {
        self->body_head = self->body_len = 0;
    }
    return body;
}

static void
put_u32(uint8_t *p, uint32_t v)
{
    wreath_store_u32_be(p, v);
}

static uint32_t
get_u32(const uint8_t *p)
{
    return wreath_load_u32_be(p);
}

/* Append raw bytes to the connection's outbound bytearray. */
static int
h2_out(Http2Protocol *self, const uint8_t *data, Py_ssize_t len)
{
    return append_raw(self->out, (const char *)data, len);
}

static int
h2_write_frame(PyObject *proto, int type, int flags, uint32_t sid,
               const uint8_t *payload, Py_ssize_t len)
{
    Http2Protocol *self = (Http2Protocol *)proto;
    uint8_t header[9];
    header[0] = (uint8_t)(len >> 16);
    header[1] = (uint8_t)(len >> 8);
    header[2] = (uint8_t)len;
    header[3] = (uint8_t)type;
    header[4] = (uint8_t)flags;
    put_u32(header + 5, sid & 0x7FFFFFFF);
    if (h2_out(self, header, 9) < 0) {
        return -1;
    }
    if (len > 0 && h2_out(self, payload, len) < 0) {
        return -1;
    }
    return 0;
}

static int
h2_flush(PyObject *proto)
{
    Http2Protocol *self = (Http2Protocol *)proto;
    if (self->transport == NULL || self->transport_write_fn == NULL ||
        self->write_paused) {
        return 0;
    }
    Py_ssize_t n = PyByteArray_GET_SIZE(self->out);
    if (n == 0) {
        return 0;
    }
    /* Transfer the completed buffer to transport and continue with a fresh
     * bytearray. asyncio transports consume bytes-like objects synchronously,
     * so this removes a full output-sized bytes copy from every flush. */
    PyObject *replacement = PyByteArray_FromStringAndSize(NULL, 0);
    if (replacement == NULL) return -1;
    PyObject *chunk = self->out;
    self->out = replacement;
    PyObject *r = PyObject_CallOneArg(self->transport_write_fn, chunk);
    if (r == NULL) {
        Py_DECREF(self->out);
        self->out = chunk;
        return -1;
    }
    Py_DECREF(chunk);
    Py_DECREF(r);
    return 0;
}

static int
body_credit_consumed(Http2Protocol *self, Http2Stream *st, Py_ssize_t amount)
{
    if (amount <= 0) return 0;
    self->conn_consumed_pending += amount;
    st->recv_consumed_pending += amount;

    if (self->conn_consumed_pending >= self->conn_credit_threshold) {
        uint8_t increment[4];
        put_u32(increment, (uint32_t)self->conn_consumed_pending);
        if (h2_write_frame((PyObject *)self, FRAME_WINDOW_UPDATE, 0, 0,
                           increment, 4) < 0) {
            return -1;
        }
        self->conn_recv_window += self->conn_consumed_pending;
        self->conn_consumed_pending = 0;
    }
    if (!st->request_ended &&
        st->recv_consumed_pending >= st->recv_credit_threshold) {
        uint8_t increment[4];
        put_u32(increment, (uint32_t)st->recv_consumed_pending);
        if (h2_write_frame((PyObject *)self, FRAME_WINDOW_UPDATE, 0, st->id,
                           increment, 4) < 0) {
            return -1;
        }
        st->recv_window += st->recv_consumed_pending;
        st->recv_consumed_pending = 0;
    }
    return 0;
}

/* Send GOAWAY(last_stream_id, code) and mark the connection closing. */
static int
h2_connection_error(Http2Protocol *self, int code)
{
    if (!self->goaway_sent) {
        uint8_t payload[8];
        put_u32(payload, self->last_stream_id);
        put_u32(payload + 4, (uint32_t)code);
        if (h2_write_frame((PyObject *)self, FRAME_GOAWAY, 0, 0, payload, 8) < 0) {
            return -1;
        }
        self->goaway_sent = 1;
    }
    self->closing = 1;
    self->fatal = 1;
    self->accepting = 0;
    if (h2_flush((PyObject *)self) < 0) {
        return -1;
    }
    if (self->transport != NULL) {
        PyObject *r = PyObject_CallMethod(self->transport, "close", NULL);
        Py_XDECREF(r);
        if (r == NULL) {
            return -1;
        }
    }
    return 0;
}

static int
h2_stream_error(PyObject *proto, uint32_t sid, int code)
{
    uint8_t payload[4];
    put_u32(payload, (uint32_t)code);
    if (h2_write_frame(proto, FRAME_RST_STREAM, 0, sid, payload, 4) < 0) {
        return -1;
    }
    /* Drop the stream if present and notify the app of disconnect. */
    Http2Protocol *self = (Http2Protocol *)proto;
    PyObject *key = PyLong_FromUnsignedLong(sid);
    if (key == NULL) {
        return -1;
    }
    PyObject *st = PyDict_GetItemWithError(self->streams, key);
    if (st != NULL) {
        Http2Stream *stream = (Http2Stream *)st;
        stream->disconnected = 1;
        stream->state = S_CLOSED;
        h2_abort_pending_send(stream);
        if (stream->receive_waiter != NULL) {
            PyObject *msg = Py_BuildValue("{s:s}", "type", "http.disconnect");
            if (msg != NULL) {
                PyObject *w = stream->receive_waiter;
                stream->receive_waiter = NULL;
                h2_future_set_result(w, msg);
                Py_DECREF(w);
                Py_DECREF(msg);
            }
        }
        if (stream->task == NULL) {
            if (PyDict_DelItem(self->streams, key) == 0) {
                if (self->active_requests > 0) self->active_requests--;
            }
        }
    }
    if (PyDict_Pop(self->stream_window_blocked, key, NULL) < 0) {
        Py_DECREF(key);
        return -1;
    }
    Py_DECREF(key);
    return 0;
}

/* ======================================================================== */
/* Http2Stream methods                                                      */
/* ======================================================================== */

static int
h2_stream_is_native(Http2Stream *self)
{
    return self->scope != NULL && wreath_request_context_check(self->scope);
}

static PyObject *
h2_body_slot(PyObject *body, int more, int disconnected)
{
    return PyTuple_Pack(3, body, more ? Py_True : Py_False,
                        disconnected ? Py_True : Py_False);
}

static PyObject *
stream_receive(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    Http2Stream *self = (Http2Stream *)op;
    /* disconnect takes priority */
    if (self->disconnected) {
        PyObject *msg = h2_stream_is_native(self)
            ? h2_body_slot(Py_None, 0, 1)
            : Py_BuildValue("{s:s}", "type", "http.disconnect");
        if (msg == NULL) {
            return NULL;
        }
        PyObject *aw = completed_value(msg);
        Py_DECREF(msg);
        return aw;
    }
    /* buffered body? */
    if (body_queue_len(self) > 0) {
        Py_ssize_t flow_bytes = 0;
        PyObject *body = body_queue_pop(self, &flow_bytes);
        if (body == NULL) {
            return NULL;
        }
        Http2Protocol *protocol = (Http2Protocol *)self->protocol;
        if (body_credit_consumed(protocol, self, flow_bytes) < 0 ||
            h2_flush((PyObject *)protocol) < 0) {
            Py_DECREF(body);
            return NULL;
        }
        int more = body_queue_len(self) > 0 || !self->request_ended;
        PyObject *msg = h2_stream_is_native(self)
            ? h2_body_slot(body, more, 0)
            : Py_BuildValue("{s:s,s:O,s:O}", "type", "http.request",
                            "body", body, "more_body",
                            more ? Py_True : Py_False);
        Py_DECREF(body);
        if (msg == NULL) {
            return NULL;
        }
        PyObject *aw = completed_value(msg);
        Py_DECREF(msg);
        return aw;
    }
    if (self->request_ended) {
        PyObject *msg = h2_stream_is_native(self)
            ? h2_body_slot(Py_None, 0, 0)
            : Py_BuildValue("{s:s,s:y#,s:O}", "type", "http.request",
                            "body", "", 0, "more_body", Py_False);
        if (msg == NULL) {
            return NULL;
        }
        PyObject *aw = completed_value(msg);
        Py_DECREF(msg);
        return aw;
    }
    /* wait for more body */
    Http2Protocol *proto = (Http2Protocol *)self->protocol;
    PyObject *fut = h2_make_future(proto);
    if (fut == NULL) {
        return NULL;
    }
    Py_XSETREF(self->receive_waiter, Py_NewRef(fut));
    return fut;  /* awaiting the future */
}


static int
response_header_parts(PyObject *pair, PyObject **name, PyObject **value)
{
    if (PyTuple_Check(pair) && PyTuple_GET_SIZE(pair) == 2) {
        *name = PyTuple_GET_ITEM(pair, 0);
        *value = PyTuple_GET_ITEM(pair, 1);
    }
    else if (PyList_Check(pair) && PyList_GET_SIZE(pair) == 2) {
        *name = PyList_GET_ITEM(pair, 0);
        *value = PyList_GET_ITEM(pair, 1);
    }
    else {
        PyErr_SetString(PyExc_RuntimeError, "response header must be a pair");
        return -1;
    }
    if (!PyBytes_Check(*name) || !PyBytes_Check(*value)) {
        PyErr_SetString(PyExc_RuntimeError, "response header must be bytes");
        return -1;
    }
    const char *name_data = PyBytes_AS_STRING(*name);
    Py_ssize_t name_size = PyBytes_GET_SIZE(*name);
    if (!(name_size > 0 && name_data[0] == ':') &&
        !wreath_field_name_valid(name_data, name_size)) {
        PyErr_SetString(PyExc_RuntimeError, "invalid response header");
        return -1;
    }
    if (!wreath_field_value_valid(
            PyBytes_AS_STRING(*value), PyBytes_GET_SIZE(*value))) {
        PyErr_SetString(PyExc_RuntimeError, "invalid response header");
        return -1;
    }
    return 0;
}

/* Encode a header list into HPACK literals appended to `block`. */
static int
encode_header_block(PyObject *block, int status, PyObject *headers,
                    PyObject *default_headers)
{
    /* :status first */
    char status_buf[4];
    int n = PyOS_snprintf(status_buf, sizeof(status_buf), "%d", status);
    if (n < 0) {
        return -1;
    }
    if (wreath_hpack_encode_literal(block, (const uint8_t *)":status", 7,
                                 (const uint8_t *)status_buf, n) < 0) {
        return -1;
    }
    int has_date = 0;
    int has_server = 0;
    PyObject *items = NULL;
    if (headers != NULL) {
        items = PySequence_Fast(headers, "response headers must be a sequence");
        if (items == NULL) {
            return -1;
        }
    }
    Py_ssize_t count = items == NULL ? 0 : PySequence_Fast_GET_SIZE(items);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PySequence_Fast_GET_ITEM(items, i);
        PyObject *name;
        PyObject *value;
        if (response_header_parts(pair, &name, &value) < 0) {
            Py_DECREF(items);
            return -1;
        }
        char *nptr, *vptr;
        Py_ssize_t nlen, vlen;
        nptr = PyBytes_AS_STRING(name);
        nlen = PyBytes_GET_SIZE(name);
        vptr = PyBytes_AS_STRING(value);
        vlen = PyBytes_GET_SIZE(value);
        /* Skip connection-specific and pseudo headers in responses. */
        int skip = (nlen > 0 && nptr[0] == ':');
        if (wreath_ascii_equal_ci(nptr, nlen, "date", 4)) has_date = 1;
        if (wreath_ascii_equal_ci(nptr, nlen, "server", 6)) has_server = 1;
        if (!skip && wreath_ascii_equal_ci(nptr, nlen, "connection", 10)) skip = 1;
        if (!skip) {
            if (wreath_hpack_encode_literal(block, (const uint8_t *)nptr, nlen,
                                         (const uint8_t *)vptr, vlen) < 0) {
                Py_DECREF(items);
                return -1;
            }
        }
    }
    Py_XDECREF(items);
    for (Py_ssize_t i = 0; i < PySequence_Fast_GET_SIZE(default_headers); i++) {
        PyObject *pair = PySequence_Fast_GET_ITEM(default_headers, i);
        PyObject *name;
        PyObject *value;
        if (response_header_parts(pair, &name, &value) < 0) {
            return -1;
        }
        char *nptr, *vptr;
        Py_ssize_t nlen, vlen;
        nptr = PyBytes_AS_STRING(name);
        nlen = PyBytes_GET_SIZE(name);
        vptr = PyBytes_AS_STRING(value);
        vlen = PyBytes_GET_SIZE(value);
        if ((has_date && wreath_ascii_equal_ci(nptr, nlen, "date", 4)) ||
            (has_server && wreath_ascii_equal_ci(nptr, nlen, "server", 6))) continue;
        if (wreath_hpack_encode_literal(block, (const uint8_t *)nptr, nlen,
                                     (const uint8_t *)vptr, vlen) < 0) {
            return -1;
        }
    }
    return 0;
}

/* The whole ASGI body message has been framed: release the retained body and
 * resolve the pending await exactly once. */
static int
h2_finish_pending_send(PyObject *proto, Http2Stream *st)
{
    int end = st->pending_end_stream;
    Py_CLEAR(st->pending_body);
    st->pending_offset = 0;
    st->pending_end_stream = 0;
    if (st->send_waiter != NULL) {
        PyObject *w = st->send_waiter;
        st->send_waiter = NULL;
        int rc = h2_future_set_result(w, Py_None);
        Py_DECREF(w);
        if (rc < 0) {
            return -1;
        }
    }
    if (end) {
        st->response_ended = 1;
        h2_maybe_close_stream(proto, st);
    }
    return 0;
}

/* Release a blocked send without completing it: drop the retained body and
 * cancel the await. Used on reset, connection error, and transport loss so an
 * application task is never left suspended on a stream that can no longer send. */
static void
h2_abort_pending_send(Http2Stream *st)
{
    Py_CLEAR(st->pending_body);
    st->pending_offset = 0;
    st->pending_end_stream = 0;
    if (st->send_waiter == NULL) {
        return;
    }
    PyObject *w = st->send_waiter;
    st->send_waiter = NULL;
    PyObject *done = PyObject_CallMethod(w, "done", NULL);
    int is_done = done ? PyObject_IsTrue(done) : 1;
    Py_XDECREF(done);
    if (!is_done) {
        PyObject *r = PyObject_CallMethod(w, "cancel", NULL);
        Py_XDECREF(r);
    }
    if (PyErr_Occurred()) {
        PyErr_Clear();
    }
    Py_DECREF(w);
}

/* A productive frame arrived (new request, real body bytes, or we framed
 * response data toward the peer): forgive accumulated unproductive frames. */
static void
h2_note_progress(Http2Protocol *self)
{
    self->idle_frames = 0;
}

/* Charge one unproductive frame. Returns 0 to continue, or the result of
 * h2_connection_error once the budget is exceeded (which sets self->fatal, so
 * callers should re-check it before doing any more work for this frame). */
static int
h2_note_unproductive(Http2Protocol *self)
{
    if (++self->idle_frames <= H2_MAX_UNPRODUCTIVE_FRAMES) {
        return 0;
    }
    return h2_connection_error(self, H2_ENHANCE_YOUR_CALM);
}

/* Add a stream to the outbound active queue exactly once. The cursor queue owns
 * a strong reference, so reset/close may safely leave a stale entry for the
 * bounded scheduler to discard later. */
static int
mark_conn_blocked(Http2Protocol *self, Http2Stream *st)
{
    if (st->in_conn_blocked) {
        return 0;
    }
    if (PyList_Append(self->conn_blocked, (PyObject *)st) < 0) {
        return -1;
    }
    st->in_conn_blocked = 1;
    return 0;
}

/* SETTINGS_INITIAL_WINDOW_SIZE is connection-wide, but applying it eagerly to
 * every live stream makes one six-byte setting O(active streams). Reconcile a
 * stream only when its send window is touched; the difference telescopes to
 * the current setting, so repeated SETTINGS frames remain O(1). */
static void
h2_sync_stream_send_window(Http2Protocol *self, Http2Stream *st)
{
    st->send_window += self->peer_initial_window - st->applied_initial_window;
    st->applied_initial_window = self->peer_initial_window;
}

static int
mark_stream_window_blocked(Http2Protocol *self, Http2Stream *st)
{
    PyObject *key = PyLong_FromUnsignedLong(st->id);
    if (key == NULL) {
        return -1;
    }
    int result = PyDict_SetItem(
        self->stream_window_blocked, key, (PyObject *)st
    );
    Py_DECREF(key);
    return result;
}

static int
unmark_stream_window_blocked(Http2Protocol *self, uint32_t stream_id)
{
    PyObject *key = PyLong_FromUnsignedLong(stream_id);
    if (key == NULL) {
        return -1;
    }
    int result = PyDict_Pop(self->stream_window_blocked, key, NULL);
    Py_DECREF(key);
    return result < 0 ? -1 : 0;
}

/* Only streams whose response is genuinely waiting for stream credit need to
 * be reconsidered after an increase. Idle/open streams stay lazy; the work for
 * blocked responses is bounded by work that can now make progress. */
static int
wake_stream_window_blocked(Http2Protocol *self)
{
    PyObject *blocked = self->stream_window_blocked;
    self->stream_window_blocked = PyDict_New();
    if (self->stream_window_blocked == NULL) {
        self->stream_window_blocked = blocked;
        return -1;
    }
    PyObject *key;
    PyObject *stream;
    Py_ssize_t pos = 0;
    while (PyDict_Next(blocked, &pos, &key, &stream)) {
        Http2Stream *st = (Http2Stream *)stream;
        h2_sync_stream_send_window(self, st);
        if (st->pending_body == NULL || st->state == S_CLOSED) {
            continue;
        }
        if (st->send_window <= 0) {
            if (PyDict_SetItem(self->stream_window_blocked, key, stream) < 0) {
                Py_DECREF(blocked);
                return -1;
            }
        }
        else if (mark_conn_blocked(self, st) < 0) {
            Py_DECREF(blocked);
            return -1;
        }
    }
    Py_DECREF(blocked);
    return h2_schedule_pending((PyObject *)self);
}

static int
h2_schedule_continuation(Http2Protocol *self)
{
    if (self->scheduler_scheduled || self->write_paused || self->fatal ||
        self->conn_send_window <= 0 ||
        self->conn_blocked_head >= PyList_GET_SIZE(self->conn_blocked)) {
        return 0;
    }
    PyObject *handle = PyObject_CallMethod(
        self->loop, "call_soon", "O", self->scheduler_callable
    );
    if (handle == NULL) {
        return -1;
    }
    Py_DECREF(handle);
    self->scheduler_scheduled = 1;
    return 0;
}

/* Frame at most one persistent DRR deficit from this stream. Bytes come
 * directly from the application's immutable body object; transport pressure,
 * stream credit, connection credit, and the deficit are all prerequisites. */
static int
h2_flush_stream_pending(PyObject *proto, Http2Stream *st)
{
    Http2Protocol *self = (Http2Protocol *)proto;
    if (st->pending_body == NULL) {
        return 0;
    }
    if (self->write_paused) {
        return mark_conn_blocked(self, st);
    }
    h2_sync_stream_send_window(self, st);
    Py_ssize_t total = PyBytes_GET_SIZE(st->pending_body);
    while (st->pending_offset < total) {
        int64_t window = st->send_window;
        if (window > self->conn_send_window) {
            window = self->conn_send_window;
        }
        if (window <= 0) {
            if (self->conn_send_window <= 0 && st->send_window > 0) {
                return mark_conn_blocked(self, st);
            }
            return mark_stream_window_blocked(self, st);
        }
        if (st->send_deficit <= 0) {
            return mark_conn_blocked(self, st);
        }
        Py_ssize_t chunk = total - st->pending_offset;
        if ((int64_t)chunk > window) {
            chunk = (Py_ssize_t)window;
        }
        if (chunk > self->our_max_frame) {
            chunk = self->our_max_frame;
        }
        if ((int64_t)chunk > st->send_deficit) {
            chunk = (Py_ssize_t)st->send_deficit;
        }
        int last = (st->pending_offset + chunk == total) && st->pending_end_stream;
        if (h2_write_frame(proto, FRAME_DATA, last ? FLAG_END_STREAM : 0, st->id,
                           (const uint8_t *)PyBytes_AS_STRING(st->pending_body)
                               + st->pending_offset,
                           chunk) < 0) {
            return -1;
        }
        st->pending_offset += chunk;
        st->send_window -= chunk;
        st->send_deficit -= chunk;
        self->conn_send_window -= chunk;
        h2_note_progress(self);
        if (self->nfr_worker != NULL) {
            st->nfr_bytes_out += (uint64_t)chunk;
        }
    }
    if (total == 0 && st->pending_end_stream) {
        if (h2_write_frame(proto, FRAME_DATA, FLAG_END_STREAM, st->id, NULL, 0) < 0) {
            return -1;
        }
    }
    return h2_finish_pending_send(proto, st);
}

static Py_ssize_t
h2_select_pending_index(Http2Protocol *self, Py_ssize_t head)
{
    if (!self->rfc9218_enabled) {
        return head;
    }
    Py_ssize_t size = PyList_GET_SIZE(self->conn_blocked);
    Py_ssize_t limit = head + H2_SCHED_MAX_STREAMS;
    if (limit > size) {
        limit = size;
    }
    Py_ssize_t best = head;
    uint8_t best_urgency = 8;
    int best_incremental = 1;
    Py_ssize_t exclusive = -1;
    uint8_t exclusive_urgency = 8;
    for (Py_ssize_t index = head; index < limit; index++) {
        Http2Stream *candidate = (Http2Stream *)PyList_GET_ITEM(
            self->conn_blocked, index);
        if (candidate->pending_body == NULL || candidate->state == S_CLOSED) {
            continue;
        }
        if (candidate->id == self->nonincremental_stream &&
            !candidate->incremental) {
            exclusive = index;
            exclusive_urgency = candidate->urgency;
        }
        if (candidate->urgency < best_urgency ||
            (candidate->urgency == best_urgency && best_incremental &&
             !candidate->incremental)) {
            best = index;
            best_urgency = candidate->urgency;
            best_incremental = candidate->incremental;
        }
    }
    if (exclusive >= 0 && exclusive_urgency <= best_urgency) {
        return exclusive;
    }
    return best;
}

static int
h2_schedule_pending(PyObject *proto)
{
    Http2Protocol *self = (Http2Protocol *)proto;
    if (self->write_paused || self->fatal) {
        return 0;
    }
    Py_ssize_t visited = 0;
    Py_ssize_t framed = 0;
    while (self->conn_blocked_head < PyList_GET_SIZE(self->conn_blocked) &&
           visited < H2_SCHED_MAX_STREAMS && framed < H2_SCHED_MAX_BYTES) {
        Py_ssize_t selected = h2_select_pending_index(
            self, self->conn_blocked_head);
        if (selected != self->conn_blocked_head) {
            PyObject *at_head = PyList_GET_ITEM(
                self->conn_blocked, self->conn_blocked_head);
            PyObject *at_selected = PyList_GET_ITEM(self->conn_blocked, selected);
            PyList_SET_ITEM(self->conn_blocked, self->conn_blocked_head,
                            at_selected);
            PyList_SET_ITEM(self->conn_blocked, selected, at_head);
        }
        Http2Stream *st = (Http2Stream *)PyList_GET_ITEM(
            self->conn_blocked, self->conn_blocked_head++
        );
        st->in_conn_blocked = 0;
        visited++;
        if (st->pending_body == NULL || st->state == S_CLOSED) {
            continue;
        }
        h2_sync_stream_send_window(self, st);
        Py_ssize_t before = PyBytes_GET_SIZE(st->pending_body) - st->pending_offset;
        /* A blocked turn earns no deficit: otherwise repeated wakeups with no
         * connection credit let the first stream hoard the next increment. */
        if (st->send_window > 0 && self->conn_send_window > 0) {
            int64_t quantum = self->rfc9218_enabled && !st->incremental
                ? 2 * H2_DRR_QUANTUM : H2_DRR_QUANTUM;
            st->send_deficit += quantum;
            if (st->send_deficit > 2 * quantum) {
                st->send_deficit = 2 * quantum;
            }
        }
        if (h2_flush_stream_pending(proto, st) < 0) {
            return -1;
        }
        Py_ssize_t after = st->pending_body == NULL
            ? 0 : PyBytes_GET_SIZE(st->pending_body) - st->pending_offset;
        Py_ssize_t progress = before - after;
        framed += progress;
        if (self->rfc9218_enabled && !st->incremental && after > 0 &&
            progress > 0) {
            self->nonincremental_stream = st->id;
        } else if (self->nonincremental_stream == st->id &&
                   (after == 0 || progress == 0)) {
            self->nonincremental_stream = 0;
        }
    }

    Py_ssize_t size = PyList_GET_SIZE(self->conn_blocked);
    Py_ssize_t live = size - self->conn_blocked_head;
    if (live == 0) {
        if (PyList_SetSlice(self->conn_blocked, 0, size, NULL) < 0) {
            return -1;
        }
        self->conn_blocked_head = 0;
    } else if (self->conn_blocked_head >= H2_SCHED_MAX_STREAMS &&
               live <= H2_SCHED_MAX_STREAMS) {
        PyObject *tail = PyList_GetSlice(self->conn_blocked,
                                         self->conn_blocked_head, size);
        if (tail == NULL) {
            return -1;
        }
        Py_SETREF(self->conn_blocked, tail);
        self->conn_blocked_head = 0;
    }
    return h2_schedule_continuation(self);
}

static PyObject *
h2_run_write_scheduler(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    Http2Protocol *self = (Http2Protocol *)op;
    self->scheduler_scheduled = 0;
    if (h2_schedule_pending(op) < 0 || h2_flush(op) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
h2_response_start(Http2Stream *self, PyObject *status_obj, PyObject *headers,
                  int trailers_expected, int flush_now)
{
    Http2Protocol *proto = (Http2Protocol *)self->protocol;
    long status = status_obj ? PyLong_AsLong(status_obj) : 200;
    if (status == -1 && PyErr_Occurred()) return NULL;
    if (status < 100 || status > 999) {
        PyErr_SetString(PyExc_ValueError, "response status must be between 100 and 999");
        return NULL;
    }
    if (self->response_started) {
        PyErr_SetString(PyExc_RuntimeError, "response already started");
        return NULL;
    }
    if (self->policy_state.native &&
        wreath_policy_egress(&proto->policy, &self->policy_state, headers) < 0) {
        return NULL;
    }
    if (self->scope != NULL) {
        wreath_request_context_update_policy(self->scope, &self->policy_state);
    }
    self->nfr_status = (int)status;
    self->trailers_expected = trailers_expected;
    PyObject *block = PyByteArray_FromStringAndSize("", 0);
    if (block == NULL) return NULL;
    if (encode_header_block(block, (int)status, headers,
                            proto->default_response_headers) < 0 ||
        h2_write_frame((PyObject *)proto, FRAME_HEADERS, FLAG_END_HEADERS,
                       self->id,
                       (const uint8_t *)PyByteArray_AS_STRING(block),
                       PyByteArray_GET_SIZE(block)) < 0) {
        Py_DECREF(block);
        return NULL;
    }
    Py_DECREF(block);
    self->response_started = 1;
    if (flush_now && h2_flush((PyObject *)proto) < 0) return NULL;
    return completed_none();
}


static PyObject *
h2_response_body(Http2Stream *self, PyObject *body, int more_body, int flush_now)
{
    Http2Protocol *proto = (Http2Protocol *)self->protocol;
    if (!self->response_started) {
        PyErr_SetString(PyExc_RuntimeError, "response not started");
        return NULL;
    }
    if (self->send_waiter != NULL) {
        PyErr_SetString(PyExc_RuntimeError, "HTTP/2 send already pending");
        return NULL;
    }
    PyObject *owned = NULL;
    if (body == NULL || body == Py_None) {
        owned = PyBytes_FromStringAndSize("", 0);
        if (owned == NULL) return NULL;
        body = owned;
    } else if (!PyBytes_Check(body)) {
        PyErr_SetString(PyExc_TypeError, "http.response.body 'body' must be bytes");
        return NULL;
    }
    Py_XSETREF(self->pending_body, Py_NewRef(body));
    Py_XDECREF(owned);
    self->pending_offset = 0;
    self->pending_end_stream = !more_body && !self->trailers_expected;
    if (mark_conn_blocked(proto, self) < 0 ||
        h2_schedule_pending((PyObject *)proto) < 0 ||
        (flush_now && h2_flush((PyObject *)proto) < 0)) {
        return NULL;
    }
    if (self->pending_body == NULL) return completed_none();
    PyObject *future = h2_make_future(proto);
    if (future == NULL) return NULL;
    self->send_waiter = Py_NewRef(future);
    return future;
}


static PyObject *
stream_send(PyObject *op, PyObject *message)
{
    Http2Stream *self = (Http2Stream *)op;
    Http2Protocol *proto = (Http2Protocol *)self->protocol;
    if (proto == NULL || self->state == S_CLOSED) return completed_none();
    PyObject *type = PyDict_GetItem(message, s_type);
    if (type == NULL) {
        PyErr_SetString(PyExc_KeyError, "message has no 'type'");
        return NULL;
    }
    const char *t = PyUnicode_AsUTF8(type);
    if (t == NULL) return NULL;
    if (strcmp(t, "http.response.start") == 0) {
        PyObject *trailers = PyDict_GetItem(message, s_trailers);
        int has_trailers = trailers != NULL && PyObject_IsTrue(trailers);
        return h2_response_start(
            self, PyDict_GetItem(message, s_status),
            PyDict_GetItem(message, s_headers), has_trailers, 1
        );
    }
    if (strcmp(t, "http.response.body") == 0) {
        PyObject *more = PyDict_GetItem(message, s_more_body);
        return h2_response_body(
            self, PyDict_GetItem(message, s_body),
            more != NULL && PyObject_IsTrue(more), 1
        );
    }
    if (strcmp(t, "http.response.trailers") == 0) {
        if (self->send_waiter != NULL) {
            PyErr_SetString(PyExc_RuntimeError, "HTTP/2 send already pending");
            return NULL;
        }
        PyObject *headers = PyDict_GetItem(message, s_headers);
        PyObject *block = PyByteArray_FromStringAndSize("", 0);
        if (block == NULL) {
            return NULL;
        }
        PyObject *items = headers != NULL
            ? PySequence_Fast(headers, "response trailers must be a sequence")
            : PyTuple_New(0);
        if (items == NULL) {
            Py_DECREF(block);
            return NULL;
        }
        Py_ssize_t count = PySequence_Fast_GET_SIZE(items);
        for (Py_ssize_t i = 0; i < count; i++) {
            PyObject *name;
            PyObject *value;
            PyObject *pair = PySequence_Fast_GET_ITEM(items, i);
            if (response_header_parts(pair, &name, &value) < 0) {
                Py_DECREF(items);
                Py_DECREF(block);
                return NULL;
            }
            char *nptr = PyBytes_AS_STRING(name);
            Py_ssize_t nlen = PyBytes_GET_SIZE(name);
            if (nptr[0] == ':'
                || wreath_ascii_equal_ci(nptr, nlen, "connection", 10)
                || wreath_ascii_equal_ci(nptr, nlen, "content-length", 14)
                || wreath_ascii_equal_ci(nptr, nlen, "transfer-encoding", 17)) {
                Py_DECREF(items);
                Py_DECREF(block);
                PyErr_SetString(PyExc_RuntimeError,
                                "invalid HTTP/2 response trailer");
                return NULL;
            }
            char *vptr = PyBytes_AS_STRING(value);
            Py_ssize_t vlen = PyBytes_GET_SIZE(value);
            if (wreath_hpack_encode_literal(
                    block, (const uint8_t *)nptr, nlen,
                    (const uint8_t *)vptr, vlen) < 0) {
                Py_DECREF(items);
                Py_DECREF(block);
                return NULL;
            }
        }
        Py_DECREF(items);
        if (h2_write_frame((PyObject *)proto, FRAME_HEADERS,
                           FLAG_END_HEADERS | FLAG_END_STREAM, self->id,
                           (const uint8_t *)PyByteArray_AS_STRING(block),
                           PyByteArray_GET_SIZE(block)) < 0) {
            Py_DECREF(block);
            return NULL;
        }
        Py_DECREF(block);
        self->response_ended = 1;
        h2_flush((PyObject *)proto);
        h2_maybe_close_stream((PyObject *)proto, self);
        return completed_none();
    }
    /* ignore unknown message types */
    return completed_none();
}


static PyObject *
stream_wreath_response(Http2Stream *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 3 && nargs != 4) {
        PyErr_Format(
            PyExc_TypeError, "_wreath_response expected 3 or 4 arguments, got %zd", nargs
        );
        return NULL;
    }
    if (self->protocol == NULL || self->state == S_CLOSED) return completed_none();
    PyObject *body = Py_NewRef(args[2]);
    int authenticated = nargs == 4 ? PyObject_IsTrue(args[3]) : 0;
    Http2Protocol *proto = (Http2Protocol *)self->protocol;
    if (authenticated < 0 || (proto->policy.response_transform && wreath_policy_response(
            &proto->policy, &self->policy_state, args[0], args[1], &body,
            authenticated) < 0)) {
        Py_DECREF(body);
        return NULL;
    }
    /* This is a one-shot response: retain HEADERS until DATA is framed below,
     * then h2_response_body emits both in one transport write. The portable
     * response.start path still flushes immediately because user code may
     * suspend before sending its body. */
    PyObject *started = h2_response_start(self, args[0], args[1], 0, 0);
    if (started == NULL) {
        Py_DECREF(body);
        return NULL;
    }
    Py_DECREF(started);
    PyObject *result = h2_response_body(self, body, 0, !proto->app_batch_active);
    Py_DECREF(body);
    return result;
}

static PyObject *
stream_wreath_response_nowait(
    Http2Stream *self, PyObject *const *args, Py_ssize_t nargs)
{
    PyObject *result = stream_wreath_response(self, args, nargs);
    if (result == immediate_none) {
        Py_DECREF(result);
        Py_RETURN_NONE;
    }
    return result;
}

static PyObject *
stream_wreath_start(Http2Stream *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 2) {
        PyErr_Format(PyExc_TypeError,
                     "_wreath_stream_start expected 2 arguments, got %zd", nargs);
        return NULL;
    }
    return h2_response_start(self, args[0], args[1], 0, 1);
}

static PyObject *
stream_wreath_body(Http2Stream *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 2) {
        PyErr_Format(PyExc_TypeError,
                     "_wreath_stream_body expected 2 arguments, got %zd", nargs);
        return NULL;
    }
    int more = PyObject_IsTrue(args[1]);
    if (more < 0) return NULL;
    return h2_response_body(self, args[0], more, 1);
}


static void
h2_maybe_close_stream(PyObject *proto, Http2Stream *st)
{
    Http2Protocol *self = (Http2Protocol *)proto;
    if (!st->response_ended) {
        return;
    }
    st->state = S_CLOSED;
    PyObject *key = PyLong_FromUnsignedLong(st->id);
    if (key == NULL) {
        PyErr_Clear();
        return;
    }
    int removed = PyDict_Pop(self->streams, key, NULL);
    int unblocked = PyDict_Pop(self->stream_window_blocked, key, NULL);
    Py_DECREF(key);
    if (removed < 0 || unblocked < 0) {
        /* Nothing here can propagate: every caller is a void or completion
         * path. Surface it rather than continuing with an exception set. */
        PyErr_WriteUnraisable((PyObject *)self);
        return;
    }
    if (removed == 1 && self->active_requests > 0) {
        self->active_requests--;
    }
    /* If the connection is draining and this was the last stream, close. */
    if (self->closing && self->active_requests == 0 && self->transport != NULL) {
        PyObject *r = PyObject_CallMethod(self->transport, "close", NULL);
        Py_XDECREF(r);
    }
}

static void
stream_finish(Http2Stream *self, PyObject *exc, PyObject *owner,
              uint8_t nfr_terminal)
{
    Http2Protocol *proto = (Http2Protocol *)self->protocol;
    if (self->timeout_handle != NULL) {
        PyObject *cancelled = PyObject_CallMethod(self->timeout_handle, "cancel", NULL);
        if (cancelled == NULL) PyErr_Clear();
        else Py_DECREF(cancelled);
        Py_CLEAR(self->timeout_handle);
    }
    /* Publish one completion cell for this stream's request. The worker lives on
     * the protocol; the context lives on the stream. */
    if (proto != NULL && proto->nfr_worker != NULL && self->nfr_active) {
        self->nfr_active = 0;
        /* Route/plan attribution stamped by Python dispatch into the scope dict
         * as a (route_id, plan_id) tuple; left as None for unattributed routes. */
        if (self->scope != NULL && PyDict_Check(self->scope)) {
            PyObject *attr = PyDict_GetItemString(self->scope, "_wreath_flight");
            if (attr != NULL && PyTuple_CheckExact(attr) &&
                PyTuple_GET_SIZE(attr) == 2) {
                unsigned long rid = PyLong_AsUnsignedLong(PyTuple_GET_ITEM(attr, 0));
                unsigned long pid = PyLong_AsUnsignedLong(PyTuple_GET_ITEM(attr, 1));
                if (!PyErr_Occurred()) {
                    flight_capi->context_route(&self->nfr_ctx, (uint32_t)rid,
                                               (uint32_t)pid);
                } else {
                    PyErr_Clear();
                }
            }
        }
        flight_capi->context_end(proto->nfr_worker, &self->nfr_ctx,
                                 wreath_flight_now_ns(), (uint32_t)self->nfr_status,
                                 nfr_terminal, 0, (uint64_t)self->body_received,
                                 self->nfr_bytes_out);
        if (wreath_request_context_check(self->scope)) {
            wreath_request_context_sever(self->scope);
        }
    }
    if (exc != NULL && exc != Py_None) {
        /* Application raised. */
        if (proto != NULL && !self->response_started) {
            /* minimal 500 */
            PyObject *block = PyByteArray_FromStringAndSize("", 0);
            if (block != NULL) {
                encode_header_block(block, 500, NULL,
                                    proto->default_response_headers);
                h2_write_frame((PyObject *)proto, FRAME_HEADERS,
                               FLAG_END_HEADERS | FLAG_END_STREAM, self->id,
                               (const uint8_t *)PyByteArray_AS_STRING(block),
                               PyByteArray_GET_SIZE(block));
                Py_DECREF(block);
                self->response_started = 1;
                self->response_ended = 1;
            }
        } else if (proto != NULL && !self->response_ended) {
            h2_stream_error((PyObject *)proto, self->id, H2_INTERNAL_ERROR);
        }
        PyErr_WriteUnraisable(owner);
    }
    if (proto != NULL) {
        if (!self->response_ended && self->response_started && !self->disconnected) {
            /* app finished without a final empty body: end the stream */
            h2_write_frame((PyObject *)proto, FRAME_DATA, FLAG_END_STREAM,
                           self->id, NULL, 0);
        }
        /* Application execution has completed: the stream is finished regardless
         * of whether it ever needed a Task or produced a full response. */
        self->response_ended = 1;
        if (!proto->app_batch_active) {
            h2_flush((PyObject *)proto);
        }
        Py_CLEAR(self->task);
        h2_maybe_close_stream((PyObject *)proto, self);
    }
}

static PyObject *
stream_done(PyObject *op, PyObject *task)
{
    Http2Stream *self = (Http2Stream *)op;
    PyObject *exc = PyObject_CallMethod(task, "exception", NULL);
    uint8_t nfr_terminal = WREATH_NFR_TERM_OK;
    if (exc == NULL) {
        /* task was cancelled */
        nfr_terminal = WREATH_NFR_TERM_CANCELLED;
        PyErr_Clear();
    }
    else if (exc != Py_None) {
        nfr_terminal = WREATH_NFR_TERM_ERROR;
    }
    stream_finish(self, exc, task, nfr_terminal);
    Py_XDECREF(exc);
    Py_RETURN_NONE;
}

static PyObject *
stream_timeout(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    Http2Stream *self = (Http2Stream *)op;
    Http2Protocol *proto = (Http2Protocol *)self->protocol;
    Py_CLEAR(self->timeout_handle);
    if (proto == NULL || self->state == S_CLOSED) Py_RETURN_NONE;
    if (!self->response_started) {
        PyObject *status = PyLong_FromLong(408);
        PyObject *headers = PyList_New(0);
        PyObject *body = PyBytes_FromStringAndSize("", 0);
        PyObject *started = status != NULL && headers != NULL
            ? h2_response_start(self, status, headers, 0, 0) : NULL;
        PyObject *finished = started != NULL && body != NULL
            ? h2_response_body(self, body, 0, 1) : NULL;
        Py_XDECREF(status);
        Py_XDECREF(headers);
        Py_XDECREF(body);
        Py_XDECREF(started);
        if (finished == NULL) return NULL;
        Py_DECREF(finished);
    }
    else if (!self->response_ended
             && h2_stream_error((PyObject *)proto, self->id, H2_CANCEL) < 0) {
        return NULL;
    }
    if (self->task != NULL) {
        PyObject *cancelled = PyObject_CallMethod(self->task, "cancel", NULL);
        if (cancelled == NULL) return NULL;
        Py_DECREF(cancelled);
    }
    Py_RETURN_NONE;
}

static int
start_stream_app(Http2Protocol *proto, Http2Stream *stream)
{
    PyObject *app_args[3] = {
        stream->scope, stream->receive_callable, stream->send_callable
    };
    PyObject *target = proto->native_app != NULL ? proto->native_app : proto->app;
    PyObject *awaitable = PyObject_Vectorcall(target, app_args, 3, NULL);
    PyObject *driver = NULL;
    PyObject *yielded = NULL;
    PyObject *continuation = NULL;
    PyObject *task = NULL;
    if (awaitable == NULL) {
        return -1;
    }

    if (PyCoro_CheckExact(awaitable) ||
        Py_IS_TYPE(awaitable, &ImmediateAwaitableType) ||
        Py_IS_TYPE(awaitable, &ValueAwaitableType)) {
        driver = awaitable;
        awaitable = NULL;
    }
    else {
        driver = wreath_awaitable_iter(awaitable);
        if (driver == NULL) {
            PyObject *error = PyErr_GetRaisedException();
            if (error != NULL) {
                stream_finish(stream, error, awaitable, WREATH_NFR_TERM_ERROR);
                Py_DECREF(error);
            }
            Py_DECREF(awaitable);
            return error == NULL ? -1 : 0;
        }
    }

    PySendResult state = PyIter_Send(driver, Py_None, &yielded);
    if (state == PYGEN_RETURN) {
        stream_finish(stream, Py_None, (PyObject *)stream, WREATH_NFR_TERM_OK);
        Py_XDECREF(awaitable);
        Py_DECREF(driver);
        Py_XDECREF(yielded);
        return 0;
    }
    if (state == PYGEN_ERROR) {
        PyObject *error = PyErr_GetRaisedException();
        if (error == NULL) {
            Py_XDECREF(awaitable);
            Py_DECREF(driver);
            return -1;
        }
        stream_finish(stream, error, driver, WREATH_NFR_TERM_ERROR);
        Py_DECREF(error);
        Py_XDECREF(awaitable);
        Py_DECREF(driver);
        return 0;
    }

    continuation = wreath_started_coroutine(driver, yielded);
    if (continuation != NULL) {
        task = PyObject_CallOneArg(proto->loop_create_task, continuation);
    }
    Py_XDECREF(awaitable);
    Py_DECREF(driver);
    Py_XDECREF(yielded);
    Py_XDECREF(continuation);
    if (task == NULL) {
        return -1;
    }
    stream->task = task;  /* owned */
    if (wreath_task_add_done_callback(task, stream->done_callable) < 0) {
        Py_CLEAR(stream->task);
        return -1;
    }
    if (proto->request_timeout > 0.0) {
        PyObject *callback = PyObject_GetAttrString((PyObject *)stream, "_on_timeout");
        if (callback == NULL) return -1;
        stream->timeout_handle = PyObject_CallMethod(
            proto->loop, "call_later", "dO", proto->request_timeout, callback);
        Py_DECREF(callback);
        if (stream->timeout_handle == NULL) return -1;
    }
    return 0;
}

static PyMethodDef stream_methods[] = {
    {"_receive", stream_receive, METH_NOARGS, NULL},
    {"_send", stream_send, METH_O, NULL},
    {"_wreath_response", (PyCFunction)(void (*)(void))stream_wreath_response,
     METH_FASTCALL, NULL},
    {"_wreath_response_nowait",
     (PyCFunction)(void (*)(void))stream_wreath_response_nowait,
     METH_FASTCALL, NULL},
    {"_wreath_stream_start", (PyCFunction)(void (*)(void))stream_wreath_start,
     METH_FASTCALL, NULL},
    {"_wreath_stream_body", (PyCFunction)(void (*)(void))stream_wreath_body,
     METH_FASTCALL, NULL},
    {"_done", stream_done, METH_O, NULL},
    {"_on_timeout", stream_timeout, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static int
stream_traverse(PyObject *op, visitproc visit, void *arg)
{
    Http2Stream *self = (Http2Stream *)op;
    Py_VISIT(self->protocol);
    Py_VISIT(self->scope);
    Py_VISIT(self->task);
    Py_VISIT(self->receive_callable);
    Py_VISIT(self->send_callable);
    Py_VISIT(self->done_callable);
    Py_VISIT(self->timeout_handle);
    for (Py_ssize_t i = self->body_head; i < self->body_len; i++) {
        Py_VISIT(self->body_chunks[i].body);
    }
    Py_VISIT(self->receive_waiter);
    Py_VISIT(self->pending_body);
    Py_VISIT(self->send_waiter);
    Py_VISIT(self->policy_state.client);
    Py_VISIT(self->policy_state.scheme);
    Py_VISIT(self->policy_state.origin);
    Py_VISIT(self->policy_state.request_id);
    Py_VISIT(self->policy_state.csrf_token);
    return 0;
}

static int
stream_clear(PyObject *op)
{
    Http2Stream *self = (Http2Stream *)op;
    Py_CLEAR(self->protocol);
    Py_CLEAR(self->scope);
    Py_CLEAR(self->task);
    Py_CLEAR(self->receive_callable);
    Py_CLEAR(self->send_callable);
    Py_CLEAR(self->done_callable);
    if (self->timeout_handle != NULL) {
        PyObject *cancelled = PyObject_CallMethod(self->timeout_handle, "cancel", NULL);
        Py_XDECREF(cancelled);
        Py_CLEAR(self->timeout_handle);
    }
    body_queue_clear(self);
    Py_CLEAR(self->receive_waiter);
    Py_CLEAR(self->pending_body);
    self->pending_offset = 0;
    self->pending_end_stream = 0;
    Py_CLEAR(self->send_waiter);
    wreath_policy_state_clear(&self->policy_state);
    return 0;
}

static void
stream_dealloc(PyObject *op)
{
    Http2Stream *self = (Http2Stream *)op;
    /* Safety net: a stream that was freed before its task completed (failed
     * spawn, teardown) releases its active slot here. The normal path already
     * cleared nfr_active in stream_done. */
    if (self->nfr_active && self->protocol != NULL) {
        Http2Protocol *proto = (Http2Protocol *)self->protocol;
        if (proto->nfr_worker != NULL) {
            flight_capi->context_abandon(proto->nfr_worker, &self->nfr_ctx);
        }
        self->nfr_active = 0;
    }
    PyObject_GC_UnTrack(op);
    stream_clear(op);
    Py_TYPE(op)->tp_free(op);
}

static PyType_Slot stream_slots[] = {
    {Py_tp_methods, stream_methods},
    {Py_tp_traverse, stream_traverse},
    {Py_tp_clear, stream_clear},
    {Py_tp_dealloc, stream_dealloc},
    {0, NULL},
};
static PyType_Spec stream_spec = {
    .name = "wreath._native._server.Http2Stream",
    .basicsize = sizeof(Http2Stream),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = stream_slots,
};

/* ======================================================================== */
/* header validation + request start                                        */
/* ======================================================================== */

/* RFC 9113 s8.2.1: a field name is an RFC 9110 token carrying no uppercase
 * letter. The token set is the one the HTTP/1.1 parser enforces, walked in the
 * same pass as the case check. */
static int
is_lowercase_token(const char *p, Py_ssize_t n)
{
    if (n == 0) {
        return 0;
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)p[i];
        if (!wreath_ascii_token[c] || (c >= 'A' && c <= 'Z')) {
            return 0;
        }
    }
    return 1;
}

/* RFC 9113 s8.2.1: a field value carries no control octet -- NUL, CR and LF
 * above all -- and neither leads nor trails with SP or HTAB. A value that
 * fails this is a malformed field, and s8.3 says a malformed field must not
 * reach the application. */
static int
is_field_value(const char *p, Py_ssize_t n)
{
    if (!wreath_field_value_valid(p, n)) {
        return 0;
    }
    if (n > 0 && (p[0] == ' ' || p[0] == '\t' ||
                  p[n - 1] == ' ' || p[n - 1] == '\t')) {
        return 0;
    }
    return 1;
}

/* Strict decimal content-length, as RFC 9113 s8.1.1 requires: all digits, no
 * sign, no whitespace (which the field-value rule above has already refused),
 * no overflow. Returns 0 and writes *out, or -1 for a malformed value. The
 * HTTP/2 path used to run this through PyLong_FromString and discard the
 * error, which both leaked the intermediate and turned every unparseable
 * length into "no declared length" -- silently skipping the end-of-stream
 * length check that catches an under-run body. */
static int
parse_h2_content_length(const char *p, Py_ssize_t n, Py_ssize_t *out)
{
    Py_ssize_t parsed = 0;
    if (n == 0) {
        return -1;
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        int digit = (unsigned char)p[i] - '0';
        if (digit < 0 || digit > 9 || parsed > (PY_SSIZE_T_MAX - digit) / 10) {
            return -1;
        }
        parsed = parsed * 10 + digit;
    }
    *out = parsed;
    return 0;
}

typedef struct {
    const char *host;
    Py_ssize_t host_len;
    int port;
} H2Authority;

/* Parse the authority shape needed to compare :authority with Host.  HTTP/2
 * control data owns routing; accepting a disagreeing Host lets an edge and the
 * application apply policy to different tenants.  Userinfo is prohibited for
 * HTTP(S) authority, IPv6 must be bracketed, and a port is decimal. */
static int
parse_h2_authority(const char *data, Py_ssize_t len, int default_port,
                   H2Authority *out)
{
    Py_ssize_t host_len = len;
    Py_ssize_t port_at = -1;
    if (len <= 0) {
        return -1;
    }
    if (data[0] == '[') {
        Py_ssize_t close = 1;
        while (close < len && data[close] != ']') {
            close++;
        }
        if (close == len || close == 1) {
            return -1;
        }
        host_len = close + 1;
        if (host_len < len) {
            if (data[host_len] != ':') {
                return -1;
            }
            port_at = host_len + 1;
        }
    }
    else {
        for (Py_ssize_t i = 0; i < len; i++) {
            unsigned char c = (unsigned char)data[i];
            if (c == '@' || c == '/' || c == '?' || c == '#' || c == '\\' ||
                c == '[' || c == ']') {
                return -1;
            }
            if (c == ':') {
                if (port_at >= 0) {
                    return -1;  /* an IPv6 literal must carry brackets */
                }
                host_len = i;
                port_at = i + 1;
            }
        }
        if (host_len == 0) {
            return -1;
        }
    }
    int port = default_port;
    if (port_at >= 0 && port_at < len) {
        port = 0;
        for (Py_ssize_t i = port_at; i < len; i++) {
            int digit = (unsigned char)data[i] - '0';
            if (digit < 0 || digit > 9 || port > (65535 - digit) / 10) {
                return -1;
            }
            port = port * 10 + digit;
        }
    }
    out->host = data;
    out->host_len = host_len;
    out->port = port;
    return 0;
}

static int
h2_authorities_equal(PyObject *authority, PyObject *host, PyObject *scheme)
{
    const char *authority_data = PyBytes_AS_STRING(authority);
    Py_ssize_t authority_len = PyBytes_GET_SIZE(authority);
    const char *host_data = PyBytes_AS_STRING(host);
    Py_ssize_t host_len = PyBytes_GET_SIZE(host);
    int default_port = -1;
    if (scheme != NULL && PyBytes_GET_SIZE(scheme) == 5 &&
        PyOS_strnicmp(PyBytes_AS_STRING(scheme), "https", 5) == 0) {
        default_port = 443;
    }
    else if (scheme != NULL && PyBytes_GET_SIZE(scheme) == 4 &&
             PyOS_strnicmp(PyBytes_AS_STRING(scheme), "http", 4) == 0) {
        default_port = 80;
    }
    H2Authority left;
    H2Authority right;
    if (parse_h2_authority(authority_data, authority_len, default_port, &left) < 0 ||
        parse_h2_authority(host_data, host_len, default_port, &right) < 0) {
        return 0;
    }
    return left.host_len == right.host_len && left.port == right.port &&
           PyOS_strnicmp(left.host, right.host, left.host_len) == 0;
}

/* Validate the decoded header list and build the ASGI scope. Returns:
 *   0 -> scope built (in *out_scope, new ref; declared body length, or -1 for
 *        none, in *out_content_length)
 *  -1 -> stream protocol error (caller sends RST PROTOCOL_ERROR)
 *  -2 -> python exception set
 */
static int
build_h2_scope(Http2Protocol *self, PyObject *header_list, PyObject **out_scope,
               Py_ssize_t *out_content_length)
{
    PyObject *method = NULL, *path = NULL, *scheme = NULL, *authority = NULL;
    PyObject *host = NULL;
    PyObject *scope_headers = wreath_header_block_new_objects(16);
    if (scope_headers == NULL) {
        return -2;
    }
    int seen_regular = 0;
    Py_ssize_t content_length = -1;
    Py_ssize_t count = wreath_headers_count(header_list);
    for (Py_ssize_t i = 0; i < count; i++) {
        const char *nptr;
        const char *vptr;
        Py_ssize_t nlen;
        Py_ssize_t vlen;
        if (wreath_headers_view(header_list, i, &nptr, &nlen,
                                &vptr, &vlen) < 0) {
            Py_DECREF(scope_headers);
            return -2;
        }
        PyObject *value = wreath_headers_value_borrowed(header_list, i);
        if (value == NULL) {
            Py_DECREF(scope_headers);
            return -2;
        }
        if (nlen == 0) {
            goto proto_err;
        }
        /* Every field value, pseudo-header included: a CR, LF or NUL that
         * reaches the scope is a splitting primitive for whatever re-emits
         * these headers over HTTP/1.1. */
        if (!is_field_value(vptr, vlen)) {
            goto proto_err;
        }
        if (nptr[0] == ':') {
            if (seen_regular) {
                goto proto_err;  /* pseudo after regular */
            }
            if (nlen == 7 && memcmp(nptr, ":method", 7) == 0) {
                if (method) goto proto_err;
                method = value;
            } else if (nlen == 5 && memcmp(nptr, ":path", 5) == 0) {
                if (path) goto proto_err;
                path = value;
            } else if (nlen == 7 && memcmp(nptr, ":scheme", 7) == 0) {
                if (scheme) goto proto_err;
                scheme = value;
            } else if (nlen == 10 && memcmp(nptr, ":authority", 10) == 0) {
                if (authority) goto proto_err;
                authority = value;
            } else {
                goto proto_err;  /* unknown/response pseudo-header */
            }
            continue;
        }
        seen_regular = 1;
        if (!is_lowercase_token(nptr, nlen)) {
            goto proto_err;
        }
        /* forbidden connection-specific header fields (RFC 9113 s8.2.2) */
        if ((nlen == 10 && memcmp(nptr, "connection", 10) == 0) ||
            (nlen == 10 && memcmp(nptr, "keep-alive", 10) == 0) ||
            (nlen == 16 && memcmp(nptr, "proxy-connection", 16) == 0) ||
            (nlen == 17 && memcmp(nptr, "transfer-encoding", 17) == 0) ||
            (nlen == 7 && memcmp(nptr, "upgrade", 7) == 0)) {
            goto proto_err;
        }
        if (nlen == 2 && memcmp(nptr, "te", 2) == 0) {
            if (!(vlen == 8 && memcmp(vptr, "trailers", 8) == 0)) {
                goto proto_err;
            }
        }
        if (nlen == 4 && memcmp(nptr, "host", 4) == 0) {
            if (host != NULL) {
                goto proto_err;
            }
            host = value;
        }
        else if (nlen == 14 && memcmp(nptr, "content-length", 14) == 0) {
            Py_ssize_t declared;
            if (parse_h2_content_length(vptr, vlen, &declared) < 0) {
                goto proto_err;
            }
            /* A second content-length that disagrees with the first is a
             * malformed request, exactly as on the HTTP/1.1 path. */
            if (content_length >= 0 && content_length != declared) {
                goto proto_err;
            }
            content_length = declared;
        }
        PyObject *name = wreath_headers_name_object(header_list, i);
        PyObject *owned_value = wreath_headers_value_object(header_list, i);
        int append_result = name != NULL && owned_value != NULL
            ? wreath_header_block_append_objects(scope_headers, name, owned_value)
            : -1;
        Py_XDECREF(name);
        Py_XDECREF(owned_value);
        if (append_result < 0) {
            Py_DECREF(scope_headers);
            return -2;
        }
    }
    int is_connect = method && PyBytes_GET_SIZE(method) == 7 &&
                     memcmp(PyBytes_AS_STRING(method), "CONNECT", 7) == 0;
    if (is_connect) {
        if (!authority) goto proto_err;
    } else {
        if (!method || !path || !scheme) goto proto_err;
        if (PyBytes_GET_SIZE(path) == 0) goto proto_err;
    }
    if (authority != NULL) {
        H2Authority parsed;
        int default_port = -1;
        if (scheme != NULL && PyBytes_GET_SIZE(scheme) == 5 &&
            PyOS_strnicmp(PyBytes_AS_STRING(scheme), "https", 5) == 0) {
            default_port = 443;
        }
        else if (scheme != NULL && PyBytes_GET_SIZE(scheme) == 4 &&
                 PyOS_strnicmp(PyBytes_AS_STRING(scheme), "http", 4) == 0) {
            default_port = 80;
        }
        if (parse_h2_authority(PyBytes_AS_STRING(authority),
                               PyBytes_GET_SIZE(authority), default_port, &parsed) < 0 ||
            (host != NULL && !h2_authorities_equal(authority, host, scheme))) {
            goto proto_err;
        }
    }
    else if (host != NULL) {
        H2Authority parsed;
        if (parse_h2_authority(PyBytes_AS_STRING(host), PyBytes_GET_SIZE(host),
                               -1, &parsed) < 0) {
            goto proto_err;
        }
    }
    /* synthesize host from :authority when no host field is present; the name
     * is a cached constant, not a fresh allocation each request */
    if (host == NULL && authority != NULL) {
        if (wreath_header_block_append_objects(
                scope_headers, header_host, authority) < 0) {
            Py_DECREF(scope_headers);
            return -2;
        }
    }

    /* Split path and query, then build the scope from the *decoded* path, the
     * same way `begin_request` does on HTTP/1.  Building it from the raw
     * `:path` made request handling depend on which protocol carried the URL:
     * `/caf%C3%A9` routed to `/café` over h1 and to the literal `/caf%C3%A9`
     * over h2 (ASGI requires `path` percent-decoded), `%2F` was a 400 over h1
     * and reached the application over h2, and `raw_path` carried the query
     * string here but not there -- so an encoded slash in an ordinary query
     * value tripped the encoded-separator guard that reads it. */
    char *pptr = (char *)"/"; Py_ssize_t plen = 1;
    if (path) {
        PyBytes_AsStringAndSize(path, &pptr, &plen);
    }
    /* Finds the query separator by hand instead of calling
     * `wreath_memmem(pptr, plen, "?", 1)`, which sends a one-byte needle
     * straight to glibc's vectorised `memchr`. Scalar on purpose, not by
     * oversight: an h2 `:path` is 30-80 bytes, so this is one instruction per
     * byte over tens of bytes -- single-digit nanoseconds against a per-request
     * budget measured in microseconds, and under any floor this repository can
     * measure. Widening a loop this short is the weaker move; there is no work
     * here to delete. */
    Py_ssize_t q = -1;
    for (Py_ssize_t i = 0; i < plen; i++) {
        if (pptr[i] == '?') { q = i; break; }
    }
    Py_ssize_t path_len = q >= 0 ? q : plen;
    int bad_path = 0;
    PyObject *path_str = wreath_decode_request_path(pptr, path_len, &bad_path);
    if (path_str == NULL) {
        if (!bad_path) {
            Py_DECREF(scope_headers);
            return -2;
        }
        /* An invalid `:path` makes the request malformed; the caller turns this
         * into RST_STREAM(PROTOCOL_ERROR) (RFC 9113 s8.3.1). */
        goto proto_err;
    }
    PyObject *raw_path = PyBytes_FromStringAndSize(pptr, path_len);
    PyObject *query = q >= 0
        ? PyBytes_FromStringAndSize(pptr + q + 1, plen - q - 1)
        : PyBytes_FromStringAndSize("", 0);
    PyObject *method_str = method
        ? PyUnicode_DecodeASCII(PyBytes_AS_STRING(method), PyBytes_GET_SIZE(method), "strict")
        : PyUnicode_FromString("GET");
    if (!query || !raw_path || !method_str) {
        Py_XDECREF(path_str); Py_XDECREF(query); Py_XDECREF(raw_path);
        Py_XDECREF(method_str); Py_DECREF(scope_headers);
        return -2;
    }
    PyObject *scope;
    if (self->native_app != NULL) {
        scope = wreath_request_context_new(
            self->scope_type, self->scope_asgi, self->scope_http_version,
            method_str, self->scope_scheme, path_str, raw_path, query,
            scope_headers, Py_None, Py_None, self->scope_root_path
        );
        Py_DECREF(method_str);
        Py_DECREF(path_str); Py_DECREF(raw_path); Py_DECREF(query);
        Py_DECREF(scope_headers);
    }
    else {
        PyObject *asgi_headers = wreath_headers_materialize(scope_headers);
        if (asgi_headers == NULL) {
            Py_DECREF(method_str);
            Py_DECREF(path_str);
            Py_DECREF(raw_path);
            Py_DECREF(query);
            Py_DECREF(scope_headers);
            return -2;
        }
        scope = Py_BuildValue(
            "{s:s,s:s,s:s,s:N,s:N,s:N,s:N,s:O}",
            "type", "http",
            "http_version", "2",
            "scheme", "https",
            "method", method_str,
            "path", path_str,
            "raw_path", raw_path,
            "query_string", query,
            "headers", asgi_headers);
        Py_DECREF(asgi_headers);
        Py_DECREF(scope_headers);
    }
    if (scope == NULL) {
        return -2;
    }
    *out_scope = scope;
    *out_content_length = content_length;
    return 0;

proto_err:
    Py_DECREF(scope_headers);
    return -1;
}

static void
parse_priority_field(const char *value, Py_ssize_t length,
                     uint8_t *urgency, uint8_t *incremental)
{
    Py_ssize_t cursor = 0;
    while (cursor < length) {
        while (cursor < length &&
               (value[cursor] == ' ' || value[cursor] == '\t' ||
                value[cursor] == ',')) {
            cursor++;
        }
        Py_ssize_t start = cursor;
        while (cursor < length && value[cursor] != ',') {
            cursor++;
        }
        Py_ssize_t end = cursor;
        while (end > start &&
               (value[end - 1] == ' ' || value[end - 1] == '\t')) {
            end--;
        }
        if (end - start == 3 && value[start] == 'u' &&
            value[start + 1] == '=' && value[start + 2] >= '0' &&
            value[start + 2] <= '7') {
            *urgency = (uint8_t)(value[start + 2] - '0');
        } else if (end - start == 1 && value[start] == 'i') {
            *incremental = 1;
        } else if (end - start == 4 &&
                   memcmp(value + start, "i=?1", 4) == 0) {
            *incremental = 1;
        } else if (end - start == 4 &&
                   memcmp(value + start, "i=?0", 4) == 0) {
            *incremental = 0;
        }
    }
}

/* Create the stream object, build scope, and stage ASGI activation. */
static int
start_request(Http2Protocol *self, uint32_t sid, PyObject *header_list,
              int end_stream)
{
    /* HEADERS opened this client stream even when admission or policy refuses
     * it below. Remember that fact before any early stream-level response so
     * DATA already in flight is never misclassified as DATA on an idle stream
     * and escalated to a connection-level PROTOCOL_ERROR. */
    if (sid > self->last_stream_id) {
        self->last_stream_id = sid;
    }
    /* concurrency limit */
    if (self->active_requests >= self->max_concurrent) {
        return h2_stream_error((PyObject *)self, sid, H2_REFUSED_STREAM);
    }
    if (!self->accepting) {
        return h2_stream_error((PyObject *)self, sid, H2_REFUSED_STREAM);
    }
    PyObject *scope = NULL;
    Py_ssize_t content_length = -1;
    int rc = build_h2_scope(self, header_list, &scope, &content_length);
    if (rc == -2) {
        return -1;
    }
    if (rc == -1) {
        return h2_stream_error((PyObject *)self, sid, H2_PROTOCOL_ERROR);
    }

    Http2Stream *st = PyObject_GC_New(Http2Stream, Http2StreamType);
    if (st == NULL) {
        Py_DECREF(scope);
        return -1;
    }
    st->protocol = Py_NewRef((PyObject *)self);
    st->id = sid;
    st->state = end_stream ? S_HALF_CLOSED_REMOTE : S_OPEN;
    st->scope = scope;
    st->task = NULL;
    st->receive_callable = NULL;
    st->send_callable = NULL;
    st->done_callable = NULL;
    st->timeout_handle = NULL;
    st->body_chunks = NULL;
    st->body_cap = st->body_len = st->body_head = 0;
    st->receive_waiter = NULL;
    st->request_ended = end_stream;
    st->disconnected = 0;
    st->content_length = content_length;  /* validated in build_h2_scope */
    st->body_received = 0;
    st->body_frames = 0;
    st->response_started = 0;
    st->response_ended = 0;
    st->trailers_expected = 0;
    st->send_window = self->peer_initial_window;
    st->applied_initial_window = self->peer_initial_window;
    st->recv_window = self->our_initial_window;
    st->recv_consumed_pending = 0;
    st->recv_credit_threshold = self->our_initial_window / 2;
    if (st->recv_credit_threshold > 16 * 1024) st->recv_credit_threshold = 16 * 1024;
    if (st->recv_credit_threshold < 1) st->recv_credit_threshold = 1;
    st->pending_body = NULL;
    st->pending_offset = 0;
    st->send_waiter = NULL;
    st->pending_end_stream = 0;
    st->in_conn_blocked = 0;
    st->send_deficit = 0;
    st->urgency = 3;
    st->incremental = 0;
    wreath_policy_state_init(&st->policy_state);
    if (self->rfc9218_enabled) {
        PyObject *priority_key = PyLong_FromUnsignedLong(sid);
        if (priority_key == NULL) {
            Py_DECREF(st);
            return -1;
        }
        PyObject *saved = PyDict_GetItemWithError(
            self->pending_priorities, priority_key);
        if (saved != NULL) {
            unsigned long packed = PyLong_AsUnsignedLong(saved);
            if (packed == (unsigned long)-1 && PyErr_Occurred()) {
                Py_DECREF(priority_key);
                Py_DECREF(st);
                return -1;
            }
            st->urgency = (uint8_t)(packed & 0xffU);
            st->incremental = (uint8_t)((packed >> 8) & 0x1U);
            if (PyDict_DelItem(self->pending_priorities, priority_key) < 0) {
                Py_DECREF(priority_key);
                Py_DECREF(st);
                return -1;
            }
        } else if (PyErr_Occurred()) {
            Py_DECREF(priority_key);
            Py_DECREF(st);
            return -1;
        }
        Py_DECREF(priority_key);
    }
    st->nfr_active = 0;
    st->nfr_bytes_out = 0;
    if (!PyObject_GC_IsTracked((PyObject *)st)) {
        PyObject_GC_Track((PyObject *)st);
    }

    if (self->policy.descriptor != NULL) {
        PyObject *policy_headers = wreath_request_context_headers(scope);
        Py_XINCREF(policy_headers);
        PyObject *policy_method = PyObject_GetAttr(scope, k_method);
        PyObject *policy_path = PyObject_GetAttr(scope, k_path);
        PyObject *policy_scheme = PyObject_GetAttr(scope, k_scheme);
        PyObject *policy_client = PyObject_GetAttr(scope, k_client);
        WreathPolicyReply reply = {0};
        if (policy_headers == NULL || policy_method == NULL || policy_path == NULL ||
            policy_scheme == NULL || policy_client == NULL) {
            Py_XDECREF(policy_headers);
            Py_XDECREF(policy_method);
            Py_XDECREF(policy_path);
            Py_XDECREF(policy_scheme);
            Py_XDECREF(policy_client);
            Py_DECREF(st);
            return -1;
        }
        int policy_result = wreath_policy_ingress(
            &self->policy, &st->policy_state, policy_method, policy_path,
            policy_scheme, policy_client, policy_headers, &reply);
        Py_DECREF(policy_headers);
        Py_DECREF(policy_method);
        Py_DECREF(policy_path);
        Py_DECREF(policy_scheme);
        Py_DECREF(policy_client);
        if (policy_result < 0) {
            wreath_policy_reply_clear(&reply);
            Py_DECREF(st);
            return -1;
        }
        wreath_request_context_set_policy(scope, &st->policy_state);
        if (policy_result > 0) {
            uint64_t refusal_started_ns = wreath_flight_now_ns();
            PyObject *status = PyLong_FromLong(reply.status);
            PyObject *started = status != NULL
                ? h2_response_start(st, status, reply.headers, 0, 0) : NULL;
            Py_XDECREF(status);
            PyObject *finished = started != NULL
                ? h2_response_body(st, reply.body, 0, 1) : NULL;
            Py_XDECREF(started);
            if (finished != NULL) {
                wreath_policy_record_completion(
                    flight_capi, self->nfr_worker, self->nfr_connection_id,
                    WREATH_NFR_PROTO_HTTP2, refusal_started_ns,
                    wreath_flight_now_ns(), header_list, &reply, 0,
                    (uint64_t)PyBytes_GET_SIZE(reply.body));
            }
            Py_XDECREF(finished);
            wreath_policy_reply_clear(&reply);
            if (finished == NULL) {
                Py_DECREF(st);
                return -1;
            }
            if (!end_stream &&
                h2_stream_error((PyObject *)self, sid, H2_NO_ERROR) < 0) {
                Py_DECREF(st);
                return -1;
            }
            Py_DECREF(st);
            return 0;
        }
        wreath_policy_reply_clear(&reply);
    }

    /* RFC 9218 priority hint; content-length was parsed during validation. */
    if (self->rfc9218_enabled) {
        Py_ssize_t hc = wreath_headers_count(header_list);
        for (Py_ssize_t i = 0; i < hc; i++) {
            const char *name;
            const char *value;
            Py_ssize_t name_size;
            Py_ssize_t value_size;
            if (wreath_headers_view(header_list, i, &name, &name_size,
                                    &value, &value_size) < 0) {
                Py_DECREF(st);
                return -1;
            }
            if (name_size == 8 && memcmp(name, "priority", 8) == 0) {
                parse_priority_field(value, value_size,
                                     &st->urgency, &st->incremental);
            }
        }
    }

    /* bound methods for ASGI callables */
    st->receive_callable = PyObject_GetAttrString((PyObject *)st, "_receive");
    st->send_callable = PyObject_GetAttrString((PyObject *)st, "_send");
    st->done_callable = PyObject_GetAttrString((PyObject *)st, "_done");
    if (!st->receive_callable || !st->send_callable || !st->done_callable) {
        Py_DECREF(st);
        return -1;
    }

    PyObject *key = PyLong_FromUnsignedLong(sid);
    if (key == NULL || PyDict_SetItem(self->streams, key, (PyObject *)st) < 0) {
        Py_XDECREF(key);
        Py_DECREF(st);
        return -1;
    }
    Py_DECREF(key);
    self->active_requests++;

    /* Begin the recorder context for this stream's request. */
    st->nfr_status = 200;
    if (self->nfr_worker != NULL) {
        flight_capi->context_start(self->nfr_worker, &st->nfr_ctx,
                                   self->nfr_connection_id, WREATH_NFR_PROTO_HTTP2,
                                   wreath_flight_now_ns());
        st->nfr_active = 1;
        /* Signal Python dispatch that a recorder is attached to this request so
         * it stamps route/plan attribution back into the scope; read at
         * completion below. Absent on the pure-ASGI path (no recorder). */
        if (wreath_request_context_check(st->scope)) {
            wreath_request_context_set_flight(
                st->scope, &st->nfr_ctx, self->nfr_worker
            );
            wreath_request_context_set_armed(st->scope);
        }
        else if (wreath_request_scope_seed_flight(st->scope, &st->nfr_ctx) < 0) {
            PyErr_Clear();
        }
        Py_ssize_t header_count = wreath_headers_count(header_list);
        for (Py_ssize_t i = 0; i < header_count; i++) {
            const char *name;
            const char *value;
            Py_ssize_t name_size;
            Py_ssize_t value_size;
            if (wreath_headers_view(header_list, i, &name, &name_size,
                                    &value, &value_size) < 0) {
                Py_DECREF(st);
                return -1;
            }
            if (name_size == 11 && memcmp(name, "traceparent", 11) == 0) {
                flight_capi->context_propagate(
                    self->nfr_worker, &st->nfr_ctx,
                    (const uint8_t *)value, value_size);
                break;
            }
        }
    }

    /* Activation waits until the parser has consumed this whole input batch.
     * A HEADERS followed by RST_STREAM in one read must be cancelled before
     * application code can answer it. Once that ordering boundary is crossed,
     * start_stream_app eagerly completes the common case without a Task. */
    if (PyList_Append(self->pending_apps, (PyObject *)st) < 0) {
        Py_DECREF(st);
        return -1;
    }
    Py_DECREF(st);  /* dict holds the reference */
    return 0;
}

/* Deliver a DATA payload to a stream's body buffer / waiter. */
static int
deliver_body(Http2Protocol *self, Http2Stream *st, const uint8_t *data,
             Py_ssize_t len, Py_ssize_t flow_bytes, int end_stream)
{
    st->body_received += len;
    if (st->content_length >= 0 && st->body_received > st->content_length) {
        return h2_stream_error((PyObject *)self, st->id, H2_PROTOCOL_ERROR);
    }
    if (self->max_body_bytes > 0 && st->body_received > self->max_body_bytes) {
        return h2_stream_error((PyObject *)self, st->id, H2_ENHANCE_YOUR_CALM);
    }
    if (end_stream) {
        st->request_ended = 1;
        st->state = (st->state == S_OPEN) ? S_HALF_CLOSED_REMOTE : st->state;
        if (st->content_length >= 0 && st->body_received != st->content_length) {
            return h2_stream_error((PyObject *)self, st->id, H2_PROTOCOL_ERROR);
        }
    }
    if (st->receive_waiter != NULL) {
        PyObject *body = PyBytes_FromStringAndSize((const char *)data, len);
        if (body == NULL) {
            return -1;
        }
        if (body_credit_consumed(self, st, flow_bytes) < 0) {
            Py_DECREF(body);
            return -1;
        }
        int more = !st->request_ended;
        PyObject *msg = h2_stream_is_native(st)
            ? h2_body_slot(body, more, 0)
            : Py_BuildValue("{s:s,s:O,s:O}", "type", "http.request",
                            "body", body, "more_body",
                            more ? Py_True : Py_False);
        Py_DECREF(body);
        if (msg == NULL) {
            return -1;
        }
        PyObject *w = st->receive_waiter;
        st->receive_waiter = NULL;
        int rc = h2_future_set_result(w, msg);
        Py_DECREF(w);
        Py_DECREF(msg);
        return rc;
    }
    if (len > 0 || flow_bytes > 0) {
        if (body_queue_coalesce(st, data, len, flow_bytes) < 0) return -1;
    }
    return 0;
}

/* ======================================================================== */
/* frame processing                                                         */
/* ======================================================================== */

/* Returns 0 if ok, -1 python error. Connection/stream errors are emitted
 * internally and set self->closing when fatal. */
static int
apply_peer_initial_window(Http2Protocol *self, int64_t value)
{
    self->peer_initial_window = value;
    return 0;
}


static int
process_settings(Http2Protocol *self, int flags, uint32_t sid,
                 const uint8_t *payload, Py_ssize_t len)
{
    if (sid != 0) {
        return h2_connection_error(self, H2_PROTOCOL_ERROR);
    }
    if (flags & FLAG_ACK) {
        if (len != 0) {
            return h2_connection_error(self, H2_FRAME_SIZE_ERROR);
        }
        /* A legitimate ACK is rare; an unlimited unsolicited run is parser
         * work which consumes no protocol credit and makes no progress. */
        return h2_note_unproductive(self);
    }
    if (len % 6 != 0) {
        return h2_connection_error(self, H2_FRAME_SIZE_ERROR);
    }
    /* Every non-ACK SETTINGS forces a reflected ACK; a flood is amplification. */
    if (h2_note_unproductive(self) < 0) {
        return -1;
    }
    if (self->fatal) {
        return 0;
    }
    int saw_initial_window = 0;
    int64_t initial_window = self->peer_initial_window;
    for (Py_ssize_t i = 0; i < len; i += 6) {
        uint16_t ident = wreath_load_u16_be(payload + i);
        uint32_t value = get_u32(payload + i + 2);
        switch (ident) {
        case SET_ENABLE_PUSH:
            if (value > 1) {
                return h2_connection_error(self, H2_PROTOCOL_ERROR);
            }
            break;
        case SET_INITIAL_WINDOW_SIZE:
            if (value > WINDOW_MAX) {
                return h2_connection_error(self, H2_FLOW_CONTROL_ERROR);
            }
            initial_window = value;
            saw_initial_window = 1;
            break;
        case SET_MAX_FRAME_SIZE:
            if (value < DEFAULT_MAX_FRAME || value > MAX_ALLOWED_FRAME) {
                return h2_connection_error(self, H2_PROTOCOL_ERROR);
            }
            self->peer_max_frame = value;
            break;
        case SET_HEADER_TABLE_SIZE:
        case SET_MAX_CONCURRENT_STREAMS:
        case SET_MAX_HEADER_LIST_SIZE:
        default:
            break;  /* accepted or ignored (unknown settings ignored) */
        }
    }
    int window_increased = saw_initial_window &&
        initial_window > self->peer_initial_window;
    if (saw_initial_window && apply_peer_initial_window(self, initial_window) < 0) {
        return -1;
    }
    /* ACK */
    if (h2_write_frame((PyObject *)self, FRAME_SETTINGS, FLAG_ACK, 0, NULL, 0) < 0) {
        return -1;
    }
    return window_increased ? wake_stream_window_blocked(self) : 0;
}

static int
finish_header_block(Http2Protocol *self, uint32_t sid, int end_stream)
{
    PyObject *header_list = wreath_header_block_new_objects(16);
    if (header_list == NULL) {
        return -1;
    }
    int h2err = 0;
    int rc = wreath_hpack_decode(&self->hpack,
                              (const uint8_t *)PyByteArray_AS_STRING(self->header_block),
                              PyByteArray_GET_SIZE(self->header_block),
                              self->max_header_count, self->max_header_list,
                              header_list, &h2err);
    if (rc < 0) {
        Py_DECREF(header_list);
        if (h2err == H2_ENHANCE_YOUR_CALM) {
            return h2_stream_error((PyObject *)self, sid, h2err);
        }
        if (h2err != 0) {
            return h2_connection_error(self, h2err);
        }
        return -1;  /* python error */
    }
    int r = start_request(self, sid, header_list, end_stream);
    Py_DECREF(header_list);
    return r;
}

static int
process_headers(Http2Protocol *self, int flags, uint32_t sid,
                const uint8_t *payload, Py_ssize_t len)
{
    if (sid == 0 || (sid % 2) == 0) {
        return h2_connection_error(self, H2_PROTOCOL_ERROR);
    }
    h2_note_progress(self);  /* a new request (or trailers) is forward progress */
    if (sid <= self->last_stream_id) {
        /* already-seen or lower stream id: new stream must be strictly higher */
        PyObject *key = PyLong_FromUnsignedLong(sid);
        int exists = key ? PyDict_Contains(self->streams, key) : -1;
        Py_XDECREF(key);
        if (exists != 1) {
            return h2_connection_error(self, H2_PROTOCOL_ERROR);
        }
    }
    /* strip padding / priority */
    Py_ssize_t off = 0;
    Py_ssize_t end = len;
    uint8_t pad = 0;
    if (flags & FLAG_PADDED) {
        if (len < 1) return h2_connection_error(self, H2_PROTOCOL_ERROR);
        pad = payload[off++];
    }
    if (flags & FLAG_PRIORITY) {
        if (end - off < 5) return h2_connection_error(self, H2_PROTOCOL_ERROR);
        off += 5;  /* ignore priority */
    }
    if (pad > end - off) {
        return h2_connection_error(self, H2_PROTOCOL_ERROR);
    }
    end -= pad;

    /* Accumulated HPACK bytes also need a hard bound: CONTINUATION frames can
     * otherwise grow this bytearray forever before decompressed limits apply. */
    Py_ssize_t encoded_limit = self->max_header_list > (PY_SSIZE_T_MAX - 4096) / 4
        ? PY_SSIZE_T_MAX : self->max_header_list * 4 + 4096;
    if (end - off > encoded_limit) {
        return h2_connection_error(self, H2_ENHANCE_YOUR_CALM);
    }
    /* accumulate into header_block */
    if (PyByteArray_Resize(self->header_block, 0) < 0) {
        return -1;
    }
    if (append_raw(self->header_block, (const char *)(payload + off), end - off) < 0) {
        return -1;
    }
    if (flags & FLAG_END_HEADERS) {
        return finish_header_block(self, sid, (flags & FLAG_END_STREAM) ? 1 : 0);
    }
    self->header_stream = sid;
    self->header_end_stream = (flags & FLAG_END_STREAM) ? 1 : 0;
    return 0;
}

static int
process_continuation(Http2Protocol *self, int flags, uint32_t sid,
                     const uint8_t *payload, Py_ssize_t len)
{
    if (self->header_stream == 0 || sid != self->header_stream) {
        return h2_connection_error(self, H2_PROTOCOL_ERROR);
    }
    Py_ssize_t current = PyByteArray_GET_SIZE(self->header_block);
    Py_ssize_t encoded_limit = self->max_header_list > (PY_SSIZE_T_MAX - 4096) / 4
        ? PY_SSIZE_T_MAX : self->max_header_list * 4 + 4096;
    if (current > encoded_limit || len > encoded_limit - current) {
        return h2_connection_error(self, H2_ENHANCE_YOUR_CALM);
    }
    if (append_raw(self->header_block, (const char *)payload, len) < 0) {
        return -1;
    }
    if (flags & FLAG_END_HEADERS) {
        uint32_t s = self->header_stream;
        int es = self->header_end_stream;
        self->header_stream = 0;
        return finish_header_block(self, s, es);
    }
    return 0;
}

static int
process_data(Http2Protocol *self, int flags, uint32_t sid,
             const uint8_t *payload, Py_ssize_t len)
{
    if (sid == 0) {
        return h2_connection_error(self, H2_PROTOCOL_ERROR);
    }
    PyObject *key = PyLong_FromUnsignedLong(sid);
    if (key == NULL) return -1;
    PyObject *stobj = PyDict_GetItemWithError(self->streams, key);
    Py_DECREF(key);
    if (stobj == NULL) {
        if (PyErr_Occurred()) return -1;
        /* DATA on idle/closed stream */
        if (sid > self->last_stream_id) {
            return h2_connection_error(self, H2_PROTOCOL_ERROR);
        }
        return h2_stream_error((PyObject *)self, sid, H2_STREAM_CLOSED);
    }
    Http2Stream *st = (Http2Stream *)stobj;
    if (st->request_ended) {
        return h2_stream_error((PyObject *)self, sid, H2_STREAM_CLOSED);
    }
    if (len > 0) {
        if (st->body_frames >= self->max_body_chunks) {
            return h2_stream_error((PyObject *)self, sid, H2_ENHANCE_YOUR_CALM);
        }
        st->body_frames++;
    }
    /* padding */
    Py_ssize_t off = 0, end = len;
    if (flags & FLAG_PADDED) {
        if (len < 1) return h2_connection_error(self, H2_PROTOCOL_ERROR);
        uint8_t pad = payload[off++];
        if (pad > end - off) return h2_connection_error(self, H2_PROTOCOL_ERROR);
        end -= pad;
    }
    /* A zero-length DATA frame consumes no flow-control credit, so flow control
     * cannot bound a flood of them: charge it against the unproductive budget.
     * A frame carrying real bytes (or padding, which does cost window) is
     * progress and forgives the counter. END_STREAM is always legitimate. */
    if (len == 0 && !(flags & FLAG_END_STREAM)) {
        if (h2_note_unproductive(self) < 0) return -1;
        if (self->fatal) return 0;
    } else if (end - off > 0) {
        h2_note_progress(self);
    }
    /* DATA frame length, including padding, consumes both receive windows. */
    self->conn_recv_window -= len;
    if (self->conn_recv_window < 0) {
        return h2_connection_error(self, H2_FLOW_CONTROL_ERROR);
    }
    st->recv_window -= len;
    if (st->recv_window < 0) {
        return h2_stream_error((PyObject *)self, sid, H2_FLOW_CONTROL_ERROR);
    }
    return deliver_body(self, st, payload + off, end - off, len,
                        (flags & FLAG_END_STREAM) ? 1 : 0);
}

static int
process_window_update(Http2Protocol *self, uint32_t sid,
                      const uint8_t *payload, Py_ssize_t len)
{
    if (len != 4) {
        return h2_connection_error(self, H2_FRAME_SIZE_ERROR);
    }
    /* WINDOW_UPDATE carries no request progress; a flood of them is abusive. */
    if (h2_note_unproductive(self) < 0) {
        return -1;
    }
    if (self->fatal) {
        return 0;
    }
    uint32_t inc = get_u32(payload) & 0x7FFFFFFF;
    if (sid == 0) {
        if (inc == 0) {
            return h2_connection_error(self, H2_PROTOCOL_ERROR);
        }
        self->conn_send_window += inc;
        if (self->conn_send_window > WINDOW_MAX) {
            return h2_connection_error(self, H2_FLOW_CONTROL_ERROR);
        }
        /* Streams blocked on connection credit remain in the cursor queue.
         * Persistent deficits divide this increment fairly in bounded work. */
        return h2_schedule_pending((PyObject *)self);
    }
    /* A zero increment on a stream is a stream error regardless of the stream's
     * current state (RFC 9113 s6.9); an idle stream id is a connection error. */
    if (sid > self->last_stream_id) {
        return h2_connection_error(self, H2_PROTOCOL_ERROR);
    }
    if (inc == 0) {
        return h2_stream_error((PyObject *)self, sid, H2_PROTOCOL_ERROR);
    }
    PyObject *key = PyLong_FromUnsignedLong(sid);
    if (key == NULL) return -1;
    PyObject *stobj = PyDict_GetItemWithError(self->streams, key);
    Py_DECREF(key);
    if (stobj == NULL) {
        if (PyErr_Occurred()) return -1;
        return 0;  /* WINDOW_UPDATE on closed stream: ignore */
    }
    Http2Stream *st = (Http2Stream *)stobj;
    h2_sync_stream_send_window(self, st);
    st->send_window += inc;
    if (st->send_window > WINDOW_MAX) {
        return h2_stream_error((PyObject *)self, sid, H2_FLOW_CONTROL_ERROR);
    }
    if (unmark_stream_window_blocked(self, sid) < 0) {
        return -1;
    }
    if (st->pending_body != NULL && mark_conn_blocked(self, st) < 0) {
        return -1;
    }
    return h2_schedule_pending((PyObject *)self);
}

static int
process_rst_stream(Http2Protocol *self, uint32_t sid, const uint8_t *payload,
                   Py_ssize_t len)
{
    if (sid == 0) {
        return h2_connection_error(self, H2_PROTOCOL_ERROR);
    }
    if (len != 4) {
        return h2_connection_error(self, H2_FRAME_SIZE_ERROR);
    }
    /* A reset makes no forward progress; a flood of them is abusive. */
    if (h2_note_unproductive(self) < 0) {
        return -1;
    }
    if (self->fatal) {
        return 0;
    }
    PyObject *key = PyLong_FromUnsignedLong(sid);
    if (key == NULL) return -1;
    PyObject *stobj = PyDict_GetItemWithError(self->streams, key);
    if (stobj == NULL) {
        Py_DECREF(key);
        if (PyErr_Occurred()) return -1;
        if (sid > self->last_stream_id) {
            return h2_connection_error(self, H2_PROTOCOL_ERROR);
        }
        return 0;
    }
    Http2Stream *st = (Http2Stream *)stobj;
    st->disconnected = 1;
    st->state = S_CLOSED;
    /* The stream can never send again: release a blocked send() too. */
    h2_abort_pending_send(st);
    if (st->receive_waiter != NULL) {
        /* The app is parked in receive(): it is not burning CPU, so honour the
         * ASGI contract and hand it http.disconnect to unwind cooperatively. */
        PyObject *msg = Py_BuildValue("{s:s}", "type", "http.disconnect");
        if (msg != NULL) {
            PyObject *w = st->receive_waiter;
            st->receive_waiter = NULL;
            h2_future_set_result(w, msg);
            Py_DECREF(w);
            Py_DECREF(msg);
        }
    } else if (st->task != NULL) {
        /* The app is doing work off-stream (a slow handler, a DB call): the peer
         * has thrown the request away, so cancel the task to reclaim the CPU it
         * would otherwise spend producing a response no one will read. A handler
         * that catches CancelledError still unwinds; stream_done treats the
         * cancelled task as a completed stream. */
        PyObject *r = PyObject_CallMethod(st->task, "cancel", NULL);
        Py_XDECREF(r);
        if (r == NULL) {
            Py_DECREF(key);
            return -1;
        }
    }
    Py_DECREF(key);
    return 0;
}

static int
process_ping(Http2Protocol *self, int flags, uint32_t sid,
             const uint8_t *payload, Py_ssize_t len)
{
    if (sid != 0) {
        return h2_connection_error(self, H2_PROTOCOL_ERROR);
    }
    if (len != 8) {
        return h2_connection_error(self, H2_FRAME_SIZE_ERROR);
    }
    if (flags & FLAG_ACK) {
        /* Ignore one response normally, but do not give ACK-only floods a way
         * around the same no-progress budget as ordinary PING frames. */
        return h2_note_unproductive(self);
    }
    /* Each PING forces a reflected ACK; a flood is pure CPU/output amplification. */
    if (h2_note_unproductive(self) < 0) {
        return -1;
    }
    if (self->fatal) {
        return 0;
    }
    return h2_write_frame((PyObject *)self, FRAME_PING, FLAG_ACK, 0, payload, 8);
}

static int
process_priority_update(Http2Protocol *self, uint32_t sid,
                        const uint8_t *payload, Py_ssize_t len)
{
    if (!self->rfc9218_enabled) {
        return 0;  /* preserve stock asyncio/uvloop scheduling */
    }
    if (sid != 0) {
        return h2_connection_error(self, H2_PROTOCOL_ERROR);
    }
    if (len < 4) {
        return h2_connection_error(self, H2_FRAME_SIZE_ERROR);
    }
    if (h2_note_unproductive(self) < 0 || self->fatal) {
        return self->fatal ? 0 : -1;
    }
    uint32_t stream_id = get_u32(payload) & 0x7fffffffU;
    if (stream_id == 0 || (stream_id & 1) == 0) {
        return 0;
    }
    PyObject *key = PyLong_FromUnsignedLong(stream_id);
    if (key == NULL) {
        return -1;
    }
    PyObject *stream_obj = PyDict_GetItemWithError(self->streams, key);
    if (stream_obj == NULL) {
        if (PyErr_Occurred()) {
            Py_DECREF(key);
            return -1;
        }
        if (stream_id > self->last_stream_id &&
            PyDict_Size(self->pending_priorities) < self->max_concurrent) {
            uint8_t urgency = 3;
            uint8_t incremental = 0;
            parse_priority_field((const char *)payload + 4, len - 4,
                                 &urgency, &incremental);
            PyObject *packed = PyLong_FromUnsignedLong(
                (unsigned long)urgency | ((unsigned long)incremental << 8));
            if (packed == NULL ||
                PyDict_SetItem(self->pending_priorities, key, packed) < 0) {
                Py_XDECREF(packed);
                Py_DECREF(key);
                return -1;
            }
            Py_DECREF(packed);
        }
        Py_DECREF(key);
        return 0;
    }
    Py_DECREF(key);
    Http2Stream *stream = (Http2Stream *)stream_obj;
    parse_priority_field((const char *)payload + 4, len - 4,
                         &stream->urgency, &stream->incremental);
    if (stream->incremental && self->nonincremental_stream == stream_id) {
        self->nonincremental_stream = 0;
    }
    return 0;
}

/* Dispatch a single fully-buffered frame. */
static int
dispatch_frame(Http2Protocol *self, int type, int flags, uint32_t sid,
               const uint8_t *payload, Py_ssize_t len)
{
    /* Any frame except CONTINUATION during a header block is a protocol error. */
    if (self->header_stream != 0 && type != FRAME_CONTINUATION) {
        return h2_connection_error(self, H2_PROTOCOL_ERROR);
    }
    switch (type) {
    case FRAME_SETTINGS:
        self->got_first_settings = 1;
        return process_settings(self, flags, sid, payload, len);
    case FRAME_HEADERS:
        return process_headers(self, flags, sid, payload, len);
    case FRAME_CONTINUATION:
        return process_continuation(self, flags, sid, payload, len);
    case FRAME_DATA:
        return process_data(self, flags, sid, payload, len);
    case FRAME_WINDOW_UPDATE:
        return process_window_update(self, sid, payload, len);
    case FRAME_RST_STREAM:
        return process_rst_stream(self, sid, payload, len);
    case FRAME_PING:
        return process_ping(self, flags, sid, payload, len);
    case FRAME_PRIORITY:
        if (len != 5) {
            return h2_connection_error(self, H2_FRAME_SIZE_ERROR);
        }
        if (h2_note_unproductive(self) < 0) {  /* ignored, but still costs us */
            return -1;
        }
        return 0;  /* RFC 7540 dependency priority remains ignored */
    case FRAME_PRIORITY_UPDATE:
        return process_priority_update(self, sid, payload, len);
    case FRAME_GOAWAY:
        self->closing = 1;
        return 0;
    case FRAME_PUSH_PROMISE:
        return h2_connection_error(self, H2_PROTOCOL_ERROR);
    default:
        /* RFC 9113 4.1 requires unknown frame types to be ignored, but does not
         * require granting an unlimited amount of CPU to a run of them. One is
         * ignored; a flood shares the same no-progress budget as PING,
         * SETTINGS, and empty DATA. */
        return h2_note_unproductive(self);
    }
}

static void
shrink_idle_input_buffer(Http2Protocol *self)
{
    const Py_ssize_t retained = 32768;
    if (self->buf_len != 0 || self->cursor != 0 || self->buf_cap <= retained) return;
    char *shrunk = PyMem_Realloc(self->buf, (size_t)retained);
    if (shrunk != NULL) {
        self->buf = shrunk;
        self->buf_cap = retained;
    }
}

/* Parse and dispatch all complete frames in the input buffer. */
static int
parse_frames(Http2Protocol *self, int borrowed)
{
    while (!self->fatal) {
        Py_ssize_t avail = self->buf_len - self->cursor;
        if (avail < 9) {
            break;
        }
        const uint8_t *p = (const uint8_t *)self->buf + self->cursor;
        Py_ssize_t flen = ((Py_ssize_t)p[0] << 16) | ((Py_ssize_t)p[1] << 8) | p[2];
        int type = p[3];
        int flags = p[4];
        uint32_t sid = get_u32(p + 5) & 0x7FFFFFFF;
        if (flen > self->our_max_frame) {
            return h2_connection_error(self, H2_FRAME_SIZE_ERROR);
        }
        if (avail - 9 < flen) {
            break;  /* wait for the rest */
        }
        self->cursor += 9 + flen;
        if (dispatch_frame(self, type, flags, sid, p + 9, flen) < 0) {
            return -1;
        }
    }
    /* compact consumed prefix when it is large */
    if (!borrowed && self->cursor > 0) {
        Py_ssize_t remaining = self->buf_len - self->cursor;
        if (remaining > 0) {
            memmove(self->buf, self->buf + self->cursor, remaining);
        }
        self->buf_len = remaining;
        self->cursor = 0;
    }
    if (!borrowed) {
        shrink_idle_input_buffer(self);
    }
    return 0;
}

static int
start_pending_apps(Http2Protocol *self)
{
    PyObject *pending = self->pending_apps;
    if (PyList_GET_SIZE(pending) == 0) {
        return 0;
    }
    PyObject *replacement = PyList_New(0);
    if (replacement == NULL) {
        return -1;
    }
    self->pending_apps = replacement;
    self->app_batch_active = 1;

    Py_ssize_t count = PyList_GET_SIZE(pending);
    for (Py_ssize_t i = 0; i < count; i++) {
        Http2Stream *stream = (Http2Stream *)PyList_GET_ITEM(pending, i);
        if (stream->disconnected || stream->state == S_CLOSED) {
            stream_finish(
                stream, Py_None, (PyObject *)stream, WREATH_NFR_TERM_CANCELLED
            );
        }
        else if (start_stream_app(self, stream) < 0) {
            self->app_batch_active = 0;
            Py_DECREF(pending);
            return -1;
        }
    }
    self->app_batch_active = 0;
    Py_DECREF(pending);
    return 0;
}

/* ======================================================================== */
/* asyncio.Protocol interface                                               */
/* ======================================================================== */

static int
send_initial_settings(Http2Protocol *self)
{
    uint8_t payload[6 * 4];
    Py_ssize_t n = 0;
    #define PUT_SETTING(id, val) do { \
        wreath_store_u16_be(payload + n, (uint16_t)(id)); \
        put_u32(payload + n + 2, (uint32_t)(val)); n += 6; } while (0)
    PUT_SETTING(SET_ENABLE_PUSH, 0);
    PUT_SETTING(SET_MAX_CONCURRENT_STREAMS, self->max_concurrent);
    PUT_SETTING(SET_INITIAL_WINDOW_SIZE, self->our_initial_window);
    PUT_SETTING(SET_MAX_HEADER_LIST_SIZE, self->max_header_list);
    #undef PUT_SETTING
    if (h2_write_frame((PyObject *)self, FRAME_SETTINGS, 0, 0, payload, n) < 0) {
        return -1;
    }
    return 0;
}

static PyObject *
h2_connection_made(PyObject *op, PyObject *transport)
{
    Http2Protocol *self = (Http2Protocol *)op;
    self->transport = Py_NewRef(transport);
    self->transport_write_fn = PyObject_GetAttrString(transport, "write");
    if (self->transport_write_fn == NULL) {
        return NULL;
    }
    if (self->registry != NULL) {
        if (PySet_Add(self->registry, op) < 0) {
            return NULL;
        }
    }
    if (send_initial_settings(self) < 0) {
        return NULL;
    }
    if (h2_flush(op) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static int
buf_reserve(Http2Protocol *self, Py_ssize_t extra)
{
    if (self->buf_len + extra <= self->buf_cap) {
        return 0;
    }
    Py_ssize_t new_cap = self->buf_cap == 0 ? 16384 : self->buf_cap;
    while (new_cap < self->buf_len + extra) {
        new_cap *= 2;
    }
    char *grown = PyMem_Realloc(self->buf, (size_t)new_cap);
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->buf = grown;
    self->buf_cap = new_cap;
    return 0;
}

static PyObject *
h2_data_received(PyObject *op, PyObject *data);


static int
h2_consume_preface(Http2Protocol *self, const char *data, Py_ssize_t len,
                   Py_ssize_t *consumed)
{
    *consumed = 0;
    if (self->preface_seen < H2_PREFACE_LEN) {
        while (self->preface_seen < H2_PREFACE_LEN && *consumed < len) {
            if (data[*consumed] != H2_PREFACE[self->preface_seen]) {
                return h2_connection_error(self, H2_PROTOCOL_ERROR);
            }
            self->preface_seen++;
            (*consumed)++;
        }
    }
    return 0;
}


static int
h2_process_owned_input(Http2Protocol *self)
{
    if (self->preface_seen < H2_PREFACE_LEN && self->buf_len > 0) {
        Py_ssize_t consumed;
        if (h2_consume_preface(self, self->buf, self->buf_len, &consumed) < 0) {
            return -1;
        }
        if (self->fatal) {
            return 0;
        }
        Py_ssize_t remaining = self->buf_len - consumed;
        if (remaining > 0) {
            memmove(self->buf, self->buf + consumed, (size_t)remaining);
        }
        self->buf_len = remaining;
    }
    if (self->preface_seen == H2_PREFACE_LEN) {
        if (parse_frames(self, 0) < 0 || start_pending_apps(self) < 0) {
            return -1;
        }
    }
    return h2_flush((PyObject *)self);
}


int
wreath_http2_feed_external(PyObject *protocol, const char *data, Py_ssize_t size)
{
    if (!wreath_http2_protocol_check(protocol)) {
        PyErr_SetString(PyExc_TypeError, "expected native Http2Protocol");
        return -1;
    }
    if (size < 0) {
        PyErr_SetString(PyExc_ValueError, "negative external read size");
        return -1;
    }
    Http2Protocol *self = (Http2Protocol *)protocol;
    if (size == 0 || self->fatal || self->transport == NULL) {
        return 0;
    }
    Py_ssize_t consumed;
    if (h2_consume_preface(self, data, size, &consumed) < 0 || self->fatal) {
        return self->fatal ? 0 : -1;
    }
    data += consumed;
    size -= consumed;
    if (size == 0) {
        return h2_flush(protocol);
    }
    if (self->buf_len != 0) {
        if (buf_reserve(self, size) < 0) {
            return -1;
        }
        memcpy(self->buf + self->buf_len, data, (size_t)size);
        self->buf_len += size;
        return h2_process_owned_input(self);
    }

    /* Borrow an io_uring provided-buffer slice for this synchronous parse.
     * Parsed objects own everything they retain; only an incomplete trailing
     * frame is copied back before the reactor recycles the buffer. */
    char *owned = self->buf;
    Py_ssize_t owned_cap = self->buf_cap;
    self->buf = (char *)data;
    self->buf_len = size;
    self->buf_cap = size;
    self->cursor = 0;
    int result = parse_frames(self, 1);
    Py_ssize_t remaining = self->buf_len - self->cursor;
    const char *tail = data + self->cursor;
    self->buf = owned;
    self->buf_cap = owned_cap;
    self->buf_len = 0;
    self->cursor = 0;
    if (result < 0 || start_pending_apps(self) < 0) {
        return -1;
    }
    if (remaining > 0) {
        if (buf_reserve(self, remaining) < 0) {
            return -1;
        }
        memcpy(self->buf, tail, (size_t)remaining);
        self->buf_len = remaining;
    }
    return h2_flush(protocol);
}


int
wreath_http2_protocol_check(PyObject *protocol)
{
    return Http2ProtocolType != NULL &&
           PyObject_TypeCheck(protocol, Http2ProtocolType);
}


int
wreath_http2_acquire_read_buffer(PyObject *protocol, char **buffer,
                                 Py_ssize_t *capacity)
{
    if (!wreath_http2_protocol_check(protocol)) {
        PyErr_SetString(PyExc_TypeError, "expected native Http2Protocol");
        return -1;
    }
    Http2Protocol *self = (Http2Protocol *)protocol;
    if (self->read_offer_size > 0) {
        PyErr_SetString(PyExc_RuntimeError, "HTTP/2 read offer already active");
        return -1;
    }
    if (buf_reserve(self, 16 * 1024) < 0) {
        return -1;
    }
    self->read_offer_offset = self->buf_len;
    self->read_offer_size = self->buf_cap - self->buf_len;
    *buffer = self->buf + self->read_offer_offset;
    *capacity = self->read_offer_size;
    return 0;
}


int
wreath_http2_commit_read(PyObject *protocol, Py_ssize_t nbytes)
{
    if (!wreath_http2_protocol_check(protocol)) {
        PyErr_SetString(PyExc_TypeError, "expected native Http2Protocol");
        return -1;
    }
    Http2Protocol *self = (Http2Protocol *)protocol;
    if (self->read_offer_size <= 0) {
        PyErr_SetString(PyExc_RuntimeError, "HTTP/2 commit without read offer");
        return -1;
    }
    if (nbytes < 0 || nbytes > self->read_offer_size) {
        self->read_offer_offset = self->read_offer_size = 0;
        PyErr_SetString(PyExc_ValueError, "HTTP/2 committed byte count out of range");
        return -1;
    }
    self->buf_len = self->read_offer_offset + nbytes;
    self->read_offer_offset = self->read_offer_size = 0;
    return nbytes == 0 ? 0 : h2_process_owned_input(self);
}


static PyObject *
h2_data_received(PyObject *op, PyObject *data)
{
    char *bytes;
    Py_ssize_t len;
    if (PyBytes_AsStringAndSize(data, &bytes, &len) < 0 ||
        wreath_http2_feed_external(op, bytes, len) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
h2_eof_received(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    Http2Protocol *self = (Http2Protocol *)op;
    self->closing = 1;
    self->fatal = 1;
    Py_RETURN_FALSE;
}

static PyObject *
h2_connection_lost(PyObject *op, PyObject *Py_UNUSED(exc))
{
    Http2Protocol *self = (Http2Protocol *)op;
    self->closing = 1;
    self->fatal = 1;
    /* deliver disconnect and cancel every live stream */
    if (self->streams != NULL) {
        PyObject *values = PyDict_Values(self->streams);
        if (values != NULL) {
            Py_ssize_t n = PyList_GET_SIZE(values);
            for (Py_ssize_t i = 0; i < n; i++) {
                Http2Stream *st = (Http2Stream *)PyList_GET_ITEM(values, i);
                st->disconnected = 1;
                h2_abort_pending_send(st);
                if (st->receive_waiter != NULL) {
                    PyObject *msg = Py_BuildValue("{s:s}", "type", "http.disconnect");
                    if (msg != NULL) {
                        PyObject *w = st->receive_waiter;
                        st->receive_waiter = NULL;
                        h2_future_set_result(w, msg);
                        Py_DECREF(w);
                        Py_DECREF(msg);
                    } else {
                        PyErr_Clear();
                    }
                }
                if (st->task != NULL) {
                    /* native-lint: allow NC005 -- connection teardown, once per
                       stream, once per connection: not a per-message path. */
                    PyObject *r = PyObject_CallMethod(st->task, "cancel", NULL);
                    Py_XDECREF(r);
                    if (r == NULL) PyErr_Clear();
                }
            }
            Py_DECREF(values);
        }
        PyDict_Clear(self->streams);
    }
    if (self->pending_apps != NULL) {
        if (PyList_SetSlice(
                self->pending_apps, 0, PyList_GET_SIZE(self->pending_apps), NULL
            ) < 0) {
            PyErr_Clear();
        }
    }
    if (self->conn_blocked != NULL) {
        Py_ssize_t queued = PyList_GET_SIZE(self->conn_blocked);
        if (PyList_SetSlice(self->conn_blocked, 0, queued, NULL) < 0) {
            PyErr_Clear();
        }
        self->conn_blocked_head = 0;
    }
    self->active_requests = 0;
    if (self->registry != NULL) {
        if (PySet_Discard(self->registry, op) < 0) {
            PyErr_Clear();
        }
    }
    Py_CLEAR(self->transport);
    Py_CLEAR(self->transport_write_fn);
    Py_RETURN_NONE;
}

static PyObject *
h2_pause_writing(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    ((Http2Protocol *)op)->write_paused = 1;
    Py_RETURN_NONE;
}

static PyObject *
h2_resume_writing(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    Http2Protocol *self = (Http2Protocol *)op;
    self->write_paused = 0;
    if (h2_flush(op) < 0 || h2_schedule_pending(op) < 0 || h2_flush(op) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
h2_stop_accepting(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    Http2Protocol *self = (Http2Protocol *)op;
    self->accepting = 0;
    if (!self->goaway_sent) {
        uint8_t payload[8];
        put_u32(payload, self->last_stream_id);
        put_u32(payload + 4, H2_NO_ERROR);
        if (h2_write_frame(op, FRAME_GOAWAY, 0, 0, payload, 8) < 0) {
            return NULL;
        }
        self->goaway_sent = 1;
    }
    self->closing = 1;
    if (h2_flush(op) < 0) {
        return NULL;
    }
    /* If nothing is in flight, close immediately. */
    if (self->active_requests == 0 && self->transport != NULL) {
        PyObject *r = PyObject_CallMethod(self->transport, "close", NULL);
        Py_XDECREF(r);
    }
    Py_RETURN_NONE;
}

static PyObject *
h2_shutdown(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    Http2Protocol *self = (Http2Protocol *)op;
    if (self->transport != NULL) {
        PyObject *r = PyObject_CallMethod(self->transport, "close", NULL);
        Py_XDECREF(r);
    }
    Py_RETURN_NONE;
}

/* The count the facade's graceful close polls; see the twin in server_http1.c
 * for why a missing attribute here is worse than a wrong one. Unlike HTTP/1.1
 * this is a real concurrent count: streams not yet closed. */
static PyObject *
h2_get_active_requests(PyObject *op, void *Py_UNUSED(closure))
{
    return PyLong_FromLong(((Http2Protocol *)op)->active_requests);
}

static PyGetSetDef h2_getset[] = {
    {"active_requests", h2_get_active_requests, NULL,
     "Streams this connection still owes a response for.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyMethodDef h2_methods[] = {
    {"connection_made", h2_connection_made, METH_O, NULL},
    {"data_received", h2_data_received, METH_O, NULL},
    {"eof_received", h2_eof_received, METH_NOARGS, NULL},
    {"connection_lost", h2_connection_lost, METH_O, NULL},
    {"pause_writing", h2_pause_writing, METH_NOARGS, NULL},
    {"resume_writing", h2_resume_writing, METH_NOARGS, NULL},
    {"_run_write_scheduler", h2_run_write_scheduler, METH_NOARGS, NULL},
    {"stop_accepting", h2_stop_accepting, METH_NOARGS, NULL},
    {"shutdown", h2_shutdown, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static int
h2_traverse(PyObject *op, visitproc visit, void *arg)
{
    Http2Protocol *self = (Http2Protocol *)op;
    Py_VISIT(self->app);
    Py_VISIT(self->native_app);
    Py_VISIT(self->policy.descriptor);
    Py_VISIT(self->loop);
    Py_VISIT(self->registry);
    Py_VISIT(self->config);
    Py_VISIT(self->default_response_headers);
    Py_VISIT(self->transport);
    Py_VISIT(self->transport_write_fn);
    Py_VISIT(self->loop_create_future);
    Py_VISIT(self->loop_create_task);
    Py_VISIT(self->scope_type);
    Py_VISIT(self->scope_asgi);
    Py_VISIT(self->scope_http_version);
    Py_VISIT(self->scope_scheme);
    Py_VISIT(self->scope_root_path);
    Py_VISIT(self->streams);
    Py_VISIT(self->pending_apps);
    Py_VISIT(self->pending_priorities);
    Py_VISIT(self->conn_blocked);
    Py_VISIT(self->stream_window_blocked);
    Py_VISIT(self->scheduler_callable);
    Py_VISIT(self->out);
    Py_VISIT(self->header_block);
    return 0;
}

static int
h2_clear(PyObject *op)
{
    Http2Protocol *self = (Http2Protocol *)op;
    Py_CLEAR(self->app);
    Py_CLEAR(self->native_app);
    wreath_policy_program_clear(&self->policy);
    Py_CLEAR(self->loop);
    Py_CLEAR(self->registry);
    Py_CLEAR(self->config);
    Py_CLEAR(self->default_response_headers);
    Py_CLEAR(self->transport);
    Py_CLEAR(self->transport_write_fn);
    Py_CLEAR(self->loop_create_future);
    Py_CLEAR(self->loop_create_task);
    Py_CLEAR(self->scope_type);
    Py_CLEAR(self->scope_asgi);
    Py_CLEAR(self->scope_http_version);
    Py_CLEAR(self->scope_scheme);
    Py_CLEAR(self->scope_root_path);
    Py_CLEAR(self->streams);
    Py_CLEAR(self->pending_apps);
    Py_CLEAR(self->pending_priorities);
    Py_CLEAR(self->conn_blocked);
    Py_CLEAR(self->stream_window_blocked);
    Py_CLEAR(self->scheduler_callable);
    Py_CLEAR(self->out);
    Py_CLEAR(self->header_block);
    wreath_hpack_table_clear(&self->hpack);
    if (self->buf != NULL) {
        PyMem_Free(self->buf);
        self->buf = NULL;
    }
    return 0;
}

static void
h2_dealloc(PyObject *op)
{
    PyObject_GC_UnTrack(op);
    h2_clear(op);
    Py_TYPE(op)->tp_free(op);
}

static PyObject *
h2_new(PyTypeObject *type, PyObject *Py_UNUSED(a), PyObject *Py_UNUSED(k))
{
    Http2Protocol *self = (Http2Protocol *)type->tp_alloc(type, 0);
    return (PyObject *)self;
}

static int
h2_init(PyObject *op, PyObject *args, PyObject *kwargs)
{
    Http2Protocol *self = (Http2Protocol *)op;
    PyObject *app, *config, *loop, *registry, *recorder = NULL;
    static char *kwlist[] = {"app", "config", "loop", "connection_registry",
                             "recorder", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "OOOO|O", kwlist, &app, &config,
                                     &loop, &registry, &recorder)) {
        return -1;
    }
    self->nfr_worker = flight_capi != NULL ? wreath_flight_worker_from(recorder) : NULL;
    self->nfr_connection_id =
        self->nfr_worker != NULL ? wreath_flight_next_connection_id() : 0;
    self->app = Py_NewRef(app);
    self->native_app = PyObject_GetAttrString(app, "_wreath_http");
    if (self->native_app == NULL) {
        PyErr_Clear();
    }
    self->policy.descriptor = NULL;
    if (self->native_app != NULL && wreath_policy_program_load(&self->policy, app) < 0) {
        return -1;
    }
    self->config = Py_NewRef(config);
    {
        PyObject *defaults = PyObject_GetAttrString(config, "_default_response_headers");
        PyObject *headers;
        if (defaults == NULL) {
            return -1;
        }
        headers = PyObject_GetAttrString(defaults, "headers");
        Py_DECREF(defaults);
        if (headers == NULL) {
            return -1;
        }
        self->default_response_headers = PySequence_Fast(
            headers, "default response headers must be a sequence");
        Py_DECREF(headers);
        if (self->default_response_headers == NULL) {
            return -1;
        }
        for (Py_ssize_t i = 0;
             i < PySequence_Fast_GET_SIZE(self->default_response_headers); i++) {
            PyObject *name;
            PyObject *value;
            if (response_header_parts(
                    PySequence_Fast_GET_ITEM(self->default_response_headers, i),
                    &name, &value) < 0) {
                return -1;
            }
        }
    }
    self->loop = Py_NewRef(loop);
    self->registry = Py_NewRef(registry);
    self->transport = NULL;
    self->transport_write_fn = NULL;
    self->loop_create_future = PyObject_GetAttrString(loop, "create_future");
    self->loop_create_task = PyObject_GetAttrString(loop, "create_task");
    self->scope_type = PyUnicode_FromString("http");
    self->scope_asgi = Py_BuildValue(
        "{s:s,s:s}", "version", "3.0", "spec_version", "2.5"
    );
    self->scope_http_version = PyUnicode_FromString("2");
    self->scope_scheme = PyUnicode_FromString("https");
    self->scope_root_path = PyUnicode_FromString("");
    if (!self->loop_create_future || !self->loop_create_task ||
        !self->scope_type || !self->scope_asgi || !self->scope_http_version ||
        !self->scope_scheme || !self->scope_root_path) {
        return -1;
    }
    self->streams = PyDict_New();
    self->pending_apps = PyList_New(0);
    self->pending_priorities = PyDict_New();
    self->conn_blocked = PyList_New(0);
    self->stream_window_blocked = PyDict_New();
    self->conn_blocked_head = 0;
    self->scheduler_callable = PyObject_GetAttrString(op, "_run_write_scheduler");
    self->scheduler_scheduled = 0;
    self->rfc9218_enabled = 0;
    self->nonincremental_stream = 0;
    PyObject *native_loop = PyObject_GetAttrString(loop, "_native_loop");
    if (native_loop != NULL) {
        self->rfc9218_enabled = PyObject_IsTrue(native_loop);
        Py_DECREF(native_loop);
        if (self->rfc9218_enabled < 0) {
            return -1;
        }
    } else {
        PyErr_Clear();
    }
    self->out = PyByteArray_FromStringAndSize("", 0);
    self->header_block = PyByteArray_FromStringAndSize("", 0);
    if (!self->streams || !self->pending_apps || !self->pending_priorities ||
        !self->conn_blocked ||
        !self->stream_window_blocked ||
        !self->scheduler_callable ||
        !self->out || !self->header_block) {
        return -1;
    }
    self->buf = NULL;
    self->buf_len = self->buf_cap = self->cursor = 0;
    self->read_offer_offset = self->read_offer_size = 0;
    self->preface_seen = 0;
    self->got_first_settings = 0;
    self->closing = 0;
    self->fatal = 0;
    self->goaway_sent = 0;
    self->accepting = 1;
    self->app_batch_active = 0;
    self->header_stream = 0;
    self->header_end_stream = 0;
    self->last_stream_id = 0;
    self->active_requests = 0;
    self->idle_frames = 0;
    self->write_paused = 0;

    Py_ssize_t v;
    if (wreath_read_ssize_attr(config, "max_concurrent_streams", &self->max_concurrent) < 0 ||
        wreath_read_ssize_attr(config, "initial_stream_window", &v) < 0) {
        return -1;
    }
    self->our_initial_window = v;
    if (wreath_read_ssize_attr(config, "initial_connection_window", &v) < 0) {
        return -1;
    }
    self->conn_recv_window = v;
    self->conn_consumed_pending = 0;
    self->conn_credit_threshold = v / 2;
    if (self->conn_credit_threshold > 64 * 1024) {
        self->conn_credit_threshold = 64 * 1024;
    }
    if (self->conn_credit_threshold < 1) self->conn_credit_threshold = 1;
    if (wreath_read_ssize_attr(config, "max_header_list_bytes", &self->max_header_list) < 0 ||
        wreath_read_ssize_attr(config, "max_header_count", &self->max_header_count) < 0 ||
        wreath_read_ssize_attr(config, "max_body_bytes", &self->max_body_bytes) < 0 ||
        wreath_read_ssize_attr(config, "max_body_chunks", &self->max_body_chunks) < 0 ||
        wreath_read_ssize_attr(config, "hpack_table_bytes", &self->hpack_max) < 0) {
        return -1;
    }
    {
        PyObject *timeout = PyObject_GetAttrString(config, "request_timeout");
        if (timeout == NULL) return -1;
        self->request_timeout = PyFloat_AsDouble(timeout);
        Py_DECREF(timeout);
        if (self->request_timeout == -1.0 && PyErr_Occurred()) return -1;
    }
    self->peer_initial_window = 65535;   /* RFC default until peer SETTINGS */
    self->conn_send_window = 65535;
    self->peer_max_frame = DEFAULT_MAX_FRAME;
    self->our_max_frame = DEFAULT_MAX_FRAME;
    wreath_hpack_table_init(&self->hpack, (size_t)self->hpack_max);
    return 0;
}

static PyType_Slot h2_slots[] = {
    {Py_tp_new, h2_new},
    {Py_tp_init, h2_init},
    {Py_tp_dealloc, h2_dealloc},
    {Py_tp_traverse, h2_traverse},
    {Py_tp_clear, h2_clear},
    {Py_tp_methods, h2_methods},
    {Py_tp_getset, h2_getset},
    {0, NULL},
};

PyType_Spec http2_protocol_spec = {
    .name = "wreath._native._server.Http2Protocol",
    .basicsize = sizeof(Http2Protocol),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_HAVE_GC,
    .slots = h2_slots,
};

/* Called from module init to register the stream/protocol types. */
int
wreath_http2_ready(PyObject *module)
{
    if (wreath_hpack_build_huffman() < 0) {
        return -1;
    }
    PyObject *stream_type = PyType_FromSpec(&stream_spec);
    if (stream_type == NULL) {
        return -1;
    }
    Http2StreamType = (PyTypeObject *)stream_type;  /* keep (leaked at shutdown) */
    PyObject *proto_type = PyType_FromSpec(&http2_protocol_spec);
    if (proto_type == NULL) {
        return -1;
    }
    Http2ProtocolType = (PyTypeObject *)proto_type;
    if (PyModule_AddObjectRef(module, "Http2Protocol", proto_type) < 0) {
        Py_DECREF(proto_type);
        return -1;
    }
    Py_DECREF(proto_type);
    return 0;
}
