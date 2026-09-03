#include "server.h"

#define WS_MAX_UNPRODUCTIVE_CONTROL_FRAMES 100

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

static const WreathTransportCAPI *transport_capi = NULL;

static void
load_transport_capi(void)
{
    if (transport_capi != NULL) {
        return;
    }
    /* native-lint: allow NC004 -- one-time sibling-extension C API resolution */
    transport_capi = PyCapsule_Import(WREATH_TRANSPORT_CAPI_NAME, 0);
    if (transport_capi == NULL) {
        PyErr_Clear();
    }
}

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
    if (self->native_transport && transport_capi != NULL) {
        return transport_capi->write(self->transport, data);
    }
    result = PyObject_CallOneArg(self->transport_write_fn, data);
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    return 0;
}


static int
transport_writelines(WreathHttpProtocol *self, PyObject *parts)
{
    if (self->transport == NULL || self->closing) {
        return 0;
    }
    if (self->native_transport && transport_capi != NULL) {
        return transport_capi->writelines(self->transport, parts);
    }
    if (self->transport_writelines_fn == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "transport writelines callable is unavailable");
        return -1;
    }
    PyObject *result = PyObject_CallOneArg(self->transport_writelines_fn, parts);
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
    /* Parenthesize the call arguments explicitly.  A body slot is itself a
     * tuple; with the bare "O" format CPython 3.14 treats that tuple as the
     * method's argument vector and calls set_result(body, more, closed). */
    PyObject *ignored = PyObject_CallMethod(future, "set_result", "(O)", result);
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
    char digits[WREATH_DIGITS_MAX];
    return response_append(self, digits, wreath_write_decimal(digits, value));
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
make_body_slot(PyObject *body, int more, int disconnected)
{
    return PyTuple_Pack(3, body, more ? Py_True : Py_False,
                        disconnected ? Py_True : Py_False);
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
    int append_result;
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
    /* The date cache updates this bytearray in place. The critical section is
     * a no-op on GIL builds and makes the direct buffer read valid under
     * free-threading when another server loop refreshes the shared config. */
    Py_BEGIN_CRITICAL_SECTION(self->default_response_wire);
    append_result = append_raw(out, PyByteArray_AS_STRING(self->default_response_wire),
                               PyByteArray_GET_SIZE(self->default_response_wire));
    Py_END_CRITICAL_SECTION();
    if (append_result < 0) goto done;
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

static int
pause_pipeline_if_needed(WreathHttpProtocol *self)
{
    if ((self->state != ST_REQUEST_RUNNING && self->state != ST_WS_HANDSHAKE) ||
        self->reading_paused ||
        self->buf_len - self->cursor < self->max_header_bytes) {
        return 0;
    }
    if (transport_method0(self, "pause_reading") < 0) {
        return -1;
    }
    self->reading_paused = 1;
    return 0;
}


static void
receive_queue_clear(WreathHttpProtocol *self, int release_storage)
{
    for (Py_ssize_t i = self->receive_head; i < self->receive_queue_len; i++) {
        Py_XDECREF(self->receive_queue[i]);
        self->receive_queue[i] = NULL;
    }
    self->receive_head = 0;
    self->receive_queue_len = 0;
    self->queued_messages = 0;
    if (release_storage) {
        PyMem_Free(self->receive_queue);
        self->receive_queue = NULL;
        self->receive_queue_cap = 0;
    }
}

/* Append to the native queue tail, retaining one message reference. */
static int
receive_queue_push(WreathHttpProtocol *self, PyObject *msg)
{
    if (self->receive_queue_len == self->receive_queue_cap) {
        /* Compact only when the consumed prefix is at least half the array:
         * the memmove then reclaims >= cap/2 slots while moving <= cap/2
         * live entries, so every pointer moved is paid for by a later push
         * (amortized O(1)). Compacting at head == 1 would move cap-1 entries
         * to reclaim one slot -- O(cap) per message whenever an app consumes
         * in lockstep with ingest near capacity. Growth stays bounded by the
         * read high-water pause. */
        if (self->receive_queue_cap > 0 &&
            self->receive_head * 2 >= self->receive_queue_cap) {
            Py_ssize_t live = self->receive_queue_len - self->receive_head;
            memmove(self->receive_queue, self->receive_queue + self->receive_head,
                    (size_t)live * sizeof(PyObject *));
            self->receive_queue_len = live;
            self->receive_head = 0;
        }
        if (self->receive_queue_len == self->receive_queue_cap) {
            Py_ssize_t cap = self->receive_queue_cap ? self->receive_queue_cap * 2 : 16;
            PyObject **grown = PyMem_Realloc(
                self->receive_queue, (size_t)cap * sizeof(PyObject *));
            if (grown == NULL) return PyErr_NoMemory(), -1;
            self->receive_queue = grown;
            self->receive_queue_cap = cap;
        }
    }
    self->receive_queue[self->receive_queue_len++] = Py_NewRef(msg);
    self->queued_messages++;
    return 0;
}

/* Transfer the queue's owned reference to the caller. */
static PyObject *
receive_queue_pop(WreathHttpProtocol *self)
{
    PyObject *msg = self->receive_queue[self->receive_head];
    self->receive_queue[self->receive_head++] = NULL;
    self->queued_messages--;
    if (self->receive_head == self->receive_queue_len) {
        self->receive_head = 0;
        self->receive_queue_len = 0;
    }
    return msg;
}

static int
enqueue_body(WreathHttpProtocol *self, PyObject *body, int more)
{
    PyObject *msg = self->native_app != NULL
        ? make_body_slot(body, more, 0) : make_request_msg(body, more);
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
            : (self->native_app != NULL
                ? make_body_slot(Py_None, 0, 1) : make_disconnect_msg());
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
    /* The message alone stops nothing: it is passive, and a handler parked on a
     * database query or an upstream call is not awaiting receive(), so the work
     * runs to completion against a socket nobody will read. Cancelling the task
     * is what reaches the query; the message is still delivered, because an
     * application that does poll for it is entitled to see it.
     *
     * Not once the response has started -- the status line is already on the
     * wire, so aborting there truncates a body rather than saving a scan, and
     * that case unwinds by itself when the next send raises _Disconnect. Not for
     * a WebSocket session either: its disconnect is an ordinary message the
     * application is already reading.
     *
     * cancel() only schedules; nothing of the application's runs from here. */
    if (!self->cancel_on_disconnect || self->ws_mode || self->response_started ||
        self->task == NULL) {
        return 0;
    }
    PyObject *cancelled = PyObject_CallMethod(self->task, "cancel", NULL);
    if (cancelled == NULL) {
        return -1;
    }
    Py_DECREF(cancelled);
    return 0;
}


static PyObject *
http_wreath_cancel_on_disconnect(WreathHttpProtocol *self, PyObject *enabled)
{
    int value = PyObject_IsTrue(enabled);
    if (value < 0) {
        return NULL;
    }
    self->cancel_on_disconnect = value;
    Py_RETURN_NONE;
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

static PyObject *
http_wreath_receive(WreathHttpProtocol *self, PyObject *Py_UNUSED(ignored))
{
    /* WebSocket is still the public ASGI message protocol. The connection owns
     * one receive callable, so switch here once the HTTP upgrade changed mode. */
    if (self->ws_mode) return http_asgi_receive(self, NULL);
    if (self->pending_empty_request) {
        PyObject *slot = make_body_slot(Py_None, 0, 0);
        if (slot == NULL) return NULL;
        self->pending_empty_request = 0;
        PyObject *awaitable = completed_value(slot);
        Py_DECREF(slot);
        return awaitable;
    }
    if (self->queued_messages > 0) {
        PyObject *slot = receive_queue_pop(self);
        if (slot == NULL) return NULL;
        PyObject *body = PyTuple_GET_ITEM(slot, 0);
        if (PyBytes_Check(body)) self->queued_bytes -= PyBytes_GET_SIZE(body);
        if (receive_pressure_resume(self) < 0) {
            Py_DECREF(slot);
            return NULL;
        }
        PyObject *awaitable = completed_value(slot);
        Py_DECREF(slot);
        return awaitable;
    }
    if (self->disconnected) {
        PyObject *slot = make_body_slot(Py_None, 0, 1);
        if (slot == NULL) return NULL;
        PyObject *awaitable = completed_value(slot);
        Py_DECREF(slot);
        return awaitable;
    }
    PyObject *waiter = make_future(self);
    if (waiter == NULL) return NULL;
    self->receive_waiter = Py_NewRef(waiter);
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
validate_trailer_block(const char *data, Py_ssize_t size, Py_ssize_t max_count)
{
    Py_ssize_t offset = 0;
    Py_ssize_t count = 0;
    while (offset < size) {
        Py_ssize_t end = offset;
        while (end + 1 < size &&
               !(data[end] == '\r' && data[end + 1] == '\n')) {
            end++;
        }
        if (end + 1 >= size || ++count > max_count) {
            return 0;
        }
        const char *colon = memchr(data + offset, ':', (size_t)(end - offset));
        if (colon == NULL || !wreath_field_name_valid(data + offset, colon - (data + offset))) {
            return 0;
        }
        const char *value = colon + 1;
        while (value < data + end && (*value == ' ' || *value == '\t')) {
            value++;
        }
        if (!wreath_field_value_valid(value, data + end - value)) {
            return 0;
        }
        Py_ssize_t name_size = colon - (data + offset);
        if ((name_size == 4 && PyOS_strnicmp(data + offset, "host", 4) == 0) ||
            (name_size == 14 && PyOS_strnicmp(data + offset, "content-length", 14) == 0) ||
            (name_size == 17 && PyOS_strnicmp(data + offset, "transfer-encoding", 17) == 0)) {
            return 0;
        }
        offset = end + 2;
    }
    return 1;
}


static int
parse_content_length_header(PyObject *value, Py_ssize_t *out)
{
    /* Fast path: plain decimal digits. Anything else falls back to int()
     * semantics, which is what RFC 9112 §6.2's grammar plus Python's own
     * integer parsing between them accept. */
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
response_headers_match_cache(WreathHttpProtocol *self, PyObject *headers)
{
    PyObject *cached = self->response_header_cache_key;
    if (cached == NULL || !PyTuple_CheckExact(cached) ||
        (!PyTuple_CheckExact(headers) && !PyList_CheckExact(headers))) {
        return 0;
    }
    Py_ssize_t count = PyTuple_GET_SIZE(cached);
    if (PySequence_Size(headers) != count) {
        return 0;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *pair = PyTuple_CheckExact(headers)
            ? PyTuple_GET_ITEM(headers, i) : PyList_GET_ITEM(headers, i);
        PyObject *cached_pair = PyTuple_GET_ITEM(cached, i);
        if (!PyTuple_CheckExact(pair) || PyTuple_GET_SIZE(pair) != 2 ||
            !PyTuple_CheckExact(cached_pair) || PyTuple_GET_SIZE(cached_pair) != 2) {
            return 0;
        }
        for (Py_ssize_t field = 0; field < 2; field++) {
            PyObject *value = PyTuple_GET_ITEM(pair, field);
            PyObject *cached_value = PyTuple_GET_ITEM(cached_pair, field);
            if (value == cached_value) {
                continue;
            }
            if (!PyBytes_CheckExact(value) || !PyBytes_CheckExact(cached_value)) {
                return 0;
            }
            Py_ssize_t size = PyBytes_GET_SIZE(value);
            if (size != PyBytes_GET_SIZE(cached_value) ||
                memcmp(PyBytes_AS_STRING(value), PyBytes_AS_STRING(cached_value),
                       (size_t)size) != 0) {
                return 0;
            }
        }
    }
    return 1;
}

static int
begin_response_parts(WreathHttpProtocol *self, PyObject *status_obj, PyObject *headers)
{
    PyObject *items = NULL;
    PyObject *cache_key = NULL;
    PyObject *cache_wire = NULL;
    long status;
    Py_ssize_t reason_size;
    Py_ssize_t header_wire_start = 0;
    const char *reason;
    int has_date = 0;
    int has_server = 0;
    int append_result;
    int cacheable = 0;

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
    if (self->policy_state.native &&
        wreath_policy_egress(&self->policy, &self->policy_state, headers) < 0) {
        return -1;
    }
    if (self->policy_context != NULL) {
        wreath_request_context_update_policy(
            self->policy_context, &self->policy_state);
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

    if (headers != NULL && (headers == self->response_header_cache_key ||
                            response_headers_match_cache(self, headers))) {
        if (response_append(self, PyBytes_AS_STRING(self->response_header_cache_wire),
                            PyBytes_GET_SIZE(self->response_header_cache_wire)) < 0) {
            goto error;
        }
        self->resp_has_length = self->response_header_cache_has_length;
        self->resp_content_length = self->response_header_cache_length;
        has_date = self->response_header_cache_has_date;
        has_server = self->response_header_cache_has_server;
    }
    else if (headers != NULL && headers != Py_None) {
        /* One small immutable response shape is retained per connection. The
         * bounds keep a one-off tuple with oversized fields from becoming
         * connection-lifetime storage; PreparedResponse's usual two fields
         * occupy only a few dozen bytes. */
        cacheable = ((PyTuple_CheckExact(headers) || PyList_CheckExact(headers)) &&
                     PySequence_Size(headers) <= 16);
        header_wire_start = self->response_bytes_len;
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
                if (!PyTuple_CheckExact(pair)) cacheable = 0;
            }
            else if (PyList_Check(pair) && PyList_GET_SIZE(pair) == 2) {
                name = PyList_GET_ITEM(pair, 0);
                value = PyList_GET_ITEM(pair, 1);
                cacheable = 0;
            }
            else {
                PyErr_SetString(PyExc_RuntimeError, "response header must be a pair");
                goto error;
            }
            if (!PyBytes_Check(name) || !PyBytes_Check(value)) {
                PyErr_SetString(PyExc_RuntimeError, "response header must be bytes");
                goto error;
            }
            if (!PyBytes_CheckExact(name) || !PyBytes_CheckExact(value)) cacheable = 0;
            name_data = PyBytes_AS_STRING(name);
            name_size = PyBytes_GET_SIZE(name);
            if (!wreath_field_name_valid(name_data, name_size) ||
                !wreath_field_value_valid(PyBytes_AS_STRING(value), PyBytes_GET_SIZE(value))) {
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
        if (cacheable) {
            const char *wire_start = (self->response_bytes != NULL
                ? PyBytes_AS_STRING(self->response_bytes) : self->response_inline)
                + header_wire_start;
            cache_wire = PyBytes_FromStringAndSize(
                wire_start, self->response_bytes_len - header_wire_start);
            if (cache_wire == NULL) goto error;
            if (PyBytes_GET_SIZE(cache_wire) <= 1024) {
                cache_key = PyTuple_CheckExact(headers)
                    ? Py_NewRef(headers) : PyList_AsTuple(headers);
                if (cache_key == NULL) goto error;
                Py_XSETREF(self->response_header_cache_key, cache_key);
                cache_key = NULL;
                Py_XSETREF(self->response_header_cache_wire, cache_wire);
                cache_wire = NULL;
                self->response_header_cache_has_length = self->resp_has_length;
                self->response_header_cache_length = self->resp_content_length;
                self->response_header_cache_has_date = has_date;
                self->response_header_cache_has_server = has_server;
            }
            Py_CLEAR(cache_wire);
        }
    }
    if (!has_date && !has_server) {
        /* The common case is one contiguous copy instead of a generic sequence
         * conversion, two pair walks and eight fragmented appends. */
        Py_BEGIN_CRITICAL_SECTION(self->default_response_wire);
        append_result = response_append(
            self, PyByteArray_AS_STRING(self->default_response_wire),
            PyByteArray_GET_SIZE(self->default_response_wire));
        Py_END_CRITICAL_SECTION();
        if (append_result < 0) goto error;
    }
    else {
        /* A response overriding one default still needs the individual fields
         * so it can suppress only the matching configured value. */
        items = PySequence_Fast(self->default_response_headers,
                                "default response headers must be a sequence");
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
    Py_XDECREF(cache_key);
    Py_XDECREF(cache_wire);
    clear_response_builder(self);
    return -1;
}


static int
begin_response(WreathHttpProtocol *self, PyObject *message)
{
    return begin_response_parts(
        self, PyDict_GetItem(message, s_status), PyDict_GetItem(message, s_headers)
    );
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
            if (self->nfr_worker != NULL) {
                self->nfr_bytes_out += (uint64_t)PyBytes_GET_SIZE(output) +
                                       (uint64_t)first_body_size;
            }
            Py_DECREF(output);
            if (pair == NULL) {
                return -1;
            }
            if (transport_writelines(self, pair) < 0) {
                Py_DECREF(pair);
                return -1;
            }
            Py_DECREF(pair);
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
write_body_parts(WreathHttpProtocol *self, PyObject *body, PyObject *more_obj)
{
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
            char size_line[WREATH_DIGITS_MAX + 2];
            int size_length = wreath_write_hex(size_line, (size_t)body_size);
            size_line[size_length++] = '\r';
            size_line[size_length++] = '\n';
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


static PyObject *
write_body(WreathHttpProtocol *self, PyObject *message)
{
    return write_body_parts(
        self, PyDict_GetItem(message, s_body), PyDict_GetItem(message, s_more_body)
    );
}


static PyObject *
http_wreath_response(
    WreathHttpProtocol *self, PyObject *const *args, Py_ssize_t nargs
)
{
    if (nargs != 3 && nargs != 4) {
        PyErr_Format(
            PyExc_TypeError, "_wreath_response expected 3 or 4 arguments, got %zd", nargs
        );
        return NULL;
    }
    if (self->disconnected && self->response_started) {
        PyErr_SetString(disconnect_error, "peer disconnected");
        return NULL;
    }
    if (self->ws_mode) {
        PyErr_SetString(PyExc_RuntimeError, "HTTP response used for WebSocket request");
        return NULL;
    }
    if (self->response_started) {
        PyErr_SetString(PyExc_RuntimeError, "response already started");
        return NULL;
    }
    PyObject *body = Py_NewRef(args[2]);
    int authenticated = nargs == 4 ? PyObject_IsTrue(args[3]) : 0;
    if (authenticated < 0 || (self->policy.response_transform && wreath_policy_response(
            &self->policy, &self->policy_state, args[0], args[1], &body,
            authenticated) < 0) || begin_response_parts(self, args[0], args[1]) < 0) {
        Py_DECREF(body);
        return NULL;
    }
    PyObject *result = write_body_parts(self, body, NULL);
    Py_DECREF(body);
    return result;
}

/* The application dispatcher is already a coroutine, but a one-shot HTTP/1
 * write normally completes synchronously.  Returning the shared completed
 * awaitable only to have Python execute GET_AWAITABLE/SEND and unwind its
 * StopIteration is boundary glue, not backpressure.  Preserve the uncommon
 * drain awaitable and spell synchronous completion as None. */
static PyObject *
http_wreath_response_nowait(
    WreathHttpProtocol *self, PyObject *const *args, Py_ssize_t nargs
)
{
    PyObject *result = http_wreath_response(self, args, nargs);
    if (result == immediate_none) {
        Py_DECREF(result);
        Py_RETURN_NONE;
    }
    return result;
}

static PyObject *
http_wreath_stream_start(
    WreathHttpProtocol *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 2) {
        PyErr_Format(PyExc_TypeError,
                     "_wreath_stream_start expected 2 arguments, got %zd", nargs);
        return NULL;
    }
    if (self->response_started || self->ws_mode) {
        PyErr_SetString(PyExc_RuntimeError, "response already started");
        return NULL;
    }
    if (begin_response_parts(self, args[0], args[1]) < 0) return NULL;
    return completed_none();
}

static PyObject *
http_wreath_stream_body(
    WreathHttpProtocol *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 2) {
        PyErr_Format(PyExc_TypeError,
                     "_wreath_stream_body expected 2 arguments, got %zd", nargs);
        return NULL;
    }
    return write_body_parts(self, args[0], args[1]);
}


static PyObject *
http_wreath_file_start(
    WreathHttpProtocol *self, PyObject *const *args, Py_ssize_t nargs
)
{
    PyObject *empty;
    PyObject *sendfile;
    PyObject *offset;
    PyObject *count;
    PyObject *awaitable;
    Py_ssize_t size;
    int body_coalesced = 0;

    if (nargs != 4) {
        PyErr_Format(
            PyExc_TypeError, "_wreath_file_start expected 4 arguments, got %zd", nargs
        );
        return NULL;
    }
    if (self->response_started || self->ws_mode) {
        PyErr_SetString(PyExc_RuntimeError, "response already started");
        return NULL;
    }
    size = PyLong_AsSsize_t(args[3]);
    if (size < 0 || (size == -1 && PyErr_Occurred())) {
        if (!PyErr_Occurred()) {
            PyErr_SetString(PyExc_ValueError, "wreath.file size cannot be negative");
        }
        return NULL;
    }
    if (begin_response_parts(self, args[0], args[1]) < 0) {
        return NULL;
    }
    if (!self->resp_has_length || self->resp_content_length != size) {
        PyErr_SetString(PyExc_RuntimeError, "wreath.file content-length mismatch");
        return NULL;
    }
    empty = PyBytes_FromStringAndSize("", 0);
    if (empty == NULL) {
        return NULL;
    }
    if (decide_framing_and_write_head(self, empty, 1, &body_coalesced) < 0) {
        Py_DECREF(empty);
        return NULL;
    }
    Py_DECREF(empty);
    if (size == 0) {
        return completed_none();
    }

    sendfile = PyObject_GetAttrString(self->loop, "sendfile");
    offset = PyLong_FromLong(0);
    count = PyLong_FromSsize_t(size);
    if (sendfile == NULL || offset == NULL || count == NULL) {
        Py_XDECREF(sendfile);
        Py_XDECREF(offset);
        Py_XDECREF(count);
        return NULL;
    }
    awaitable = PyObject_CallFunctionObjArgs(
        sendfile, self->transport, args[2], offset, count, NULL
    );
    Py_DECREF(sendfile);
    Py_DECREF(offset);
    Py_DECREF(count);
    return awaitable;
}


static PyObject *
http_wreath_file_finish(WreathHttpProtocol *self, PyObject *Py_UNUSED(ignored))
{
    if (!self->response_started || self->response_complete) {
        PyErr_SetString(PyExc_RuntimeError, "wreath.file response is not active");
        return NULL;
    }
    self->response_body_sent = self->resp_content_length;
    if (finish_response(self, self->response_keep_alive) < 0) {
        return NULL;
    }
    return completed_none();
}


/* --- ASGI send ----------------------------------------------------------- */

static int
wreath_ws_subprotocol_valid(const char *data, Py_ssize_t size)
{
    return wreath_field_name_valid(data, size);
}

static int
wreath_ws_key_valid(const char *data, Py_ssize_t size)
{
    if (size != 24 || data[22] != '=' || data[23] != '=') {
        return 0;
    }
    for (Py_ssize_t i = 0; i < 22; i++) {
        unsigned char ch = (unsigned char)data[i];
        if (!((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') ||
              (ch >= '0' && ch <= '9') || ch == '+' || ch == '/')) {
            return 0;
        }
    }
    return data[21] == 'A' || data[21] == 'Q' ||
           data[21] == 'g' || data[21] == 'w';
}

static int
wreath_ws_accept_header_allowed(const char *name, Py_ssize_t size)
{
    return !wreath_ascii_equal_ci(name, size, "upgrade", 7)
        && !wreath_ascii_equal_ci(name, size, "connection", 10)
        && !wreath_ascii_equal_ci(name, size, "sec-websocket-accept", 20)
        && !wreath_ascii_equal_ci(name, size, "sec-websocket-protocol", 22)
        && !wreath_ascii_equal_ci(name, size, "sec-websocket-extensions", 24);
}

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
            if (data == NULL) return NULL;
            if (!wreath_ws_subprotocol_valid(data, size)) {
                PyErr_SetString(PyExc_RuntimeError,
                                "invalid websocket subprotocol");
                return NULL;
            }
            if (
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
                if (!wreath_field_name_valid(lname, name_size) ||
                    !wreath_ws_accept_header_allowed(lname, name_size) ||
                    !wreath_field_value_valid(PyBytes_AS_STRING(value),
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
        if (!self->closing && receive_pressure_resume(self) < 0) {
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

static void
reset_response_state(WreathHttpProtocol *self, int keep_alive)
{
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

    self->response_keep_alive = keep_alive;
}


static PyObject *
build_scope(WreathHttpProtocol *self, PyObject *method, PyObject *path, PyObject *raw_path,
            PyObject *query_string, PyObject *headers)
{
    PyObject *scope;
    PyObject *version = self->http11 ? self->http_version_11 : self->http_version_10;
    PyObject *scheme = self->policy_state.native
        ? self->policy_state.scheme : self->scheme;
    PyObject *client = self->policy_state.native
        ? self->policy_state.client : self->client_address;

    if (self->native_app != NULL) {
        PyObject *context = wreath_request_context_new(
            self->scope_type, self->asgi_metadata, version, method, scheme,
            path, raw_path, query_string, headers, self->server_address,
            client, self->root_path
        );
        if (context != NULL && self->policy_state.native) {
            wreath_request_context_set_policy(context, &self->policy_state);
            Py_XSETREF(self->policy_context, Py_NewRef(context));
        }
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
    PyObject *asgi_headers = wreath_headers_materialize(headers);
    if (asgi_headers == NULL) {
        Py_DECREF(scope);
        return NULL;
    }
    if (PyDict_SetItem(scope, s_type, self->scope_type) < 0 ||
        PyDict_SetItem(scope, k_asgi, self->asgi_metadata) < 0 ||
        PyDict_SetItem(scope, k_http_version, version) < 0 ||
        PyDict_SetItem(scope, k_method, method) < 0 ||
        PyDict_SetItem(scope, k_scheme, scheme) < 0 ||
        PyDict_SetItem(scope, k_path, path) < 0 ||
        PyDict_SetItem(scope, k_raw_path, raw_path) < 0 ||
        PyDict_SetItem(scope, k_query_string, query_string) < 0 ||
        PyDict_SetItem(scope, s_headers, asgi_headers) < 0 ||
        PyDict_SetItem(scope, k_server, self->server_address) < 0 ||
        PyDict_SetItem(scope, k_client, client) < 0 ||
        PyDict_SetItem(scope, k_root_path, self->root_path) < 0 ||
        PyDict_SetItem(scope, k_extensions, extensions_dict) < 0) {
        Py_DECREF(asgi_headers);
        Py_DECREF(scope);
        return NULL;
    }
    Py_DECREF(asgi_headers);
    return scope;
}


static int
spawn_app_task(WreathHttpProtocol *self, PyObject *scope)
{
    PyObject *awaitable = NULL;
    PyObject *driver = NULL;
    PyObject *yielded = NULL;
    PyObject *continuation = NULL;
    PyObject *task = NULL;
    int result = -1;

    PyObject *app_args[3] = {scope, self->receive_callable, self->send_callable};
    awaitable = PyObject_Vectorcall(
        self->native_app != NULL && wreath_request_context_check(scope)
            ? self->native_app : self->app,
        app_args, 3, NULL
    );
    if (awaitable == NULL) goto done;

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
            if (error == NULL) goto done;
            if (apply_app_outcome(self, error, 0) < 0) goto done;
            result = 0;
            goto done;
        }
    }

    /* Most Wreath handlers only await completed receive/send objects. Step the
     * coroutine directly so that path owns no asyncio Task at all. */
    PySendResult state = PyIter_Send(driver, Py_None, &yielded);
    if (state == PYGEN_RETURN) {
        if (apply_app_outcome(self, Py_NewRef(Py_None), 0) < 0) goto done;
        result = 0;
        goto done;
    }
    if (state == PYGEN_ERROR) {
        PyObject *error = PyErr_GetRaisedException();
        if (error == NULL) goto done;
        if (apply_app_outcome(self, error, 0) < 0) goto done; /* steals error */
        result = 0;
        goto done;
    }

    /* A real suspension needs loop ownership. The trampoline adopts the value
     * already yielded by the first step and forwards results, errors and
     * cancellation into the original coroutine. */
    continuation = wreath_started_coroutine(driver, yielded);
    if (continuation == NULL) goto done;
    task = PyObject_CallOneArg(self->loop_create_task, continuation);
    if (task == NULL) goto done;
    if (wreath_task_add_done_callback(task, self->done_callable) < 0) goto done;
    Py_XSETREF(self->task, task);
    task = NULL;
    result = 0;
done:
    Py_XDECREF(awaitable);
    Py_XDECREF(driver);
    Py_XDECREF(yielded);
    Py_XDECREF(continuation);
    Py_XDECREF(task);
    return result;
}


static int
send_policy_reply(WreathHttpProtocol *self, int keep_alive,
                  WreathPolicyReply *reply)
{
    PyObject *status = PyLong_FromLong(reply->status);
    PyObject *awaitable = NULL;
    if (status == NULL) return -1;
    reset_response_state(self, keep_alive);
    self->state = ST_REQUEST_RUNNING;
    self->request_more_body = 0;
    /* A native ingress refusal has no application task to drain a request body
     * or advance keep-alive from _on_app_done. Close after the complete reply;
     * the wire response is unchanged and untrusted body bytes cannot be parsed
     * as the next request. */
    self->response_keep_alive = 0;
    if (begin_response_parts(self, status, reply->headers) < 0) {
        Py_DECREF(status);
        return -1;
    }
    Py_DECREF(status);
    awaitable = write_body_parts(self, reply->body, NULL);
    if (awaitable == NULL) return -1;
    Py_DECREF(awaitable);
    return 0;
}


static int
send_recorded_policy_reply(WreathHttpProtocol *self, int keep_alive,
                           PyObject *headers, WreathPolicyReply *reply)
{
    uint64_t started_ns = wreath_flight_now_ns();
    if (send_policy_reply(self, keep_alive, reply) < 0) return -1;
    wreath_policy_record_completion(
        flight_capi, self->nfr_worker, self->nfr_connection_id,
        WREATH_NFR_PROTO_HTTP1, started_ns, wreath_flight_now_ns(), headers,
        reply, self->nfr_bytes_in, self->nfr_bytes_out);
    return 0;
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
    PyObject *materialized = wreath_headers_materialize(headers);
    if (materialized == NULL) return -1;
    headers = materialized;
    Py_ssize_t n = PyList_GET_SIZE(headers);
    int key_count = 0;
    int version_count = 0;
    int result = -1;

    protocols = PyList_New(0);
    if (protocols == NULL) {
        Py_DECREF(materialized);
        return -1;
    }
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *pair = PyList_GET_ITEM(headers, i);
        PyObject *name = PyTuple_GET_ITEM(pair, 0);
        PyObject *value = PyTuple_GET_ITEM(pair, 1);
        Py_ssize_t name_size = PyBytes_GET_SIZE(name);
        const char *name_data = PyBytes_AS_STRING(name);
        if (name_size == 17 && memcmp(name_data, "sec-websocket-key", 17) == 0) {
            if (++key_count == 1) key = value;
        }
        else if (name_size == 21 &&
                 memcmp(name_data, "sec-websocket-version", 21) == 0) {
            if (++version_count == 1) version = value;
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
            key_count != 1 || version_count > 1 || !wreath_ws_key_valid(kd, ks)) {
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

    PyObject *effective_scheme = self->policy_state.scheme != NULL
        ? self->policy_state.scheme : self->scheme;
    PyObject *effective_client = self->policy_state.client != NULL
        ? self->policy_state.client : self->client_address;
    scheme = PyUnicode_CompareWithASCIIString(effective_scheme, "https") == 0
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
        PyDict_SetItem(scope, k_client, effective_client) < 0 ||
        PyDict_SetItem(scope, k_root_path, self->root_path) < 0 ||
        PyDict_SetItem(scope, k_subprotocols, protocols) < 0 ||
        PyDict_SetItemString(scope, "_wreath_policy_native", Py_True) < 0) {
        goto done;
    }

    self->ws_mode = 1;
    /* A handshake is a GET, so begin_request armed this; a session observes its
     * own `websocket.disconnect` and must not be torn out from under itself. */
    self->cancel_on_disconnect = 0;
    self->ws_accepted = 0;
    self->ws_close_sent = 0;
    self->ws_frag_opcode = -1;
    Py_CLEAR(self->ws_frag_buffer);
    self->ws_frag_size = 0;
    self->ws_frag_count = 0;
    self->body_chunks = 0;

    connect_msg = PyDict_New();
    receive_queue_clear(self, 0);
    if (connect_msg == NULL ||
        PyDict_SetItem(connect_msg, s_type, s_ws_connect) < 0 ||
        receive_queue_push(self, connect_msg) < 0) {
        goto done;
    }
    Py_CLEAR(self->receive_waiter);
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
        if (wreath_request_scope_seed_flight(scope, &self->nfr_ctx) < 0) {
            PyErr_Clear();
        }
        /* Correlate with an incoming W3C traceparent, if the client sent one. */
        Py_ssize_t header_count = wreath_headers_count(headers);
        for (Py_ssize_t i = 0; i < header_count; i++) {
            const char *name;
            const char *value;
            Py_ssize_t name_size;
            Py_ssize_t value_size;
            if (wreath_headers_view(headers, i, &name, &name_size,
                                    &value, &value_size) < 0) goto done;
            if (name_size == 11 && memcmp(name, "traceparent", 11) == 0) {
                flight_capi->context_propagate(
                    self->nfr_worker, &self->nfr_ctx,
                    (const uint8_t *)value, value_size);
                break;
            }
        }
    }

    if (spawn_app_task(self, scope) < 0) {
        goto done;
    }
    result = 1;
done:
    Py_DECREF(materialized);
    Py_XDECREF(protocols);
    Py_XDECREF(scope);
    Py_XDECREF(connect_msg);
    return result;
}


static int
begin_request(WreathHttpProtocol *self, PyObject *method, long minor, PyObject *target,
              PyObject *headers, const WreathHttpRequestMeta *request_meta)
{
    const char *td = PyBytes_AS_STRING(target);
    Py_ssize_t ts = PyBytes_GET_SIZE(target);
    Py_ssize_t q = -1;
    PyObject *raw_path = NULL;
    PyObject *query_string = NULL;
    PyObject *path = NULL;
    PyObject *scope = NULL;
    int kind = request_meta->kind;
    Py_ssize_t length = request_meta->length;
    int err_status = request_meta->err_status;
    int send_continue = request_meta->send_continue;
    int keep_alive = request_meta->keep_alive;
    int upgrade_request = request_meta->upgrade_request;
    int bad = 0;
    int result = -1;
    WreathPolicyReply policy_reply = {0};

    self->http11 = (minor == 1);
    self->method_is_head = PyUnicode_CompareWithASCIIString(method, "HEAD") == 0;
    /* Safe methods have no intended effect on the server, so losing
     * the client can cost nothing but the work in flight; everything else is
     * left to finish, because unwinding a POST rolls its transaction back and
     * not the job it already enqueued. GET first: it is the common case, and a
     * miss here costs three comparisons. */
    self->cancel_on_disconnect =
        PyUnicode_CompareWithASCIIString(method, "GET") == 0 ||
        self->method_is_head ||
        PyUnicode_CompareWithASCIIString(method, "OPTIONS") == 0 ||
        PyUnicode_CompareWithASCIIString(method, "QUERY") == 0;

    /* Splits the request target at the query separator by hand, where
     * `wreath_memmem(td, ts, "?", 1)` would dispatch the one-byte needle to
     * glibc's vectorised `memchr`. Deliberate: a request target is 30-80 bytes
     * in practice and the loop is one instruction per byte, so the whole scan
     * is tens of instructions per request. Nothing here clears a measurable
     * floor, and the rule prefers deleting work to widening it. */
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
    path = wreath_decode_request_path(
        PyBytes_AS_STRING(raw_path), PyBytes_GET_SIZE(raw_path), &bad);
    if (path == NULL) {
        result = bad ? (send_error(self, 400) < 0 ? -1 : 0) : -1;
        goto done;
    }
    /* The parser resolved framing while every validated header span was still
     * hot. Keep the body ceiling here because it belongs to this protocol
     * configuration rather than to HTTP syntax. */
    if (err_status == 0 && kind == 1 && length > self->max_body_bytes) {
        err_status = 413;
    }
    if (err_status != 0) {
        result = send_error(self, err_status) < 0 ? -1 : 0;
        goto done;
    }
    {
        int policy_result = wreath_policy_ingress(
            &self->policy, &self->policy_state, method, path, self->scheme,
            self->client_address, headers, &policy_reply);
        if (policy_result < 0) goto done;
        if (policy_result > 0) {
            result = send_recorded_policy_reply(
                self, keep_alive, headers, &policy_reply) < 0 ? -1 : 0;
            goto done;
        }
    }
    if (upgrade_request) {
        int origin_result = wreath_policy_websocket_origin(
            &self->policy, headers, &policy_reply);
        if (origin_result < 0) goto done;
        if (origin_result > 0) {
            result = send_recorded_policy_reply(
                self, keep_alive, headers, &policy_reply) < 0 ? -1 : 0;
            goto done;
        }
        if (kind == 2 || length > 0 || send_continue) {
            result = send_error(self, 400) < 0 ? -1 : 0;
        }
        else {
            result = begin_websocket(self, method, minor, path, raw_path,
                                     query_string, headers);
        }
        goto done;
    }
    if (send_continue && (kind == 2 || length > 0) &&
        transport_write_raw(self, "HTTP/1.1 100 Continue\r\n\r\n", 25) < 0) {
        goto done;
    }
    scope = build_scope(self, method, path, raw_path, query_string, headers);
    if (scope == NULL) {
        goto done;
    }

    reset_response_state(self, keep_alive);
    receive_queue_clear(self, 0);
    Py_CLEAR(self->receive_waiter);
    self->queued_bytes = 0;
    self->disconnected = 0;
    self->request_more_body = 1;
    self->remaining = 0;
    self->chunk_remaining = 0;
    self->body_received = 0;
    self->body_chunks = 0;

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
        Py_ssize_t header_count = wreath_headers_count(headers);
        for (Py_ssize_t i = 0; i < header_count; i++) {
            const char *name;
            const char *value;
            Py_ssize_t name_size;
            Py_ssize_t value_size;
            if (wreath_headers_view(headers, i, &name, &name_size,
                                    &value, &value_size) < 0) goto done;
            if (name_size == 11 && memcmp(name, "traceparent", 11) == 0) {
                flight_capi->context_propagate(
                    self->nfr_worker, &self->nfr_ctx,
                    (const uint8_t *)value, value_size);
                break;
            }
        }
        /* The arming decision (and its scratch slot) is made by context_start,
         * after build_scope attached the recorder context, so the armed state is
         * promoted onto the context here — before dispatch reads `flight`. An
         * armed context is retained so completion can sever its borrowed
         * recorder pointers; unarmed requests retain nothing. */
        if (wreath_request_context_check(scope) &&
            wreath_request_context_set_armed(scope)) {
            Py_INCREF(scope);
            Py_XSETREF(self->nfr_http_scope, scope);
        }
    }

    if (spawn_app_task(self, scope) < 0) {
        goto done;
    }
    result = 1;  /* continue draining any buffered body */
done:
    wreath_policy_reply_clear(&policy_reply);
    Py_XDECREF(raw_path);
    Py_XDECREF(query_string);
    Py_XDECREF(path);
    Py_XDECREF(scope);
    return result;
}


/* --- websocket ------------------------------------------------------------
 *
 * Mirrors the Python reference: the upgrade handshake, the ASGI websocket
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
        wreath_store_u16_be(header + 2, (uint16_t)size);
        header_len = 4;
    }
    else {
        header[1] = 127;
        wreath_store_u64_be(header + 2, (uint64_t)size);
        header_len = 10;
    }
    if (payload_obj != NULL && size > 16384 && self->transport_writelines_fn != NULL &&
        self->transport != NULL && !self->closing) {
        PyObject *head_bytes = PyBytes_FromStringAndSize((const char *)header, header_len);
        PyObject *pair;
        if (head_bytes == NULL) {
            return -1;
        }
        pair = PyTuple_Pack(2, head_bytes, payload_obj);
        Py_DECREF(head_bytes);
        if (pair == NULL) {
            return -1;
        }
        if (transport_writelines(self, pair) < 0) {
            Py_DECREF(pair);
            return -1;
        }
        Py_DECREF(pair);
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
        wreath_store_u16_be((uint8_t *)payload, (uint16_t)code);
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
 * counts bytes -- identical to the Python reference). */
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
        if (rc == 0) {
            self->body_chunks = 0;
        }
        return rc;
    }
    int rc = ws_enqueue_value(self, opcode, payload);
    if (rc == 0) {
        self->body_chunks = 0;
    }
    return rc;
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
    fin = header.fin;
    opcode = header.opcode;
    if ((opcode == WS_OP_CLOSE || opcode == WS_OP_PING || opcode == WS_OP_PONG) &&
        (!fin || header.payload_len > 125)) {
        return ws_fail(self, 1002) < 0 ? -1 : 0;
    }
    if ((opcode == WS_OP_PING || opcode == WS_OP_PONG) &&
        ++self->body_chunks > WS_MAX_UNPRODUCTIVE_CONTROL_FRAMES) {
        return ws_fail(self, 1008) < 0 ? -1 : 0;
    }
    /* Size limits are enforced per message on delivery (and per buffered
     * fragment), matching the Python reference's order of checks. */
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
                code = wreath_load_u16_be((const uint8_t *)data);
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
            if (rc == 0) {
                self->body_chunks = 0;
            }
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
    WreathHttpRequestMeta request_meta = {0};
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
        &consumed, self->max_header_count, &request_meta
    );
    if (parsed == -2) {
        return send_error(self, 431) < 0 ? -1 : 0;
    }
    if (parsed < 0) {
        if (PyErr_ExceptionMatches(PyExc_ValueError)) {
            PyErr_Clear();
            return send_error(self, 400) < 0 ? -1 : 0;
        }
        return -1;
    }
    if (parsed == 0) return 0;
    if (minor == 1 && request_meta.host_count != 1) {
        Py_DECREF(method);
        Py_DECREF(target);
        Py_DECREF(headers);
        return send_error(self, 400) < 0 ? -1 : 0;
    }
    do_consume(self, consumed);
    if (set_deadline(self, self->request_timeout, 1) < 0) {
        Py_DECREF(method);
        Py_DECREF(target);
        Py_DECREF(headers);
        return -1;
    }
    rc = begin_request(self, method, minor, target, headers, &request_meta);
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
    if (self->body_chunks >= self->max_body_chunks) {
        return body_error(self, 413) < 0 ? -1 : 0;
    }
    self->body_chunks++;
    if (chunk_size > self->max_body_bytes ||
        self->body_received > self->max_body_bytes - chunk_size) {
        return body_error(self, 413) < 0 ? -1 : 0;
    }
    self->body_received += chunk_size;
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
        if (!validate_trailer_block(p, end + 2, self->max_header_count)) {
            return body_error(self, 400) < 0 ? -1 : 0;
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


static void
shrink_idle_input_buffer(WreathHttpProtocol *self)
{
    const Py_ssize_t retained = 32768;
    if (self->buf_cap <= retained || self->buf_len != 0 || self->cursor != 0 ||
        self->read_exports > 0 || self->read_offer_size > 0) {
        return;
    }
    char *shrunk = PyMem_Realloc(self->buf, (size_t)retained);
    if (shrunk != NULL) {
        self->buf = shrunk;
        self->buf_cap = retained;
    }
}

static int
reset_request(WreathHttpProtocol *self)
{
    receive_queue_clear(self, 0);
    Py_CLEAR(self->receive_waiter);
    self->pending_empty_request = 0;
    self->receive_head = 0;
    self->queued_messages = 0;
    self->queued_bytes = 0;
    self->remaining = 0;
    self->chunk_remaining = 0;
    self->body_received = 0;
    self->body_chunks = 0;
    self->nfr_bytes_in = 0;
    self->nfr_bytes_out = 0;
    if (self->reading_paused) {
        if (transport_method0(self, "resume_reading") < 0) {
            return -1;
        }
        self->reading_paused = 0;
    }
    shrink_idle_input_buffer(self);
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
    self->native_transport = 0;
    if (strcmp(Py_TYPE(transport)->tp_name,
               "wreath._native._reactor.SocketTransport") == 0) {
        load_transport_capi();
        self->native_transport = transport_capi != NULL &&
            transport_capi->version == WREATH_TRANSPORT_CAPI_VERSION &&
            transport_capi->check(transport);
    }
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
    if (run_drive(self) < 0 || pause_pipeline_if_needed(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


/* --- asyncio.BufferedProtocol receive path -------------------------------- */

/* Bounded per-read receive target. asyncio passes sizehint=-1; smaller
 * positive hints are honored, larger ones are capped (the transport simply
 * performs another read cycle). Private until measurements justify config. */
#define WREATH_RECV_CHUNK 32768

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


static int
http_prepare_read(WreathHttpProtocol *self, Py_ssize_t sizehint,
                  char **buffer, Py_ssize_t *capacity)
{
    Py_ssize_t target;
    if (self->read_offer_size > 0 || self->read_exports > 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "get_buffer() called while a previous read offer is live");
        return -1;
    }
    apply_deferred_compaction(self);
    target = (sizehint > 0 && sizehint < WREATH_RECV_CHUNK) ? sizehint : WREATH_RECV_CHUNK;
    if (buf_reserve(self, target) < 0) {
        return -1;
    }
    self->read_offer_offset = self->buf_len;
    self->read_offer_size = self->buf_cap - self->buf_len;
    *buffer = self->buf + self->read_offer_offset;
    *capacity = self->read_offer_size;
    return 0;
}


static int
http_commit_read_buffer(WreathHttpProtocol *self, Py_ssize_t nbytes)
{
    if (self->read_offer_size <= 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "buffer_updated() without an active read offer");
        return -1;
    }
    if (nbytes < 0 || nbytes > self->read_offer_size) {
        self->read_offer_size = 0;
        self->read_offer_offset = 0;
        PyErr_SetString(PyExc_ValueError,
                        "buffer_updated() byte count is out of range");
        return -1;
    }
    self->buf_len = self->read_offer_offset + nbytes;
    self->read_offer_size = 0;
    self->read_offer_offset = 0;
    if (nbytes == 0 || self->closing) {
        return 0;
    }
    int result = run_drive(self);
    if (result < 0 || pause_pipeline_if_needed(self) < 0) {
        return -1;
    }
    return result;
}


static PyObject *
http_get_buffer(WreathHttpProtocol *self, PyObject *arg)
{
    Py_ssize_t sizehint = PyLong_AsSsize_t(arg);
    char *buffer;
    Py_ssize_t capacity;
    PyObject *view;
    if (sizehint == -1 && PyErr_Occurred()) {
        return NULL;
    }
    if (http_prepare_read(self, sizehint, &buffer, &capacity) < 0) {
        return NULL;
    }
    view = PyMemoryView_FromObject((PyObject *)self);
    if (view == NULL) {
        self->read_offer_offset = 0;
        self->read_offer_size = 0;
    }
    return view;
}


static PyObject *
http_buffer_updated(WreathHttpProtocol *self, PyObject *arg)
{
    Py_ssize_t nbytes = PyLong_AsSsize_t(arg);
    if ((nbytes == -1 && PyErr_Occurred()) ||
        http_commit_read_buffer(self, nbytes) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}


static PyTypeObject *http1_protocol_type = NULL;

void
wreath_http1_protocol_set_type(PyObject *type)
{
    http1_protocol_type = type == NULL ? NULL : (PyTypeObject *)type;
}

int
wreath_http1_protocol_check(PyObject *protocol)
{
    return http1_protocol_type != NULL &&
           PyObject_TypeCheck(protocol, http1_protocol_type);
}

int
wreath_http1_acquire_read_buffer(PyObject *protocol, char **buffer,
                                 Py_ssize_t *capacity)
{
    if (!wreath_http1_protocol_check(protocol)) {
        PyErr_SetString(PyExc_TypeError, "expected native Http1Protocol");
        return -1;
    }
    return http_prepare_read((WreathHttpProtocol *)protocol, -1, buffer, capacity);
}

int
wreath_http1_commit_read(PyObject *protocol, Py_ssize_t nbytes)
{
    if (!wreath_http1_protocol_check(protocol)) {
        PyErr_SetString(PyExc_TypeError, "expected native Http1Protocol");
        return -1;
    }
    return http_commit_read_buffer((WreathHttpProtocol *)protocol, nbytes);
}


int
wreath_http1_feed_external(PyObject *protocol, const char *data, Py_ssize_t size)
{
    if (!wreath_http1_protocol_check(protocol)) {
        PyErr_SetString(PyExc_TypeError, "expected native Http1Protocol");
        return -1;
    }
    if (size < 0) {
        PyErr_SetString(PyExc_ValueError, "negative external read size");
        return -1;
    }
    if (size == 0) {
        return 0;
    }

    WreathHttpProtocol *self = (WreathHttpProtocol *)protocol;
    apply_deferred_compaction(self);
    if (self->buf_len != 0 || self->cursor != 0) {
        if (buf_reserve(self, size) < 0) {
            return -1;
        }
        memcpy(self->buf + self->buf_len, data, (size_t)size);
        self->buf_len += size;
        int result = run_drive(self);
        if (result < 0 || pause_pipeline_if_needed(self) < 0) {
            return -1;
        }
        return result;
    }
    if (self->read_offer_size > 0 || self->read_exports > 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "external read while parser storage is exported");
        return -1;
    }

    /* Borrow the provided-buffer slice only for this synchronous drive. Every
     * parsed object takes ownership of data it retains. An incomplete tail is
     * copied back into parser-owned storage before the reactor recycles it. */
    char *owned_buffer = self->buf;
    Py_ssize_t owned_capacity = self->buf_cap;
    self->buf = (char *)data;
    self->buf_cap = size;
    self->buf_len = size;
    self->cursor = 0;
    self->read_exports = 1;  /* suppress compaction of borrowed storage */
    int result = run_drive(self);
    Py_ssize_t consumed = self->cursor;
    Py_ssize_t remaining = self->buf_len - consumed;

    self->buf = owned_buffer;
    self->buf_cap = owned_capacity;
    self->buf_len = 0;
    self->cursor = 0;
    self->read_exports = 0;
    self->compact_pending = 0;
    if (result < 0) {
        return -1;
    }
    if (remaining > 0) {
        if (buf_reserve(self, remaining) < 0) {
            return -1;
        }
        memcpy(self->buf, data + consumed, (size_t)remaining);
        self->buf_len = remaining;
    }
    return pause_pipeline_if_needed(self);
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


/* What the facade's graceful close polls to decide whether this connection
 * still owes a response.
 *
 * HTTP/1.1 carries one request at a time, so the count is 0 or 1 and the
 * condition is the same one `http_stop_accepting` closes on: parked in
 * ST_READING_HEAD with no application task is idle, anything else is in
 * flight. Published because `Server._has_work_to_drain` reads this attribute
 * with `getattr(..., None)` and treats a missing one as *busy* -- a shipped
 * protocol falling into that fallback makes every graceful close spend the
 * whole shutdown_timeout draining a connection that owes nothing. */
static PyObject *
http_get_active_requests(PyObject *op, void *Py_UNUSED(closure))
{
    WreathHttpProtocol *self = (WreathHttpProtocol *)op;
    int idle = (self->state == ST_READING_HEAD && self->task == NULL);
    return PyLong_FromLong(idle ? 0 : 1);
}


static PyGetSetDef http_protocol_getset[] = {
    {"active_requests", http_get_active_requests, NULL,
     "Requests this connection still owes a response for (0 or 1).", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};


/* --- timeouts ------------------------------------------------------------ */

/* The owned enforcement for the currently-armed deadline: a keep-alive deadline
 * closes; a request deadline aborts a started response or answers 408. Shared by
 * the real timer callback and the replay trigger. */
static int
enforce_deadline(WreathHttpProtocol *self)
{
    if (!self->deadline_is_request) {
        return protocol_close(self);
    }
    if (self->response_started) {
        return protocol_abort(self);
    }
    return send_error(self, 408);
}

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
    if (enforce_deadline(self) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

/* Replay/test only: force the currently-armed deadline's owned enforcement,
 * bypassing the wall clock so a virtual-clock TIMEOUT fault fires the real timeout
 * path deterministically. A no-op when no finite deadline is armed. */
static PyObject *
http_replay_fire_timeout(WreathHttpProtocol *self, PyObject *Py_UNUSED(ignored))
{
    if (self->closing || isinf(self->deadline)) {
        Py_RETURN_NONE;
    }
    Py_CLEAR(self->timer_handle);
    if (enforce_deadline(self) < 0) {
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
    PyObject *exc = wreath_task_exception(task);
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
        /* Mirror the Python reference's _run_ws_app post-conditions. The session's
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
    /* The armed context (if any) is spent with its completion: sever its
     * recorder pointers so any escaped `_flight_phase` binding goes inert. */
    if (self->nfr_http_scope != NULL) {
        wreath_request_context_sever(self->nfr_http_scope);
        Py_CLEAR(self->nfr_http_scope);
    }
    Py_CLEAR(self->policy_context);
    wreath_policy_state_clear(&self->policy_state);
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
    Py_VISIT(self->default_response_headers);
    Py_VISIT(self->default_response_wire);
    Py_VISIT(self->response_header_cache_key);
    Py_VISIT(self->response_header_cache_wire);
    Py_VISIT(self->loop_create_future);
    Py_VISIT(self->loop_create_task);
    Py_VISIT(self->loop_call_later);
    Py_VISIT(self->deadline_callable);
    Py_VISIT(self->policy.descriptor);
    Py_VISIT(self->policy_state.client);
    Py_VISIT(self->policy_state.scheme);
    Py_VISIT(self->policy_state.origin);
    Py_VISIT(self->policy_state.request_id);
    Py_VISIT(self->policy_state.csrf_token);
    Py_VISIT(self->policy_context);
    for (Py_ssize_t i = self->receive_head; i < self->receive_queue_len; i++) {
        Py_VISIT(self->receive_queue[i]);
    }
    Py_VISIT(self->receive_waiter);
    Py_VISIT(self->drain_waiter);
    Py_VISIT(self->task);
    Py_VISIT(self->timer_handle);
    Py_VISIT(self->response_bytes);
    Py_VISIT(self->ws_frag_buffer);
    Py_VISIT(self->ws_key);
    Py_VISIT(self->nfr_ws_scope);
    Py_VISIT(self->nfr_http_scope);
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
    Py_CLEAR(self->default_response_headers);
    Py_CLEAR(self->default_response_wire);
    Py_CLEAR(self->response_header_cache_key);
    Py_CLEAR(self->response_header_cache_wire);
    Py_CLEAR(self->loop_create_future);
    Py_CLEAR(self->loop_create_task);
    Py_CLEAR(self->loop_call_later);
    Py_CLEAR(self->deadline_callable);
    wreath_policy_program_clear(&self->policy);
    wreath_policy_state_clear(&self->policy_state);
    Py_CLEAR(self->policy_context);
    receive_queue_clear(self, 1);
    Py_CLEAR(self->receive_waiter);
    Py_CLEAR(self->drain_waiter);
    Py_CLEAR(self->task);
    Py_CLEAR(self->timer_handle);
    clear_response_builder(self);
    Py_CLEAR(self->ws_frag_buffer);
    Py_CLEAR(self->ws_key);
    Py_CLEAR(self->nfr_ws_scope);
    if (self->nfr_http_scope != NULL) {
        /* The context may be kept alive elsewhere (an escaped marker binding);
         * sever before dropping so it cannot reach the dying protocol. */
        wreath_request_context_sever(self->nfr_http_scope);
        Py_CLEAR(self->nfr_http_scope);
    }
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
        wreath_policy_state_init(&self->policy_state);
        self->state = ST_READING_HEAD;
        self->accepting = 1;
        self->http11 = 1;  /* pre-request default matches the Python reference */
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
    if (self->native_app != NULL &&
        wreath_policy_program_load(&self->policy, app) < 0) {
        return -1;
    }
    Py_INCREF(config);
    Py_XSETREF(self->config, config);
    Py_INCREF(loop);
    Py_XSETREF(self->loop, loop);
    Py_INCREF(registry);
    Py_XSETREF(self->registry, registry);

    /* Resolve the default response header sequence once per connection (config
     * is immutable while serving), so begin_response_parts does not re-walk the
     * attribute protocol twice on every response. Config-dependent, so this
     * runs on every init rather than only the first. */
    {
        PyObject *defaults = PyObject_GetAttrString(config, "_default_response_headers");
        PyObject *header_seq;
        PyObject *wire;
        if (defaults == NULL) {
            return -1;
        }
        header_seq = PyObject_GetAttrString(defaults, "headers");
        if (header_seq == NULL) {
            Py_DECREF(defaults);
            return -1;
        }
        wire = PyObject_GetAttrString(defaults, "wire");
        Py_DECREF(defaults);
        if (wire == NULL) {
            Py_DECREF(header_seq);
            return -1;
        }
        if (!PyByteArray_Check(wire)) {
            Py_DECREF(header_seq);
            Py_DECREF(wire);
            PyErr_SetString(PyExc_TypeError, "default response wire must be bytearray");
            return -1;
        }
        Py_XSETREF(self->default_response_headers, header_seq);
        Py_XSETREF(self->default_response_wire, wire);
    }

    if (read_ssize_attr(config, "max_request_line", &self->max_request_line) < 0 ||
        read_ssize_attr(config, "max_header_count", &self->max_header_count) < 0 ||
        read_ssize_attr(config, "max_header_bytes", &self->max_header_bytes) < 0 ||
        read_ssize_attr(config, "max_body_bytes", &self->max_body_bytes) < 0 ||
        read_ssize_attr(config, "max_body_chunks", &self->max_body_chunks) < 0 ||
        read_ssize_attr(config, "read_high_water", &self->read_high_water) < 0 ||
        read_ssize_attr(config, "read_high_water_messages",
                        &self->read_high_water_messages) < 0 ||
        read_ssize_attr(config, "max_ws_fragments", &self->max_ws_fragments) < 0 ||
        read_double_attr(config, "keep_alive_timeout", &self->keep_alive_timeout) < 0 ||
        read_double_attr(config, "request_timeout", &self->request_timeout) < 0) {
        return -1;
    }

    if (self->receive_callable == NULL) {
        self->receive_callable = PyObject_GetAttrString(
            (PyObject *)self,
            self->native_app != NULL ? "_wreath_receive" : "_asgi_receive");
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
    if (self->receive_callable == NULL || self->send_callable == NULL ||
        self->done_callable == NULL ||
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
    {"_wreath_receive", (PyCFunction)http_wreath_receive, METH_NOARGS,
     "Private body-slot receive callable for Wreath."},
    {"_asgi_send", (PyCFunction)http_asgi_send, METH_O,
     "ASGI send callable (returns an awaitable)."},
    {"_wreath_response", (PyCFunction)(void (*)(void))http_wreath_response,
     METH_FASTCALL, "Emit one complete Wreath response without an ASGI message dict."},
    {"_wreath_response_nowait",
     (PyCFunction)(void (*)(void))http_wreath_response_nowait, METH_FASTCALL,
     "Emit one response, returning an awaitable only when backpressure suspends."},
    {"_wreath_stream_start",
     (PyCFunction)(void (*)(void))http_wreath_stream_start, METH_FASTCALL,
     "Start a Wreath stream without an ASGI message dict."},
    {"_wreath_stream_body",
     (PyCFunction)(void (*)(void))http_wreath_stream_body, METH_FASTCALL,
     "Emit a Wreath stream body slot without an ASGI message dict."},
    {"_wreath_cancel_on_disconnect",
     (PyCFunction)http_wreath_cancel_on_disconnect, METH_O,
     "Override this request's cancel-on-disconnect for the route matched."},
    {"_wreath_file_start", (PyCFunction)(void (*)(void))http_wreath_file_start,
     METH_FASTCALL, "Start a private wreath.file descriptor response."},
    {"_wreath_file_finish", (PyCFunction)http_wreath_file_finish, METH_NOARGS,
     "Finish a private wreath.file descriptor response."},
    {"_on_app_done", (PyCFunction)http_on_app_done, METH_O,
     "Finalize a completed application task."},
    {"_on_deadline", (PyCFunction)http_on_deadline, METH_NOARGS,
     "Enforce or re-arm the connection's timeout deadline."},
    {"_replay_fire_timeout", (PyCFunction)http_replay_fire_timeout, METH_NOARGS,
     "Replay only: force the armed deadline's owned enforcement."},
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
    {Py_tp_getset, (void *)http_protocol_getset},
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
