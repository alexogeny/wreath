/* HTTP/3 ASGI bridge: nghttp3 framing/QPACK callbacks + per-stream ASGI.
 *
 * Built only when WREATH_BUILD_HTTP3=1 (ADR 0011). Maps QUIC request streams to
 * ASGI http scopes (http_version == "3") and turns ASGI responses back into
 * nghttp3 responses. The response body is buffered and submitted once complete
 * so ngtcp2's retransmission vecs never point into a reallocated buffer.
 */
#include "http3.h"

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

/* --- small future helpers ------------------------------------------------ */
static PyObject *
resolved_future(PyObject *loop, PyObject *value)
{
    PyObject *fut = PyObject_CallMethod(loop, "create_future", NULL);
    if (fut == NULL) {
        return NULL;
    }
    PyObject *r = PyObject_CallMethod(fut, "set_result", "O", value);
    if (r == NULL) {
        Py_DECREF(fut);
        return NULL;
    }
    Py_DECREF(r);
    return fut;
}

static PyObject *endpoint_loop(WreathH3Conn *c) { return c->endpoint->loop; }
static PyObject *endpoint_app(WreathH3Conn *c) { return c->endpoint->app; }

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
static PyObject *k_host_name = NULL;  /* b"host", synthesized once per absence */

int
wreath_h3_init_message_keys(void)
{
    if (k_type != NULL) {
        return 0;
    }
    if ((k_type = PyUnicode_InternFromString("type")) == NULL ||
        (k_status = PyUnicode_InternFromString("status")) == NULL ||
        (k_headers = PyUnicode_InternFromString("headers")) == NULL ||
        (k_body = PyUnicode_InternFromString("body")) == NULL ||
        (k_more_body = PyUnicode_InternFromString("more_body")) == NULL ||
        (k_host_name = PyBytes_FromString("host")) == NULL) {
        return -1;
    }
    return 0;
}

/* --- Http3Stream: ASGI plumbing ------------------------------------------ */

static int submit_response(WreathH3Stream *s, int default_status);

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
    if (s->conn == NULL || s->disconnected) {
        PyObject *msg = Py_BuildValue("{s:s}", "type", "http.disconnect");
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
        PyObject *msg = Py_BuildValue("{s:s,s:O,s:O}", "type", "http.request",
                                      "body", body, "more_body",
                                      more ? Py_True : Py_False);
        Py_DECREF(body);
        if (msg == NULL) return NULL;
        PyObject *f = resolved_future(loop, msg);
        Py_DECREF(msg);
        return f;
    }
    if (s->request_ended) {
        PyObject *msg = Py_BuildValue("{s:s,s:y#,s:O}", "type", "http.request",
                                      "body", "", 0, "more_body", Py_False);
        if (msg == NULL) return NULL;
        PyObject *f = resolved_future(loop, msg);
        Py_DECREF(msg);
        return f;
    }
    PyObject *fut = PyObject_CallMethod(loop, "create_future", NULL);
    if (fut == NULL) return NULL;
    Py_XSETREF(s->receive_waiter, Py_NewRef(fut));
    return fut;
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
    PyObject *loop = endpoint_loop(s->conn);
    PyObject *type = PyDict_GetItem(message, k_type);
    if (type == NULL) {
        PyErr_SetString(PyExc_KeyError, "message has no 'type'");
        return NULL;
    }
    const char *t = PyUnicode_AsUTF8(type);
    if (t == NULL) return NULL;

    if (strcmp(t, "http.response.start") == 0) {
        PyObject *status_obj = PyDict_GetItem(message, k_status);
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
        PyObject *headers = PyDict_GetItem(message, k_headers);
        PyObject *header_items = headers
            ? PySequence_Fast(headers, "response headers must be a sequence")
            : PyTuple_New(0);
        if (header_items == NULL) return NULL;
        s->status = (int)status;
        Py_XSETREF(s->resp_headers, header_items);
        s->response_started = 1;
        /* Submit headers and the data reader now, not after the final body
         * message: bytes must reach the wire while the app is still streaming. */
        if (submit_response(s, 200) < 0) return NULL;
        return resolved_future(loop, Py_None);
    }
    if (strcmp(t, "http.response.body") == 0) {
        if (!s->response_started) {
            PyErr_SetString(PyExc_RuntimeError, "response not started");
            return NULL;
        }
        PyObject *body = PyDict_GetItem(message, k_body);
        PyObject *more = PyDict_GetItem(message, k_more_body);
        int more_body = (more != NULL && PyObject_IsTrue(more));
        if (body != NULL && body != Py_None) {
            if (!PyBytes_Check(body)) {
                PyErr_SetString(PyExc_TypeError,
                                "http.response.body 'body' must be bytes");
                return NULL;
            }
            /* Retain the app's exact bytes; never concatenate into a bytearray
             * whose reallocation would move addresses nghttp3 still holds. */
            if (PyBytes_GET_SIZE(body) > 0) {
                if (s->resp_chunks_len == s->resp_chunks_cap) {
                    Py_ssize_t cap = s->resp_chunks_cap ? s->resp_chunks_cap * 2 : 16;
                    PyObject **grown = PyMem_Realloc(
                        s->resp_chunks, (size_t)cap * sizeof(PyObject *));
                    if (grown == NULL) return PyErr_NoMemory();
                    s->resp_chunks = grown;
                    s->resp_chunks_cap = cap;
                }
                s->resp_chunks[s->resp_chunks_len++] = Py_NewRef(body);
            }
            if (s->nfr_active) {
                s->nfr_bytes_out += (uint64_t)PyBytes_GET_SIZE(body);
            }
        }
        if (!more_body) {
            s->resp_eof = 1;
        }
        if (s->conn != NULL && s->conn->h3 != NULL) {
            /* New data (or EOF) is available for the data reader. */
            nghttp3_conn_resume_stream(s->conn->h3, s->stream_id);
            wreath_h3_flush(s->conn);
        }
        return resolved_future(loop, Py_None);
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

static int
h3_response_header_is(const char *name, Py_ssize_t size, const char *expected,
                      Py_ssize_t expected_size)
{
    if (size != expected_size) return 0;
    for (Py_ssize_t i = 0; i < size; i++) {
        unsigned char c = (unsigned char)name[i];
        if (c >= 'A' && c <= 'Z') c = (unsigned char)(c + ('a' - 'A'));
        if (c != (unsigned char)expected[i]) return 0;
    }
    return 1;
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
    PyObject *defaults = PyObject_GetAttrString(
        s->conn->endpoint->config, "_default_response_headers"
    );
    PyObject *default_headers = defaults ? PyObject_GetAttrString(defaults, "headers") : NULL;
    Py_XDECREF(defaults);
    if (default_headers == NULL) {
        return -1;
    }
    PyObject *default_items = PySequence_Fast(
        default_headers, "default response headers must be a sequence"
    );
    Py_DECREF(default_headers);
    if (default_items == NULL) {
        return -1;
    }
    Py_ssize_t dcount = PySequence_Fast_GET_SIZE(default_items);
    nghttp3_nv *nva = PyMem_Malloc(
        sizeof(nghttp3_nv) * (size_t)(hcount + dcount + 1)
    );
    if (nva == NULL) {
        Py_DECREF(default_items);
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
        PyObject *name = PySequence_GetItem(pair, 0);
        PyObject *value = PySequence_GetItem(pair, 1);
        if (name == NULL || value == NULL) {
            Py_XDECREF(name); Py_XDECREF(value); PyErr_Clear(); continue;
        }
        char *np, *vp; Py_ssize_t nl, vl;
        if (PyBytes_AsStringAndSize(name, &np, &nl) == 0 &&
            PyBytes_AsStringAndSize(value, &vp, &vl) == 0 &&
            !(nl > 0 && np[0] == ':')) {
            if (h3_response_header_is(np, nl, "date", 4)) has_date = 1;
            if (h3_response_header_is(np, nl, "server", 6)) has_server = 1;
            nva[n].name = (uint8_t *)np;
            nva[n].namelen = (size_t)nl;
            nva[n].value = (uint8_t *)vp;
            nva[n].valuelen = (size_t)vl;
            nva[n].flags = NGHTTP3_NV_FLAG_NONE;
            n++;
        }
        Py_DECREF(name);
        Py_DECREF(value);
    }
    for (Py_ssize_t i = 0; i < dcount; i++) {
        PyObject *pair = PySequence_Fast_GET_ITEM(default_items, i);
        PyObject *name = PyTuple_GET_ITEM(pair, 0);
        PyObject *value = PyTuple_GET_ITEM(pair, 1);
        char *np, *vp;
        Py_ssize_t nl, vl;
        if (PyBytes_AsStringAndSize(name, &np, &nl) < 0 ||
            PyBytes_AsStringAndSize(value, &vp, &vl) < 0) {
            PyErr_Clear();
            continue;
        }
        if ((has_date && h3_response_header_is(np, nl, "date", 4)) ||
            (has_server && h3_response_header_is(np, nl, "server", 6))) continue;
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
    Py_DECREF(default_items);
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
            PyObject *attr = PyDict_GetItemString(s->scope, "_wreath_flight");
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
            PyObject *r = PyObject_CallMethod(w, "set_result", "O", msg);
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
    PyObject *start;
    PyObject *body;
    PyObject *result;
    if (nargs != 3) {
        PyErr_Format(
            PyExc_TypeError, "_wreath_response expected 3 arguments, got %zd", nargs
        );
        return NULL;
    }
    start = Py_BuildValue(
        "{s:s,s:O,s:O}", "type", "http.response.start",
        "status", args[0], "headers", args[1]
    );
    if (start == NULL) return NULL;
    result = h3_stream_send((PyObject *)self, start);
    Py_DECREF(start);
    if (result == NULL) return NULL;
    Py_DECREF(result);
    body = Py_BuildValue(
        "{s:s,s:O}", "type", "http.response.body", "body", args[2]
    );
    if (body == NULL) return NULL;
    result = h3_stream_send((PyObject *)self, body);
    Py_DECREF(body);
    return result;
}


static PyMethodDef h3_stream_methods[] = {
    {"_receive", h3_stream_receive, METH_NOARGS, NULL},
    {"_send", h3_stream_send, METH_O, NULL},
    {"_wreath_response", (PyCFunction)(void (*)(void))h3_stream_wreath_response,
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
    s->header_list = PyList_New(0);
    s->body_buffer = PyByteArray_FromStringAndSize(NULL, 0);
    s->body_received = 0;
    s->receive_waiter = NULL;
    s->request_ended = s->disconnected = 0;
    s->response_started = s->response_ended = 0;
    s->resp_chunks = NULL;
    s->resp_chunks_cap = s->resp_chunks_len = 0;
    s->resp_head = 0;
    s->resp_read_index = 0;
    s->resp_read_offset = 0;
    s->resp_payload_acked = 0;
    s->status = 0;
    s->resp_headers = NULL;
    s->resp_eof = 0;
    s->nfr_active = 0;
    s->nfr_bytes_out = 0;
    PyObject_GC_Track((PyObject *)s);
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

static int
recv_header_cb(nghttp3_conn *conn, int64_t stream_id, int32_t token,
               nghttp3_rcbuf *name, nghttp3_rcbuf *value, uint8_t flags,
               void *cu, void *su)
{
    (void)conn; (void)stream_id; (void)token; (void)flags; (void)cu;
    WreathH3Stream *s = (WreathH3Stream *)su;
    if (s == NULL) return 0;
    nghttp3_vec nv = nghttp3_rcbuf_get_buf(name);
    nghttp3_vec vv = nghttp3_rcbuf_get_buf(value);
    PyObject *pn = PyBytes_FromStringAndSize((const char *)nv.base, (Py_ssize_t)nv.len);
    PyObject *pv = PyBytes_FromStringAndSize((const char *)vv.base, (Py_ssize_t)vv.len);
    if (pn == NULL || pv == NULL) {
        Py_XDECREF(pn); Py_XDECREF(pv);
        return NGHTTP3_ERR_CALLBACK_FAILURE;
    }
    PyObject *pair = PyTuple_Pack(2, pn, pv);
    Py_DECREF(pn); Py_DECREF(pv);
    if (pair == NULL) return NGHTTP3_ERR_CALLBACK_FAILURE;
    int rc = PyList_Append(s->header_list, pair);
    Py_DECREF(pair);
    return rc < 0 ? NGHTTP3_ERR_CALLBACK_FAILURE : 0;
}

/* Build the ASGI scope from the collected header list and spawn the app task. */
static int
start_request(WreathH3Stream *s)
{
    WreathH3Conn *c = s->conn;
    PyObject *method = NULL, *path = NULL, *scheme = NULL, *authority = NULL;
    PyObject *traceparent = NULL;  /* borrowed; captured in the one header pass */
    PyObject *scope_headers = PyList_New(0);
    if (scope_headers == NULL) return -1;
    int has_host = 0;
    Py_ssize_t n = PyList_GET_SIZE(s->header_list);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *pair = PyList_GET_ITEM(s->header_list, i);
        PyObject *name = PyTuple_GET_ITEM(pair, 0);
        PyObject *value = PyTuple_GET_ITEM(pair, 1);
        char *np; Py_ssize_t nl;
        PyBytes_AsStringAndSize(name, &np, &nl);
        if (nl > 0 && np[0] == ':') {
            if (nl == 7 && memcmp(np, ":method", 7) == 0) method = value;
            else if (nl == 5 && memcmp(np, ":path", 5) == 0) path = value;
            else if (nl == 7 && memcmp(np, ":scheme", 7) == 0) scheme = value;
            else if (nl == 10 && memcmp(np, ":authority", 10) == 0) authority = value;
            continue;
        }
        /* Note host presence here rather than rescanning the scope headers
         * once the loop is done; the synthesized name is a cached constant. */
        if (nl == 4 && memcmp(np, "host", 4) == 0) has_host = 1;
        /* Capture the recorder correlation header in this same pass, so a
         * traceparent never costs a second header walk. */
        else if (nl == 11 && memcmp(np, "traceparent", 11) == 0) traceparent = value;
        if (PyList_Append(scope_headers, pair) < 0) { Py_DECREF(scope_headers); return -1; }
    }
    if (authority != NULL) {
        if (!has_host) {
            PyObject *hp = PyTuple_Pack(2, k_host_name, authority);
            if (hp == NULL) { Py_DECREF(scope_headers); return -1; }
            int inserted = PyList_Insert(scope_headers, 0, hp);
            Py_DECREF(hp);
            if (inserted < 0) { Py_DECREF(scope_headers); return -1; }
        }
    }
    char *pp = (char *)"/"; Py_ssize_t pl = 1;
    if (path) PyBytes_AsStringAndSize(path, &pp, &pl);
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
        Py_XDECREF(method_str); Py_XDECREF(scheme_str); Py_DECREF(scope_headers);
        return -1;
    }
    PyObject *scope;
    if (c->endpoint->native_app != NULL) {
        PyObject *scope_type = PyUnicode_FromString("http");
        PyObject *asgi = Py_BuildValue("{s:s,s:s}", "version", "3.0", "spec_version", "2.5");
        PyObject *http_version = PyUnicode_FromString("3");
        PyObject *root_path = PyUnicode_FromString("");
        if (scope_type == NULL || asgi == NULL || http_version == NULL || root_path == NULL) {
            Py_XDECREF(scope_type); Py_XDECREF(asgi); Py_XDECREF(http_version);
            Py_XDECREF(root_path); Py_DECREF(path_str); Py_DECREF(raw_path);
            Py_DECREF(query); Py_DECREF(method_str); Py_DECREF(scheme_str);
            Py_DECREF(scope_headers);
            return -1;
        }
        scope = wreath_request_context_new(
            scope_type, asgi, http_version, method_str, scheme_str, path_str,
            raw_path, query, scope_headers, Py_None, Py_None, root_path
        );
        Py_DECREF(scope_type); Py_DECREF(asgi); Py_DECREF(http_version);
        Py_DECREF(root_path); Py_DECREF(path_str); Py_DECREF(raw_path);
        Py_DECREF(query); Py_DECREF(method_str); Py_DECREF(scheme_str);
        Py_DECREF(scope_headers);
    }
    else {
        scope = Py_BuildValue(
            "{s:s,s:s,s:N,s:N,s:N,s:N,s:N,s:O}",
            "type", "http", "http_version", "3",
            "scheme", scheme_str, "method", method_str, "path", path_str,
            "raw_path", raw_path, "query_string", query, "headers", scope_headers);
        Py_DECREF(scope_headers);
    }
    if (scope == NULL) return -1;
    s->scope = scope;

    s->receive_callable = PyObject_GetAttrString((PyObject *)s, "_receive");
    s->send_callable = PyObject_GetAttrString((PyObject *)s, "_send");
    s->done_callable = PyObject_GetAttrString((PyObject *)s, "_done");
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
        else if (PyDict_SetItemString(scope, "_wreath_flight", Py_None) < 0) {
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
    PyObject *create_task = PyObject_GetAttrString(endpoint_loop(c), "create_task");
    if (create_task == NULL) { Py_DECREF(coro); return -1; }
    PyObject *task = PyObject_CallOneArg(create_task, coro);
    Py_DECREF(create_task);
    Py_DECREF(coro);
    if (task == NULL) return -1;
    s->task = task;
    PyObject *cb = PyObject_CallMethod(task, "add_done_callback", "O", s->done_callable);
    Py_XDECREF(cb);
    return cb == NULL ? -1 : 0;
}

static int
end_headers_cb(nghttp3_conn *conn, int64_t stream_id, int fin, void *cu, void *su)
{
    (void)conn; (void)stream_id; (void)cu;
    WreathH3Stream *s = (WreathH3Stream *)su;
    if (s == NULL) return 0;
    if (fin) s->request_ended = 1;
    if (start_request(s) < 0) {
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
    if (s == NULL || s->disconnected) return 0;
    WreathH3Conn *c = s->conn;

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
        PyObject *msg = Py_BuildValue("{s:s,s:O,s:O}", "type", "http.request",
                                      "body", body, "more_body", Py_True);
        Py_DECREF(body);
        if (msg == NULL) return NGHTTP3_ERR_CALLBACK_FAILURE;
        PyObject *w = s->receive_waiter;
        s->receive_waiter = NULL;
        PyObject *r = PyObject_CallMethod(w, "set_result", "O", msg);
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
        PyObject *msg = Py_BuildValue("{s:s,s:y#,s:O}", "type", "http.request",
                                      "body", "", 0, "more_body", Py_False);
        if (msg != NULL) {
            PyObject *w = s->receive_waiter;
            s->receive_waiter = NULL;
            PyObject *r = PyObject_CallMethod(w, "set_result", "O", msg);
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
