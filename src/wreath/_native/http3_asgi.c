/* HTTP/3 ASGI bridge: nghttp3 framing/QPACK callbacks + per-stream ASGI.
 *
 * Built only when WREATH_BUILD_HTTP3=1 (HTTP/3 is opt-in at build time). Maps QUIC request streams to
 * ASGI http scopes (http_version == "3") and turns ASGI responses back into
 * nghttp3 responses. Response segments are submitted while streaming and retained
 * under bounded acknowledgement-driven credit while ngtcp2 may retransmit them.
 */
#include "http3.h"
#include "ascii.h"
#include "simd.h"

#include <string.h>

/* --- ALPN ---------------------------------------------------------------- */
int
wreath_h3_alpn_select_cb(SSL *ssl, const unsigned char **out, unsigned char *outlen,
                      const unsigned char *in, unsigned int inlen, void *arg)
{
    (void)ssl;
    (void)arg;
    static const unsigned char h3[] = {2, 'h', '3'};
    if (SSL_select_next_proto((unsigned char **)out, outlen, h3, sizeof(h3), in,
                              inlen) != OPENSSL_NPN_NEGOTIATED) {
        return SSL_TLSEXT_ERR_ALERT_FATAL;
    }
    return SSL_TLSEXT_ERR_OK;
}

/* --- immediate value awaitable ------------------------------------------- */
typedef struct {
    PyObject_HEAD
    PyObject *value;
} H3ValueAwaitable;

static PyObject *
h3_value_await(PyObject *self)
{
    return Py_NewRef(self);
}

static PyObject *
h3_value_next(PyObject *op)
{
    H3ValueAwaitable *self = (H3ValueAwaitable *)op;
    PyObject *value = self->value;
    if (value == NULL) {
        PyErr_SetNone(PyExc_StopIteration);
        return NULL;
    }
    self->value = NULL;
    if (PyTuple_Check(value) || PyExceptionInstance_Check(value)) {
        PyObject *exc = PyObject_CallOneArg(PyExc_StopIteration, value);
        Py_DECREF(value);
        if (exc == NULL) return NULL;
        PyErr_SetObject(PyExc_StopIteration, exc);
        Py_DECREF(exc);
        return NULL;
    }
    PyErr_SetObject(PyExc_StopIteration, value);
    Py_DECREF(value);
    return NULL;
}

static void
h3_value_dealloc(PyObject *op)
{
    Py_XDECREF(((H3ValueAwaitable *)op)->value);
    Py_TYPE(op)->tp_free(op);
}

static PyAsyncMethods h3_value_async = {.am_await = h3_value_await};

static PyTypeObject H3ValueAwaitableType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._http3._ValueAwaitable",
    .tp_basicsize = sizeof(H3ValueAwaitable),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_dealloc = h3_value_dealloc,
    .tp_as_async = &h3_value_async,
    .tp_iter = PyObject_SelfIter,
    .tp_iternext = h3_value_next,
};

static PyObject *
resolved_future(PyObject *Py_UNUSED(loop), PyObject *value)
{
    H3ValueAwaitable *awaitable = PyObject_New(
        H3ValueAwaitable, &H3ValueAwaitableType
    );
    if (awaitable == NULL) return NULL;
    awaitable->value = Py_NewRef(value);
    return (PyObject *)awaitable;
}

static PyObject *endpoint_loop(WreathH3Conn *c) { return c->endpoint->loop; }
static PyObject *endpoint_app(WreathH3Conn *c) { return c->endpoint->app; }

static int
h3_response_over_high_water(WreathH3Stream *s)
{
    WreathH3Conn *c = s->conn;
    WreathH3Endpoint *ep = c->endpoint;
    return s->resp_retained_bytes > ep->response_high_water ||
           s->resp_retained_segments > ep->response_high_water_segments ||
           c->retained_response_bytes > ep->response_high_water ||
           c->retained_response_segments > ep->response_high_water_segments;
}

static int
h3_stream_below_response_low_water(WreathH3Stream *s)
{
    WreathH3Endpoint *ep = s->conn->endpoint;
    return s->resp_retained_bytes <= ep->response_low_water &&
           s->resp_retained_segments <= ep->response_low_water_segments;
}

static int
h3_resolve_send_waiter(WreathH3Stream *s)
{
    PyObject *waiter = s->send_waiter;
    if (waiter == NULL) {
        return 0;
    }
    s->send_waiter = NULL;
    if (s->conn != NULL && s->conn->endpoint->response_backpressure_waiters > 0) {
        s->conn->endpoint->response_backpressure_waiters--;
    }
    PyObject *done = PyObject_CallMethod(waiter, "done", NULL);
    if (done == NULL) {
        Py_DECREF(waiter);
        return -1;
    }
    int is_done = PyObject_IsTrue(done);
    Py_DECREF(done);
    if (is_done < 0) {
        Py_DECREF(waiter);
        return -1;
    }
    if (!is_done) {
        PyObject *result = PyObject_CallMethod(waiter, "set_result", "(O)", Py_None);
        if (result == NULL) {
            Py_DECREF(waiter);
            return -1;
        }
        Py_DECREF(result);
    }
    Py_DECREF(waiter);
    return 0;
}

static void
h3_cancel_send_waiter(WreathH3Stream *s)
{
    PyObject *waiter = s->send_waiter;
    if (waiter == NULL) {
        return;
    }
    s->send_waiter = NULL;
    if (s->conn != NULL && s->conn->endpoint->response_backpressure_waiters > 0) {
        s->conn->endpoint->response_backpressure_waiters--;
    }
    PyObject *done = PyObject_CallMethod(waiter, "done", NULL);
    int is_done = done != NULL ? PyObject_IsTrue(done) : 1;
    Py_XDECREF(done);
    if (is_done == 0) {
        PyObject *result = PyObject_CallMethod(waiter, "cancel", NULL);
        Py_XDECREF(result);
    }
    if (PyErr_Occurred()) {
        PyErr_Clear();
    }
    Py_DECREF(waiter);
}

static int
h3_maybe_resume_senders(WreathH3Conn *c)
{
    WreathH3Endpoint *ep = c->endpoint;
    if (c->retained_response_bytes > ep->response_low_water ||
        c->retained_response_segments > ep->response_low_water_segments) {
        return 0;
    }
    PyObject *streams = PyDict_Values(c->streams);
    if (streams == NULL) {
        return -1;
    }
    Py_ssize_t count = PyList_GET_SIZE(streams);
    for (Py_ssize_t i = 0; i < count; i++) {
        WreathH3Stream *s = (WreathH3Stream *)PyList_GET_ITEM(streams, i);
        if (s->send_waiter != NULL && h3_stream_below_response_low_water(s) &&
            h3_resolve_send_waiter(s) < 0) {
            Py_DECREF(streams);
            return -1;
        }
    }
    Py_DECREF(streams);
    return 0;
}

static PyObject *
h3_response_send_result(WreathH3Stream *s, PyObject *loop)
{
    if (!h3_response_over_high_water(s)) {
        return resolved_future(loop, Py_None);
    }
    if (s->send_waiter == NULL) {
        s->send_waiter = PyObject_CallNoArgs(
            s->conn->endpoint->loop_create_future
        );
        if (s->send_waiter == NULL) {
            return NULL;
        }
        s->conn->endpoint->response_backpressure_waiters++;
        s->conn->endpoint->response_backpressure_pauses++;
    }
    return Py_NewRef(s->send_waiter);
}

static PyObject *
wreath_request_context_new(
    PyObject *scope_type, PyObject *asgi, PyObject *http_version,
    PyObject *method, PyObject *scheme, PyObject *path, PyObject *raw_path,
    PyObject *query, PyObject *headers, PyObject *server, PyObject *client,
    PyObject *root_path
)
{
    return wreath_h3_request_capi->new_context(
        scope_type, asgi, http_version, method, scheme, path, raw_path,
        query, headers, server, client, root_path
    );
}

/* Interned ASGI message keys, resolved once.
 *
 * PyDict_GetItemString builds a fresh str for its key on every call, so reading
 * an ASGI message with it allocated one temporary per field, per message. The
 * HTTP/1 protocol already caches these (see s_type in server_common.c); this is
 * the same thing for the separate _http3 extension. */
static PyObject *k_type = NULL;
static PyObject *k_status = NULL;
static PyObject *k_headers = NULL;
static PyObject *k_body = NULL;
static PyObject *k_more_body = NULL;
static PyObject *k_method = NULL;
static PyObject *k_path = NULL;
static PyObject *k_scheme = NULL;
static PyObject *k_client = NULL;
static PyObject *k_receive = NULL;
static PyObject *k_send = NULL;
static PyObject *k_done = NULL;
static PyObject *k_wreath_flight = NULL;
static PyObject *k_host_name = NULL;  /* b"host", synthesized once per absence */

int
wreath_h3_init_message_keys(void)
{
    if (k_type != NULL) {
        return 0;
    }
    if (PyType_Ready(&H3ValueAwaitableType) < 0 ||
        (k_type = PyUnicode_InternFromString("type")) == NULL ||
        (k_status = PyUnicode_InternFromString("status")) == NULL ||
        (k_headers = PyUnicode_InternFromString("headers")) == NULL ||
        (k_body = PyUnicode_InternFromString("body")) == NULL ||
        (k_more_body = PyUnicode_InternFromString("more_body")) == NULL ||
        (k_method = PyUnicode_InternFromString("method")) == NULL ||
        (k_path = PyUnicode_InternFromString("path")) == NULL ||
        (k_scheme = PyUnicode_InternFromString("scheme")) == NULL ||
        (k_client = PyUnicode_InternFromString("client")) == NULL ||
        (k_receive = PyUnicode_InternFromString("_receive")) == NULL ||
        (k_send = PyUnicode_InternFromString("_send")) == NULL ||
        (k_done = PyUnicode_InternFromString("_done")) == NULL ||
        (k_wreath_flight = PyUnicode_InternFromString("_wreath_flight")) == NULL ||
        (k_host_name = PyBytes_FromString("host")) == NULL) {
        return -1;
    }
    return 0;
}

/* --- Http3Stream: ASGI plumbing ------------------------------------------ */

static int submit_response(WreathH3Stream *s, int default_status);

static int
h3_response_header_parts(PyObject *pair, PyObject **name, PyObject **value)
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
    if (!(name_size > 0 && name_data[0] == ':')) {
        if (name_size == 0) {
            PyErr_SetString(PyExc_RuntimeError, "invalid response header");
            return -1;
        }
        for (Py_ssize_t i = 0; i < name_size; i++) {
            if (!wreath_ascii_token[(unsigned char)name_data[i]]) {
                PyErr_SetString(PyExc_RuntimeError, "invalid response header");
                return -1;
            }
        }
    }
    if (wreath_value_run(PyBytes_AS_STRING(*value), PyBytes_GET_SIZE(*value)) !=
        PyBytes_GET_SIZE(*value)) {
        PyErr_SetString(PyExc_RuntimeError, "invalid response header");
        return -1;
    }
    return 0;
}

int
wreath_h3_validate_response_headers(PyObject *headers)
{
    for (Py_ssize_t i = 0; i < PySequence_Fast_GET_SIZE(headers); i++) {
        PyObject *name;
        PyObject *value;
        if (h3_response_header_parts(
                PySequence_Fast_GET_ITEM(headers, i), &name, &value) < 0) {
            return -1;
        }
    }
    return 0;
}

static PyObject *
h3_take_buffered_body(WreathH3Stream *s)
{
    Py_ssize_t size = PyByteArray_GET_SIZE(s->body_buffer);
    PyObject *body = PyBytes_FromStringAndSize(
        PyByteArray_AS_STRING(s->body_buffer), size);
    if (body == NULL) return NULL;
    if (PyByteArray_Resize(s->body_buffer, 0) < 0) {
        Py_DECREF(body);
        return NULL;
    }
    WreathH3Conn *c = s->conn;
    if (c != NULL) {
        c->queued_body_bytes -= size;
        if (size > 0 && c->queued_body_messages > 0) c->queued_body_messages--;
        if (c->conn != NULL) {
            ngtcp2_conn_extend_max_stream_offset(c->conn, s->stream_id, (uint64_t)size);
            ngtcp2_conn_extend_max_offset(c->conn, (uint64_t)size);
        }
    }
    return body;
}

static PyObject *
h3_stream_receive(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    WreathH3Stream *s = (WreathH3Stream *)op;
    PyObject *loop = s->loop;
    if (loop == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "HTTP/3 stream has no event loop");
        return NULL;
    }
    int native = s->conn != NULL && s->conn->endpoint->native_app != NULL;
    if (s->conn == NULL || s->disconnected) {
        PyObject *msg = native
            ? PyTuple_Pack(3, Py_None, Py_False, Py_True)
            : Py_BuildValue("{s:s}", "type", "http.disconnect");
        if (msg == NULL) return NULL;
        PyObject *f = resolved_future(loop, msg);
        Py_DECREF(msg);
        return f;
    }
    if (PyByteArray_GET_SIZE(s->body_buffer) > 0) {
        PyObject *body = h3_take_buffered_body(s);
        if (body == NULL) return NULL;
        if (s->conn != NULL && wreath_h3_flush(s->conn) < 0) {
            Py_DECREF(body);
            return NULL;
        }
        int more = !s->request_ended;
        PyObject *msg = native
            ? PyTuple_Pack(3, body, more ? Py_True : Py_False, Py_False)
            : Py_BuildValue("{s:s,s:O,s:O}", "type", "http.request",
                            "body", body, "more_body",
                            more ? Py_True : Py_False);
        Py_DECREF(body);
        if (msg == NULL) return NULL;
        PyObject *f = resolved_future(loop, msg);
        Py_DECREF(msg);
        return f;
    }
    if (s->request_ended) {
        PyObject *msg = native
            ? PyTuple_Pack(3, Py_None, Py_False, Py_False)
            : Py_BuildValue("{s:s,s:y#,s:O}", "type", "http.request",
                            "body", "", 0, "more_body", Py_False);
        if (msg == NULL) return NULL;
        PyObject *f = resolved_future(loop, msg);
        Py_DECREF(msg);
        return f;
    }
    PyObject *fut = PyObject_CallNoArgs(s->conn->endpoint->loop_create_future);
    if (fut == NULL) return NULL;
    Py_XSETREF(s->receive_waiter, Py_NewRef(fut));
    return fut;
}

static PyObject *
h3_response_start(WreathH3Stream *s, PyObject *status_obj, PyObject *headers)
{
    PyObject *loop = endpoint_loop(s->conn);
    long status = status_obj ? PyLong_AsLong(status_obj) : 200;
    if (status == -1 && PyErr_Occurred()) return NULL;
    if (status < 100 || status > 999) {
        PyErr_SetString(PyExc_ValueError, "response status must be between 100 and 999");
        return NULL;
    }
    if (s->response_started) {
        PyErr_SetString(PyExc_RuntimeError, "response already started");
        return NULL;
    }
    if (s->policy_state.native &&
        wreath_policy_egress(&s->conn->endpoint->policy,
                             &s->policy_state, headers) < 0) {
        return NULL;
    }
    if (s->scope != NULL) {
        wreath_h3_request_capi->update_policy(s->scope, &s->policy_state);
    }
    PyObject *header_items = headers
        ? PySequence_Fast(headers, "response headers must be a sequence")
        : PyTuple_New(0);
    if (header_items == NULL) return NULL;
    s->status = (int)status;
    Py_XSETREF(s->resp_headers, header_items);
    if (submit_response(s, 200) < 0) return NULL;
    s->response_started = 1;
    return resolved_future(loop, Py_None);
}


static PyObject *
h3_response_body(WreathH3Stream *s, PyObject *body, int more_body)
{
    PyObject *loop = endpoint_loop(s->conn);
    if (!s->response_started) {
        PyErr_SetString(PyExc_RuntimeError, "response not started");
        return NULL;
    }
    if (body != NULL && body != Py_None) {
        if (!PyBytes_Check(body)) {
            PyErr_SetString(PyExc_TypeError, "http.response.body 'body' must be bytes");
            return NULL;
        }
        Py_ssize_t body_size = PyBytes_GET_SIZE(body);
        if (body_size > 0) {
            WreathH3Conn *c = s->conn;
            WreathH3Endpoint *ep = c->endpoint;
            if (body_size > PY_SSIZE_T_MAX - s->resp_retained_bytes ||
                body_size > PY_SSIZE_T_MAX - c->retained_response_bytes ||
                body_size > PY_SSIZE_T_MAX - ep->retained_response_bytes) {
                PyErr_SetString(PyExc_OverflowError,
                                "retained HTTP/3 response bytes overflow");
                return NULL;
            }
            if (s->resp_chunks_len == s->resp_chunks_cap) {
                if (s->resp_chunks_cap > PY_SSIZE_T_MAX / 2) return PyErr_NoMemory();
                Py_ssize_t cap = s->resp_chunks_cap ? s->resp_chunks_cap * 2 : 16;
                if ((size_t)cap > SIZE_MAX / sizeof(PyObject *)) return PyErr_NoMemory();
                PyObject **grown = PyMem_Realloc(
                    s->resp_chunks, (size_t)cap * sizeof(PyObject *));
                if (grown == NULL) return PyErr_NoMemory();
                s->resp_chunks = grown;
                s->resp_chunks_cap = cap;
            }
            s->resp_chunks[s->resp_chunks_len++] = Py_NewRef(body);
            s->resp_retained_bytes += body_size;
            s->resp_retained_segments++;
            c->retained_response_bytes += body_size;
            c->retained_response_segments++;
            ep->retained_response_bytes += body_size;
            ep->retained_response_segments++;
        }
        if (s->nfr_active) s->nfr_bytes_out += (uint64_t)PyBytes_GET_SIZE(body);
    }
    if (!more_body) s->resp_eof = 1;
    if (s->conn != NULL && s->conn->h3 != NULL) {
        nghttp3_conn_resume_stream(s->conn->h3, s->stream_id);
        wreath_h3_flush(s->conn);
    }
    return h3_response_send_result(s, loop);
}


static PyObject *
h3_stream_send(PyObject *op, PyObject *message)
{
    WreathH3Stream *s = (WreathH3Stream *)op;
    if (s->conn == NULL) {
        /* Stream gone: swallow the message and let the app unwind. `_send` is
         * awaited, so this must still be an awaitable, not a bare None. */
        return s->loop ? resolved_future(s->loop, Py_None) : NULL;
    }
    PyObject *type = PyDict_GetItem(message, k_type);
    if (type == NULL) {
        PyErr_SetString(PyExc_KeyError, "message has no 'type'");
        return NULL;
    }
    const char *t = PyUnicode_AsUTF8(type);
    if (t == NULL) return NULL;

    if (strcmp(t, "http.response.start") == 0) {
        return h3_response_start(
            s, PyDict_GetItem(message, k_status), PyDict_GetItem(message, k_headers)
        );
    }
    if (strcmp(t, "http.response.body") == 0) {
        PyObject *body = PyDict_GetItem(message, k_body);
        PyObject *more = PyDict_GetItem(message, k_more_body);
        int more_body = (more != NULL && PyObject_IsTrue(more));
        return h3_response_body(s, body, more_body);
    }
    PyErr_Format(PyExc_RuntimeError, "unsupported ASGI message type: %s", t);
    return NULL;
}

/* nghttp3 pulls response body from the queued immutable segments.
 *
 * Each vector points straight into a retained bytes object, so the addresses
 * stay valid for as long as nghttp3/ngtcp2 may retransmit them; they are
 * released only by the acknowledgement path. */
static nghttp3_ssize
read_response_data(nghttp3_conn *conn, int64_t stream_id, nghttp3_vec *vec,
                   size_t veccnt, uint32_t *pflags, void *conn_user_data,
                   void *stream_user_data)
{
    (void)conn;
    (void)stream_id;
    (void)conn_user_data;
    WreathH3Stream *s = (WreathH3Stream *)stream_user_data;
    Py_ssize_t size = s->resp_chunks_len;
    size_t n = 0;
    while (n < veccnt && s->resp_read_index < size) {
        PyObject *seg = s->resp_chunks[s->resp_read_index];
        Py_ssize_t avail = PyBytes_GET_SIZE(seg) - s->resp_read_offset;
        if (avail <= 0) {
            /* An empty chunk is not end-of-body: skip it. */
            s->resp_read_index++;
            s->resp_read_offset = 0;
            continue;
        }
        vec[n].base = (uint8_t *)PyBytes_AS_STRING(seg) + s->resp_read_offset;
        vec[n].len = (size_t)avail;
        n++;
        s->resp_read_index++;
        s->resp_read_offset = 0;
    }
    if (n == 0) {
        if (s->resp_eof) {
            *pflags = NGHTTP3_DATA_FLAG_EOF;
            return 0;
        }
        return NGHTTP3_ERR_WOULDBLOCK;  /* temporarily empty, more to come */
    }
    /* EOF only once the app is done and every queued byte has been offered. */
    if (s->resp_eof && s->resp_read_index >= size) {
        *pflags = NGHTTP3_DATA_FLAG_EOF;
    }
    return (nghttp3_ssize)n;
}

/* Release response segments the peer has acknowledged.
 *
 * `datalen` is a count of application DATA payload bytes as accounted by
 * nghttp3 (see the contract documented at wreath_h3_acked_stream_data), not raw
 * QUIC stream bytes. Payload is acknowledged in order, so this treats `datalen`
 * as credit against the front of the queue and releases a segment only once the
 * credit covers it whole. A partially acknowledged segment is kept: nghttp3 may
 * still retransmit from its address. */
static void
h3_release_acked(WreathH3Stream *s, uint64_t datalen)
{
    if (s->resp_chunks == NULL) {
        return;
    }
    s->resp_payload_acked += datalen;
    Py_ssize_t size = s->resp_chunks_len;
    while (s->resp_head < size) {
        PyObject *seg = s->resp_chunks[s->resp_head];
        uint64_t n = (uint64_t)PyBytes_GET_SIZE(seg);
        if (n > s->resp_payload_acked) {
            break;  /* only partially acknowledged: still exposed */
        }
        s->resp_payload_acked -= n;
        /* Drop the payload reference as soon as acknowledgement covers it. */
        Py_CLEAR(s->resp_chunks[s->resp_head]);
        s->resp_retained_bytes -= (Py_ssize_t)n;
        s->resp_retained_segments--;
        if (s->conn != NULL) {
            WreathH3Conn *c = s->conn;
            c->retained_response_bytes -= (Py_ssize_t)n;
            c->retained_response_segments--;
            c->endpoint->retained_response_bytes -= (Py_ssize_t)n;
            c->endpoint->retained_response_segments--;
        }
        s->resp_head++;
    }
    /* Compact the released prefix occasionally: the payload is already gone, so
     * this only reclaims slots, and doing it on every ack would be quadratic.
     * Acked bytes were necessarily offered first, so resp_read_index never
     * trails resp_head and the emptied slots are never read. */
    Py_ssize_t drop = s->resp_head;
    if (drop == 0) {
        return;
    }
    if (drop >= size || (drop >= 64 && drop * 2 >= size)) {
        Py_ssize_t live = size - drop;
        if (live > 0) {
            memmove(s->resp_chunks, s->resp_chunks + drop,
                    (size_t)live * sizeof(PyObject *));
        }
        s->resp_chunks_len = live;
        s->resp_read_index -= drop;
        s->resp_head = 0;
    }
}


/* Build the nghttp3 header vector and submit the response. Idempotent. */
static int
submit_response(WreathH3Stream *s, int default_status)
{
    if (s->response_ended || s->conn == NULL || s->conn->h3 == NULL) {
        return 0;
    }
    int status = s->status ? s->status : default_status;
    char status_buf[4];
    PyOS_snprintf(status_buf, sizeof(status_buf), "%d", status);

    Py_ssize_t hcount = s->resp_headers ? PySequence_Fast_GET_SIZE(s->resp_headers) : 0;
    PyObject *default_items = s->conn->endpoint->default_response_headers;
    Py_ssize_t dcount = PySequence_Fast_GET_SIZE(default_items);
    nghttp3_nv *nva = PyMem_Malloc(
        sizeof(nghttp3_nv) * (size_t)(hcount + dcount + 1)
    );
    if (nva == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    int has_date = 0;
    int has_server = 0;
    size_t n = 0;
    nva[n].name = (uint8_t *)":status";
    nva[n].namelen = 7;
    nva[n].value = (uint8_t *)status_buf;
    nva[n].valuelen = strlen(status_buf);
    nva[n].flags = NGHTTP3_NV_FLAG_NONE;
    n++;
    for (Py_ssize_t i = 0; i < hcount; i++) {
        PyObject *pair = PySequence_Fast_GET_ITEM(s->resp_headers, i);
        PyObject *name;
        PyObject *value;
        if (h3_response_header_parts(pair, &name, &value) < 0) {
            PyMem_Free(nva);
            return -1;
        }
        char *np = PyBytes_AS_STRING(name);
        char *vp = PyBytes_AS_STRING(value);
        Py_ssize_t nl = PyBytes_GET_SIZE(name);
        Py_ssize_t vl = PyBytes_GET_SIZE(value);
        if (!(nl > 0 && np[0] == ':')) {
            if (wreath_ascii_equal_ci(np, nl, "date", 4)) has_date = 1;
            if (wreath_ascii_equal_ci(np, nl, "server", 6)) has_server = 1;
            nva[n].name = (uint8_t *)np;
            nva[n].namelen = (size_t)nl;
            nva[n].value = (uint8_t *)vp;
            nva[n].valuelen = (size_t)vl;
            nva[n].flags = NGHTTP3_NV_FLAG_NONE;
            n++;
        }
    }
    for (Py_ssize_t i = 0; i < dcount; i++) {
        PyObject *pair = PySequence_Fast_GET_ITEM(default_items, i);
        PyObject *name;
        PyObject *value;
        if (h3_response_header_parts(pair, &name, &value) < 0) {
            PyMem_Free(nva);
            return -1;
        }
        char *np = PyBytes_AS_STRING(name);
        char *vp = PyBytes_AS_STRING(value);
        Py_ssize_t nl = PyBytes_GET_SIZE(name);
        Py_ssize_t vl = PyBytes_GET_SIZE(value);
        if ((has_date && wreath_ascii_equal_ci(np, nl, "date", 4)) ||
            (has_server && wreath_ascii_equal_ci(np, nl, "server", 6))) continue;
        nva[n].name = (uint8_t *)np;
        nva[n].namelen = (size_t)nl;
        nva[n].value = (uint8_t *)vp;
        nva[n].valuelen = (size_t)vl;
        nva[n].flags = NGHTTP3_NV_FLAG_NONE;
        n++;
    }
    nghttp3_data_reader dr = {read_response_data};
    int rc = nghttp3_conn_submit_response(s->conn->h3, s->stream_id, nva, n, &dr);
    PyMem_Free(nva);
    if (rc != 0) {
        PyErr_Format(PyExc_RuntimeError, "nghttp3 response submission failed: %d", rc);
        return -1;
    }
    s->response_ended = 1;
    nghttp3_conn_resume_stream(s->conn->h3, s->stream_id);
    wreath_h3_flush(s->conn);
    return 0;
}

static PyObject *
h3_stream_done(PyObject *op, PyObject *task)
{
    WreathH3Stream *s = (WreathH3Stream *)op;
    PyObject *exc = PyObject_CallMethod(task, "exception", NULL);
    uint8_t nfr_terminal = WREATH_NFR_TERM_OK;
    if (exc == NULL) {
        nfr_terminal = WREATH_NFR_TERM_CANCELLED;
        PyErr_Clear();
    } else if (exc != Py_None) {
        nfr_terminal = WREATH_NFR_TERM_ERROR;
        if (!s->response_started) {
            s->status = 500;
        }
        PyErr_WriteUnraisable(task);
    }
    Py_XDECREF(exc);
    if (s->conn != NULL && !s->response_ended) {
        /* App finished without a response ever being submitted. */
        s->resp_eof = 1;
        if (submit_response(s, s->response_started ? (s->status ? s->status : 200) : 500) < 0) {
            PyErr_WriteUnraisable(task);
        }
    } else if (s->conn != NULL && !s->resp_eof) {
        /* The response is already streaming and the app finished without a
         * final body message: end the body so the stream can complete. */
        s->resp_eof = 1;
        if (s->conn->h3 != NULL) {
            nghttp3_conn_resume_stream(s->conn->h3, s->stream_id);
        }
        wreath_h3_flush(s->conn);
    }
    /* Publish one completion cell for this stream's request. The worker lives on
     * the endpoint; the context lives on the stream. bytes_in is the request
     * payload accepted, bytes_out the response body payload framed. */
    if (s->nfr_active && s->conn != NULL &&
        s->conn->endpoint->nfr_worker != NULL) {
        s->nfr_active = 0;
        /* Route/plan attribution stamped by Python dispatch into the scope dict
         * as a (route_id, plan_id) tuple; left as None for unattributed routes. */
        if (s->scope != NULL && PyDict_Check(s->scope)) {
            PyObject *attr = PyDict_GetItemWithError(s->scope, k_wreath_flight);
            if (attr != NULL && PyTuple_CheckExact(attr) &&
                PyTuple_GET_SIZE(attr) == 2) {
                unsigned long rid = PyLong_AsUnsignedLong(PyTuple_GET_ITEM(attr, 0));
                unsigned long pid = PyLong_AsUnsignedLong(PyTuple_GET_ITEM(attr, 1));
                if (!PyErr_Occurred()) {
                    wreath_h3_flight_capi->context_route(&s->nfr_ctx, (uint32_t)rid,
                                                         (uint32_t)pid);
                } else {
                    PyErr_Clear();
                }
            }
        }
        wreath_h3_flight_capi->context_end(
            s->conn->endpoint->nfr_worker, &s->nfr_ctx, wreath_h3_timestamp(),
            (uint32_t)(s->status ? s->status : 200), nfr_terminal, 0,
            (uint64_t)s->body_received, s->nfr_bytes_out);
        if (wreath_h3_request_capi->check(s->scope)) {
            wreath_h3_request_capi->sever(s->scope);
        }
    }
    Py_CLEAR(s->task);
    Py_RETURN_NONE;
}

/* Resolve a blocked receive() with http.disconnect, exactly once. Safe when no
 * receiver is waiting or when the future was already resolved or cancelled. */
static void
h3_release_receiver(WreathH3Stream *s)
{
    if (s->receive_waiter == NULL) {
        return;
    }
    PyObject *w = s->receive_waiter;
    s->receive_waiter = NULL;
    PyObject *done = PyObject_CallMethod(w, "done", NULL);
    int is_done = done ? PyObject_IsTrue(done) : 1;
    Py_XDECREF(done);
    if (!is_done) {
        PyObject *msg = Py_BuildValue("{s:s}", "type", "http.disconnect");
        if (msg != NULL) {
            PyObject *r = PyObject_CallMethod(w, "set_result", "(O)", msg);
            Py_XDECREF(r);
            Py_DECREF(msg);
        }
    }
    if (PyErr_Occurred()) {
        PyErr_Clear();
    }
    Py_DECREF(w);
}

void
wreath_h3_stream_disconnect(WreathH3Stream *s)
{
    s->disconnected = 1;
    WreathH3Conn *c = s->conn;
    h3_cancel_send_waiter(s);
    if (c != NULL && s->resp_retained_segments > 0) {
        c->retained_response_bytes -= s->resp_retained_bytes;
        c->retained_response_segments -= s->resp_retained_segments;
        c->endpoint->retained_response_bytes -= s->resp_retained_bytes;
        c->endpoint->retained_response_segments -= s->resp_retained_segments;
        s->resp_retained_bytes = 0;
        s->resp_retained_segments = 0;
        if (h3_maybe_resume_senders(c) < 0) {
            PyErr_Clear();
        }
    }
    if (s->conn != NULL && s->body_buffer != NULL) {
        Py_ssize_t queued = PyByteArray_GET_SIZE(s->body_buffer);
        s->conn->queued_body_bytes -= queued;
        if (queued > 0 && s->conn->queued_body_messages > 0) {
            s->conn->queued_body_messages--;
        }
    }
    h3_release_receiver(s);
    /* Release the recorder's active slot while the worker is still reachable
     * (conn is about to detach). A request torn down by connection loss emits no
     * completion cell; abandon is idempotent, and clearing nfr_active makes the
     * later h3_stream_done a no-op. */
    if (s->nfr_active && s->conn != NULL &&
        s->conn->endpoint->nfr_worker != NULL) {
        wreath_h3_flight_capi->context_abandon(s->conn->endpoint->nfr_worker,
                                               &s->nfr_ctx);
        s->nfr_active = 0;
    }
    s->conn = NULL;  /* detach: the connection is going away */
}

static int
h3_stream_traverse(PyObject *op, visitproc visit, void *arg)
{
    WreathH3Stream *s = (WreathH3Stream *)op;
    Py_VISIT(s->loop);
    Py_VISIT(s->scope);
    Py_VISIT(s->task);
    Py_VISIT(s->receive_callable);
    Py_VISIT(s->send_callable);
    Py_VISIT(s->done_callable);
    Py_VISIT(s->header_list);
    Py_VISIT(s->body_buffer);
    Py_VISIT(s->receive_waiter);
    Py_VISIT(s->resp_headers);
    Py_VISIT(s->send_waiter);
    Py_VISIT(s->policy_state.client);
    Py_VISIT(s->policy_state.scheme);
    Py_VISIT(s->policy_state.origin);
    Py_VISIT(s->policy_state.request_id);
    Py_VISIT(s->policy_state.csrf_token);
    for (Py_ssize_t i = s->resp_head; i < s->resp_chunks_len; i++) {
        Py_VISIT(s->resp_chunks[i]);
    }
    return 0;
}

static int
h3_stream_clear(PyObject *op)
{
    WreathH3Stream *s = (WreathH3Stream *)op;
    Py_CLEAR(s->loop);
    Py_CLEAR(s->scope);
    Py_CLEAR(s->task);
    Py_CLEAR(s->receive_callable);
    Py_CLEAR(s->send_callable);
    Py_CLEAR(s->done_callable);
    Py_CLEAR(s->header_list);
    Py_CLEAR(s->body_buffer);
    Py_CLEAR(s->receive_waiter);
    Py_CLEAR(s->resp_headers);
    h3_cancel_send_waiter(s);
    for (Py_ssize_t i = s->resp_head; i < s->resp_chunks_len; i++) {
        Py_XDECREF(s->resp_chunks[i]);
    }
    PyMem_Free(s->resp_chunks);
    s->resp_chunks = NULL;
    s->resp_chunks_cap = s->resp_chunks_len = 0;
    s->resp_head = 0;
    s->resp_read_index = 0;
    s->resp_read_offset = 0;
    s->resp_payload_acked = 0;
    s->resp_retained_bytes = 0;
    s->resp_retained_segments = 0;
    wreath_policy_state_clear(&s->policy_state);
    return 0;
}

static void
h3_stream_dealloc(PyObject *op)
{
    PyObject_GC_UnTrack(op);
    h3_stream_clear(op);
    Py_TYPE(op)->tp_free(op);
}

static PyObject *
h3_stream_wreath_response(
    WreathH3Stream *self, PyObject *const *args, Py_ssize_t nargs
)
{
    if (nargs != 3 && nargs != 4) {
        PyErr_Format(
            PyExc_TypeError, "_wreath_response expected 3 or 4 arguments, got %zd", nargs
        );
        return NULL;
    }
    if (self->conn == NULL) {
        return self->loop ? resolved_future(self->loop, Py_None) : NULL;
    }
    PyObject *body = Py_NewRef(args[2]);
    int authenticated = nargs == 4 ? PyObject_IsTrue(args[3]) : 0;
    if (authenticated < 0 || (self->conn->endpoint->policy.response_transform &&
        wreath_policy_response(
            &self->conn->endpoint->policy, &self->policy_state,
            args[0], args[1], &body, authenticated) < 0)) {
        Py_DECREF(body);
        return NULL;
    }
    PyObject *started = h3_response_start(self, args[0], args[1]);
    if (started == NULL) {
        Py_DECREF(body);
        return NULL;
    }
    Py_DECREF(started);
    PyObject *result = h3_response_body(self, body, 0);
    Py_DECREF(body);
    return result;
}

/* HTTP/3 flow control can suspend here, so the common application entry point
 * keeps returning that awaitable.  The method exists on every native protocol
 * so dispatch does not branch on transport type. */
static PyObject *
h3_stream_wreath_response_nowait(
    WreathH3Stream *self, PyObject *const *args, Py_ssize_t nargs)
{
    return h3_stream_wreath_response(self, args, nargs);
}

static PyObject *
h3_stream_wreath_start(
    WreathH3Stream *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 2) {
        PyErr_Format(PyExc_TypeError,
                     "_wreath_stream_start expected 2 arguments, got %zd", nargs);
        return NULL;
    }
    return h3_response_start(self, args[0], args[1]);
}

static PyObject *
h3_stream_wreath_body(
    WreathH3Stream *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 2) {
        PyErr_Format(PyExc_TypeError,
                     "_wreath_stream_body expected 2 arguments, got %zd", nargs);
        return NULL;
    }
    int more = PyObject_IsTrue(args[1]);
    if (more < 0) return NULL;
    return h3_response_body(self, args[0], more);
}


static PyMethodDef h3_stream_methods[] = {
    {"_receive", h3_stream_receive, METH_NOARGS, NULL},
    {"_send", h3_stream_send, METH_O, NULL},
    {"_wreath_response", (PyCFunction)(void (*)(void))h3_stream_wreath_response,
     METH_FASTCALL, NULL},
    {"_wreath_response_nowait",
     (PyCFunction)(void (*)(void))h3_stream_wreath_response_nowait,
     METH_FASTCALL, NULL},
    {"_wreath_stream_start", (PyCFunction)(void (*)(void))h3_stream_wreath_start,
     METH_FASTCALL, NULL},
    {"_wreath_stream_body", (PyCFunction)(void (*)(void))h3_stream_wreath_body,
     METH_FASTCALL, NULL},
    {"_done", h3_stream_done, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};

static PyType_Slot h3_stream_slots[] = {
    {Py_tp_methods, h3_stream_methods},
    {Py_tp_traverse, h3_stream_traverse},
    {Py_tp_clear, h3_stream_clear},
    {Py_tp_dealloc, h3_stream_dealloc},
    {0, NULL},
};
PyType_Spec wreath_h3_stream_spec = {
    .name = "wreath._native._http3.Http3Stream",
    .basicsize = sizeof(WreathH3Stream),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = h3_stream_slots,
};

/* --- request assembly + ASGI dispatch ------------------------------------ */

static int
begin_headers_cb(nghttp3_conn *conn, int64_t stream_id, void *cu, void *su)
{
    (void)su;
    WreathH3Conn *c = (WreathH3Conn *)cu;
    WreathH3Stream *s = PyObject_GC_New(WreathH3Stream, WreathH3StreamType);
    if (s == NULL) return NGHTTP3_ERR_CALLBACK_FAILURE;
    s->conn = c;
    s->stream_id = stream_id;
    s->loop = Py_NewRef(endpoint_loop(c));
    s->scope = NULL;
    s->task = NULL;
    s->receive_callable = s->send_callable = s->done_callable = NULL;
    s->header_list = wreath_header_block_new_objects(16);
    s->body_buffer = PyByteArray_FromStringAndSize(NULL, 0);
    s->body_received = 0;
    s->body_frames = 0;
    s->receive_waiter = NULL;
    s->request_ended = s->request_refused = s->disconnected = 0;
    s->response_started = s->response_ended = 0;
    s->resp_chunks = NULL;
    s->resp_chunks_cap = s->resp_chunks_len = 0;
    s->resp_head = 0;
    s->resp_read_index = 0;
    s->resp_read_offset = 0;
    s->resp_payload_acked = 0;
    s->resp_retained_bytes = 0;
    s->resp_retained_segments = 0;
    s->send_waiter = NULL;
    s->status = 0;
    s->resp_headers = NULL;
    s->resp_eof = 0;
    s->nfr_active = 0;
    s->nfr_bytes_out = 0;
    wreath_policy_state_init(&s->policy_state);
    if (!PyObject_GC_IsTracked((PyObject *)s)) {
        PyObject_GC_Track((PyObject *)s);
    }
    if (s->header_list == NULL || s->body_buffer == NULL) {
        Py_DECREF(s);
        return NGHTTP3_ERR_CALLBACK_FAILURE;
    }
    PyObject *key = PyLong_FromLongLong(stream_id);
    if (key == NULL || PyDict_SetItem(c->streams, key, (PyObject *)s) < 0) {
        Py_XDECREF(key);
        Py_DECREF(s);
        return NGHTTP3_ERR_CALLBACK_FAILURE;
    }
    Py_DECREF(key);
    /* Hand the stream to nghttp3 while we still hold a reference: after the
     * decref below, `s` is alive only because c->streams owns it. */
    nghttp3_conn_set_stream_user_data(conn, stream_id, s);
    Py_DECREF(s);  /* the dict owns it now */
    return 0;
}

static void h3_reject_stream(WreathH3Stream *s, uint64_t app_error);

/* Refuse a request whose complete failure is known before ASGI activation.
 * Header callbacks stop materialising Python pairs as soon as the configured
 * count is reached, but nghttp3 is allowed to finish the compressed block. A
 * STOP_SENDING or RESET_STREAM here makes standards-conforming clients report
 * a QUIC error even if response bytes follow; the bounded answer is therefore
 * an ordinary empty 431 once END_HEADERS arrives. */
static int
h3_refuse_unstarted_request(WreathH3Stream *s, int status)
{
    if (s->response_started) return 0;
    WreathH3Conn *c = s->conn;
    if (c == NULL) return 0;
    if (s->body_buffer != NULL) {
        Py_ssize_t queued = PyByteArray_GET_SIZE(s->body_buffer);
        c->queued_body_bytes -= queued;
        if (queued > 0 && c->queued_body_messages > 0) {
            c->queued_body_messages--;
        }
        if (PyByteArray_Resize(s->body_buffer, 0) < 0) return -1;
    }

    PyObject *headers = PyTuple_New(0);
    if (headers == NULL) return -1;
    Py_XSETREF(s->resp_headers, headers);
    s->request_ended = 1;
    s->request_refused = 1;
    s->response_started = 1;
    s->resp_eof = 1;
    s->status = status;
    h3_release_receiver(s);
    return submit_response(s, status);
}

static int
recv_header_cb(nghttp3_conn *conn, int64_t stream_id, int32_t token,
               nghttp3_rcbuf *name, nghttp3_rcbuf *value, uint8_t flags,
               void *cu, void *su)
{
    (void)conn; (void)stream_id; (void)token; (void)flags; (void)cu;
    WreathH3Stream *s = (WreathH3Stream *)su;
    if (s == NULL || s->disconnected || s->request_refused) return 0;
    if (wreath_headers_count(s->header_list) >=
        s->conn->endpoint->max_header_count) {
        s->request_refused = 1;
        s->status = 431;
        return 0;
    }
    nghttp3_vec nv = nghttp3_rcbuf_get_buf(name);
    nghttp3_vec vv = nghttp3_rcbuf_get_buf(value);
    PyObject *pn = PyBytes_FromStringAndSize((const char *)nv.base, (Py_ssize_t)nv.len);
    PyObject *pv = PyBytes_FromStringAndSize((const char *)vv.base, (Py_ssize_t)vv.len);
    if (pn == NULL || pv == NULL) {
        Py_XDECREF(pn); Py_XDECREF(pv);
        return NGHTTP3_ERR_CALLBACK_FAILURE;
    }
    int rc = wreath_header_block_append_objects(s->header_list, pn, pv);
    Py_DECREF(pn);
    Py_DECREF(pv);
    return rc < 0 ? NGHTTP3_ERR_CALLBACK_FAILURE : 0;
}

typedef struct {
    const char *host;
    Py_ssize_t host_len;
    int port;
} H3Authority;

/* Parse the HTTP(S) authority shape used to compare :authority with Host.
 * HTTP/3 control data owns routing; allowing a disagreeing Host would let an
 * edge and application middleware enforce policy against different tenants.
 * Keep this deliberately identical to the HTTP/2 boundary. */
static int
parse_h3_authority(const char *data, Py_ssize_t len, int default_port,
                   H3Authority *out)
{
    Py_ssize_t host_len = len;
    Py_ssize_t port_at = -1;
    if (len <= 0) return -1;
    if (data[0] == '[') {
        Py_ssize_t close = 1;
        while (close < len && data[close] != ']') close++;
        if (close == len || close == 1) return -1;
        host_len = close + 1;
        if (host_len < len) {
            if (data[host_len] != ':') return -1;
            port_at = host_len + 1;
        }
    }
    else {
        for (Py_ssize_t i = 0; i < len; i++) {
            unsigned char ch = (unsigned char)data[i];
            if (ch == '@' || ch == '/' || ch == '?' || ch == '#' ||
                ch == '\\' || ch == '[' || ch == ']') {
                return -1;
            }
            if (ch == ':') {
                if (port_at >= 0) return -1;
                host_len = i;
                port_at = i + 1;
            }
        }
        if (host_len == 0) return -1;
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
h3_authorities_equal(PyObject *authority, PyObject *host, PyObject *scheme)
{
    int default_port = -1;
    if (scheme != NULL && PyBytes_GET_SIZE(scheme) == 5 &&
        PyOS_strnicmp(PyBytes_AS_STRING(scheme), "https", 5) == 0) {
        default_port = 443;
    }
    else if (scheme != NULL && PyBytes_GET_SIZE(scheme) == 4 &&
             PyOS_strnicmp(PyBytes_AS_STRING(scheme), "http", 4) == 0) {
        default_port = 80;
    }
    H3Authority left;
    H3Authority right;
    if (parse_h3_authority(PyBytes_AS_STRING(authority),
                           PyBytes_GET_SIZE(authority), default_port, &left) < 0 ||
        parse_h3_authority(PyBytes_AS_STRING(host), PyBytes_GET_SIZE(host),
                           default_port, &right) < 0) {
        return 0;
    }
    return left.host_len == right.host_len && left.port == right.port &&
           PyOS_strnicmp(left.host, right.host, left.host_len) == 0;
}

/* Build the ASGI scope from the collected header list and spawn the app task. */
static int
start_request(WreathH3Stream *s)
{
    WreathH3Conn *c = s->conn;
    PyObject *method = NULL, *path = NULL, *scheme = NULL, *authority = NULL;
    PyObject *host = NULL;
    PyObject *traceparent = NULL;  /* borrowed; captured in the one header pass */
    PyObject *scope_headers = wreath_header_block_new_objects(16);
    if (scope_headers == NULL) return -1;
    /* `_http3` and `_server` are separate extension modules, so each has its
     * own private HeaderBlock type.  The request context belongs to `_server`:
     * populate one of its blocks through the capsule instead of handing it an
     * opaque `_http3` block that it cannot scan or materialize.  This duplicates
     * only the native pointer arrays and references; it still creates no ASGI
     * header list or pair tuples before Python observes `Request.headers`. */
    PyObject *context_headers = c->endpoint->native_app != NULL
        ? wreath_h3_request_capi->header_block_new_objects(16)
        : NULL;
    if (c->endpoint->native_app != NULL && context_headers == NULL) {
        Py_DECREF(scope_headers);
        return -1;
    }
    int has_host = 0;
    Py_ssize_t n = wreath_headers_count(s->header_list);
    for (Py_ssize_t i = 0; i < n; i++) {
        const char *np;
        const char *vp;
        Py_ssize_t nl;
        Py_ssize_t vl;
        if (wreath_headers_view(s->header_list, i, &np, &nl, &vp, &vl) < 0) {
            Py_XDECREF(context_headers);
            Py_DECREF(scope_headers);
            return -1;
        }
        PyObject *value = wreath_headers_value_borrowed(s->header_list, i);
        if (value == NULL) {
            Py_XDECREF(context_headers);
            Py_DECREF(scope_headers);
            return -1;
        }
        if (nl > 0 && np[0] == ':') {
            if (nl == 7 && memcmp(np, ":method", 7) == 0) method = value;
            else if (nl == 5 && memcmp(np, ":path", 5) == 0) path = value;
            else if (nl == 7 && memcmp(np, ":scheme", 7) == 0) scheme = value;
            else if (nl == 10 && memcmp(np, ":authority", 10) == 0) authority = value;
            continue;
        }
        /* Note host presence here rather than rescanning the scope headers
         * once the loop is done; the synthesized name is a cached constant. */
        if (nl == 4 && memcmp(np, "host", 4) == 0) {
            if (host != NULL) goto message_err;
            host = value;
            has_host = 1;
        }
        /* Capture the recorder correlation header in this same pass, so a
         * traceparent never costs a second header walk. */
        else if (nl == 11 && memcmp(np, "traceparent", 11) == 0) traceparent = value;
        PyObject *name = wreath_headers_name_object(s->header_list, i);
        PyObject *owned_value = wreath_headers_value_object(s->header_list, i);
        int append_result = name != NULL && owned_value != NULL
            ? wreath_header_block_append_objects(scope_headers, name, owned_value)
            : -1;
        if (append_result == 0 && context_headers != NULL) {
            append_result = wreath_h3_request_capi->header_block_append_objects(
                context_headers, name, owned_value);
        }
        Py_XDECREF(name);
        Py_XDECREF(owned_value);
        if (append_result < 0) {
            Py_XDECREF(context_headers);
            Py_DECREF(scope_headers);
            return -1;
        }
    }
    if (authority != NULL) {
        H3Authority parsed;
        int default_port = -1;
        if (scheme != NULL && PyBytes_GET_SIZE(scheme) == 5 &&
            PyOS_strnicmp(PyBytes_AS_STRING(scheme), "https", 5) == 0) {
            default_port = 443;
        }
        else if (scheme != NULL && PyBytes_GET_SIZE(scheme) == 4 &&
                 PyOS_strnicmp(PyBytes_AS_STRING(scheme), "http", 4) == 0) {
            default_port = 80;
        }
        if (parse_h3_authority(PyBytes_AS_STRING(authority),
                               PyBytes_GET_SIZE(authority), default_port, &parsed) < 0 ||
            (host != NULL && !h3_authorities_equal(authority, host, scheme))) {
            goto message_err;
        }
        if (!has_host) {
            if (wreath_header_block_append_objects(
                    scope_headers, k_host_name, authority) < 0) {
                Py_XDECREF(context_headers);
                Py_DECREF(scope_headers);
                return -1;
            }
            if (context_headers != NULL &&
                wreath_h3_request_capi->header_block_append_objects(
                    context_headers, k_host_name, authority) < 0) {
                Py_DECREF(context_headers);
                Py_DECREF(scope_headers);
                return -1;
            }
        }
    }
    else if (host != NULL) {
        H3Authority parsed;
        if (parse_h3_authority(PyBytes_AS_STRING(host), PyBytes_GET_SIZE(host),
                               -1, &parsed) < 0) {
            goto message_err;
        }
    }
    char *pp = (char *)"/"; Py_ssize_t pl = 1;
    if (path) PyBytes_AsStringAndSize(path, &pp, &pl);
    /* The query separator, scanned by hand rather than by
     * `wreath_memmem(pp, pl, "?", 1)` and its dispatch to glibc's vectorised
     * `memchr`. Same decision as the h2 and h1 copies of this line: an h3
     * `:path` is 30-80 bytes, one instruction per byte, so a perfect vector
     * version saves single-digit nanoseconds out of a request measured in
     * microseconds. Left scalar deliberately. */
    Py_ssize_t q = -1;
    for (Py_ssize_t i = 0; i < pl; i++) { if (pp[i] == '?') { q = i; break; } }
    PyObject *path_str = PyUnicode_DecodeUTF8(pp, q >= 0 ? q : pl, "surrogateescape");
    PyObject *raw_path = PyBytes_FromStringAndSize(pp, pl);
    PyObject *query = q >= 0 ? PyBytes_FromStringAndSize(pp + q + 1, pl - q - 1)
                             : PyBytes_FromStringAndSize("", 0);
    PyObject *method_str = method
        ? PyUnicode_DecodeASCII(PyBytes_AS_STRING(method), PyBytes_GET_SIZE(method), "strict")
        : PyUnicode_FromString("GET");
    PyObject *scheme_str = scheme
        ? PyUnicode_DecodeASCII(PyBytes_AS_STRING(scheme), PyBytes_GET_SIZE(scheme), "replace")
        : PyUnicode_FromString("https");
    if (!path_str || !raw_path || !query || !method_str || !scheme_str) {
        Py_XDECREF(path_str); Py_XDECREF(raw_path); Py_XDECREF(query);
        Py_XDECREF(method_str); Py_XDECREF(scheme_str);
        Py_XDECREF(context_headers); Py_DECREF(scope_headers);
        return -1;
    }
    PyObject *scope;
    PyObject *policy_headers = NULL;
    if (c->endpoint->native_app != NULL) {
        policy_headers = Py_NewRef(scope_headers);
        scope = wreath_request_context_new(
            c->endpoint->scope_type, c->endpoint->scope_asgi,
            c->endpoint->scope_http_version, method_str, scheme_str, path_str,
            raw_path, query, context_headers, Py_None, Py_None,
            c->endpoint->scope_root_path
        );
        Py_DECREF(path_str); Py_DECREF(raw_path);
        Py_DECREF(query); Py_DECREF(method_str); Py_DECREF(scheme_str);
        Py_DECREF(context_headers);
        Py_DECREF(scope_headers);
    }
    else {
        PyObject *asgi_headers = wreath_headers_materialize(scope_headers);
        if (asgi_headers == NULL) {
            Py_DECREF(path_str); Py_DECREF(raw_path); Py_DECREF(query);
            Py_DECREF(method_str); Py_DECREF(scheme_str); Py_DECREF(scope_headers);
            return -1;
        }
        scope = Py_BuildValue(
            "{s:s,s:s,s:N,s:N,s:N,s:N,s:N,s:O}",
            "type", "http", "http_version", "3",
            "scheme", scheme_str, "method", method_str, "path", path_str,
            "raw_path", raw_path, "query_string", query, "headers", asgi_headers);
        Py_DECREF(asgi_headers);
        Py_DECREF(scope_headers);
    }
    if (scope == NULL) return -1;
    s->scope = scope;

    if (c->endpoint->policy.descriptor != NULL) {
        if (policy_headers == NULL) {
            policy_headers = PyObject_GetAttr(scope, k_headers);
        }
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
            return -1;
        }
        int policy_result = wreath_policy_ingress(
            &c->endpoint->policy, &s->policy_state, policy_method, policy_path,
            policy_scheme, policy_client, policy_headers, &reply);
        Py_DECREF(policy_method);
        Py_DECREF(policy_path);
        Py_DECREF(policy_scheme);
        Py_DECREF(policy_client);
        if (policy_result < 0) {
            Py_CLEAR(policy_headers);
            wreath_policy_reply_clear(&reply);
            return -1;
        }
        wreath_h3_request_capi->set_policy(scope, &s->policy_state);
        if (policy_result > 0) {
            uint64_t refusal_started_ns = wreath_h3_timestamp();
            PyObject *status = PyLong_FromLong(reply.status);
            PyObject *started = status != NULL
                ? h3_response_start(s, status, reply.headers) : NULL;
            Py_XDECREF(status);
            PyObject *finished = started != NULL
                ? h3_response_body(s, reply.body, 0) : NULL;
            Py_XDECREF(started);
            if (finished == NULL) {
                Py_CLEAR(policy_headers);
                wreath_policy_reply_clear(&reply);
                return -1;
            }
            wreath_policy_record_completion(
                wreath_h3_flight_capi, c->endpoint->nfr_worker,
                c->nfr_connection_id, WREATH_NFR_PROTO_HTTP3,
                refusal_started_ns, wreath_h3_timestamp(), policy_headers,
                &reply, 0, (uint64_t)PyBytes_GET_SIZE(reply.body));
            Py_DECREF(finished);
            wreath_policy_reply_clear(&reply);
            Py_CLEAR(policy_headers);
            return 0;
        }
        wreath_policy_reply_clear(&reply);
    }
    Py_CLEAR(policy_headers);

    s->receive_callable = PyObject_GetAttr((PyObject *)s, k_receive);
    s->send_callable = PyObject_GetAttr((PyObject *)s, k_send);
    s->done_callable = PyObject_GetAttr((PyObject *)s, k_done);
    if (!s->receive_callable || !s->send_callable || !s->done_callable) return -1;

    /* Begin the recorder context now the request is committed to running; it is
     * guaranteed to reach h3_stream_done (completion) or a disconnect (abandon).
     * Off / no-telemetry is a single not-taken branch. */
    if (c->endpoint->nfr_worker != NULL) {
        wreath_nfr_worker *w = c->endpoint->nfr_worker;
        wreath_h3_flight_capi->context_start(w, &s->nfr_ctx, c->nfr_connection_id,
                                             WREATH_NFR_PROTO_HTTP3,
                                             wreath_h3_timestamp());
        s->nfr_active = 1;
        /* Signal Python dispatch that a recorder is attached so it stamps
         * route/plan attribution back into the scope; read at completion in
         * h3_stream_done. Absent on the pure-ASGI path (no recorder). */
        if (wreath_h3_request_capi->check(scope)) {
            wreath_h3_request_capi->set_flight(scope, &s->nfr_ctx, w);
            wreath_h3_request_capi->set_armed(scope);
        }
        else if (wreath_h3_request_capi->seed_flight(scope, &s->nfr_ctx) < 0) {
            PyErr_Clear();
        }
        /* Correlate with the incoming W3C traceparent captured above, if any. */
        if (traceparent != NULL) {
            wreath_h3_flight_capi->context_propagate(
                w, &s->nfr_ctx, (const uint8_t *)PyBytes_AS_STRING(traceparent),
                PyBytes_GET_SIZE(traceparent));
        }
    }

    PyObject *app_args[3] = {scope, s->receive_callable, s->send_callable};
    PyObject *target = c->endpoint->native_app != NULL
        ? c->endpoint->native_app : endpoint_app(c);
    PyObject *coro = PyObject_Vectorcall(target, app_args, 3, NULL);
    if (coro == NULL) return -1;
    PyObject *task = PyObject_CallOneArg(c->endpoint->loop_create_task, coro);
    Py_DECREF(coro);
    if (task == NULL) return -1;
    s->task = task;
    PyObject *cb = PyObject_CallMethod(task, "add_done_callback", "O", s->done_callable);
    Py_XDECREF(cb);
    return cb == NULL ? -1 : 0;

message_err:
    Py_XDECREF(context_headers);
    Py_DECREF(scope_headers);
    h3_reject_stream(s, NGHTTP3_H3_MESSAGE_ERROR);
    return 1;
}

static int
end_headers_cb(nghttp3_conn *conn, int64_t stream_id, int fin, void *cu, void *su)
{
    (void)conn; (void)stream_id; (void)cu;
    WreathH3Stream *s = (WreathH3Stream *)su;
    if (s == NULL || s->disconnected) return 0;
    if (fin) s->request_ended = 1;
    if (s->request_refused) {
        return h3_refuse_unstarted_request(s, s->status) < 0
            ? NGHTTP3_ERR_CALLBACK_FAILURE : 0;
    }
    int started = start_request(s);
    if (started < 0) {
        return NGHTTP3_ERR_CALLBACK_FAILURE;
    }
    return 0;
}

/* Reject one request stream that broke a configured bound.
 *
 * Stream-local by construction: ngtcp2_conn_shutdown_stream resets only this
 * request stream (RESET_STREAM + STOP_SENDING), so other streams multiplexed on
 * the same QUIC connection keep running. H3_EXCESSIVE_LOAD is the HTTP/3
 * application error for a peer exceeding a locally configured limit. */
static void
h3_reject_stream(WreathH3Stream *s, uint64_t app_error)
{
    WreathH3Conn *c = s->conn;   /* disconnect() detaches it; keep it to flush */
    if (c != NULL) {
        if (c->conn != NULL) {
            ngtcp2_conn_shutdown_stream(c->conn, 0, s->stream_id, app_error);
        }
        if (c->h3 != NULL) {
            nghttp3_conn_shutdown_stream_read(c->h3, s->stream_id);
        }
    }
    /* Rejected bytes must not stay reachable or consume connection budget. */
    if (s->body_buffer != NULL) {
        Py_ssize_t queued = PyByteArray_GET_SIZE(s->body_buffer);
        if (c != NULL) {
            c->queued_body_bytes -= queued;
            if (queued > 0 && c->queued_body_messages > 0) c->queued_body_messages--;
        }
        if (PyByteArray_Resize(s->body_buffer, 0) < 0) PyErr_Clear();
    }
    s->request_ended = 1;
    s->disconnected = 1;
    /* Stay attached: the stream is torn down by the nghttp3 close callback. Only
     * the request is rejected, so the app must still be able to unwind. */
    h3_release_receiver(s);
    if (c != NULL) {
        wreath_h3_flush(c);
    }
}

static int
recv_data_cb(nghttp3_conn *conn, int64_t stream_id, const uint8_t *data,
             size_t datalen, void *cu, void *su)
{
    (void)conn; (void)stream_id; (void)cu;
    WreathH3Stream *s = (WreathH3Stream *)su;
    if (s == NULL || s->disconnected || s->request_refused) return 0;
    WreathH3Conn *c = s->conn;

    if (datalen > 0) {
        if (s->body_frames >= c->endpoint->max_body_chunks) {
            h3_reject_stream(s, NGHTTP3_H3_EXCESSIVE_LOAD);
            return 0;
        }
        s->body_frames++;
    }

    /* Enforce the limit before narrowing size_t and before allocating: a chunk
     * that breaks the bound must never become a Python object. The bound counts
     * payload bytes across every chunk, not the number of chunks. */
    if (datalen > (size_t)PY_SSIZE_T_MAX ||
        s->body_received > PY_SSIZE_T_MAX - (Py_ssize_t)datalen) {
        h3_reject_stream(s, NGHTTP3_H3_EXCESSIVE_LOAD);
        return 0;
    }
    Py_ssize_t total = s->body_received + (Py_ssize_t)datalen;
    Py_ssize_t limit = (c != NULL) ? c->endpoint->max_body_bytes : 0;
    if (limit > 0 && total > limit) {
        h3_reject_stream(s, NGHTTP3_H3_EXCESSIVE_LOAD);
        return 0;
    }
    s->body_received = total;

    Py_ssize_t chunk_bytes = (Py_ssize_t)datalen;
    Py_ssize_t queued_body_bytes = c != NULL ? c->queued_body_bytes : 0;
    Py_ssize_t read_high_water = c != NULL ? c->endpoint->read_high_water : PY_SSIZE_T_MAX;
    int was_empty = PyByteArray_GET_SIZE(s->body_buffer) == 0;
    if (chunk_bytes > read_high_water - queued_body_bytes ||
        (c != NULL && was_empty && chunk_bytes > 0 &&
         c->queued_body_messages >= c->endpoint->read_high_water_messages)) {
        h3_reject_stream(s, NGHTTP3_H3_EXCESSIVE_LOAD);
        return 0;
    }

    Py_ssize_t old_size = PyByteArray_GET_SIZE(s->body_buffer);
    /* native-lint: allow NC004 -- connection read_high_water is a hard cap */
    if (PyByteArray_Resize(s->body_buffer, old_size + chunk_bytes) < 0) {
        return NGHTTP3_ERR_CALLBACK_FAILURE;
    }
    if (chunk_bytes > 0) {
        memcpy(PyByteArray_AS_STRING(s->body_buffer) + old_size,
               data, (size_t)chunk_bytes);
        if (c != NULL) {
            c->queued_body_bytes += chunk_bytes;
            if (was_empty) c->queued_body_messages++;
        }
    }

    if (s->receive_waiter != NULL) {
        PyObject *body = h3_take_buffered_body(s);
        if (body == NULL) return NGHTTP3_ERR_CALLBACK_FAILURE;
        PyObject *msg = s->conn != NULL && s->conn->endpoint->native_app != NULL
            ? PyTuple_Pack(3, body, Py_True, Py_False)
            : Py_BuildValue("{s:s,s:O,s:O}", "type", "http.request",
                            "body", body, "more_body", Py_True);
        Py_DECREF(body);
        if (msg == NULL) return NGHTTP3_ERR_CALLBACK_FAILURE;
        PyObject *w = s->receive_waiter;
        s->receive_waiter = NULL;
        PyObject *r = PyObject_CallMethod(w, "set_result", "(O)", msg);
        Py_XDECREF(r);
        Py_DECREF(w);
        Py_DECREF(msg);
    }
    return 0;
}

static int
end_stream_cb(nghttp3_conn *conn, int64_t stream_id, void *cu, void *su)
{
    (void)conn; (void)stream_id; (void)cu;
    WreathH3Stream *s = (WreathH3Stream *)su;
    if (s == NULL) return 0;
    s->request_ended = 1;
    if (s->receive_waiter != NULL) {
        PyObject *msg = s->conn != NULL && s->conn->endpoint->native_app != NULL
            ? PyTuple_Pack(3, Py_None, Py_False, Py_False)
            : Py_BuildValue("{s:s,s:y#,s:O}", "type", "http.request",
                            "body", "", 0, "more_body", Py_False);
        if (msg != NULL) {
            PyObject *w = s->receive_waiter;
            s->receive_waiter = NULL;
            PyObject *r = PyObject_CallMethod(w, "set_result", "(O)", msg);
            Py_XDECREF(r);
            Py_DECREF(w);
            Py_DECREF(msg);
        }
    }
    return 0;
}

static int
h3_stream_close_cb(nghttp3_conn *conn, int64_t stream_id, uint64_t err,
                   void *cu, void *su)
{
    (void)conn; (void)err; (void)su;
    WreathH3Conn *c = (WreathH3Conn *)cu;
    PyObject *key = PyLong_FromLongLong(stream_id);
    if (key == NULL) return NGHTTP3_ERR_CALLBACK_FAILURE;
    WreathH3Stream *s = (WreathH3Stream *)PyDict_GetItemWithError(c->streams, key);
    if (s == NULL) {
        Py_DECREF(key);
        return PyErr_Occurred() ? NGHTTP3_ERR_CALLBACK_FAILURE : 0;
    }
    /* disconnect can run Python callbacks that touch c->streams, so hold a
     * strong reference across it rather than trusting the borrowed one. */
    Py_INCREF(s);
    wreath_h3_stream_disconnect(s);
    Py_DECREF(s);
    /* Pop tolerates a callback having already dropped the entry; DelItem would
     * raise KeyError for that. */
    int removed = PyDict_Pop(c->streams, key, NULL);
    Py_DECREF(key);
    return removed < 0 ? NGHTTP3_ERR_CALLBACK_FAILURE : 0;
}

static int
deferred_consume_cb(nghttp3_conn *conn, int64_t stream_id, size_t consumed,
                    void *cu, void *su)
{
    (void)conn; (void)su;
    WreathH3Conn *c = (WreathH3Conn *)cu;
    if (c->conn != NULL) {
        ngtcp2_conn_extend_max_stream_offset(c->conn, stream_id, consumed);
        ngtcp2_conn_extend_max_offset(c->conn, consumed);
    }
    return 0;
}

/* nghttp3 reports how many application DATA payload bytes the peer has
 * acknowledged on this stream; that is what makes retained segments safe to
 * release. See the contract note at wreath_h3_acked_stream_data. */
static int
acked_stream_data_cb(nghttp3_conn *conn, int64_t stream_id, uint64_t datalen,
                     void *cu, void *su)
{
    (void)conn; (void)stream_id; (void)cu;
    WreathH3Stream *s = (WreathH3Stream *)su;
    if (s == NULL) {
        return 0;
    }
    h3_release_acked(s, datalen);
    if (s->conn != NULL && h3_maybe_resume_senders(s->conn) < 0) {
        return NGHTTP3_ERR_CALLBACK_FAILURE;
    }
    return 0;
}

static nghttp3_callbacks h3_callbacks = {
    .acked_stream_data = acked_stream_data_cb,
    .stream_close = h3_stream_close_cb,
    .recv_data = recv_data_cb,
    .deferred_consume = deferred_consume_cb,
    .begin_headers = begin_headers_cb,
    .recv_header = recv_header_cb,
    .end_headers = end_headers_cb,
    .end_stream = end_stream_cb,
};

/* --- exported hooks used by http3_connection.c --------------------------- */

int
wreath_h3_setup_httpconn(WreathH3Conn *c)
{
    WreathH3Endpoint *ep = c->endpoint;
    nghttp3_settings settings;
    nghttp3_settings_default(&settings);
    settings.qpack_max_dtable_capacity = (uint64_t)ep->qpack_table_bytes;
    settings.qpack_blocked_streams = (uint64_t)ep->qpack_blocked_streams;
    if (ep->max_header_list_bytes > 0) {
        settings.max_field_section_size = (uint64_t)ep->max_header_list_bytes;
    }
    if (nghttp3_conn_server_new(&c->h3, &h3_callbacks, &settings, NULL, c) != 0) {
        return -1;
    }
    int64_t ctrl_id, enc_id, dec_id;
    if (ngtcp2_conn_open_uni_stream(c->conn, &ctrl_id, NULL) != 0 ||
        ngtcp2_conn_open_uni_stream(c->conn, &enc_id, NULL) != 0 ||
        ngtcp2_conn_open_uni_stream(c->conn, &dec_id, NULL) != 0) {
        return -1;
    }
    if (nghttp3_conn_bind_control_stream(c->h3, ctrl_id) != 0 ||
        nghttp3_conn_bind_qpack_streams(c->h3, enc_id, dec_id) != 0) {
        return -1;
    }
    return 0;
}

nghttp3_ssize
wreath_h3_writev(WreathH3Conn *c, int64_t *stream_id, int *fin, nghttp3_vec *vec,
              size_t veccnt)
{
    return nghttp3_conn_writev_stream(c->h3, stream_id, fin, vec, veccnt);
}

int
wreath_h3_recv_stream_data(WreathH3Conn *c, int64_t stream_id, int fin,
                        const uint8_t *data, size_t datalen)
{
    if (c->h3 == NULL) {
        return 0;
    }
    nghttp3_ssize n = nghttp3_conn_read_stream(c->h3, stream_id, data, datalen, fin);
    if (n < 0) {
        return -1;
    }
    return (int)n;
}

int
wreath_h3_stream_close(WreathH3Conn *c, int64_t stream_id, uint64_t app_error)
{
    if (c->h3 != NULL) {
        nghttp3_conn_close_stream(c->h3, stream_id, app_error);
    }
    return 0;
}

/* Acknowledgement contract between ngtcp2, nghttp3, and this file.
 *
 * ngtcp2's acked_stream_data_offset callback reports acknowledged *QUIC stream*
 * bytes. Those are not application payload bytes: an HTTP/3 stream also carries
 * frame headers, and payload is not at a fixed offset within it. Deriving
 * releasable payload ranges from this number directly would be invented
 * arithmetic.
 *
 * Instead the raw count is handed to nghttp3_conn_add_ack_offset, which owns
 * the HTTP/3 framing and therefore the mapping. nghttp3 then invokes the
 * acked_stream_data callback (see acked_stream_data_cb) with the number of
 * bytes "supplied from application" that the peer acknowledged — application
 * DATA payload only. That callback, and only that callback, is what releases
 * retained response segments. */
int
wreath_h3_acked_stream_data(WreathH3Conn *c, int64_t stream_id, uint64_t datalen)
{
    if (c->h3 != NULL) {
        nghttp3_conn_add_ack_offset(c->h3, stream_id, datalen);
    }
    return 0;
}
