#include "server.h"

/* --- HTTP/1.1 protocol internal forward declarations -------------------- */
static int run_drive(WreathHttpProtocol *self);
static int reset_request(WreathHttpProtocol *self);
static int cancel_deadline_timer(WreathHttpProtocol *self);
static int finalize_app_task(WreathHttpProtocol *self, PyObject *task, int drive_buffered);
static int apply_app_outcome(WreathHttpProtocol *self, PyObject *exc, int drive_buffered);
static int ws_write_frame(WreathHttpProtocol *self, int opcode, const char *payload,
                          Py_ssize_t size, PyObject *payload_obj);
static int ws_send_close_frame(WreathHttpProtocol *self, int code, const char *reason,
                               Py_ssize_t reason_size);
static PyObject *make_ws_disconnect_msg(int code);
static int drive_fixed_body(WreathHttpProtocol *self);
static int drive_chunk_size(WreathHttpProtocol *self);
static int drive_chunk_data(WreathHttpProtocol *self);
static int drive_chunk_trailers(WreathHttpProtocol *self);




/* --- transport / loop calls ---------------------------------------------- */

static int
transport_method0(WreathHttpProtocol *self, const char *name)
{
    PyObject *result;
    if (self->transport == NULL) {
        return 0;
    }
    result = PyObject_CallMethod(self->transport, name, NULL);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    return 0;
}


static int
transport_write(WreathHttpProtocol *self, PyObject *data)
{
    PyObject *result;
    if (self->transport == NULL || self->closing) {
        return 0;
    }
    if (self->transport_write_fn == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "transport write callable is unavailable");
        return -1;
    }
    if (self->nfr_worker != NULL && PyBytes_Check(data)) {
        self->nfr_bytes_out += (uint64_t)PyBytes_GET_SIZE(data);
    }
    result = PyObject_CallOneArg(self->transport_write_fn, data);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    return 0;
}


static int
transport_write_raw(WreathHttpProtocol *self, const char *data, Py_ssize_t size)
{
    PyObject *bytes;
    int result;
    if (self->transport == NULL || self->closing) {
        return 0;
    }
    bytes = PyBytes_FromStringAndSize(data, size);
    if (bytes == NULL) {
        return -1;
    }
    result = transport_write(self, bytes);
    Py_DECREF(bytes);
    return result;
}


static PyObject *
make_future(WreathHttpProtocol *self)
{
    return PyObject_CallNoArgs(self->loop_create_future);
}


static int
future_set_result(PyObject *future, PyObject *result)
{
    PyObject *ignored = PyObject_CallMethod(future, "set_result", "O", result);
    if (ignored == NULL) {
        return -1;
    }
    Py_DECREF(ignored);
    return 0;
}


/* --- buffer management --------------------------------------------------- */

static int
buf_reserve(WreathHttpProtocol *self, Py_ssize_t extra)
{
    Py_ssize_t need;
    Py_ssize_t new_cap;
    char *grown;
    if (extra < 0 || self->buf_len > PY_SSIZE_T_MAX - extra) {
        PyErr_SetString(PyExc_OverflowError, "request buffer overflow");
        return -1;
    }
    need = self->buf_len + extra;
    if (need <= self->buf_cap) {
        return 0;
    }
    if (self->read_exports > 0) {
        /* Realloc could move memory out from under a live Py_buffer export. */
        PyErr_SetString(PyExc_RuntimeError,
                        "cannot grow the request buffer while it is exported");
        return -1;
    }
    new_cap = self->buf_cap ? self->buf_cap : 4096;
    while (new_cap < need) {
        if (new_cap > PY_SSIZE_T_MAX / 2) {
            new_cap = need;
            break;
        }
        new_cap *= 2;
    }
    grown = PyMem_Realloc(self->buf, (size_t)new_cap);
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->buf = grown;
    self->buf_cap = new_cap;
    return 0;
}


static void
do_consume(WreathHttpProtocol *self, Py_ssize_t count)
{
    /* Every delimiter scan cursor is an offset from `cursor`, so consuming any
     * bytes invalidates all of them. A scan that is still searching never
     * consumes, so nothing in progress is lost by resetting here. */
    self->head_terminator_scan = 0;
    self->request_line_scan = 0;
    self->chunk_line_scan = 0;
    self->trailer_terminator_scan = 0;
    self->cursor += count;
    if (self->read_offer_size > 0 || self->read_exports > 0) {
        /* A live buffered-read export pins both the allocation and buf_len:
         * any reset or memmove here would desynchronize the offered region or
         * invalidate the exported address. Defer to the next get_buffer(). */
        self->compact_pending = 1;
        return;
    }
    if (self->cursor == self->buf_len) {
        /* Fully drained: reset in place so keep-alive traffic never grows
         * the buffer or pays a compaction memmove. */
        self->cursor = 0;
        self->buf_len = 0;
        return;
    }
    /* Compact only once the consumed prefix is material, to avoid repeated
     * front shifts on small reads. */
    if (self->cursor > 65536 && self->cursor * 2 >= self->buf_len) {
        Py_ssize_t remaining = self->buf_len - self->cursor;
        if (remaining > 0) {
            memmove(self->buf, self->buf + self->cursor, (size_t)remaining);
        }
        self->buf_len = remaining;
        self->cursor = 0;
    }
}


/* Apply a reset/compaction that do_consume() deferred while a buffered read
 * was exported. Callers must ensure read_exports == 0 and no active offer. */
static void
apply_deferred_compaction(WreathHttpProtocol *self)
{
    if (!self->compact_pending) {
        return;
    }
    self->compact_pending = 0;
    if (self->cursor == self->buf_len) {
        self->cursor = 0;
        self->buf_len = 0;
        return;
    }
    if (self->cursor > 65536 && self->cursor * 2 >= self->buf_len) {
        Py_ssize_t remaining = self->buf_len - self->cursor;
        memmove(self->buf, self->buf + self->cursor, (size_t)remaining);
        self->buf_len = remaining;
        self->cursor = 0;
    }
}


/* --- response scratch buffer ---------------------------------------------- */

static int
out_reserve(WreathHttpProtocol *self, Py_ssize_t extra)
{
    Py_ssize_t need;
    Py_ssize_t new_cap;
    char *grown;
    if (extra < 0 || self->out_len > PY_SSIZE_T_MAX - extra) {
        PyErr_SetString(PyExc_OverflowError, "HTTP response is too large");
        return -1;
    }
    need = self->out_len + extra;
    if (need <= self->out_cap) {
        return 0;
    }
    new_cap = self->out_cap ? self->out_cap : 1024;
    while (new_cap < need) {
        if (new_cap > PY_SSIZE_T_MAX / 2) {
            new_cap = need;
            break;
        }
        new_cap *= 2;
    }
    grown = PyMem_Realloc(self->out_buf, (size_t)new_cap);
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    self->out_buf = grown;
    self->out_cap = new_cap;
    return 0;
}


static int
out_append(WreathHttpProtocol *self, const char *data, Py_ssize_t size)
{
    if (out_reserve(self, size) < 0) {
        return -1;
    }
    memcpy(self->out_buf + self->out_len, data, (size_t)size);
    self->out_len += size;
    return 0;
}


static void
clear_response_builder(WreathHttpProtocol *self)
{
    Py_CLEAR(self->response_bytes);
    self->response_bytes_len = 0;
    self->response_bytes_cap = (Py_ssize_t)sizeof(self->response_inline);
}


static int
response_reserve(WreathHttpProtocol *self, Py_ssize_t extra)
{
    Py_ssize_t need;
    Py_ssize_t capacity;
    if (extra < 0 || self->response_bytes_len > PY_SSIZE_T_MAX - extra) {
        PyErr_SetString(PyExc_OverflowError, "HTTP response is too large");
        return -1;
    }
    need = self->response_bytes_len + extra;
    if (need <= self->response_bytes_cap) {
        return 0;
    }
    capacity = self->response_bytes_cap
                   ? self->response_bytes_cap
                   : (Py_ssize_t)sizeof(self->response_inline);
    while (capacity < need) {
        if (capacity > PY_SSIZE_T_MAX / 2) {
            capacity = need;
            break;
        }
        capacity *= 2;
    }
    if (self->response_bytes == NULL) {
        self->response_bytes = PyBytes_FromStringAndSize(NULL, capacity);
        if (self->response_bytes == NULL) {
            return -1;
        }
        memcpy(PyBytes_AS_STRING(self->response_bytes), self->response_inline,
               (size_t)self->response_bytes_len);
    }
    else if (_PyBytes_Resize(&self->response_bytes, capacity) < 0) {
        self->response_bytes_cap = 0;
        self->response_bytes_len = 0;
        return -1;
    }
    self->response_bytes_cap = capacity;
    return 0;
}


static int
response_append(WreathHttpProtocol *self, const char *data, Py_ssize_t size)
{
    if (response_reserve(self, size) < 0) {
        return -1;
    }
    memcpy((self->response_bytes != NULL
                ? PyBytes_AS_STRING(self->response_bytes)
                : self->response_inline) + self->response_bytes_len,
           data, (size_t)size);
    self->response_bytes_len += size;
    return 0;
}


static int
response_append_lower(WreathHttpProtocol *self, const char *data, Py_ssize_t size)
{
    char *target;
    if (response_reserve(self, size) < 0) {
        return -1;
    }
    target = (self->response_bytes != NULL
                  ? PyBytes_AS_STRING(self->response_bytes)
                  : self->response_inline) + self->response_bytes_len;
    for (Py_ssize_t i = 0; i < size; i++) {
        char c = data[i];
        target[i] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c;
    }
    self->response_bytes_len += size;
    return 0;
}


static int
response_append_decimal(WreathHttpProtocol *self, Py_ssize_t value)
{
    char digits[32];
    int size = PyOS_snprintf(digits, sizeof(digits), "%zd", value);
    if (size < 0 || size >= (int)sizeof(digits)) {
        PyErr_SetString(PyExc_OverflowError, "integer formatting failed");
        return -1;
    }
    return response_append(self, digits, size);
}


static int
header_name_equals(const char *data, Py_ssize_t size,
                   const char *expected, Py_ssize_t expected_size)
{
    if (size != expected_size) {
        return 0;
    }
    for (Py_ssize_t i = 0; i < size; i++) {
        unsigned char c = (unsigned char)data[i];
        if (c >= 'A' && c <= 'Z') {
            c = (unsigned char)(c + 32);
        }
        if (c != (unsigned char)expected[i]) {
            return 0;
        }
    }
    return 1;
}


static PyObject *
take_response_bytes(WreathHttpProtocol *self)
{
    PyObject *result = self->response_bytes;
    if (result == NULL) {
        result = PyBytes_FromStringAndSize(self->response_inline, self->response_bytes_len);
        if (result == NULL) {
            return NULL;
        }
        self->response_bytes_len = 0;
        self->response_bytes_cap = (Py_ssize_t)sizeof(self->response_inline);
        return result;
    }
    self->response_bytes = NULL;
    if (self->response_bytes_len != self->response_bytes_cap &&
        _PyBytes_Resize(&result, self->response_bytes_len) < 0) {
        self->response_bytes_len = 0;
        self->response_bytes_cap = 0;
        return NULL;
    }
    self->response_bytes_len = 0;
    self->response_bytes_cap = (Py_ssize_t)sizeof(self->response_inline);
    return result;
}


/* --- ASGI message construction ------------------------------------------- */

static PyObject *
make_request_msg(PyObject *body, int more)
{
    PyObject *msg = PyDict_New();
    if (msg == NULL) {
        return NULL;
    }
    if (PyDict_SetItem(msg, s_type, s_http_request) < 0 ||
        PyDict_SetItem(msg, s_body, body) < 0 ||
        PyDict_SetItem(msg, s_more_body, more ? Py_True : Py_False) < 0) {
        Py_DECREF(msg);
        return NULL;
    }
    return msg;
}


static PyObject *
make_disconnect_msg(void)
{
    PyObject *msg = PyDict_New();
    if (msg == NULL) {
        return NULL;
    }
    if (PyDict_SetItem(msg, s_type, s_http_disconnect) < 0) {
        Py_DECREF(msg);
        return NULL;
    }
    return msg;
}


/* --- close / abort ------------------------------------------------------- */

static int
protocol_close(WreathHttpProtocol *self)
{
    if (self->closing) {
        return 0;
    }
    self->closing = 1;
    self->state = ST_CLOSING;
    if (cancel_deadline_timer(self) < 0) {
        return -1;
    }
    if (self->transport != NULL) {
        return transport_method0(self, "close");
    }
    return 0;
}


static int
protocol_abort(WreathHttpProtocol *self)
{
    if (self->transport != NULL && transport_method0(self, "abort") < 0) {
        return -1;
    }
    return protocol_close(self);
}


/* --- error responses ----------------------------------------------------- */

static int
write_error(WreathHttpProtocol *self, int status)
{
    PyObject *out;
    Py_ssize_t reason_size;
    const char *reason = reason_phrase(status, &reason_size);
    Py_ssize_t body_size = reason_size + 1;
    int result = -1;

    out = PyByteArray_FromStringAndSize(NULL, 0);
    if (out == NULL) {
        return -1;
    }
    if (append_raw(out, self->http11 ? "HTTP/1.1 " : "HTTP/1.0 ", 9) < 0 ||
        append_decimal(out, status) < 0 || append_raw(out, " ", 1) < 0 ||
        append_raw(out, reason, reason_size) < 0 || append_raw(out, "\r\n", 2) < 0 ||
        append_raw(out, "content-type: text/plain; charset=utf-8\r\ncontent-length: ",
                   sizeof("content-type: text/plain; charset=utf-8\r\ncontent-length: ") - 1) < 0 ||
        append_decimal(out, body_size) < 0 || append_raw(out, "\r\n", 2) < 0) {
        goto done;
    }
    {
        PyObject *defaults = PyObject_GetAttrString(self->config, "_default_response_headers");
        PyObject *headers = defaults ? PyObject_GetAttrString(defaults, "headers") : NULL;
        Py_XDECREF(defaults);
        if (headers == NULL) goto done;
        PyObject *items = PySequence_Fast(headers, "default response headers must be a sequence");
        Py_DECREF(headers);
        if (items == NULL) goto done;
        for (Py_ssize_t i = 0; i < PySequence_Fast_GET_SIZE(items); i++) {
            PyObject *pair = PySequence_Fast_GET_ITEM(items, i);
            PyObject *name = PyTuple_GET_ITEM(pair, 0);
            PyObject *value = PyTuple_GET_ITEM(pair, 1);
            if (append_raw(out, PyBytes_AS_STRING(name), PyBytes_GET_SIZE(name)) < 0 ||
                append_raw(out, ": ", 2) < 0 ||
                append_raw(out, PyBytes_AS_STRING(value), PyBytes_GET_SIZE(value)) < 0 ||
                append_raw(out, "\r\n", 2) < 0) {
                Py_DECREF(items);
                goto done;
            }
        }
        Py_DECREF(items);
    }
    if (append_raw(out, "connection: close\r\n\r\n",
                   sizeof("connection: close\r\n\r\n") - 1) < 0) goto done;
    /* A HEAD response carries content-length but never the body. */
    if (!self->method_is_head) {
        if (append_raw(out, reason, reason_size) < 0 || append_raw(out, "\n", 1) < 0) {
            goto done;
        }
    }
    result = transport_write_raw(self, PyByteArray_AS_STRING(out), PyByteArray_GET_SIZE(out));
done:
    Py_DECREF(out);
    return result;
}


static int
send_error(WreathHttpProtocol *self, int status)
{
    /* Emit a minimal error response and close. Never invokes the app. */
    if (self->response_started) {
        return protocol_close(self);
    }
    if (write_error(self, status) < 0) {
        return -1;
    }
    return protocol_close(self);
}


static int
body_error(WreathHttpProtocol *self, int status)
{
    self->framing_error = 1;
    return send_error(self, status);
}


/* --- receive plumbing ---------------------------------------------------- */

/* Apply both receive watermarks after the queue grew.
 *
 * A message may carry zero payload bytes, so a byte watermark alone cannot
 * bound the queue: an empty-message flood keeps `queued_bytes` at zero forever.
 * Either bound may pause reading. */
static int
receive_pressure_pause(WreathHttpProtocol *self)
{
    if (self->reading_paused) {
        return 0;
    }
    if (self->queued_bytes > self->read_high_water ||
        self->queued_messages >= self->read_high_water_messages) {
        if (transport_method0(self, "pause_reading") < 0) {
            return -1;
        }
        self->reading_paused = 1;
    }
    return 0;
}

/* Resume only when both measures have fallen to half their watermark: one of
 * them being low says nothing about the other. */
static int
receive_pressure_resume(WreathHttpProtocol *self)
{
    if (!self->reading_paused) {
        return 0;
    }
    if (self->queued_bytes <= self->read_high_water / 2 &&
        self->queued_messages <= self->read_high_water_messages / 2) {
        if (transport_method0(self, "resume_reading") < 0) {
            return -1;
        }
        self->reading_paused = 0;
    }
    return 0;
}

/* Append to the queue tail, maintaining the logical length. */
static int
receive_queue_push(WreathHttpProtocol *self, PyObject *msg)
{
    if (PyList_Append(self->receive_queue, msg) < 0) {
        return -1;
    }
    self->queued_messages++;
    return 0;
}

/* Take a strong reference to the front message, then advance and maybe compact.
 * The reference is taken before any compaction, so the returned object is never
 * a borrowed pointer into a prefix that is about to be released. */
static PyObject *
receive_queue_pop(WreathHttpProtocol *self)
{
    PyObject *msg = Py_NewRef(PyList_GET_ITEM(self->receive_queue, self->receive_head));
    self->receive_head++;
    self->queued_messages--;
    Py_ssize_t size = PyList_GET_SIZE(self->receive_queue);
    if (self->receive_head >= size) {
        if (PyList_SetSlice(self->receive_queue, 0, size, NULL) < 0) {
            Py_DECREF(msg);
            return NULL;
        }
        self->receive_head = 0;
    }
    else if (self->receive_head >= 64 && self->receive_head * 2 >= size) {
        if (PyList_SetSlice(self->receive_queue, 0, self->receive_head, NULL) < 0) {
            Py_DECREF(msg);
            return NULL;
        }
        self->receive_head = 0;
    }
    return msg;
}

static int
enqueue_body(WreathHttpProtocol *self, PyObject *body, int more)
{
    PyObject *msg = make_request_msg(body, more);
    if (msg == NULL) {
        return -1;
    }
    if (self->nfr_worker != NULL) {
        self->nfr_bytes_in += (uint64_t)PyBytes_GET_SIZE(body);
    }
    if (self->receive_waiter != NULL) {
        /* The app is waiting: deliver directly without buffering. */
        PyObject *waiter = self->receive_waiter;
        self->receive_waiter = NULL;
        if (future_set_result(waiter, msg) < 0) {
            Py_DECREF(waiter);
            Py_DECREF(msg);
            return -1;
        }
        Py_DECREF(waiter);
    }
    else {
        if (receive_queue_push(self, msg) < 0) {
            Py_DECREF(msg);
            return -1;
        }
        self->queued_bytes += PyBytes_GET_SIZE(body);
        if (receive_pressure_pause(self) < 0) {
            Py_DECREF(msg);
            return -1;
        }
    }
    Py_DECREF(msg);
    if (!more) {
        self->request_more_body = 0;
    }
    return 0;
}


static int
deliver_disconnect(WreathHttpProtocol *self)
{
    self->disconnected = 1;
    if (self->receive_waiter != NULL) {
        PyObject *waiter = self->receive_waiter;
        PyObject *msg = self->ws_mode ? make_ws_disconnect_msg(1006)
                                      : make_disconnect_msg();
        self->receive_waiter = NULL;
        if (msg == NULL) {
            Py_DECREF(waiter);
            return -1;
        }
        if (future_set_result(waiter, msg) < 0) {
            Py_DECREF(waiter);
            Py_DECREF(msg);
            return -1;
        }
        Py_DECREF(waiter);
        Py_DECREF(msg);
    }
    return 0;
}


static PyObject *
http_asgi_receive(WreathHttpProtocol *self, PyObject *Py_UNUSED(ignored))
{
    if (self->pending_empty_request) {
        PyObject *empty = PyBytes_FromStringAndSize("", 0);
        PyObject *msg;
        PyObject *awaitable;
        if (empty == NULL) {
            return NULL;
        }
        msg = make_request_msg(empty, 0);
        Py_DECREF(empty);
        if (msg == NULL) {
            return NULL;
        }
        self->pending_empty_request = 0;
        awaitable = completed_value(msg);
        Py_DECREF(msg);
        return awaitable;
    }
    if (self->queued_messages > 0) {
        PyObject *msg = receive_queue_pop(self);
        PyObject *type;
        PyObject *awaitable;
        if (msg == NULL) {
            return NULL;
        }
        type = PyDict_GetItem(msg, s_type);
        if (type != NULL && (type == s_http_request ||
                             PyUnicode_CompareWithASCIIString(type, "http.request") == 0)) {
            PyObject *body = PyDict_GetItem(msg, s_body);
            if (body != NULL && PyBytes_Check(body)) {
                self->queued_bytes -= PyBytes_GET_SIZE(body);
            }
        }
        else if (type != NULL &&
                 (type == s_ws_receive ||
                  PyUnicode_CompareWithASCIIString(type, "websocket.receive") == 0)) {
            PyObject *payload = PyDict_GetItem(msg, s_bytes);
            if (payload != NULL && PyBytes_Check(payload)) {
                self->queued_bytes -= PyBytes_GET_SIZE(payload);
            }
            else {
                PyObject *text = PyDict_GetItem(msg, s_text);
                if (text != NULL && PyUnicode_Check(text)) {
                    self->queued_bytes -= PyUnicode_GET_LENGTH(text);
                }
            }
        }
        if (receive_pressure_resume(self) < 0) {
            Py_DECREF(msg);
            return NULL;
        }
        awaitable = completed_value(msg);
        Py_DECREF(msg);
        return awaitable;
    }
    if (self->disconnected) {
        PyObject *msg = self->ws_mode ? make_ws_disconnect_msg(1006)
                                      : make_disconnect_msg();
        PyObject *awaitable;
        if (msg == NULL) {
            return NULL;
        }
        awaitable = completed_value(msg);
        Py_DECREF(msg);
        return awaitable;
    }
    /* Nothing available yet: hand back a pending future to resolve on arrival. */
    PyObject *waiter = make_future(self);
    if (waiter == NULL) {
        return NULL;
    }
    Py_INCREF(waiter);
    self->receive_waiter = waiter;  /* one reference for us, one returned */
    return waiter;
}


/* --- write backpressure -------------------------------------------------- */

static PyObject *
maybe_drain(WreathHttpProtocol *self)
{
    if (!self->write_paused) {
        return completed_none();
    }
    if (self->drain_waiter == NULL) {
        self->drain_waiter = make_future(self);
        if (self->drain_waiter == NULL) {
            return NULL;
        }
    }
    Py_INCREF(self->drain_waiter);
    return self->drain_waiter;
}


static int
resolve_drain(WreathHttpProtocol *self)
{
    if (self->drain_waiter != NULL) {
        PyObject *waiter = self->drain_waiter;
        PyObject *done_obj;
        int done;
        self->drain_waiter = NULL;
        done_obj = PyObject_CallMethod(waiter, "done", NULL);
        if (done_obj == NULL) {
            Py_DECREF(waiter);
            return -1;
        }
        done = PyObject_IsTrue(done_obj);
        Py_DECREF(done_obj);
        if (done == 0 && future_set_result(waiter, Py_None) < 0) {
            Py_DECREF(waiter);
            return -1;
        }
        Py_DECREF(waiter);
    }
    return 0;
}


/* --- timers ----------------------------------------------------------------
 *
 * Timeout enforcement is deadline-based.  The connection stores the active
 * deadline (keep-alive or request) as a monotonic double; moving the deadline
 * on every request costs no Python calls.  At most one loop timer is pending
 * per connection: when it fires it compares against the live deadline and
 * either enforces the timeout or re-arms for the remainder.  The only time a
 * pending timer is cancelled is when a new deadline lands *earlier* than the
 * pending fire time, which cannot happen in steady-state keep-alive traffic.
 */

static double
mono_now(void)
{
    PyTime_t now;
    (void)PyTime_MonotonicRaw(&now);
    return PyTime_AsSecondsDouble(now);
}


static int
cancel_deadline_timer(WreathHttpProtocol *self)
{
    if (self->timer_handle != NULL) {
        PyObject *result = PyObject_CallMethod(self->timer_handle, "cancel", NULL);
        Py_CLEAR(self->timer_handle);
        if (result == NULL) {
            return -1;
        }
        Py_DECREF(result);
    }
    return 0;
}


static int
arm_deadline_timer(WreathHttpProtocol *self, double delay)
{
    PyObject *delay_obj;
    PyObject *handle;
    if (delay < 0.0) {
        delay = 0.0;
    }
    delay_obj = PyFloat_FromDouble(delay);
    if (delay_obj == NULL) {
        return -1;
    }
    handle = PyObject_CallFunctionObjArgs(self->loop_call_later, delay_obj,
                                          self->deadline_callable, NULL);
    Py_DECREF(delay_obj);
    if (handle == NULL) {
        return -1;
    }
    Py_XSETREF(self->timer_handle, handle);
    self->timer_target = mono_now() + delay;
    return 0;
}


static int
set_deadline(WreathHttpProtocol *self, double timeout, int is_request)
{
    self->deadline_is_request = is_request;
    if (timeout <= 0.0) {
        /* This class of timeout is disabled; a pending timer (if any) will
         * no-op when it fires and re-arm on the next finite deadline. */
        self->deadline = Py_HUGE_VAL;
        return 0;
    }
    self->deadline = mono_now() + timeout;
    if (self->timer_handle == NULL) {
        return arm_deadline_timer(self, timeout);
    }
    if (self->timer_target > self->deadline + 1e-3) {
        if (cancel_deadline_timer(self) < 0) {
            return -1;
        }
        return arm_deadline_timer(self, timeout);
    }
    return 0;
}


/* --- response framing ---------------------------------------------------- */

static int
valid_header_name(const char *data, Py_ssize_t size)
{
    if (size == 0) {
        return 0;
    }
    for (Py_ssize_t i = 0; i < size; i++) {
        unsigned char c = (unsigned char)data[i];
        if (c == ':' || c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\0' ||
            c < 0x20) {
            return 0;
        }
    }
    return 1;
}


static int
valid_header_value(const char *data, Py_ssize_t size)
{
    for (Py_ssize_t i = 0; i < size; i++) {
        unsigned char c = (unsigned char)data[i];
        if (c == 0x00 || c == 0x0A || c == 0x0D) {
            return 0;
        }
    }
    return 1;
}


static int
parse_content_length_header(PyObject *value, Py_ssize_t *out)
{
    /* Fast path: plain decimal digits.  Anything else falls back to int()
     * semantics to stay behaviorally identical to the pure twin. */
    const char *data = PyBytes_AS_STRING(value);
    Py_ssize_t size = PyBytes_GET_SIZE(value);
    Py_ssize_t parsed = 0;
    if (size > 0 && size <= 18) {
        Py_ssize_t i = 0;
        for (; i < size; i++) {
            int digit = (unsigned char)data[i] - '0';
            if (digit < 0 || digit > 9) {
                break;
            }
            parsed = parsed * 10 + digit;
        }
        if (i == size) {
            *out = parsed;
            return 0;
        }
    }
    {
        PyObject *number = PyObject_CallFunctionObjArgs((PyObject *)&PyLong_Type,
                                                        value, NULL);
        if (number == NULL) {
            PyErr_Clear();
            PyErr_SetString(PyExc_RuntimeError, "invalid content-length");
            return -1;
        }
        parsed = PyLong_AsSsize_t(number);
        Py_DECREF(number);
        if (parsed == -1 && PyErr_Occurred()) {
            PyErr_Clear();
            PyErr_SetString(PyExc_RuntimeError, "invalid content-length");
            return -1;
        }
        if (parsed < 0) {
            PyErr_SetString(PyExc_RuntimeError, "invalid content-length");
            return -1;
        }
        *out = parsed;
        return 0;
    }
}


static int
begin_response(WreathHttpProtocol *self, PyObject *message)
{
    PyObject *status_obj = PyDict_GetItem(message, s_status);
    PyObject *headers = PyDict_GetItem(message, s_headers);
    PyObject *items = NULL;
    long status;
    Py_ssize_t reason_size;
    const char *reason;
    int has_date = 0;
    int has_server = 0;

    clear_response_builder(self);
    if (status_obj == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "response start missing status");
        return -1;
    }
    status = PyLong_AsLong(status_obj);
    if (status == -1 && PyErr_Occurred()) {
        return -1;
    }
    if (status < 100 || status > 999) {
        PyErr_SetString(PyExc_ValueError, "response status must be between 100 and 999");
        return -1;
    }
    self->resp_status = (int)status;
    self->response_started = 1;
    if (status < 200 || status == 204 || status == 304) {
        self->response_suppress_body = 1;
    }
    if (response_append(self, self->http11 ? "HTTP/1.1 " : "HTTP/1.0 ", 9) < 0 ||
        response_append_decimal(self, self->resp_status) < 0 ||
        response_append(self, " ", 1) < 0) {
        return -1;
    }
    reason = reason_phrase(self->resp_status, &reason_size);
    if (response_append(self, reason, reason_size) < 0 ||
        response_append(self, "\r\n", 2) < 0) {
        return -1;
    }

    if (headers != NULL && headers != Py_None) {
        items = PySequence_Fast(headers, "response headers must be a sequence");
        if (items == NULL) {
            return -1;
        }
        for (Py_ssize_t i = 0; i < PySequence_Fast_GET_SIZE(items); i++) {
            PyObject *pair = PySequence_Fast_GET_ITEM(items, i);
            PyObject *name;
            PyObject *value;
            const char *name_data;
            Py_ssize_t name_size;
            if (PyTuple_Check(pair) && PyTuple_GET_SIZE(pair) == 2) {
                name = PyTuple_GET_ITEM(pair, 0);
                value = PyTuple_GET_ITEM(pair, 1);
            }
            else if (PyList_Check(pair) && PyList_GET_SIZE(pair) == 2) {
                name = PyList_GET_ITEM(pair, 0);
                value = PyList_GET_ITEM(pair, 1);
            }
            else {
                PyErr_SetString(PyExc_RuntimeError, "response header must be a pair");
                goto error;
            }
            if (!PyBytes_Check(name) || !PyBytes_Check(value)) {
                PyErr_SetString(PyExc_RuntimeError, "response header must be bytes");
                goto error;
            }
            name_data = PyBytes_AS_STRING(name);
            name_size = PyBytes_GET_SIZE(name);
            if (!valid_header_name(name_data, name_size) ||
                !valid_header_value(PyBytes_AS_STRING(value), PyBytes_GET_SIZE(value))) {
                PyErr_SetString(PyExc_RuntimeError, "invalid response header");
                goto error;
            }
            if (header_name_equals(name_data, name_size, "date", 4)) {
                has_date = 1;
            }
            else if (header_name_equals(name_data, name_size, "server", 6)) {
                has_server = 1;
            }
            if (header_name_equals(name_data, name_size, "content-length", 14)) {
                if (parse_content_length_header(value, &self->resp_content_length) < 0) {
                    goto error;
                }
                self->resp_has_length = 1;
                continue;
            }
            if (header_name_equals(name_data, name_size, "transfer-encoding", 17) ||
                header_name_equals(name_data, name_size, "connection", 10)) {
                continue;
            }
            if (response_append_lower(self, name_data, name_size) < 0 ||
                response_append(self, ": ", 2) < 0 ||
                response_append(self, PyBytes_AS_STRING(value), PyBytes_GET_SIZE(value)) < 0 ||
                response_append(self, "\r\n", 2) < 0) {
                goto error;
            }
        }
        Py_CLEAR(items);
    }
    {
        PyObject *defaults = PyObject_GetAttrString(self->config, "_default_response_headers");
        PyObject *default_headers;
        if (defaults == NULL) {
            goto error;
        }
        default_headers = PyObject_GetAttrString(defaults, "headers");
        Py_DECREF(defaults);
        if (default_headers == NULL) {
            goto error;
        }
        items = PySequence_Fast(default_headers, "default response headers must be a sequence");
        Py_DECREF(default_headers);
        if (items == NULL) {
            goto error;
        }
        for (Py_ssize_t i = 0; i < PySequence_Fast_GET_SIZE(items); i++) {
            PyObject *pair = PySequence_Fast_GET_ITEM(items, i);
            PyObject *name = PyTuple_GET_ITEM(pair, 0);
            PyObject *value = PyTuple_GET_ITEM(pair, 1);
            const char *name_data = PyBytes_AS_STRING(name);
            Py_ssize_t name_size = PyBytes_GET_SIZE(name);
            if ((has_date && header_name_equals(name_data, name_size, "date", 4)) ||
                (has_server && header_name_equals(name_data, name_size, "server", 6))) {
                continue;
            }
            if (response_append(self, name_data, name_size) < 0 ||
                response_append(self, ": ", 2) < 0 ||
                response_append(self, PyBytes_AS_STRING(value), PyBytes_GET_SIZE(value)) < 0 ||
                response_append(self, "\r\n", 2) < 0) {
                goto error;
            }
        }
        Py_CLEAR(items);
    }
    self->head_written = 0;
    return 0;

error:
    Py_XDECREF(items);
    clear_response_builder(self);
    return -1;
}


static int
decide_framing_and_write_head(WreathHttpProtocol *self, PyObject *first_body, int streaming,
                              int *body_coalesced)
{
    Py_ssize_t first_body_size = first_body == NULL ? 0 : PyBytes_GET_SIZE(first_body);
    Py_ssize_t content_length = self->resp_content_length;
    int has_length = self->resp_has_length;
    int keep_alive = self->response_keep_alive;
    int pair_write = 0;
    PyObject *output;

    if (self->response_suppress_body) {
        self->response_chunked = 0;
    }
    else if (!streaming && !has_length) {
        content_length = first_body_size;
        has_length = 1;
        self->response_chunked = 0;
        self->resp_content_length = content_length;
        self->resp_has_length = 1;
    }
    else if (has_length) {
        self->response_chunked = 0;
    }
    else if (self->http11) {
        self->response_chunked = 1;
    }
    else {
        self->response_chunked = 0;
        keep_alive = 0;
    }
    self->response_keep_alive = keep_alive;

    if (!streaming && !self->response_suppress_body && first_body_size > 0) {
        if (first_body_size <= 16384) {
            *body_coalesced = 1;
        }
        else if (self->transport_writelines_fn != NULL) {
            *body_coalesced = 1;
            pair_write = 1;
        }
        else {
            *body_coalesced = 0;
        }
    }
    else {
        *body_coalesced = 0;
    }

    if (has_length) {
        if (response_append(self, "content-length: ", 16) < 0 ||
            response_append_decimal(self, content_length) < 0 ||
            response_append(self, "\r\n", 2) < 0) {
            return -1;
        }
    }
    else if (self->response_chunked && !self->response_suppress_body) {
        if (response_append(self, "transfer-encoding: chunked\r\n", 28) < 0) {
            return -1;
        }
    }
    if (response_append(self, self->response_keep_alive
                              ? "connection: keep-alive\r\n"
                              : "connection: close\r\n",
                        self->response_keep_alive ? 24 : 19) < 0 ||
        response_append(self, "\r\n", 2) < 0 ||
        (!pair_write && *body_coalesced &&
         response_append(self, PyBytes_AS_STRING(first_body), first_body_size) < 0)) {
        return -1;
    }
    output = take_response_bytes(self);
    if (output == NULL) {
        return -1;
    }

    if (pair_write) {
        if (self->transport != NULL && !self->closing) {
            PyObject *pair = PyTuple_Pack(2, output, first_body);
            PyObject *result;
            if (self->nfr_worker != NULL) {
                self->nfr_bytes_out += (uint64_t)PyBytes_GET_SIZE(output) +
                                       (uint64_t)first_body_size;
            }
            Py_DECREF(output);
            if (pair == NULL) {
                return -1;
            }
            result = PyObject_CallOneArg(self->transport_writelines_fn, pair);
            Py_DECREF(pair);
            if (result == NULL) {
                return -1;
            }
            Py_DECREF(result);
        }
        else {
            Py_DECREF(output);
        }
    }
    else {
        int result = transport_write(self, output);
        Py_DECREF(output);
        if (result < 0) {
            return -1;
        }
    }
    self->head_written = 1;
    return 0;
}


static int
finish_response(WreathHttpProtocol *self, int keep_alive)
{
    if (self->response_complete) {
        return 0;
    }
    if (self->response_started && !self->head_written) {
        int body_coalesced;
        if (decide_framing_and_write_head(self, NULL, 0, &body_coalesced) < 0) {
            return -1;
        }
        keep_alive = 0;
    }
    self->response_complete = 1;
    if (self->framing_error) {
        keep_alive = 0;
    }
    if (!keep_alive || self->closing || !self->accepting || self->disconnected) {
        return protocol_close(self);
    }
    /* The keep-alive countdown starts as soon as the response completes; the
     * per-request deadline is superseded in place. */
    if (set_deadline(self, self->keep_alive_timeout, 0) < 0) {
        return -1;
    }
    /* Keep-alive: defer the next request until the finishing task unwinds (in
     * _on_app_done) so the connection never re-enters the state machine while
     * that task is still on the stack. */
    self->want_next = 1;
    return 0;
}


static PyObject *
write_body(WreathHttpProtocol *self, PyObject *message)
{
    PyObject *body = PyDict_GetItem(message, s_body);
    PyObject *more_obj = PyDict_GetItem(message, s_more_body);
    Py_ssize_t body_size;
    int more;
    int suppress = self->response_suppress_body;
    int chunked;
    int body_coalesced = 0;

    if (body == NULL) {
        body = PyBytes_FromStringAndSize("", 0);
        if (body == NULL) {
            return NULL;
        }
    }
    else {
        Py_INCREF(body);
    }
    if (!PyBytes_Check(body)) {
        Py_DECREF(body);
        PyErr_SetString(PyExc_TypeError, "response body must be bytes");
        return NULL;
    }
    more = more_obj != NULL && PyObject_IsTrue(more_obj);
    if (more < 0) {
        Py_DECREF(body);
        return NULL;
    }
    body_size = PyBytes_GET_SIZE(body);

    if (!self->head_written) {
        if (decide_framing_and_write_head(self, body, more, &body_coalesced) < 0) {
            Py_DECREF(body);
            return NULL;
        }
    }
    chunked = self->response_chunked;

    if (body_size > 0 && !suppress) {
        if (self->resp_has_length) {
            Py_ssize_t expected = self->resp_content_length;
            if (self->response_body_sent > PY_SSIZE_T_MAX - body_size ||
                self->response_body_sent + body_size > expected) {
                Py_DECREF(body);
                PyErr_SetString(PyExc_RuntimeError, "response body exceeds content-length");
                return NULL;
            }
            self->response_body_sent += body_size;
            if (!body_coalesced && transport_write(self, body) < 0) {
                Py_DECREF(body);
                return NULL;
            }
        }
        else if (chunked) {
            char size_line[32];
            int size_length = PyOS_snprintf(size_line, sizeof(size_line), "%zx\r\n",
                                             (size_t)body_size);
            if (size_length < 0 || size_length >= (int)sizeof(size_line)) {
                Py_DECREF(body);
                PyErr_SetString(PyExc_OverflowError, "integer formatting failed");
                return NULL;
            }
            self->out_len = 0;
            if (out_append(self, size_line, size_length) < 0 ||
                out_append(self, PyBytes_AS_STRING(body), body_size) < 0 ||
                out_append(self, "\r\n", 2) < 0 ||
                transport_write_raw(self, self->out_buf, self->out_len) < 0) {
                Py_DECREF(body);
                return NULL;
            }
            self->out_len = 0;
        }
        else if (!body_coalesced && transport_write(self, body) < 0) {
            Py_DECREF(body);
            return NULL;
        }
    }
    Py_DECREF(body);

    if (!more) {
        if (self->resp_has_length && !suppress) {
            if (self->response_body_sent != self->resp_content_length) {
                /* Short body: framing is now ambiguous, force close. */
                self->response_keep_alive = 0;
            }
        }
        if (chunked && !suppress && transport_write_raw(self, "0\r\n\r\n", 5) < 0) {
            return NULL;
        }
        if (finish_response(self, self->response_keep_alive) < 0) {
            return NULL;
        }
        return completed_none();
    }
    return maybe_drain(self);
}


/* --- ASGI send ----------------------------------------------------------- */

static PyObject *ws_asgi_send(WreathHttpProtocol *self, PyObject *message, PyObject *type);


static PyObject *
http_asgi_send(WreathHttpProtocol *self, PyObject *message)
{
    PyObject *type;
    if (self->disconnected && (self->response_started || self->ws_mode)) {
        PyErr_SetString(disconnect_error, "peer disconnected");
        return NULL;
    }
    if (!PyDict_Check(message)) {
        PyErr_SetString(PyExc_TypeError, "ASGI message must be a dict");
        return NULL;
    }
    type = PyDict_GetItem(message, s_type);
    if (type == NULL || !PyUnicode_Check(type)) {
        PyErr_SetString(PyExc_RuntimeError, "ASGI message requires a str type");
        return NULL;
    }
    if (self->ws_mode) {
        return ws_asgi_send(self, message, type);
    }
    if (type == s_wreath_response ||
        PyUnicode_CompareWithASCIIString(type, "wreath.response") == 0) {
        /* One-shot response extension: status, headers, and the complete
         * body in a single message.  Framing and validation are identical
         * to a start+body pair; only the message traffic is halved. */
        if (self->response_started) {
            PyErr_SetString(PyExc_RuntimeError, "response already started");
            return NULL;
        }
        if (begin_response(self, message) < 0) {
            return NULL;
        }
        return write_body(self, message);
    }
    if (type == s_resp_body ||
        PyUnicode_CompareWithASCIIString(type, "http.response.body") == 0) {
        if (!self->response_started) {
            PyErr_SetString(PyExc_RuntimeError, "body before response start");
            return NULL;
        }
        if (self->response_complete) {
            PyErr_SetString(PyExc_RuntimeError, "body after response completed");
            return NULL;
        }
        return write_body(self, message);
    }
    if (type == s_resp_start ||
        PyUnicode_CompareWithASCIIString(type, "http.response.start") == 0) {
        if (self->response_started) {
            PyErr_SetString(PyExc_RuntimeError, "response already started");
            return NULL;
        }
        if (begin_response(self, message) < 0) {
            return NULL;
        }
        return completed_none();
    }
    PyErr_Format(PyExc_RuntimeError, "unexpected ASGI message: %R", type);
    return NULL;
}


static PyObject *
ws_asgi_send(WreathHttpProtocol *self, PyObject *message, PyObject *type)
{
    if (type == s_ws_send_msg ||
        PyUnicode_CompareWithASCIIString(type, "websocket.send") == 0) {
        PyObject *text;
        if (!self->ws_accepted || self->ws_close_sent) {
            PyErr_SetString(PyExc_RuntimeError, "websocket is not open");
            return NULL;
        }
        text = PyDict_GetItem(message, s_text);
        if (text != NULL && text != Py_None) {
            Py_ssize_t size;
            const char *utf8;
            if (!PyUnicode_Check(text)) {
                PyErr_SetString(PyExc_TypeError, "websocket.send 'text' must be str");
                return NULL;
            }
            utf8 = PyUnicode_AsUTF8AndSize(text, &size);
            if (utf8 == NULL || ws_write_frame(self, WS_OP_TEXT, utf8, size, NULL) < 0) {
                return NULL;
            }
        }
        else {
            PyObject *payload = PyDict_GetItem(message, s_bytes);
            if (payload == NULL || !PyBytes_Check(payload)) {
                PyErr_SetString(PyExc_TypeError,
                                "websocket.send requires 'text' or 'bytes'");
                return NULL;
            }
            if (ws_write_frame(self, WS_OP_BINARY, PyBytes_AS_STRING(payload),
                               PyBytes_GET_SIZE(payload), payload) < 0) {
                return NULL;
            }
        }
        return maybe_drain(self);
    }
    if (type == s_ws_accept_msg ||
        PyUnicode_CompareWithASCIIString(type, "websocket.accept") == 0) {
        PyObject *digest_input = NULL;
        PyObject *digest_obj = NULL;
        PyObject *digest = NULL;
        PyObject *accept = NULL;
        PyObject *subprotocol;
        PyObject *headers;
        if (self->ws_accepted) {
            PyErr_SetString(PyExc_RuntimeError, "websocket already accepted");
            return NULL;
        }
        digest_input = PyBytes_FromStringAndSize(NULL,
            PyBytes_GET_SIZE(self->ws_key) + (Py_ssize_t)sizeof(WS_GUID) - 1);
        if (digest_input == NULL) {
            return NULL;
        }
        memcpy(PyBytes_AS_STRING(digest_input), PyBytes_AS_STRING(self->ws_key),
               (size_t)PyBytes_GET_SIZE(self->ws_key));
        memcpy(PyBytes_AS_STRING(digest_input) + PyBytes_GET_SIZE(self->ws_key),
               WS_GUID, sizeof(WS_GUID) - 1);
        digest_obj = PyObject_CallOneArg(sha1_fn, digest_input);
        Py_DECREF(digest_input);
        if (digest_obj == NULL) {
            return NULL;
        }
        digest = PyObject_CallMethod(digest_obj, "digest", NULL);
        Py_DECREF(digest_obj);
        if (digest == NULL) {
            return NULL;
        }
        accept = PyObject_CallOneArg(b64encode_fn, digest);
        Py_DECREF(digest);
        if (accept == NULL) {
            return NULL;
        }

        self->out_len = 0;
        if (out_append(self, "HTTP/1.1 101 Switching Protocols\r\n"
                             "upgrade: websocket\r\nconnection: Upgrade\r\n"
                             "sec-websocket-accept: ",
                       sizeof("HTTP/1.1 101 Switching Protocols\r\n"
                              "upgrade: websocket\r\nconnection: Upgrade\r\n"
                              "sec-websocket-accept: ") - 1) < 0 ||
            out_append(self, PyBytes_AS_STRING(accept), PyBytes_GET_SIZE(accept)) < 0 ||
            out_append(self, "\r\n", 2) < 0) {
            Py_DECREF(accept);
            return NULL;
        }
        Py_DECREF(accept);
        subprotocol = PyDict_GetItem(message, s_subprotocol);
        if (subprotocol != NULL && subprotocol != Py_None) {
            Py_ssize_t size;
            const char *data;
            if (!PyUnicode_Check(subprotocol)) {
                PyErr_SetString(PyExc_TypeError, "subprotocol must be str");
                return NULL;
            }
            data = PyUnicode_AsUTF8AndSize(subprotocol, &size);
            if (data == NULL ||
                out_append(self, "sec-websocket-protocol: ", 24) < 0 ||
                out_append(self, data, size) < 0 || out_append(self, "\r\n", 2) < 0) {
                return NULL;
            }
        }
        headers = PyDict_GetItem(message, s_headers);
        if (headers != NULL && headers != Py_None) {
            PyObject *items = PySequence_Fast(headers, "accept headers must be a sequence");
            if (items == NULL) {
                return NULL;
            }
            for (Py_ssize_t i = 0; i < PySequence_Fast_GET_SIZE(items); i++) {
                PyObject *pair = PySequence_Fast_GET_ITEM(items, i);
                PyObject *name;
                PyObject *value;
                const char *name_data;
                Py_ssize_t name_size;
                char *lname;
                if (!PyTuple_Check(pair) || PyTuple_GET_SIZE(pair) != 2 ||
                    !PyBytes_Check(PyTuple_GET_ITEM(pair, 0)) ||
                    !PyBytes_Check(PyTuple_GET_ITEM(pair, 1))) {
                    Py_DECREF(items);
                    PyErr_SetString(PyExc_RuntimeError, "response header must be bytes");
                    return NULL;
                }
                name = PyTuple_GET_ITEM(pair, 0);
                value = PyTuple_GET_ITEM(pair, 1);
                name_data = PyBytes_AS_STRING(name);
                name_size = PyBytes_GET_SIZE(name);
                if (out_reserve(self, name_size) < 0) {
                    Py_DECREF(items);
                    return NULL;
                }
                lname = self->out_buf + self->out_len;
                for (Py_ssize_t j = 0; j < name_size; j++) {
                    char c = name_data[j];
                    lname[j] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c;
                }
                if (!valid_header_name(lname, name_size) ||
                    !valid_header_value(PyBytes_AS_STRING(value),
                                        PyBytes_GET_SIZE(value))) {
                    Py_DECREF(items);
                    PyErr_SetString(PyExc_RuntimeError, "invalid response header");
                    return NULL;
                }
                self->out_len += name_size;
                if (out_append(self, ": ", 2) < 0 ||
                    out_append(self, PyBytes_AS_STRING(value),
                               PyBytes_GET_SIZE(value)) < 0 ||
                    out_append(self, "\r\n", 2) < 0) {
                    Py_DECREF(items);
                    return NULL;
                }
            }
            Py_DECREF(items);
        }
        if (out_append(self, "\r\n", 2) < 0 ||
            transport_write_raw(self, self->out_buf, self->out_len) < 0) {
            return NULL;
        }
        self->out_len = 0;
        self->ws_accepted = 1;
        /* Long-lived connection: no request or keep-alive deadline. */
        self->deadline = Py_HUGE_VAL;
        self->state = ST_WS_OPEN;
        /* Frames may already sit in the buffer behind the handshake. */
        if (self->cursor < self->buf_len && run_drive(self) < 0) {
            return NULL;
        }
        return completed_none();
    }
    if (type == s_ws_close_msg ||
        PyUnicode_CompareWithASCIIString(type, "websocket.close") == 0) {
        if (!self->ws_accepted) {
            /* Rejected handshake: a plain HTTP error response. */
            if (send_error(self, 403) < 0) {
                return NULL;
            }
            self->ws_close_sent = 1;
            return completed_none();
        }
        if (!self->ws_close_sent) {
            PyObject *code_obj = PyDict_GetItem(message, s_code);
            PyObject *reason_obj = PyDict_GetItem(message, s_reason);
            long code = 1000;
            const char *reason = NULL;
            Py_ssize_t reason_size = 0;
            if (code_obj != NULL && code_obj != Py_None) {
                code = PyLong_AsLong(code_obj);
                if (code == -1 && PyErr_Occurred()) {
                    return NULL;
                }
                if (code == 0) {
                    code = 1000;
                }
            }
            if (reason_obj != NULL && reason_obj != Py_None &&
                PyUnicode_Check(reason_obj)) {
                reason = PyUnicode_AsUTF8AndSize(reason_obj, &reason_size);
                if (reason == NULL) {
                    return NULL;
                }
            }
            if (ws_send_close_frame(self, (int)code, reason, reason_size) < 0) {
                return NULL;
            }
        }
        if (protocol_close(self) < 0) {
            return NULL;
        }
        return completed_none();
    }
    PyErr_Format(PyExc_RuntimeError, "unexpected ASGI message: %R", type);
    return NULL;
}


/* --- scope construction and request start -------------------------------- */

static PyObject *
address_tuple(PyObject *info)
{
    PyObject *first;
    PyObject *second;
    PyObject *pair;
    if (info == NULL || info == Py_None || !PySequence_Check(info) ||
        PySequence_Size(info) < 2) {
        Py_RETURN_NONE;
    }
    first = PySequence_GetItem(info, 0);
    second = PySequence_GetItem(info, 1);
    if (first == NULL || second == NULL) {
        Py_XDECREF(first);
        Py_XDECREF(second);
        return NULL;
    }
    pair = PyTuple_Pack(2, first, second);
    Py_DECREF(first);
    Py_DECREF(second);
    return pair;
}


static PyObject *
get_extra_info(WreathHttpProtocol *self, const char *name)
{
    if (self->transport == NULL) {
        Py_RETURN_NONE;
    }
    return PyObject_CallMethod(self->transport, "get_extra_info", "s", name);
}


static PyObject *
decode_path(const char *data, Py_ssize_t size, int *bad)
{
    /* Percent-decode (without plus-as-space), then strict UTF-8, matching the
     * pure reference. */
    char *decoded = PyMem_Malloc(size ? (size_t)size : 1);
    Py_ssize_t out = 0;
    PyObject *path;
    *bad = 0;
    if (decoded == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    for (Py_ssize_t i = 0; i < size; i++) {
        char c = data[i];
        if (c == '%' && i + 2 < size) {
            int hi = (unsigned char)data[i + 1];
            int lo = (unsigned char)data[i + 2];
            int hv = (hi >= '0' && hi <= '9') ? hi - '0'
                   : (hi >= 'a' && hi <= 'f') ? hi - 'a' + 10
                   : (hi >= 'A' && hi <= 'F') ? hi - 'A' + 10 : -1;
            int lv = (lo >= '0' && lo <= '9') ? lo - '0'
                   : (lo >= 'a' && lo <= 'f') ? lo - 'a' + 10
                   : (lo >= 'A' && lo <= 'F') ? lo - 'A' + 10 : -1;
            if (hv >= 0 && lv >= 0) {
                decoded[out++] = (char)((hv << 4) | lv);
                i += 2;
                continue;
            }
        }
        decoded[out++] = c;
    }
    path = PyUnicode_DecodeUTF8(decoded, out, "strict");
    PyMem_Free(decoded);
    if (path == NULL) {
        if (PyErr_ExceptionMatches(PyExc_UnicodeDecodeError)) {
            PyErr_Clear();
            *bad = 1;
        }
        return NULL;
    }
    return path;
}


static int
decide_framing(WreathHttpProtocol *self, PyObject *headers, int *kind, Py_ssize_t *length,
               int *err_status)
{
    /* kind: 0 none, 1 fixed, 2 chunked. err_status non-zero => reject. */
    PyObject *first_length = NULL;
    int saw_transfer = 0;
    int chunked_count = 0;
    Py_ssize_t i;
    Py_ssize_t n = PyList_GET_SIZE(headers);
    *err_status = 0;
    *kind = 0;
    *length = 0;

    for (i = 0; i < n; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        PyObject *name = PyTuple_GET_ITEM(pair, 0);
        PyObject *value = PyTuple_GET_ITEM(pair, 1);
        const char *nd = PyBytes_AS_STRING(name);
        Py_ssize_t ns = PyBytes_GET_SIZE(name);
        if (ns == 14 && memcmp(nd, "content-length", 14) == 0) {
            const char *vd = PyBytes_AS_STRING(value);
            Py_ssize_t vs = PyBytes_GET_SIZE(value);
            PyObject *trimmed;
            while (vs > 0 && (vd[0] == ' ' || vd[0] == '\t')) { vd++; vs--; }
            while (vs > 0 && (vd[vs - 1] == ' ' || vd[vs - 1] == '\t')) { vs--; }
            trimmed = PyBytes_FromStringAndSize(vd, vs);
            if (trimmed == NULL) {
                Py_XDECREF(first_length);
                return -1;
            }
            if (first_length == NULL) {
                first_length = trimmed;
            }
            else {
                int equal = PyObject_RichCompareBool(first_length, trimmed, Py_EQ);
                Py_DECREF(trimmed);
                if (equal < 0) {
                    Py_DECREF(first_length);
                    return -1;
                }
                if (equal == 0) {
                    Py_DECREF(first_length);
                    *err_status = 400;
                    return 0;
                }
            }
        }
        else if (ns == 17 && memcmp(nd, "transfer-encoding", 17) == 0) {
            const char *vd = PyBytes_AS_STRING(value);
            Py_ssize_t vs = PyBytes_GET_SIZE(value);
            Py_ssize_t start = 0;
            saw_transfer = 1;
            while (start <= vs) {
                Py_ssize_t end = start;
                const char *part;
                Py_ssize_t part_size;
                while (end < vs && vd[end] != ',') { end++; }
                part = vd + start;
                part_size = end - start;
                while (part_size > 0 && (part[0] == ' ' || part[0] == '\t')) {
                    part++;
                    part_size--;
                }
                while (part_size > 0 &&
                       (part[part_size - 1] == ' ' || part[part_size - 1] == '\t')) {
                    part_size--;
                }
                if (part_size != 7 || PyOS_strnicmp(part, "chunked", 7) != 0) {
                    Py_XDECREF(first_length);
                    *err_status = 400;
                    return 0;
                }
                chunked_count++;
                if (end == vs) {
                    break;
                }
                start = end + 1;
            }
        }
    }

    if (saw_transfer && first_length != NULL) {
        Py_DECREF(first_length);
        *err_status = 400;
        return 0;
    }
    if (saw_transfer) {
        if (chunked_count != 1) {
            *err_status = 400;
            return 0;
        }
        *kind = 2;
        return 0;
    }
    if (first_length != NULL) {
        const char *d = PyBytes_AS_STRING(first_length);
        Py_ssize_t s = PyBytes_GET_SIZE(first_length);
        Py_ssize_t value = 0;
        if (s == 0) {
            Py_DECREF(first_length);
            *err_status = 400;
            return 0;
        }
        for (i = 0; i < s; i++) {
            int digit = (unsigned char)d[i] - '0';
            if (digit < 0 || digit > 9 || value > (PY_SSIZE_T_MAX - digit) / 10) {
                Py_DECREF(first_length);
                *err_status = 400;
                return 0;
            }
            value = value * 10 + digit;
        }
        Py_DECREF(first_length);
        if (value > self->max_body_bytes) {
            *err_status = 413;
            return 0;
        }
        *kind = 1;
        *length = value;
        return 0;
    }
    return 0;
}


static void
reset_response_state(WreathHttpProtocol *self, PyObject *headers)
{
    Py_ssize_t i;
    Py_ssize_t n = PyList_GET_SIZE(headers);
    const char *conn = NULL;
    Py_ssize_t conn_size = 0;

    self->response_started = 0;
    self->response_complete = 0;
    self->response_body_sent = 0;
    self->resp_has_length = 0;
    self->resp_content_length = 0;
    self->response_chunked = 0;
    self->response_suppress_body = self->method_is_head;
    self->head_written = 0;
    self->resp_status = 200;
    clear_response_builder(self);
    self->out_len = 0;
    self->framing_error = 0;

    for (i = 0; i < n; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        PyObject *name = PyTuple_GET_ITEM(pair, 0);
        if (PyBytes_GET_SIZE(name) == 10 &&
            memcmp(PyBytes_AS_STRING(name), "connection", 10) == 0) {
            PyObject *value = PyTuple_GET_ITEM(pair, 1);
            conn = PyBytes_AS_STRING(value);
            conn_size = PyBytes_GET_SIZE(value);
            break;
        }
    }
    if (self->http11) {
        self->response_keep_alive = !(conn && contains_ci(conn, conn_size, "close"));
    }
    else {
        self->response_keep_alive = conn && contains_ci(conn, conn_size, "keep-alive");
    }
}


static PyObject *
build_scope(WreathHttpProtocol *self, PyObject *method, PyObject *path, PyObject *raw_path,
            PyObject *query_string, PyObject *headers)
{
    PyObject *scope;
    PyObject *version = self->http11 ? self->http_version_11 : self->http_version_10;

    if (self->native_app != NULL) {
        PyObject *context = wreath_request_context_new(
            self->scope_type, self->asgi_metadata, version, method, self->scheme,
            path, raw_path, query_string, headers, self->server_address,
            self->client_address, self->root_path
        );
        if (context != NULL && self->nfr_worker != NULL) {
            /* Let dispatch stamp the matched route onto this request's recorder
             * context; the pointer stays valid for the request's lifetime. */
            wreath_request_context_set_flight(context, &self->nfr_ctx,
                                              self->nfr_worker);
        }
        return context;
    }
    scope = PyDict_New();
    if (scope == NULL) return NULL;
    if (PyDict_SetItem(scope, s_type, self->scope_type) < 0 ||
        PyDict_SetItem(scope, k_asgi, self->asgi_metadata) < 0 ||
        PyDict_SetItem(scope, k_http_version, version) < 0 ||
        PyDict_SetItem(scope, k_method, method) < 0 ||
        PyDict_SetItem(scope, k_scheme, self->scheme) < 0 ||
        PyDict_SetItem(scope, k_path, path) < 0 ||
        PyDict_SetItem(scope, k_raw_path, raw_path) < 0 ||
        PyDict_SetItem(scope, k_query_string, query_string) < 0 ||
        PyDict_SetItem(scope, s_headers, headers) < 0 ||
        PyDict_SetItem(scope, k_server, self->server_address) < 0 ||
        PyDict_SetItem(scope, k_client, self->client_address) < 0 ||
        PyDict_SetItem(scope, k_root_path, self->root_path) < 0 ||
        PyDict_SetItem(scope, k_extensions, extensions_dict) < 0) {
        Py_DECREF(scope);
        return NULL;
    }
    return scope;
}


static int
spawn_app_task(WreathHttpProtocol *self, PyObject *scope)
{
    PyObject *coro = NULL;
    PyObject *task = NULL;
    PyObject *ignored = NULL;
    int result = -1;

    PyObject *app_args[3] = {scope, self->receive_callable, self->send_callable};
    coro = PyObject_Vectorcall(
        self->native_app != NULL && wreath_request_context_check(scope)
            ? self->native_app : self->app,
        app_args, 3, NULL
    );
    if (coro == NULL) {
        goto done;
    }
    /* Eager start: the first coroutine step runs here, synchronously.  A
     * handler whose awaits all resolve immediately (our receive/send
     * awaitables) completes without ever being scheduled on the loop; only
     * genuinely suspending applications pay for scheduling.  Completion
     * handling still goes through add_done_callback, so a finished task
     * unwinds via call_soon exactly as before. */
    {
        PyObject *task_args[3] = {coro, self->loop, Py_True};
        task = PyObject_Vectorcall(task_class, task_args, 1, task_kwnames);
    }
    if (task == NULL) {
        goto done;
    }
    /* Eagerly completed tasks are finalized inline: the coroutine has fully
     * unwound by the time Task() returns, so nothing is on the stack that a
     * deferred done-callback would have protected.  This skips the
     * add_done_callback + call_soon round-trip per request.  Buffered
     * pipelined bytes stay untouched: the outer run_drive() loop that led
     * here resumes parsing them once the state machine is reset.
     *
     * Completion is detected by asking for the task's exception directly
     * rather than probing Task.done() first: a still-pending task raises
     * InvalidStateError, while a finished one hands back its exception (or
     * None) that apply_app_outcome consumes.  The synchronous fast path thus
     * crosses into Python once here, not once to probe and again to finalize. */
    {
        PyObject *exc_args[1] = {task};
        PyObject *exc = PyObject_Vectorcall(task_exception_fn, exc_args, 1, NULL);
        if (exc != NULL) {
            if (apply_app_outcome(self, exc, 0) < 0) {
                goto done;
            }
            result = 0;
            goto done;
        }
        if (!PyErr_ExceptionMatches(invalid_state_error)) {
            /* Done and cancelled: exception() raised CancelledError. */
            PyErr_Clear();
            if (apply_app_outcome(self, NULL, 0) < 0) {
                goto done;
            }
            result = 0;
            goto done;
        }
        /* Still pending: the application genuinely suspended. */
        PyErr_Clear();
    }
    {
        PyObject *cb_args[2] = {task, self->done_callable};
        ignored = PyObject_Vectorcall(task_add_done_callback, cb_args, 2, NULL);
    }
    if (ignored == NULL) {
        goto done;
    }
    Py_XSETREF(self->task, task);
    task = NULL;
    result = 0;
done:
    Py_XDECREF(coro);
    Py_XDECREF(task);
    Py_XDECREF(ignored);
    return result;
}


static int
is_upgrade_request(PyObject *headers)
{
    PyObject *upgrade = NULL;
    PyObject *connection = NULL;
    Py_ssize_t n = PyList_GET_SIZE(headers);
    const char *data;
    Py_ssize_t size;

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        PyObject *name = PyTuple_GET_ITEM(pair, 0);
        Py_ssize_t name_size = PyBytes_GET_SIZE(name);
        const char *name_data = PyBytes_AS_STRING(name);
        if (upgrade == NULL && name_size == 7 && memcmp(name_data, "upgrade", 7) == 0) {
            upgrade = PyTuple_GET_ITEM(pair, 1);
        }
        else if (connection == NULL && name_size == 10 &&
                 memcmp(name_data, "connection", 10) == 0) {
            connection = PyTuple_GET_ITEM(pair, 1);
        }
    }
    if (upgrade == NULL || connection == NULL) {
        return 0;
    }
    data = PyBytes_AS_STRING(upgrade);
    size = PyBytes_GET_SIZE(upgrade);
    while (size > 0 && (data[0] == ' ' || data[0] == '\t')) { data++; size--; }
    while (size > 0 && (data[size - 1] == ' ' || data[size - 1] == '\t')) { size--; }
    if (size != 9 || PyOS_strnicmp(data, "websocket", 9) != 0) {
        return 0;
    }
    return contains_ci(PyBytes_AS_STRING(connection), PyBytes_GET_SIZE(connection),
                       "upgrade");
}


static int
begin_websocket(WreathHttpProtocol *self, PyObject *method, long minor, PyObject *path,
                PyObject *raw_path, PyObject *query_string, PyObject *headers)
{
    PyObject *key = NULL;
    PyObject *version = NULL;
    PyObject *protocols = NULL;
    PyObject *scope = NULL;
    PyObject *scheme;
    PyObject *connect_msg = NULL;
    PyObject *queue = NULL;
    Py_ssize_t n = PyList_GET_SIZE(headers);
    int result = -1;

    protocols = PyList_New(0);
    if (protocols == NULL) {
        return -1;
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        PyObject *name = PyTuple_GET_ITEM(pair, 0);
        PyObject *value = PyTuple_GET_ITEM(pair, 1);
        Py_ssize_t name_size = PyBytes_GET_SIZE(name);
        const char *name_data = PyBytes_AS_STRING(name);
        if (key == NULL && name_size == 17 &&
            memcmp(name_data, "sec-websocket-key", 17) == 0) {
            key = value;
        }
        else if (version == NULL && name_size == 21 &&
                 memcmp(name_data, "sec-websocket-version", 21) == 0) {
            version = value;
        }
        else if (name_size == 22 && memcmp(name_data, "sec-websocket-protocol", 22) == 0) {
            /* Comma-separated client subprotocol offers, in order. */
            const char *vd = PyBytes_AS_STRING(value);
            Py_ssize_t vs = PyBytes_GET_SIZE(value);
            Py_ssize_t start = 0;
            while (start <= vs) {
                Py_ssize_t end = start;
                const char *part;
                Py_ssize_t part_size;
                while (end < vs && vd[end] != ',') { end++; }
                part = vd + start;
                part_size = end - start;
                while (part_size > 0 && (part[0] == ' ' || part[0] == '\t')) {
                    part++;
                    part_size--;
                }
                while (part_size > 0 &&
                       (part[part_size - 1] == ' ' || part[part_size - 1] == '\t')) {
                    part_size--;
                }
                if (part_size > 0) {
                    PyObject *proto = PyUnicode_DecodeLatin1(part, part_size, NULL);
                    if (proto == NULL || PyList_Append(protocols, proto) < 0) {
                        Py_XDECREF(proto);
                        goto done;
                    }
                    Py_DECREF(proto);
                }
                if (end == vs) {
                    break;
                }
                start = end + 1;
            }
        }
    }

    {
        const char *kd;
        Py_ssize_t ks = 0;
        if (key != NULL) {
            kd = PyBytes_AS_STRING(key);
            ks = PyBytes_GET_SIZE(key);
            while (ks > 0 && (kd[0] == ' ' || kd[0] == '\t')) { kd++; ks--; }
            while (ks > 0 && (kd[ks - 1] == ' ' || kd[ks - 1] == '\t')) { ks--; }
        }
        if (PyUnicode_CompareWithASCIIString(method, "GET") != 0 || minor != 1 ||
            ks == 0) {
            result = send_error(self, 400) < 0 ? -1 : 0;
            goto done;
        }
        {
            PyObject *trimmed = PyBytes_FromStringAndSize(kd, ks);
            if (trimmed == NULL) {
                goto done;
            }
            Py_XSETREF(self->ws_key, trimmed);
        }
    }
    if (version == NULL) {
        result = send_error(self, 426) < 0 ? -1 : 0;
        goto done;
    }
    {
        const char *vd = PyBytes_AS_STRING(version);
        Py_ssize_t vs = PyBytes_GET_SIZE(version);
        while (vs > 0 && (vd[0] == ' ' || vd[0] == '\t')) { vd++; vs--; }
        while (vs > 0 && (vd[vs - 1] == ' ' || vd[vs - 1] == '\t')) { vs--; }
        if (vs != 2 || memcmp(vd, "13", 2) != 0) {
            result = send_error(self, 426) < 0 ? -1 : 0;
            goto done;
        }
    }

    scheme = PyUnicode_CompareWithASCIIString(self->scheme, "https") == 0
                 ? s_wss_scheme
                 : s_ws_scheme;
    scope = PyDict_New();
    if (scope == NULL) {
        goto done;
    }
    if (PyDict_SetItem(scope, s_type, s_websocket) < 0 ||
        PyDict_SetItem(scope, k_asgi, self->asgi_metadata) < 0 ||
        PyDict_SetItem(scope, k_http_version, self->http_version_11) < 0 ||
        PyDict_SetItem(scope, k_scheme, scheme) < 0 ||
        PyDict_SetItem(scope, k_path, path) < 0 ||
        PyDict_SetItem(scope, k_raw_path, raw_path) < 0 ||
        PyDict_SetItem(scope, k_query_string, query_string) < 0 ||
        PyDict_SetItem(scope, s_headers, headers) < 0 ||
        PyDict_SetItem(scope, k_server, self->server_address) < 0 ||
        PyDict_SetItem(scope, k_client, self->client_address) < 0 ||
        PyDict_SetItem(scope, k_root_path, self->root_path) < 0 ||
        PyDict_SetItem(scope, k_subprotocols, protocols) < 0) {
        goto done;
    }

    self->ws_mode = 1;
    self->ws_accepted = 0;
    self->ws_close_sent = 0;
    self->ws_frag_opcode = -1;
    Py_CLEAR(self->ws_frag_buffer);
    self->ws_frag_size = 0;
    self->ws_frag_count = 0;

    connect_msg = PyDict_New();
    queue = PyList_New(0);
    if (connect_msg == NULL || queue == NULL ||
        PyDict_SetItem(connect_msg, s_type, s_ws_connect) < 0 ||
        PyList_Append(queue, connect_msg) < 0) {
        goto done;
    }
    Py_XSETREF(self->receive_queue, queue);
    queue = NULL;
    Py_CLEAR(self->receive_waiter);
    self->receive_head = 0;
    self->queued_messages = 1;  /* the websocket.connect message just queued */
    self->queued_bytes = 0;
    self->disconnected = 0;
    self->pending_empty_request = 0;
    /* The request deadline (armed at head parse) stays live until the
     * application accepts, so an unanswered handshake still times out. */
    self->state = ST_WS_HANDSHAKE;

    /* Begin the recorder context for the whole WebSocket session; it reaches a
     * completion cell in apply_app_outcome (the ws_mode branch) when the app
     * task ends, or an abandon on abrupt connection teardown. The protocol is
     * WEBSOCKET, distinguishing these cells from the HTTP request that carried
     * the handshake. Off / no-telemetry is a not-taken branch. */
    if (self->nfr_worker != NULL) {
        flight_capi->context_start(self->nfr_worker, &self->nfr_ctx,
                                   self->nfr_connection_id, WREATH_NFR_PROTO_WEBSOCKET,
                                   wreath_flight_now_ns());
        self->nfr_active = 1;
        /* Retain the scope and signal dispatch to stamp route/plan attribution
         * into it; apply_app_outcome reads it back before it emits the cell. */
        Py_INCREF(scope);
        Py_XSETREF(self->nfr_ws_scope, scope);
        if (PyDict_SetItemString(scope, "_wreath_flight", Py_None) < 0) {
            PyErr_Clear();
        }
        /* Correlate with an incoming W3C traceparent, if the client sent one. */
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(headers); i++) {
            PyObject *pair = PyList_GET_ITEM(headers, i);
            PyObject *name = PyTuple_GET_ITEM(pair, 0);
            if (PyBytes_GET_SIZE(name) == 11 &&
                memcmp(PyBytes_AS_STRING(name), "traceparent", 11) == 0) {
                PyObject *value = PyTuple_GET_ITEM(pair, 1);
                flight_capi->context_propagate(
                    self->nfr_worker, &self->nfr_ctx,
                    (const uint8_t *)PyBytes_AS_STRING(value), PyBytes_GET_SIZE(value));
                break;
            }
        }
    }

    if (spawn_app_task(self, scope) < 0) {
        goto done;
    }
    result = 1;
done:
    Py_XDECREF(protocols);
    Py_XDECREF(scope);
    Py_XDECREF(connect_msg);
    Py_XDECREF(queue);
    return result;
}


static int
begin_request(WreathHttpProtocol *self, PyObject *method, long minor, PyObject *target,
              PyObject *headers)
{
    const char *td = PyBytes_AS_STRING(target);
    Py_ssize_t ts = PyBytes_GET_SIZE(target);
    Py_ssize_t q = -1;
    PyObject *raw_path = NULL;
    PyObject *query_string = NULL;
    PyObject *path = NULL;
    PyObject *scope = NULL;
    int kind;
    Py_ssize_t length;
    int err_status;
    int bad = 0;
    int result = -1;

    self->http11 = (minor == 1);
    self->method_is_head = PyUnicode_CompareWithASCIIString(method, "HEAD") == 0;

    for (Py_ssize_t i = 0; i < ts; i++) {
        if (td[i] == '?') {
            q = i;
            break;
        }
    }
    if (q < 0) {
        raw_path = PyBytes_FromStringAndSize(td, ts);
        query_string = PyBytes_FromStringAndSize("", 0);
    }
    else {
        raw_path = PyBytes_FromStringAndSize(td, q);
        query_string = PyBytes_FromStringAndSize(td + q + 1, ts - q - 1);
    }
    if (raw_path == NULL || query_string == NULL) {
        goto done;
    }
    path = decode_path(PyBytes_AS_STRING(raw_path), PyBytes_GET_SIZE(raw_path), &bad);
    if (path == NULL) {
        result = bad ? (send_error(self, 400) < 0 ? -1 : 0) : -1;
        goto done;
    }
    if (is_upgrade_request(headers)) {
        result = begin_websocket(self, method, minor, path, raw_path, query_string,
                                 headers);
        goto done;
    }
    if (decide_framing(self, headers, &kind, &length, &err_status) < 0) {
        goto done;
    }
    if (err_status != 0) {
        result = send_error(self, err_status) < 0 ? -1 : 0;
        goto done;
    }
    scope = build_scope(self, method, path, raw_path, query_string, headers);
    if (scope == NULL) {
        goto done;
    }

    reset_response_state(self, headers);
    {
        PyObject *queue = PyList_New(0);
        if (queue == NULL) {
            goto done;
        }
        Py_XSETREF(self->receive_queue, queue);
    }
    Py_CLEAR(self->receive_waiter);
    self->receive_head = 0;
    self->queued_messages = 0;
    self->queued_bytes = 0;
    self->disconnected = 0;
    self->request_more_body = 1;
    self->remaining = 0;
    self->chunk_remaining = 0;

    if (kind == 0) {
        self->request_more_body = 0;
        self->state = ST_REQUEST_RUNNING;
    }
    else if (kind == 1) {
        self->remaining = length;
        self->state = length > 0 ? ST_READING_FIXED_BODY : ST_REQUEST_RUNNING;
        if (length == 0) {
            self->request_more_body = 0;
        }
    }
    else {
        self->chunk_remaining = 0;
        self->state = ST_READING_CHUNK_SIZE;
    }

    if (self->state == ST_REQUEST_RUNNING) {
        self->pending_empty_request = 1;
    }
    else {
        /* Drain body bytes that already sit in the connection buffer before
         * the application starts.  With eager task execution the app's first
         * receive() can then resolve immediately instead of suspending on a
         * waiter future that the same buffer would resolve moments later. */
        int rc2 = 1;
        while (rc2 == 1 && !self->closing) {
            switch (self->state) {
                case ST_READING_FIXED_BODY: rc2 = drive_fixed_body(self); break;
                case ST_READING_CHUNK_SIZE: rc2 = drive_chunk_size(self); break;
                case ST_READING_CHUNK_DATA: rc2 = drive_chunk_data(self); break;
                case ST_READING_CHUNK_TRAILERS: rc2 = drive_chunk_trailers(self); break;
                default: rc2 = 0; break;
            }
        }
        if (rc2 < 0) {
            goto done;
        }
        if (self->closing) {
            result = 0;  /* a body framing error already sent the response */
            goto done;
        }
    }

    /* Begin the recorder context once the request is committed to running, so
     * it is guaranteed to reach a completion (apply_app_outcome) or an abandon
     * (connection loss). Off / no-telemetry is a not-taken branch. */
    if (self->nfr_worker != NULL) {
        flight_capi->context_start(self->nfr_worker, &self->nfr_ctx,
                                   self->nfr_connection_id, WREATH_NFR_PROTO_HTTP1,
                                   wreath_flight_now_ns());
        self->nfr_active = 1;
        /* Correlate with an incoming W3C traceparent, if the client sent one. */
        for (Py_ssize_t i = 0; i < PyList_GET_SIZE(headers); i++) {
            PyObject *pair = PyList_GET_ITEM(headers, i);
            PyObject *name = PyTuple_GET_ITEM(pair, 0);
            if (PyBytes_GET_SIZE(name) == 11 &&
                memcmp(PyBytes_AS_STRING(name), "traceparent", 11) == 0) {
                PyObject *value = PyTuple_GET_ITEM(pair, 1);
                flight_capi->context_propagate(
                    self->nfr_worker, &self->nfr_ctx,
                    (const uint8_t *)PyBytes_AS_STRING(value), PyBytes_GET_SIZE(value));
                break;
            }
        }
        /* The arming decision (and its scratch slot) is made by context_start,
         * after build_scope attached the recorder context, so the armed state is
         * promoted onto the context here — before dispatch reads `flight`. */
        if (wreath_request_context_check(scope)) {
            wreath_request_context_set_armed(scope);
        }
    }

    if (spawn_app_task(self, scope) < 0) {
        goto done;
    }
    result = 1;  /* continue draining any buffered body */
done:
    Py_XDECREF(raw_path);
    Py_XDECREF(query_string);
    Py_XDECREF(path);
    Py_XDECREF(scope);
    return result;
}


/* --- websocket ------------------------------------------------------------
 *
 * Mirrors the pure reference: the upgrade handshake, the ASGI websocket
 * message flow, automatic ping/pong, fragmented message reassembly, strict
 * UTF-8 text validation, and RFC 6455 close-code rules.  Frame parsing goes
 * through the shared wreath.ws accelerator; outgoing frames serialize into the
 * connection's scratch buffer (large binary payloads pair with the header in
 * one vectored write). */

static int
ws_write_frame(WreathHttpProtocol *self, int opcode, const char *payload, Py_ssize_t size,
               PyObject *payload_obj)
{
    /* payload_obj, when given and large, is written zero-copy after the
     * header via writelines; otherwise the payload is copied inline. */
    uint8_t header[10];
    Py_ssize_t header_len;

    header[0] = (uint8_t)(0x80 | opcode);
    if (size < 126) {
        header[1] = (uint8_t)size;
        header_len = 2;
    }
    else if (size < 65536) {
        header[1] = 126;
        header[2] = (uint8_t)(size >> 8);
        header[3] = (uint8_t)size;
        header_len = 4;
    }
    else {
        uint64_t length = (uint64_t)size;
        header[1] = 127;
        for (int i = 0; i < 8; i++) {
            header[2 + i] = (uint8_t)(length >> (56 - 8 * i));
        }
        header_len = 10;
    }
    if (payload_obj != NULL && size > 16384 && self->transport_writelines_fn != NULL &&
        self->transport != NULL && !self->closing) {
        PyObject *head_bytes = PyBytes_FromStringAndSize((const char *)header, header_len);
        PyObject *pair;
        PyObject *result;
        if (head_bytes == NULL) {
            return -1;
        }
        pair = PyTuple_Pack(2, head_bytes, payload_obj);
        Py_DECREF(head_bytes);
        if (pair == NULL) {
            return -1;
        }
        result = PyObject_CallOneArg(self->transport_writelines_fn, pair);
        Py_DECREF(pair);
        if (result == NULL) {
            return -1;
        }
        Py_DECREF(result);
        return 0;
    }
    self->out_len = 0;
    if (out_append(self, (const char *)header, header_len) < 0 ||
        (size > 0 && out_append(self, payload, size) < 0) ||
        transport_write_raw(self, self->out_buf, self->out_len) < 0) {
        return -1;
    }
    self->out_len = 0;
    return 0;
}


static int
ws_send_close_frame(WreathHttpProtocol *self, int code, const char *reason, Py_ssize_t reason_size)
{
    char payload[125];
    Py_ssize_t size = 0;
    if (code != 0) {
        payload[0] = (char)(code >> 8);
        payload[1] = (char)code;
        size = 2;
        if (reason_size > 123) {
            reason_size = 123;
        }
        if (reason_size > 0) {
            memcpy(payload + 2, reason, (size_t)reason_size);
            size += reason_size;
        }
    }
    if (ws_write_frame(self, WS_OP_CLOSE, payload, size, NULL) < 0) {
        return -1;
    }
    self->ws_close_sent = 1;
    return 0;
}


static PyObject *
make_ws_disconnect_msg(int code)
{
    PyObject *msg = PyDict_New();
    PyObject *code_obj;
    if (msg == NULL) {
        return NULL;
    }
    code_obj = PyLong_FromLong(code);
    if (code_obj == NULL || PyDict_SetItem(msg, s_type, s_ws_disconnect) < 0 ||
        PyDict_SetItem(msg, s_code, code_obj) < 0) {
        Py_XDECREF(code_obj);
        Py_DECREF(msg);
        return NULL;
    }
    Py_DECREF(code_obj);
    return msg;
}


/* Deliver a websocket message to the app: directly into a pending waiter, or
 * queued with read-backpressure accounting (text counts code points, binary
 * counts bytes -- identical to the pure reference). */
static int
ws_enqueue(WreathHttpProtocol *self, PyObject *msg, Py_ssize_t size)
{
    if (self->receive_waiter != NULL) {
        PyObject *waiter = self->receive_waiter;
        self->receive_waiter = NULL;
        if (future_set_result(waiter, msg) < 0) {
            Py_DECREF(waiter);
            return -1;
        }
        Py_DECREF(waiter);
        return 0;
    }
    if (receive_queue_push(self, msg) < 0) {
        return -1;
    }
    self->queued_bytes += size;
    /* A WebSocket message may be zero bytes, so this is the path where only the
     * message-count watermark can apply backpressure. */
    return receive_pressure_pause(self);
}


static int
ws_deliver_disconnect(WreathHttpProtocol *self, int code)
{
    PyObject *msg = make_ws_disconnect_msg(code);
    int rc;
    if (msg == NULL) {
        return -1;
    }
    self->disconnected = 1;
    rc = ws_enqueue(self, msg, 0);
    Py_DECREF(msg);
    return rc;
}


/* Protocol failure: close frame with the given code, disconnect to the app,
 * drop the connection. */
static int
ws_fail(WreathHttpProtocol *self, int code)
{
    /* Release the reassembly accumulator first: this message can never
     * complete, and the connection is going away regardless of what the close
     * handshake below does. */
    self->ws_frag_opcode = -1;
    Py_CLEAR(self->ws_frag_buffer);
    self->ws_frag_size = 0;
    self->ws_frag_count = 0;
    if (!self->ws_close_sent && ws_send_close_frame(self, code, NULL, 0) < 0) {
        return -1;
    }
    if (ws_deliver_disconnect(self, code) < 0) {
        return -1;
    }
    return protocol_close(self);
}


/* Enqueue one completed WebSocket message from its final decoded value: a str
 * for text, an exact bytes for binary. The value is stored as-is, never copied
 * again. */
static int
ws_enqueue_value(WreathHttpProtocol *self, int opcode, PyObject *value)
{
    PyObject *msg = PyDict_New();
    Py_ssize_t size;
    int rc;

    if (msg == NULL || PyDict_SetItem(msg, s_type, s_ws_receive) < 0) {
        Py_XDECREF(msg);
        return -1;
    }
    if (opcode == WS_OP_TEXT) {
        rc = PyDict_SetItem(msg, s_text, value);
        size = PyUnicode_GET_LENGTH(value);
    }
    else {
        rc = PyDict_SetItem(msg, s_bytes, value);
        size = PyBytes_GET_SIZE(value);
    }
    if (rc < 0) {
        Py_DECREF(msg);
        return -1;
    }
    rc = ws_enqueue(self, msg, size);
    Py_DECREF(msg);
    return rc;
}


static int
ws_deliver_message(WreathHttpProtocol *self, int opcode, PyObject *payload)
{
    if (PyBytes_GET_SIZE(payload) > self->max_body_bytes) {
        return ws_fail(self, 1009);
    }
    if (opcode == WS_OP_TEXT) {
        PyObject *text = PyUnicode_DecodeUTF8(PyBytes_AS_STRING(payload),
                                              PyBytes_GET_SIZE(payload), "strict");
        int rc;
        if (text == NULL) {
            if (PyErr_ExceptionMatches(PyExc_UnicodeDecodeError)) {
                PyErr_Clear();
                return ws_fail(self, 1007);
            }
            return -1;
        }
        rc = ws_enqueue_value(self, opcode, text);
        Py_DECREF(text);
        return rc;
    }
    return ws_enqueue_value(self, opcode, payload);
}


/* Parse and dispatch one frame; same tri-state returns as the HTTP drivers:
 * 1 keep driving, 0 need more data (or connection closed), -1 error. */
static int
drive_ws_frame(WreathHttpProtocol *self)
{
    const uint8_t *p = (const uint8_t *)(self->buf + self->cursor);
    Py_ssize_t n = self->buf_len - self->cursor;
    WreathWsFrameHeader header;
    PyObject *payload;
    int opcode;
    int fin;
    int rc;

    if (n < 2) {
        return 0;
    }
    rc = core_capi->ws_parse_header(p, n, &header);
    if (rc < 0) {
        return ws_fail(self, 1002) < 0 ? -1 : 0;
    }
    if (rc == 1 || n - header.header_len < header.payload_len) {
        /* Incomplete frame: wait for more bytes, within the size limit. */
        if (n > self->max_body_bytes + 14) {
            return ws_fail(self, 1009) < 0 ? -1 : 0;
        }
        return 0;
    }
    if (!header.masked) {
        /* Clients must mask every frame (RFC 6455 5.1). */
        return ws_fail(self, 1002) < 0 ? -1 : 0;
    }
    /* Size limits are enforced per message on delivery (and per buffered
     * fragment), matching the pure reference's order of checks. */
    fin = header.fin;
    opcode = header.opcode;
    payload = PyBytes_FromStringAndSize(NULL, header.payload_len);
    if (payload == NULL) {
        return -1;
    }
    core_capi->ws_unmask((uint8_t *)PyBytes_AS_STRING(payload), p + header.header_len,
                         header.payload_len, header.mask_key);
    do_consume(self, header.header_len + header.payload_len);
    /* Ingress payload bytes for this session's completion cell (all opcodes,
     * matching the HTTP body convention of counting application data, not the
     * frame headers/mask). Accumulated only while telemetry is on. */
    if (self->nfr_worker != NULL) {
        self->nfr_bytes_in += (uint64_t)header.payload_len;
    }

    if (opcode == WS_OP_CLOSE || opcode == WS_OP_PING || opcode == WS_OP_PONG) {
        Py_ssize_t size = PyBytes_GET_SIZE(payload);
        if (!fin || size > 125) {
            Py_DECREF(payload);
            return ws_fail(self, 1002) < 0 ? -1 : 0;
        }
        if (opcode == WS_OP_PING) {
            int rc = 0;
            if (!self->ws_close_sent) {
                rc = ws_write_frame(self, WS_OP_PONG, PyBytes_AS_STRING(payload), size,
                                    NULL);
            }
            Py_DECREF(payload);
            return rc < 0 ? -1 : 1;
        }
        if (opcode == WS_OP_PONG) {
            Py_DECREF(payload);
            return 1;
        }
        /* Close frame. */
        {
            int code = 1005;
            const char *data = PyBytes_AS_STRING(payload);
            if (size >= 2) {
                code = ((uint8_t)data[0] << 8) | (uint8_t)data[1];
                if (code < 1000 || code == 1004 || code == 1005 || code == 1006 ||
                    (code >= 1015 && code < 3000)) {
                    Py_DECREF(payload);
                    return ws_fail(self, 1002) < 0 ? -1 : 0;
                }
                if (size > 2) {
                    PyObject *reason = PyUnicode_DecodeUTF8(data + 2, size - 2, "strict");
                    if (reason == NULL) {
                        Py_DECREF(payload);
                        if (PyErr_ExceptionMatches(PyExc_UnicodeDecodeError)) {
                            PyErr_Clear();
                            return ws_fail(self, 1007) < 0 ? -1 : 0;
                        }
                        return -1;
                    }
                    Py_DECREF(reason);
                }
            }
            else if (size == 1) {
                Py_DECREF(payload);
                return ws_fail(self, 1002) < 0 ? -1 : 0;
            }
            Py_DECREF(payload);
            if (!self->ws_close_sent &&
                ws_send_close_frame(self, code == 1005 ? 1000 : code, NULL, 0) < 0) {
                return -1;
            }
            if (ws_deliver_disconnect(self, code) < 0) {
                return -1;
            }
            return protocol_close(self) < 0 ? -1 : 0;
        }
    }

    if (opcode == WS_OP_TEXT || opcode == WS_OP_BINARY) {
        int rc;
        if (self->ws_frag_opcode != -1) {
            Py_DECREF(payload);
            return ws_fail(self, 1002) < 0 ? -1 : 0;
        }
        if (!fin) {
            /* Start the accumulator sized to this payload. Fragments are stored
             * as bytes in one buffer, so the retained object count no longer
             * grows with the number of frames. */
            Py_ssize_t len = PyBytes_GET_SIZE(payload);
            PyObject *buffer;
            if (len > self->max_body_bytes) {
                Py_DECREF(payload);
                return ws_fail(self, 1009) < 0 ? -1 : 0;
            }
            buffer = PyByteArray_FromStringAndSize(PyBytes_AS_STRING(payload), len);
            Py_DECREF(payload);
            if (buffer == NULL) {
                return -1;
            }
            Py_XSETREF(self->ws_frag_buffer, buffer);
            self->ws_frag_opcode = opcode;
            self->ws_frag_size = len;
            self->ws_frag_count = 1;
            return 1;
        }
        rc = ws_deliver_message(self, opcode, payload);
        Py_DECREF(payload);
        return rc < 0 ? -1 : (self->closing ? 0 : 1);
    }
    if (opcode == WS_OP_CONT) {
        int rc;
        Py_ssize_t len;
        if (self->ws_frag_opcode == -1) {
            Py_DECREF(payload);
            return ws_fail(self, 1002) < 0 ? -1 : 0;
        }
        len = PyBytes_GET_SIZE(payload);
        /* Count every continuation, empty ones included: an empty fragment adds
         * no bytes but still costs a parse and an unmask, so max_body_bytes
         * alone leaves the per-message work unbounded. */
        self->ws_frag_count += 1;
        if (self->ws_frag_count > self->max_ws_fragments) {
            Py_DECREF(payload);
            return ws_fail(self, 1009) < 0 ? -1 : 0;
        }
        /* Check the bound before growing, so an over-limit message is refused
         * without the accumulator ever holding it. ws_frag_size never exceeds
         * max_body_bytes, so the subtraction cannot go negative. */
        if (len > self->max_body_bytes - self->ws_frag_size) {
            Py_DECREF(payload);
            return ws_fail(self, 1009) < 0 ? -1 : 0;
        }
        if (len > 0) {
            Py_ssize_t offset = PyByteArray_GET_SIZE(self->ws_frag_buffer);
            if (PyByteArray_Resize(self->ws_frag_buffer, offset + len) < 0) {
                Py_DECREF(payload);
                return -1;
            }
            memcpy(PyByteArray_AS_STRING(self->ws_frag_buffer) + offset,
                   PyBytes_AS_STRING(payload), (size_t)len);
            self->ws_frag_size += len;  /* only after a successful append */
        }
        /* An empty continuation resizes and copies nothing. */
        Py_DECREF(payload);
        if (!fin) {
            return 1;
        }
        {
            /* Build the one final object straight from the accumulator: text
             * decodes from the buffer, binary copies it once. Neither path
             * builds an intermediate concatenation. */
            int frag_opcode = self->ws_frag_opcode;
            const char *data = PyByteArray_AS_STRING(self->ws_frag_buffer);
            Py_ssize_t size = PyByteArray_GET_SIZE(self->ws_frag_buffer);
            PyObject *final = frag_opcode == WS_OP_TEXT
                ? PyUnicode_DecodeUTF8(data, size, "strict")
                : PyBytes_FromStringAndSize(data, size);
            /* Reset fragment state before delivering the completed message. */
            self->ws_frag_opcode = -1;
            Py_CLEAR(self->ws_frag_buffer);
            self->ws_frag_size = 0;
            self->ws_frag_count = 0;
            if (final == NULL) {
                if (PyErr_ExceptionMatches(PyExc_UnicodeDecodeError)) {
                    PyErr_Clear();
                    return ws_fail(self, 1007) < 0 ? -1 : 0;
                }
                return -1;
            }
            rc = ws_enqueue_value(self, frag_opcode, final);
            Py_DECREF(final);
            return rc < 0 ? -1 : (self->closing ? 0 : 1);
        }
    }
    Py_DECREF(payload);
    return ws_fail(self, 1002) < 0 ? -1 : 0;  /* reserved data opcode */
}


/* --- state machine drivers ----------------------------------------------- */

static int
drive_head(WreathHttpProtocol *self)
{
    const char *p = self->buf + self->cursor;
    Py_ssize_t n = self->buf_len - self->cursor;
    /* Resume both searches where they left off: a byte-at-a-time peer would
     * otherwise make every arrival rescan the whole buffered head. */
    Py_ssize_t head_end = find_sub_from(p, n, "\r\n\r\n", 4,
                                        &self->head_terminator_scan);
    Py_ssize_t line_end;
    PyObject *method = NULL;
    PyObject *target = NULL;
    PyObject *headers = NULL;
    Py_ssize_t consumed = 0;
    int minor = 0;
    int parsed;
    int rc;

    if (head_end < 0) {
        line_end = find_sub_from(p, n, "\r\n", 2, &self->request_line_scan);
        if (line_end < 0) {
            if (n > self->max_request_line)
                return send_error(self, 414) < 0 ? -1 : 0;
            return 0;
        }
        if (line_end > self->max_request_line)
            return send_error(self, 414) < 0 ? -1 : 0;
        if (n > self->max_header_bytes)
            return send_error(self, 431) < 0 ? -1 : 0;
        return 0;
    }
    if (head_end + 4 > self->max_header_bytes)
        return send_error(self, 431) < 0 ? -1 : 0;
    line_end = find_sub_from(p, n, "\r\n", 2, &self->request_line_scan);
    if (line_end > self->max_request_line)
        return send_error(self, 414) < 0 ? -1 : 0;

    parsed = wreath_http_parse_request_parts(
        (const uint8_t *)p, n, head_end, &method, &target, &minor, &headers,
        &consumed
    );
    if (parsed < 0) {
        if (PyErr_ExceptionMatches(PyExc_ValueError)) {
            PyErr_Clear();
            return send_error(self, 400) < 0 ? -1 : 0;
        }
        return -1;
    }
    if (parsed == 0) return 0;
    if (PyList_GET_SIZE(headers) > self->max_header_count) {
        Py_DECREF(method);
        Py_DECREF(target);
        Py_DECREF(headers);
        return send_error(self, 431) < 0 ? -1 : 0;
    }
    do_consume(self, consumed);
    if (set_deadline(self, self->request_timeout, 1) < 0) {
        Py_DECREF(method);
        Py_DECREF(target);
        Py_DECREF(headers);
        return -1;
    }
    rc = begin_request(self, method, minor, target, headers);
    Py_DECREF(method);
    Py_DECREF(target);
    Py_DECREF(headers);
    return rc;
}


static int
drive_fixed_body(WreathHttpProtocol *self)
{
    const char *p = self->buf + self->cursor;
    Py_ssize_t n = self->buf_len - self->cursor;
    Py_ssize_t take;
    int more;
    PyObject *chunk;

    if (n == 0) {
        return 0;
    }
    take = self->remaining < n ? self->remaining : n;
    chunk = PyBytes_FromStringAndSize(p, take);
    if (chunk == NULL) {
        return -1;
    }
    do_consume(self, take);
    self->remaining -= take;
    more = self->remaining > 0;
    if (enqueue_body(self, chunk, more) < 0) {
        Py_DECREF(chunk);
        return -1;
    }
    Py_DECREF(chunk);
    if (!more) {
        self->state = ST_REQUEST_RUNNING;
        return 0;
    }
    return 1;
}


static int
parse_hex(const char *data, Py_ssize_t size, Py_ssize_t *out)
{
    Py_ssize_t value = 0;
    if (size == 0) {
        return -1;
    }
    for (Py_ssize_t i = 0; i < size; i++) {
        unsigned char c = (unsigned char)data[i];
        int v;
        if (c >= '0' && c <= '9') v = c - '0';
        else if (c >= 'a' && c <= 'f') v = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') v = c - 'A' + 10;
        else return -1;
        if (value > (PY_SSIZE_T_MAX - v) / 16) {
            return -1;
        }
        value = value * 16 + v;
    }
    *out = value;
    return 0;
}


static int
drive_chunk_size(WreathHttpProtocol *self)
{
    const char *p = self->buf + self->cursor;
    Py_ssize_t n = self->buf_len - self->cursor;
    Py_ssize_t line_end = find_sub_from(p, n, "\r\n", 2, &self->chunk_line_scan);
    const char *field;
    Py_ssize_t field_size;
    Py_ssize_t chunk_size;

    if (line_end < 0) {
        if (n > self->max_request_line) {
            return body_error(self, 400) < 0 ? -1 : 0;
        }
        return 0;
    }
    field = p;
    field_size = line_end;
    for (Py_ssize_t i = 0; i < field_size; i++) {
        if (field[i] == ';') {  /* chunk extensions are ignored */
            field_size = i;
            break;
        }
    }
    while (field_size > 0 && (field[0] == ' ' || field[0] == '\t')) { field++; field_size--; }
    while (field_size > 0 && (field[field_size - 1] == ' ' || field[field_size - 1] == '\t')) {
        field_size--;
    }
    if (parse_hex(field, field_size, &chunk_size) < 0) {
        return body_error(self, 400) < 0 ? -1 : 0;
    }
    do_consume(self, line_end + 2);
    if (chunk_size == 0) {
        self->state = ST_READING_CHUNK_TRAILERS;
        return 1;
    }
    if (chunk_size > self->max_body_bytes ||
        self->queued_bytes > self->max_body_bytes - chunk_size) {
        return body_error(self, 413) < 0 ? -1 : 0;
    }
    self->chunk_remaining = chunk_size;
    self->state = ST_READING_CHUNK_DATA;
    return 1;
}


static int
drive_chunk_data(WreathHttpProtocol *self)
{
    const char *p = self->buf + self->cursor;
    Py_ssize_t n = self->buf_len - self->cursor;
    Py_ssize_t take;
    PyObject *chunk;

    if (n == 0) {
        return 0;
    }
    take = self->chunk_remaining < n ? self->chunk_remaining : n;
    chunk = PyBytes_FromStringAndSize(p, take);
    if (chunk == NULL) {
        return -1;
    }
    do_consume(self, take);
    self->chunk_remaining -= take;
    if (take > 0) {
        if (enqueue_body(self, chunk, 1) < 0) {
            Py_DECREF(chunk);
            return -1;
        }
    }
    Py_DECREF(chunk);
    if (self->chunk_remaining == 0) {
        const char *q = self->buf + self->cursor;
        Py_ssize_t m = self->buf_len - self->cursor;
        if (m < 2) {
            return 0;
        }
        if (q[0] != '\r' || q[1] != '\n') {
            return body_error(self, 400) < 0 ? -1 : 0;
        }
        do_consume(self, 2);
        self->state = ST_READING_CHUNK_SIZE;
    }
    return 1;
}


static int
drive_chunk_trailers(WreathHttpProtocol *self)
{
    const char *p = self->buf + self->cursor;
    Py_ssize_t n = self->buf_len - self->cursor;
    PyObject *empty;

    /* An empty trailer section is exactly CRLF. Testing the two leading bytes
     * decides that directly; the previous first-CRLF search existed only to
     * detect it, and rescanned the whole buffered block on every arrival. */
    if (n >= 2 && p[0] == '\r' && p[1] == '\n') {
        do_consume(self, 2);
    }
    else {
        /* Otherwise the section ends at the blank line. One resumable scan. */
        Py_ssize_t end = find_sub_from(p, n, "\r\n\r\n", 4,
                                       &self->trailer_terminator_scan);
        if (end < 0) {
            if (n > self->max_header_bytes) {
                return body_error(self, 431) < 0 ? -1 : 0;
            }
            return 0;
        }
        do_consume(self, end + 4);
    }
    empty = PyBytes_FromStringAndSize("", 0);
    if (empty == NULL) {
        return -1;
    }
    if (enqueue_body(self, empty, 0) < 0) {
        Py_DECREF(empty);
        return -1;
    }
    Py_DECREF(empty);
    self->state = ST_REQUEST_RUNNING;
    return 0;
}


static int
run_drive(WreathHttpProtocol *self)
{
    while (!self->closing) {
        int rc;
        switch (self->state) {
            case ST_READING_HEAD: rc = drive_head(self); break;
            case ST_READING_FIXED_BODY: rc = drive_fixed_body(self); break;
            case ST_READING_CHUNK_SIZE: rc = drive_chunk_size(self); break;
            case ST_READING_CHUNK_DATA: rc = drive_chunk_data(self); break;
            case ST_READING_CHUNK_TRAILERS: rc = drive_chunk_trailers(self); break;
            case ST_WS_OPEN: rc = drive_ws_frame(self); break;
            default: return 0;  /* REQUEST_RUNNING / WS_HANDSHAKE / CLOSING */
        }
        if (rc < 0) {
            return -1;
        }
        if (rc == 0) {
            return 0;
        }
    }
    return 0;
}


static int
reset_request(WreathHttpProtocol *self)
{
    Py_ssize_t queued = PyList_GET_SIZE(self->receive_queue);
    if (queued > 0 && PyList_SetSlice(self->receive_queue, 0, queued, NULL) < 0) {
        return -1;
    }
    Py_CLEAR(self->receive_waiter);
    self->pending_empty_request = 0;
    self->receive_head = 0;
    self->queued_messages = 0;
    self->queued_bytes = 0;
    self->remaining = 0;
    self->chunk_remaining = 0;
    self->nfr_bytes_in = 0;
    self->nfr_bytes_out = 0;
    if (self->reading_paused) {
        if (transport_method0(self, "resume_reading") < 0) {
            return -1;
        }
        self->reading_paused = 0;
    }
    return 0;
}


/* --- asyncio.Protocol callbacks ------------------------------------------ */

static PyObject *
http_connection_made(WreathHttpProtocol *self, PyObject *transport)
{
    PyObject *sockname = NULL;
    PyObject *peername = NULL;
    PyObject *sslctx = NULL;
    PyObject *server = NULL;
    PyObject *client = NULL;
    PyObject *scheme = NULL;
    PyObject *write_fn = NULL;

    if (self->nfr_worker != NULL) {
        self->nfr_connection_id = wreath_flight_next_connection_id();
    }
    Py_INCREF(transport);
    Py_XSETREF(self->transport, transport);
    write_fn = PyObject_GetAttrString(transport, "write");
    {
        PyObject *writelines_fn = PyObject_GetAttrString(transport, "writelines");
        if (writelines_fn == NULL) {
            PyErr_Clear();  /* optional: large responses fall back to write() */
        }
        Py_XSETREF(self->transport_writelines_fn, writelines_fn);
    }
    sockname = get_extra_info(self, "sockname");
    peername = get_extra_info(self, "peername");
    sslctx = get_extra_info(self, "sslcontext");
    if (write_fn == NULL || sockname == NULL || peername == NULL || sslctx == NULL) {
        goto error;
    }
    server = address_tuple(sockname);
    client = address_tuple(peername);
    scheme = PyUnicode_FromString(sslctx != Py_None ? "https" : "http");
    if (server == NULL || client == NULL || scheme == NULL) {
        goto error;
    }
    Py_XSETREF(self->transport_write_fn, write_fn);
    Py_XSETREF(self->server_address, server);
    Py_XSETREF(self->client_address, client);
    Py_XSETREF(self->scheme, scheme);
    write_fn = server = client = scheme = NULL;
    Py_DECREF(sockname);
    Py_DECREF(peername);
    Py_DECREF(sslctx);
    if (PySet_Add(self->registry, (PyObject *)self) < 0) {
        return NULL;
    }
    if (set_deadline(self, self->keep_alive_timeout, 0) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;

error:
    Py_XDECREF(write_fn);
    Py_XDECREF(sockname);
    Py_XDECREF(peername);
    Py_XDECREF(sslctx);
    Py_XDECREF(server);
    Py_XDECREF(client);
    Py_XDECREF(scheme);
    return NULL;
}


/* Compatibility ingestion path. Production asyncio socket transports use the
 * BufferedProtocol pair get_buffer()/buffer_updated() below and never copy;
 * this remains for direct test harnesses and delegating transports (e.g.
 * NegotiatingHttpProtocol) that feed bytes objects. */
static PyObject *
http_data_received(WreathHttpProtocol *self, PyObject *data)
{
    Py_buffer view;
    if (self->closing) {
        Py_RETURN_NONE;
    }
    if (self->read_offer_size > 0 || self->read_exports > 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "data_received() while a buffered read is outstanding");
        return NULL;
    }
    apply_deferred_compaction(self);
    if (PyObject_GetBuffer(data, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    if (buf_reserve(self, view.len) < 0) {
        PyBuffer_Release(&view);
        return NULL;
    }
    if (view.len > 0) {
        memcpy(self->buf + self->buf_len, view.buf, (size_t)view.len);
        self->buf_len += view.len;
    }
    PyBuffer_Release(&view);
    if (run_drive(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


/* --- asyncio.BufferedProtocol receive path -------------------------------- */

/* Bounded per-read receive target. asyncio passes sizehint=-1; smaller
 * positive hints are honored, larger ones are capped (the transport simply
 * performs another read cycle). Private until measurements justify config. */
#define WREATH_RECV_CHUNK 65536

/* Buffer-protocol exporter backing get_buffer(). The exported region is only
 * the unused writable tail buf[buf_len:buf_cap] recorded by the active offer;
 * consumed or already-filled bytes are never exposed. Py_buffer.obj retains
 * the protocol, so the allocation outlives every view of it. */
static int
http_protocol_getbuffer(WreathHttpProtocol *self, Py_buffer *view, int flags)
{
    if (self->read_offer_size <= 0) {
        PyErr_SetString(PyExc_BufferError,
                        "no active receive offer; obtain buffers via get_buffer()");
        view->obj = NULL;
        return -1;
    }
    if (PyBuffer_FillInfo(view, (PyObject *)self,
                          self->buf + self->read_offer_offset,
                          self->read_offer_size, 0 /* writable */, flags) < 0) {
        return -1;
    }
    self->read_exports++;
    return 0;
}


static void
http_protocol_releasebuffer(WreathHttpProtocol *self, Py_buffer *Py_UNUSED(view))
{
    if (--self->read_exports <= 0) {
        self->read_exports = 0;
        /* The transport dropped its last view. If it never called
         * buffer_updated() (recv raised, or read zero bytes), the offer is
         * abandoned; clearing it lets the next get_buffer() succeed. */
        self->read_offer_size = 0;
        self->read_offer_offset = 0;
    }
}


static PyObject *
http_get_buffer(WreathHttpProtocol *self, PyObject *arg)
{
    Py_ssize_t sizehint;
    Py_ssize_t target;
    PyObject *view;

    sizehint = PyLong_AsSsize_t(arg);
    if (sizehint == -1 && PyErr_Occurred()) {
        return NULL;
    }
    if (self->read_offer_size > 0 || self->read_exports > 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "get_buffer() called while a previous read offer is live");
        return NULL;
    }
    apply_deferred_compaction(self);
    target = (sizehint > 0 && sizehint < WREATH_RECV_CHUNK) ? sizehint : WREATH_RECV_CHUNK;
    if (buf_reserve(self, target) < 0) {
        return NULL;
    }
    /* Offer the whole writable tail (>= target, so never empty: asyncio
     * treats a zero-length buffer as a fatal protocol error). */
    self->read_offer_offset = self->buf_len;
    self->read_offer_size = self->buf_cap - self->buf_len;
    view = PyMemoryView_FromObject((PyObject *)self);
    if (view == NULL) {
        self->read_offer_offset = 0;
        self->read_offer_size = 0;
        return NULL;
    }
    return view;
}


static PyObject *
http_buffer_updated(WreathHttpProtocol *self, PyObject *arg)
{
    Py_ssize_t nbytes = PyLong_AsSsize_t(arg);
    if (nbytes == -1 && PyErr_Occurred()) {
        return NULL;
    }
    if (self->read_offer_size <= 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "buffer_updated() without an active read offer");
        return NULL;
    }
    if (nbytes < 0 || nbytes > self->read_offer_size) {
        /* The transport's accounting is wrong; drop the offer and raise so
         * asyncio's fatal-error path closes the connection. */
        self->read_offer_size = 0;
        self->read_offer_offset = 0;
        PyErr_SetString(PyExc_ValueError,
                        "buffer_updated() byte count is out of range");
        return NULL;
    }
    /* read_offer_offset == buf_len by invariant (data_received refuses and
     * do_consume defers while the offer is active). */
    self->buf_len = self->read_offer_offset + nbytes;
    self->read_offer_size = 0;
    self->read_offer_offset = 0;
    if (nbytes == 0 || self->closing) {
        Py_RETURN_NONE;
    }
    if (run_drive(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


static PyObject *
http_eof_received(WreathHttpProtocol *self, PyObject *Py_UNUSED(ignored))
{
    self->disconnected = 1;
    if (self->task != NULL && deliver_disconnect(self) < 0) {
        return NULL;
    }
    Py_RETURN_FALSE;
}


static PyObject *
http_connection_lost(WreathHttpProtocol *self, PyObject *Py_UNUSED(exc))
{
    self->disconnected = 1;
    self->closing = 1;
    self->state = ST_CLOSING;
    /* Clear the active offer so no late buffer_updated() ingests bytes, but
     * leave read_exports alone: live views still reference self->buf, which
     * is freed only in dealloc (unreachable while Py_buffer.obj pins us). */
    self->read_offer_size = 0;
    self->read_offer_offset = 0;
    if (cancel_deadline_timer(self) < 0) {
        return NULL;
    }
    if (self->task != NULL && deliver_disconnect(self) < 0) {
        return NULL;
    }
    if (resolve_drain(self) < 0) {
        return NULL;
    }
    if (PySet_Discard(self->registry, (PyObject *)self) < 0) {
        return NULL;
    }
    Py_CLEAR(self->transport_write_fn);
    Py_CLEAR(self->transport_writelines_fn);
    Py_CLEAR(self->transport);
    Py_RETURN_NONE;
}


static PyObject *
http_pause_writing(WreathHttpProtocol *self, PyObject *Py_UNUSED(ignored))
{
    self->write_paused = 1;
    Py_RETURN_NONE;
}


static PyObject *
http_resume_writing(WreathHttpProtocol *self, PyObject *Py_UNUSED(ignored))
{
    self->write_paused = 0;
    if (resolve_drain(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


static PyObject *
http_stop_accepting(WreathHttpProtocol *self, PyObject *Py_UNUSED(ignored))
{
    self->accepting = 0;
    if (self->state == ST_READING_HEAD && self->task == NULL) {
        if (protocol_close(self) < 0) {
            return NULL;
        }
    }
    Py_RETURN_NONE;
}


static PyObject *
http_shutdown(WreathHttpProtocol *self, PyObject *Py_UNUSED(ignored))
{
    if (protocol_close(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


/* --- timeouts ------------------------------------------------------------ */

static PyObject *
http_on_deadline(WreathHttpProtocol *self, PyObject *Py_UNUSED(ignored))
{
    double now;

    Py_CLEAR(self->timer_handle);
    if (self->closing || isinf(self->deadline)) {
        /* Nothing to enforce; the next finite deadline re-arms the timer. */
        Py_RETURN_NONE;
    }
    now = mono_now();
    if (now + 1e-3 < self->deadline) {
        /* The deadline moved since this timer was armed: sleep the rest. */
        if (arm_deadline_timer(self, self->deadline - now) < 0) {
            return NULL;
        }
        Py_RETURN_NONE;
    }
    if (!self->deadline_is_request) {
        if (protocol_close(self) < 0) {
            return NULL;
        }
    }
    else if (self->response_started) {
        if (protocol_abort(self) < 0) {
            return NULL;
        }
    }
    else if (send_error(self, 408) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


/* --- application task completion ----------------------------------------- */

static int
log_app_exception(WreathHttpProtocol *self, PyObject *exc)
{
    PyObject *context = Py_BuildValue("{s:s,s:O}", "message",
                                      "Exception in ASGI application", "exception", exc);
    PyObject *ignored;
    if (context == NULL) {
        return -1;
    }
    ignored = PyObject_CallMethod(self->loop, "call_exception_handler", "O", context);
    Py_DECREF(context);
    if (ignored == NULL) {
        return -1;
    }
    Py_DECREF(ignored);
    return 0;
}


/* Finalize a finished application task.  `drive_buffered` selects whether
 * buffered pipelined bytes are processed here (the deferred done-callback
 * path) or left to an outer run_drive() loop already on the stack (the
 * inline eager-completion path in spawn_app_task). */
static int
finalize_app_task(WreathHttpProtocol *self, PyObject *task, int drive_buffered)
{
    /* Reached only for a task already known to be done, so exception() cannot
     * be pending here: any raise is CancelledError, which apply_app_outcome
     * reads as a NULL exception. */
    PyObject *args[1] = {task};
    PyObject *exc = PyObject_Vectorcall(task_exception_fn, args, 1, NULL);
    if (exc == NULL) {
        PyErr_Clear();
    }
    return apply_app_outcome(self, exc, drive_buffered);
}


/* Act on a finished application task. `exc` is what task.exception() returned:
 * the exception instance or Py_None (both stolen), or NULL when the task was
 * cancelled. Shared by the eager inline path and the deferred done callback. */
static int
apply_app_outcome(WreathHttpProtocol *self, PyObject *exc, int drive_buffered)
{
    Py_CLEAR(self->task);
    uint8_t nfr_terminal = WREATH_NFR_TERM_OK;

    if (exc == NULL) {
        nfr_terminal = WREATH_NFR_TERM_CANCELLED;
        if (protocol_abort(self) < 0) {
            return -1;
        }
        goto advance;
    }
    if (self->ws_mode) {
        /* Mirror the pure reference's _run_ws_app post-conditions. The session's
         * completion cell records the handshake disposition as `status` (101 when
         * the socket was established, else the rejection status) and how the
         * session ended as `terminal`. */
        uint8_t ws_terminal = WREATH_NFR_TERM_OK;
        uint32_t ws_status = self->ws_accepted ? 101u : 0u;
        if (exc != Py_None) {
            int is_disconnect = PyObject_IsInstance(exc, disconnect_error) == 1;
            if (is_disconnect) {
                ws_terminal = WREATH_NFR_TERM_DISCONNECTED;
                if (protocol_abort(self) < 0) {
                    Py_DECREF(exc);
                    return -1;
                }
            }
            else {
                ws_terminal = WREATH_NFR_TERM_ERROR;
                if (log_app_exception(self, exc) < 0) {
                    Py_DECREF(exc);
                    return -1;
                }
                if (!self->ws_accepted) {
                    ws_status = 500u;  /* app raised before accepting the handshake */
                    if (!self->ws_close_sent && send_error(self, 500) < 0) {
                        Py_DECREF(exc);
                        return -1;
                    }
                }
                else if (!self->ws_close_sent) {
                    if (ws_send_close_frame(self, 1011, NULL, 0) < 0 ||
                        protocol_close(self) < 0) {
                        Py_DECREF(exc);
                        return -1;
                    }
                }
            }
        }
        else if (!self->ws_accepted) {
            /* App returned without accepting: reject the handshake. */
            ws_status = 403u;
            if (!self->ws_close_sent && send_error(self, 403) < 0) {
                Py_DECREF(exc);
                return -1;
            }
        }
        else if (!self->ws_close_sent) {
            if (ws_send_close_frame(self, 1000, NULL, 0) < 0 ||
                protocol_close(self) < 0) {
                Py_DECREF(exc);
                return -1;
            }
        }
        /* Publish exactly one completion cell for the whole WebSocket session. */
        if (self->nfr_worker != NULL && self->nfr_active) {
            self->nfr_active = 0;
            /* Route/plan attribution stamped by dispatch into the retained scope
             * as a (route_id, plan_id) tuple; left None for an unmatched route. */
            if (self->nfr_ws_scope != NULL) {
                PyObject *attr = PyDict_GetItemString(self->nfr_ws_scope,
                                                      "_wreath_flight");
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
            flight_capi->context_end(self->nfr_worker, &self->nfr_ctx,
                                     wreath_flight_now_ns(), ws_status, ws_terminal,
                                     0, self->nfr_bytes_in, self->nfr_bytes_out);
        }
        Py_CLEAR(self->nfr_ws_scope);
        Py_DECREF(exc);
        return 0;
    }
    if (exc != Py_None) {
        int is_disconnect = PyObject_IsInstance(exc, disconnect_error) == 1;
        nfr_terminal = is_disconnect ? WREATH_NFR_TERM_DISCONNECTED
                                     : WREATH_NFR_TERM_ERROR;
        if (is_disconnect) {
            if (protocol_abort(self) < 0) {
                Py_DECREF(exc);
                return -1;
            }
        }
        else {
            if (!self->response_started) {
                if (write_error(self, 500) < 0 || finish_response(self, 0) < 0) {
                    Py_DECREF(exc);
                    return -1;
                }
            }
            else if (protocol_abort(self) < 0) {
                Py_DECREF(exc);
                return -1;
            }
            if (log_app_exception(self, exc) < 0) {
                Py_DECREF(exc);
                return -1;
            }
        }
    }
    else {
        if (!self->response_started) {
            if (write_error(self, 500) < 0 || finish_response(self, 0) < 0) {
                Py_DECREF(exc);
                return -1;
            }
        }
        else if (!self->response_complete) {
            if (finish_response(self, 0) < 0) {
                Py_DECREF(exc);
                return -1;
            }
        }
    }
    Py_DECREF(exc);

advance:
    /* Publish exactly one completion cell for this request, then the context is
     * spent. bytes_out is the serialized response length the protocol tracked. */
    if (self->nfr_worker != NULL && self->nfr_active) {
        self->nfr_active = 0;
        flight_capi->context_end(self->nfr_worker, &self->nfr_ctx,
                                 wreath_flight_now_ns(), (uint32_t)self->resp_status,
                                 nfr_terminal, 0, self->nfr_bytes_in,
                                 self->nfr_bytes_out);
    }
    if (self->want_next && !self->closing) {
        self->want_next = 0;
        if (reset_request(self) < 0) {
            return -1;
        }
        /* The keep-alive deadline was already set when the response
         * completed; only the parser state needs resetting here. */
        self->state = ST_READING_HEAD;
        if (drive_buffered && self->cursor < self->buf_len && run_drive(self) < 0) {
            return -1;
        }
    }
    return 0;
}


static PyObject *
http_on_app_done(WreathHttpProtocol *self, PyObject *task)
{
    if (finalize_app_task(self, task, 1) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


/* --- type plumbing ------------------------------------------------------- */

static int
http_protocol_traverse(WreathHttpProtocol *self, visitproc visit, void *arg)
{
    Py_VISIT(self->app);
    Py_VISIT(self->native_app);
    Py_VISIT(self->loop);
    Py_VISIT(self->registry);
    Py_VISIT(self->config);
    Py_VISIT(self->transport);
    Py_VISIT(self->transport_write_fn);
    Py_VISIT(self->transport_writelines_fn);
    Py_VISIT(self->server_address);
    Py_VISIT(self->client_address);
    Py_VISIT(self->scheme);
    Py_VISIT(self->receive_callable);
    Py_VISIT(self->send_callable);
    Py_VISIT(self->done_callable);
    Py_VISIT(self->asgi_metadata);
    Py_VISIT(self->scope_type);
    Py_VISIT(self->http_version_10);
    Py_VISIT(self->http_version_11);
    Py_VISIT(self->root_path);
    Py_VISIT(self->loop_create_future);
    Py_VISIT(self->loop_create_task);
    Py_VISIT(self->loop_call_later);
    Py_VISIT(self->deadline_callable);
    Py_VISIT(self->receive_queue);
    Py_VISIT(self->receive_waiter);
    Py_VISIT(self->drain_waiter);
    Py_VISIT(self->task);
    Py_VISIT(self->timer_handle);
    Py_VISIT(self->response_bytes);
    Py_VISIT(self->ws_frag_buffer);
    Py_VISIT(self->ws_key);
    Py_VISIT(self->nfr_ws_scope);
    return 0;
}


static int
http_protocol_clear(WreathHttpProtocol *self)
{
    Py_CLEAR(self->app);
    Py_CLEAR(self->native_app);
    Py_CLEAR(self->loop);
    Py_CLEAR(self->registry);
    Py_CLEAR(self->config);
    Py_CLEAR(self->transport);
    Py_CLEAR(self->transport_write_fn);
    Py_CLEAR(self->transport_writelines_fn);
    Py_CLEAR(self->server_address);
    Py_CLEAR(self->client_address);
    Py_CLEAR(self->scheme);
    Py_CLEAR(self->receive_callable);
    Py_CLEAR(self->send_callable);
    Py_CLEAR(self->done_callable);
    Py_CLEAR(self->asgi_metadata);
    Py_CLEAR(self->scope_type);
    Py_CLEAR(self->http_version_10);
    Py_CLEAR(self->http_version_11);
    Py_CLEAR(self->root_path);
    Py_CLEAR(self->loop_create_future);
    Py_CLEAR(self->loop_create_task);
    Py_CLEAR(self->loop_call_later);
    Py_CLEAR(self->deadline_callable);
    Py_CLEAR(self->receive_queue);
    self->receive_head = 0;
    self->queued_messages = 0;
    Py_CLEAR(self->receive_waiter);
    Py_CLEAR(self->drain_waiter);
    Py_CLEAR(self->task);
    Py_CLEAR(self->timer_handle);
    clear_response_builder(self);
    Py_CLEAR(self->ws_frag_buffer);
    Py_CLEAR(self->ws_key);
    Py_CLEAR(self->nfr_ws_scope);
    return 0;
}


static void
http_protocol_dealloc(WreathHttpProtocol *self)
{
    PyObject_GC_UnTrack(self);
    /* Safety net: a request context should always reach context_end via the
     * task completion path, but if the loop was torn down mid-request, release
     * its active slot so the table does not leak. */
    if (self->nfr_worker != NULL && self->nfr_active) {
        self->nfr_active = 0;
        flight_capi->context_abandon(self->nfr_worker, &self->nfr_ctx);
    }
    if (self->buf != NULL) {
        if (self->read_exports == 0) {
            PyMem_Free(self->buf);
        }
        /* read_exports > 0 here is impossible (every export holds a strong
         * reference via Py_buffer.obj); if it ever happens, leak rather than
         * free memory a view still points at. */
        self->buf = NULL;
    }
    if (self->out_buf != NULL) {
        PyMem_Free(self->out_buf);
        self->out_buf = NULL;
    }
    http_protocol_clear(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}


static PyObject *
http_protocol_new(PyTypeObject *type, PyObject *Py_UNUSED(args), PyObject *Py_UNUSED(kwargs))
{
    WreathHttpProtocol *self = (WreathHttpProtocol *)type->tp_alloc(type, 0);
    if (self != NULL) {
        self->state = ST_READING_HEAD;
        self->accepting = 1;
        self->http11 = 1;  /* pre-request default matches the pure reference */
        self->request_more_body = 1;
        self->response_keep_alive = 1;
        self->deadline = Py_HUGE_VAL;
        self->ws_frag_opcode = -1;
        self->ws_frag_count = 0;
    }
    return (PyObject *)self;
}


static int
read_ssize_attr(PyObject *config, const char *name, Py_ssize_t *out)
{
    PyObject *value = PyObject_GetAttrString(config, name);
    if (value == NULL) {
        return -1;
    }
    *out = PyLong_AsSsize_t(value);
    Py_DECREF(value);
    return (*out == -1 && PyErr_Occurred()) ? -1 : 0;
}


static int
read_double_attr(PyObject *config, const char *name, double *out)
{
    PyObject *value = PyObject_GetAttrString(config, name);
    if (value == NULL) {
        return -1;
    }
    *out = PyFloat_AsDouble(value);
    Py_DECREF(value);
    return (*out == -1.0 && PyErr_Occurred()) ? -1 : 0;
}


static int
http_protocol_init(WreathHttpProtocol *self, PyObject *args, PyObject *kwargs)
{
    PyObject *app;
    PyObject *config;
    PyObject *loop;
    PyObject *registry;
    PyObject *recorder = NULL;
    static char *kwlist[] = {"app", "config", "loop", "connection_registry",
                             "recorder", NULL};

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "OOOO|O:HttpProtocol", kwlist,
                                     &app, &config, &loop, &registry, &recorder)) {
        return -1;
    }
    /* A borrowed worker pointer (the recorder object outlives the connection via
     * the Server that owns it). NULL keeps every hook a not-taken branch. */
    self->nfr_worker = flight_capi != NULL ? wreath_flight_worker_from(recorder) : NULL;
    Py_INCREF(app);
    Py_XSETREF(self->app, app);
    Py_XSETREF(self->native_app, PyObject_GetAttrString(app, "_wreath_http"));
    if (self->native_app == NULL) {
        if (!PyErr_ExceptionMatches(PyExc_AttributeError)) return -1;
        PyErr_Clear();
    }
    Py_INCREF(config);
    Py_XSETREF(self->config, config);
    Py_INCREF(loop);
    Py_XSETREF(self->loop, loop);
    Py_INCREF(registry);
    Py_XSETREF(self->registry, registry);

    if (read_ssize_attr(config, "max_request_line", &self->max_request_line) < 0 ||
        read_ssize_attr(config, "max_header_count", &self->max_header_count) < 0 ||
        read_ssize_attr(config, "max_header_bytes", &self->max_header_bytes) < 0 ||
        read_ssize_attr(config, "max_body_bytes", &self->max_body_bytes) < 0 ||
        read_ssize_attr(config, "read_high_water", &self->read_high_water) < 0 ||
        read_ssize_attr(config, "read_high_water_messages",
                        &self->read_high_water_messages) < 0 ||
        read_ssize_attr(config, "max_ws_fragments", &self->max_ws_fragments) < 0 ||
        read_double_attr(config, "keep_alive_timeout", &self->keep_alive_timeout) < 0 ||
        read_double_attr(config, "request_timeout", &self->request_timeout) < 0) {
        return -1;
    }

    if (self->receive_queue == NULL) {
        self->receive_queue = PyList_New(0);
    }
    if (self->receive_callable == NULL) {
        self->receive_callable = PyObject_GetAttrString((PyObject *)self, "_asgi_receive");
        self->send_callable = PyObject_GetAttrString((PyObject *)self, "_asgi_send");
        self->done_callable = PyObject_GetAttrString((PyObject *)self, "_on_app_done");
        self->asgi_metadata = Py_BuildValue("{s:s,s:s}", "version", "3.0",
                                            "spec_version", "2.5");
        self->scope_type = PyUnicode_FromString("http");
        self->http_version_10 = PyUnicode_FromString("1.0");
        self->http_version_11 = PyUnicode_FromString("1.1");
        self->root_path = PyUnicode_FromString("");
        self->loop_create_future = PyObject_GetAttrString(loop, "create_future");
        self->loop_create_task = PyObject_GetAttrString(loop, "create_task");
        self->loop_call_later = PyObject_GetAttrString(loop, "call_later");
        self->deadline_callable = PyObject_GetAttrString((PyObject *)self, "_on_deadline");
    }
    if (self->receive_queue == NULL || self->receive_callable == NULL ||
        self->send_callable == NULL || self->done_callable == NULL ||
        self->asgi_metadata == NULL || self->scope_type == NULL ||
        self->http_version_10 == NULL || self->http_version_11 == NULL ||
        self->root_path == NULL || self->loop_create_future == NULL ||
        self->loop_create_task == NULL || self->loop_call_later == NULL ||
        self->deadline_callable == NULL) {
        return -1;
    }
    return 0;
}


static PyMethodDef http_protocol_methods[] = {
    {"connection_made", (PyCFunction)http_connection_made, METH_O,
     "Attach the asyncio transport."},
    {"data_received", (PyCFunction)http_data_received, METH_O,
     "Consume bytes from the asyncio transport (compatibility copy path)."},
    {"get_buffer", (PyCFunction)http_get_buffer, METH_O,
     "Offer the writable tail of the C request buffer for a transport read."},
    {"buffer_updated", (PyCFunction)http_buffer_updated, METH_O,
     "Commit bytes a transport wrote into the offered buffer."},
    {"eof_received", (PyCFunction)http_eof_received, METH_NOARGS,
     "Handle EOF from the peer."},
    {"connection_lost", (PyCFunction)http_connection_lost, METH_O,
     "Release connection resources after transport loss."},
    {"pause_writing", (PyCFunction)http_pause_writing, METH_NOARGS,
     "Apply transport write backpressure."},
    {"resume_writing", (PyCFunction)http_resume_writing, METH_NOARGS,
     "Release transport write backpressure."},
    {"stop_accepting", (PyCFunction)http_stop_accepting, METH_NOARGS,
     "Stop accepting new requests during graceful shutdown."},
    {"shutdown", (PyCFunction)http_shutdown, METH_NOARGS,
     "Close the connection immediately."},
    {"_asgi_receive", (PyCFunction)http_asgi_receive, METH_NOARGS,
     "ASGI receive callable (returns an awaitable)."},
    {"_asgi_send", (PyCFunction)http_asgi_send, METH_O,
     "ASGI send callable (returns an awaitable)."},
    {"_on_app_done", (PyCFunction)http_on_app_done, METH_O,
     "Finalize a completed application task."},
    {"_on_deadline", (PyCFunction)http_on_deadline, METH_NOARGS,
     "Enforce or re-arm the connection's timeout deadline."},
    {NULL, NULL, 0, NULL},
};


static PyType_Slot http_protocol_slots[] = {
    {Py_tp_doc, (void *)"Native HTTP/1.1 connection protocol."},
    {Py_tp_new, (void *)http_protocol_new},
    {Py_tp_init, (void *)http_protocol_init},
    {Py_tp_dealloc, (void *)http_protocol_dealloc},
    {Py_tp_traverse, (void *)http_protocol_traverse},
    {Py_tp_clear, (void *)http_protocol_clear},
    {Py_tp_methods, (void *)http_protocol_methods},
    {Py_bf_getbuffer, (void *)http_protocol_getbuffer},
    {Py_bf_releasebuffer, (void *)http_protocol_releasebuffer},
    {0, NULL},
};


PyType_Spec http_protocol_spec = {
    .name = "wreath._native._server.Http1Protocol",
    .basicsize = sizeof(WreathHttpProtocol),
    .itemsize = 0,
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_HAVE_GC |
             Py_TPFLAGS_MANAGED_WEAKREF,
    .slots = http_protocol_slots,
};
