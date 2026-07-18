#include "server.h"

/* --- shared module globals ---------------------------------------------- */
/* Borrowed from the live module; cleared by server_module_free(). */
PyObject *disconnect_error = NULL;    /* module-private _Disconnect */
PyObject *immediate_none = NULL;       /* stateless completed awaitable */


/* Cached callables for the per-request hot path. */
PyObject *task_class = NULL;               /* asyncio.Task */
PyObject *task_kwnames = NULL;             /* ("loop", "eager_start") */
PyObject *task_add_done_callback = NULL;   /* unbound Task.add_done_callback */
PyObject *task_exception_fn = NULL;        /* unbound Task.exception */
PyObject *invalid_state_error = NULL;      /* asyncio.InvalidStateError */

/* Interned key/value constants so hot dict operations skip per-call string
 * creation and hashing. */
PyObject *s_type = NULL;
PyObject *s_body = NULL;
PyObject *s_more_body = NULL;
PyObject *s_status = NULL;
PyObject *s_headers = NULL;
PyObject *s_http_request = NULL;      /* "http.request" */
PyObject *s_http_disconnect = NULL;   /* "http.disconnect" */
PyObject *s_resp_start = NULL;        /* "http.response.start" */
PyObject *s_resp_body = NULL;         /* "http.response.body" */
PyObject *s_wreath_response = NULL;      /* "wreath.response" one-shot message */
PyObject *k_extensions = NULL;
PyObject *extensions_dict = NULL;     /* {"wreath.response": {}} shared */

/* WebSocket constants and cold-path helpers. */
const WreathCoreCAPI *core_capi = NULL;  /* C-level parsers from _core */

/* Native Flight Recorder vtable, resolved once (optional). */
const WreathFlightCAPI *flight_capi = NULL;
static _Atomic uint64_t nfr_connection_counter = 0;

uint64_t
wreath_flight_now_ns(void)
{
    PyTime_t now = 0;
    (void)PyTime_MonotonicRaw(&now);
    return (uint64_t)now;
}

uint64_t
wreath_flight_next_connection_id(void)
{
    return atomic_fetch_add_explicit(&nfr_connection_counter, 1,
                                     memory_order_relaxed) + 1;
}

wreath_nfr_worker *
wreath_flight_worker_from(PyObject *recorder)
{
    if (recorder == NULL || recorder == Py_None) {
        return NULL;
    }
    PyObject *method = PyObject_GetAttrString(recorder, "worker_capsule");
    if (method == NULL) {
        PyErr_Clear();
        return NULL;
    }
    PyObject *capsule = PyObject_CallNoArgs(method);
    Py_DECREF(method);
    if (capsule == NULL) {
        PyErr_Clear();
        return NULL;
    }
    void *pointer = PyCapsule_GetPointer(capsule, WREATH_FLIGHT_WORKER_CAPSULE);
    Py_DECREF(capsule);
    if (pointer == NULL) {
        PyErr_Clear();
        return NULL;
    }
    return (wreath_nfr_worker *)pointer;
}
PyObject *sha1_fn = NULL;             /* hashlib.sha1 */
PyObject *b64encode_fn = NULL;        /* base64.b64encode */
PyObject *s_websocket = NULL;         /* "websocket" scope type */
PyObject *s_ws_scheme = NULL;         /* "ws" */
PyObject *s_wss_scheme = NULL;        /* "wss" */
PyObject *s_ws_connect = NULL;        /* "websocket.connect" */
PyObject *s_ws_receive = NULL;        /* "websocket.receive" */
PyObject *s_ws_disconnect = NULL;     /* "websocket.disconnect" */
PyObject *s_ws_send_msg = NULL;       /* "websocket.send" */
PyObject *s_ws_accept_msg = NULL;     /* "websocket.accept" */
PyObject *s_ws_close_msg = NULL;      /* "websocket.close" */
PyObject *s_text = NULL;
PyObject *s_bytes = NULL;
PyObject *s_code = NULL;
PyObject *s_reason = NULL;
PyObject *s_subprotocol = NULL;
PyObject *k_subprotocols = NULL;
PyObject *header_host = NULL;  /* b"host", synthesized once per absent host */
PyObject *k_asgi = NULL;
PyObject *k_http_version = NULL;
PyObject *k_method = NULL;
PyObject *k_scheme = NULL;
PyObject *k_path = NULL;
PyObject *k_raw_path = NULL;
PyObject *k_query_string = NULL;
PyObject *k_server = NULL;
PyObject *k_client = NULL;
PyObject *k_root_path = NULL;


typedef struct {
    PyObject_HEAD
} ImmediateAwaitable;


static PyObject *
immediate_await(PyObject *self)
{
    return Py_NewRef(self);
}


static PyObject *
immediate_next(PyObject *Py_UNUSED(self))
{
    PyErr_SetObject(PyExc_StopIteration, Py_None);
    return NULL;
}


static PyAsyncMethods immediate_async = {
    .am_await = immediate_await,
};


PyTypeObject ImmediateAwaitableType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._server._ImmediateAwaitable",
    .tp_basicsize = sizeof(ImmediateAwaitable),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = PyType_GenericNew,
    .tp_as_async = &immediate_async,
    .tp_iter = PyObject_SelfIter,
    .tp_iternext = immediate_next,
};


PyObject *
completed_none(void)
{
    return Py_NewRef(immediate_none);
}


/* An awaitable that resolves to a value on the first await without ever
 * suspending the coroutine.  It replaces loop.create_future()+set_result()
 * when an ASGI receive() can be satisfied immediately, which both removes
 * the Future machinery from the hot path and lets an eagerly started
 * application task run to completion in a single step. */
typedef struct {
    PyObject_HEAD
    PyObject *value;
} ValueAwaitable;


static PyObject *
value_awaitable_next(PyObject *op)
{
    ValueAwaitable *self = (ValueAwaitable *)op;
    PyObject *value = self->value;
    if (value == NULL) {
        PyErr_SetNone(PyExc_StopIteration);
        return NULL;
    }
    self->value = NULL;
    if (PyTuple_Check(value) || PyExceptionInstance_Check(value)) {
        /* Ambiguous for PyErr_SetObject: wrap in an explicit instance. */
        PyObject *exc = PyObject_CallOneArg(PyExc_StopIteration, value);
        Py_DECREF(value);
        if (exc == NULL) {
            return NULL;
        }
        PyErr_SetObject(PyExc_StopIteration, exc);
        Py_DECREF(exc);
        return NULL;
    }
    PyErr_SetObject(PyExc_StopIteration, value);
    Py_DECREF(value);
    return NULL;
}


static void
value_awaitable_dealloc(PyObject *op)
{
    ValueAwaitable *self = (ValueAwaitable *)op;
    Py_XDECREF(self->value);
    Py_TYPE(op)->tp_free(op);
}


static PyAsyncMethods value_awaitable_async = {
    .am_await = immediate_await,
};


PyTypeObject ValueAwaitableType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._server._ValueAwaitable",
    .tp_basicsize = sizeof(ValueAwaitable),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_dealloc = value_awaitable_dealloc,
    .tp_as_async = &value_awaitable_async,
    .tp_iter = PyObject_SelfIter,
    .tp_iternext = value_awaitable_next,
};


PyObject *
completed_value(PyObject *value)
{
    ValueAwaitable *aw = PyObject_New(ValueAwaitable, &ValueAwaitableType);
    if (aw == NULL) {
        return NULL;
    }
    aw->value = Py_NewRef(value);
    return (PyObject *)aw;
}


/* --- small helpers ------------------------------------------------------- */

Py_ssize_t
find_sub(const char *hay, Py_ssize_t n, const char *needle, Py_ssize_t m)
{
    if (m == 0) {
        return 0;
    }
    for (Py_ssize_t i = 0; i + m <= n; i++) {
        if (hay[i] == needle[0] && memcmp(hay + i, needle, (size_t)m) == 0) {
            return i;
        }
    }
    return -1;
}


/* Resumable delimiter search.
 *
 * A slow peer delivers a head or chunk-size line a byte at a time, and each
 * arrival re-drives the parser. Searching from offset zero every time makes the
 * work quadratic in the buffered prefix even though only one byte is new. The
 * caller keeps `*scan_from` per parser state, so each byte is examined a
 * constant number of times instead.
 *
 * On a miss, `*scan_from` advances to the last position that could still start
 * a match once more bytes arrive: a needle beginning in the final
 * `needle_len - 1` bytes is not yet decidable, so that suffix must be rescanned
 * and everything before it must not be. Offsets are relative to the caller's
 * `hay`, so they stay valid if the backing allocation moves.
 */
Py_ssize_t
find_sub_from(
    const char *hay, Py_ssize_t hay_len, const char *needle, Py_ssize_t needle_len,
    Py_ssize_t *scan_from
)
{
    Py_ssize_t start = *scan_from;
    if (needle_len <= 0) {
        return 0;
    }
    if (start < 0) {
        start = 0;
    }
    if (start > hay_len) {
        start = hay_len;
    }
    for (Py_ssize_t i = start; i + needle_len <= hay_len; i++) {
        if (hay[i] == needle[0] && memcmp(hay + i, needle, (size_t)needle_len) == 0) {
            /* Leave the cursor at the match: the caller may be called again
             * before consuming, and the match must not be skipped. */
            *scan_from = i;
            return i;
        }
    }
    Py_ssize_t resume = hay_len - (needle_len - 1);
    *scan_from = resume > 0 ? resume : 0;
    return -1;
}


int
contains_ci(const char *hay, Py_ssize_t n, const char *needle)
{
    Py_ssize_t m = (Py_ssize_t)strlen(needle);
    if (m == 0) {
        return 1;
    }
    for (Py_ssize_t i = 0; i + m <= n; i++) {
        Py_ssize_t j = 0;
        for (; j < m; j++) {
            char c = hay[i + j];
            if (c >= 'A' && c <= 'Z') {
                c = (char)(c + 32);
            }
            if (c != needle[j]) {
                break;
            }
        }
        if (j == m) {
            return 1;
        }
    }
    return 0;
}


const char *
reason_phrase(int status, Py_ssize_t *size)
{
    const char *reason;
    switch (status) {
        case 200: reason = "OK"; break;
        case 204: reason = "No Content"; break;
        case 304: reason = "Not Modified"; break;
        case 400: reason = "Bad Request"; break;
        case 403: reason = "Forbidden"; break;
        case 408: reason = "Request Timeout"; break;
        case 413: reason = "Payload Too Large"; break;
        case 414: reason = "URI Too Long"; break;
        case 417: reason = "Expectation Failed"; break;
        case 426: reason = "Upgrade Required"; break;
        case 431: reason = "Request Header Fields Too Large"; break;
        case 500: reason = "Internal Server Error"; break;
        case 505: reason = "HTTP Version Not Supported"; break;
        default: reason = "Unknown"; break;
    }
    *size = (Py_ssize_t)strlen(reason);
    return reason;
}


int
append_raw(PyObject *buffer, const char *data, Py_ssize_t size)
{
    Py_ssize_t offset = PyByteArray_GET_SIZE(buffer);
    if (size < 0 || offset > PY_SSIZE_T_MAX - size) {
        PyErr_SetString(PyExc_OverflowError, "HTTP response is too large");
        return -1;
    }
    if (PyByteArray_Resize(buffer, offset + size) < 0) {
        return -1;
    }
    memcpy(PyByteArray_AS_STRING(buffer) + offset, data, (size_t)size);
    return 0;
}


int
append_decimal(PyObject *buffer, Py_ssize_t value)
{
    char digits[32];
    int size = PyOS_snprintf(digits, sizeof(digits), "%zd", value);
    if (size < 0 || size >= (int)sizeof(digits)) {
        PyErr_SetString(PyExc_OverflowError, "integer formatting failed");
        return -1;
    }
    return append_raw(buffer, digits, size);
}


/* --- module lifecycle ---------------------------------------------------- */


void
server_module_free(void *Py_UNUSED(module))
{
    Py_CLEAR(immediate_none);
    Py_CLEAR(task_class);
    Py_CLEAR(task_kwnames);
    Py_CLEAR(task_add_done_callback);
    Py_CLEAR(invalid_state_error);
    Py_CLEAR(task_exception_fn);
    Py_CLEAR(header_host);
    Py_CLEAR(s_type);
    Py_CLEAR(s_body);
    Py_CLEAR(s_more_body);
    Py_CLEAR(s_status);
    Py_CLEAR(s_headers);
    Py_CLEAR(s_http_request);
    Py_CLEAR(s_http_disconnect);
    Py_CLEAR(s_resp_start);
    Py_CLEAR(s_resp_body);
    Py_CLEAR(s_wreath_response);
    Py_CLEAR(k_extensions);
    Py_CLEAR(extensions_dict);
    core_capi = NULL;
    Py_CLEAR(sha1_fn);
    Py_CLEAR(b64encode_fn);
    Py_CLEAR(s_websocket);
    Py_CLEAR(s_ws_scheme);
    Py_CLEAR(s_wss_scheme);
    Py_CLEAR(s_ws_connect);
    Py_CLEAR(s_ws_receive);
    Py_CLEAR(s_ws_disconnect);
    Py_CLEAR(s_ws_send_msg);
    Py_CLEAR(s_ws_accept_msg);
    Py_CLEAR(s_ws_close_msg);
    Py_CLEAR(s_text);
    Py_CLEAR(s_bytes);
    Py_CLEAR(s_code);
    Py_CLEAR(s_reason);
    Py_CLEAR(s_subprotocol);
    Py_CLEAR(k_subprotocols);
    Py_CLEAR(k_asgi);
    Py_CLEAR(k_http_version);
    Py_CLEAR(k_method);
    Py_CLEAR(k_scheme);
    Py_CLEAR(k_path);
    Py_CLEAR(k_raw_path);
    Py_CLEAR(k_query_string);
    Py_CLEAR(k_server);
    Py_CLEAR(k_client);
    Py_CLEAR(k_root_path);
    disconnect_error = NULL;
}


int
init_cached_constants(void)
{
    PyObject *asyncio;

    if ((s_type = PyUnicode_InternFromString("type")) == NULL ||
        (s_body = PyUnicode_InternFromString("body")) == NULL ||
        (s_more_body = PyUnicode_InternFromString("more_body")) == NULL ||
        (s_status = PyUnicode_InternFromString("status")) == NULL ||
        (s_headers = PyUnicode_InternFromString("headers")) == NULL ||
        (s_http_request = PyUnicode_InternFromString("http.request")) == NULL ||
        (s_http_disconnect = PyUnicode_InternFromString("http.disconnect")) == NULL ||
        (s_resp_start = PyUnicode_InternFromString("http.response.start")) == NULL ||
        (s_resp_body = PyUnicode_InternFromString("http.response.body")) == NULL ||
        (s_wreath_response = PyUnicode_InternFromString("wreath.response")) == NULL ||
        (k_extensions = PyUnicode_InternFromString("extensions")) == NULL ||
        (k_asgi = PyUnicode_InternFromString("asgi")) == NULL ||
        (k_http_version = PyUnicode_InternFromString("http_version")) == NULL ||
        (k_method = PyUnicode_InternFromString("method")) == NULL ||
        (k_scheme = PyUnicode_InternFromString("scheme")) == NULL ||
        (k_path = PyUnicode_InternFromString("path")) == NULL ||
        (k_raw_path = PyUnicode_InternFromString("raw_path")) == NULL ||
        (k_query_string = PyUnicode_InternFromString("query_string")) == NULL ||
        (k_server = PyUnicode_InternFromString("server")) == NULL ||
        (k_client = PyUnicode_InternFromString("client")) == NULL ||
        (k_root_path = PyUnicode_InternFromString("root_path")) == NULL ||
        (s_websocket = PyUnicode_InternFromString("websocket")) == NULL ||
        (s_ws_scheme = PyUnicode_InternFromString("ws")) == NULL ||
        (s_wss_scheme = PyUnicode_InternFromString("wss")) == NULL ||
        (s_ws_connect = PyUnicode_InternFromString("websocket.connect")) == NULL ||
        (s_ws_receive = PyUnicode_InternFromString("websocket.receive")) == NULL ||
        (s_ws_disconnect = PyUnicode_InternFromString("websocket.disconnect")) == NULL ||
        (s_ws_send_msg = PyUnicode_InternFromString("websocket.send")) == NULL ||
        (s_ws_accept_msg = PyUnicode_InternFromString("websocket.accept")) == NULL ||
        (s_ws_close_msg = PyUnicode_InternFromString("websocket.close")) == NULL ||
        (s_text = PyUnicode_InternFromString("text")) == NULL ||
        (s_bytes = PyUnicode_InternFromString("bytes")) == NULL ||
        (s_code = PyUnicode_InternFromString("code")) == NULL ||
        (s_reason = PyUnicode_InternFromString("reason")) == NULL ||
        (s_subprotocol = PyUnicode_InternFromString("subprotocol")) == NULL ||
        (k_subprotocols = PyUnicode_InternFromString("subprotocols")) == NULL ||
        (header_host = PyBytes_FromString("host")) == NULL) {
        return -1;
    }
    core_capi = (const WreathCoreCAPI *)PyCapsule_Import(WREATH_CORE_CAPI_NAME, 0);
    if (core_capi == NULL) {
        /* PyCapsule_Import walks `wreath._native._core._C_API` as attributes, and
         * wreath._native.__init__ sets `_core` to None on purpose under WREATH_PURE=1
         * -- so this arrives as AttributeError on None, not ImportError. Every
         * caller guards on ImportError to fall back to the pure server, so
         * report the condition that actually holds: this extension needs the
         * _core C API and it is not loaded. The original error stays as the
         * cause so a genuinely missing or broken _core is still diagnosable. */
        PyObject *cause = PyErr_GetRaisedException();
        PyErr_SetString(PyExc_ImportError,
                        "wreath._native._server requires the wreath._native._core C "
                        "API, which is not loaded (WREATH_PURE=1 disables it)");
        PyObject *raised = PyErr_GetRaisedException();
        if (raised != NULL) {
            PyException_SetCause(raised, cause);  /* steals the cause */
            PyErr_SetRaisedException(raised);
        }
        else {
            Py_XDECREF(cause);
        }
        return -1;
    }
    /* The Flight Recorder is optional. PyCapsule_Import resolves a dotted name
     * by attribute walk, not by importing submodules, so the extension must be
     * imported first (it is not pulled in by wreath._native.__init__ the way
     * _core is). A missing extension just leaves flight_capi NULL. */
    {
        PyObject *flight_module = PyImport_ImportModule("wreath._native._flight");
        if (flight_module == NULL) {
            PyErr_Clear();
        } else {
            Py_DECREF(flight_module);
            flight_capi =
                (const WreathFlightCAPI *)PyCapsule_Import(WREATH_FLIGHT_CAPI_NAME, 0);
            if (flight_capi == NULL) {
                PyErr_Clear();
            } else if (flight_capi->version != WREATH_FLIGHT_CAPI_VERSION) {
                flight_capi = NULL;  /* an ABI we do not understand: ignore it */
            }
        }
    }
    {
        PyObject *module = PyImport_ImportModule("hashlib");
        if (module == NULL) {
            return -1;
        }
        sha1_fn = PyObject_GetAttrString(module, "sha1");
        Py_DECREF(module);
        if (sha1_fn == NULL) {
            return -1;
        }
        module = PyImport_ImportModule("base64");
        if (module == NULL) {
            return -1;
        }
        b64encode_fn = PyObject_GetAttrString(module, "b64encode");
        Py_DECREF(module);
        if (b64encode_fn == NULL) {
            return -1;
        }
    }
    asyncio = PyImport_ImportModule("asyncio");
    if (asyncio == NULL) {
        return -1;
    }
    task_class = PyObject_GetAttrString(asyncio, "Task");
    invalid_state_error = PyObject_GetAttrString(asyncio, "InvalidStateError");
    Py_DECREF(asyncio);
    if (task_class == NULL || invalid_state_error == NULL) {
        return -1;
    }
    task_add_done_callback = PyObject_GetAttrString(task_class, "add_done_callback");
    task_exception_fn = PyObject_GetAttrString(task_class, "exception");
    if (task_add_done_callback == NULL || task_exception_fn == NULL) {
        return -1;
    }
    task_kwnames = Py_BuildValue("(ss)", "loop", "eager_start");
    if (task_kwnames == NULL) {
        return -1;
    }
    /* One shared extensions mapping for every scope; consumers treat scope
     * contents as read-only, matching the pure twin's module-level constant. */
    extensions_dict = Py_BuildValue("{s:{}}", "wreath.response");
    if (extensions_dict == NULL) {
        return -1;
    }
    return 0;
}
