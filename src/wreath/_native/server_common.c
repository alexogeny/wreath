#include "server.h"

#include "simd.h"

/* --- field validation ---------------------------------------------------- */

const uint8_t wreath_field_token[256] = {
    /* RFC 9110 token characters: ALPHA / DIGIT / !#$%&'*+-.^_`|~ */
    ['!'] = 1, ['#'] = 1, ['$'] = 1, ['%'] = 1, ['&'] = 1, ['\''] = 1,
    ['*'] = 1, ['+'] = 1, ['-'] = 1, ['.'] = 1, ['^'] = 1, ['_'] = 1,
    ['`'] = 1, ['|'] = 1, ['~'] = 1,
    ['0'] = 1, ['1'] = 1, ['2'] = 1, ['3'] = 1, ['4'] = 1,
    ['5'] = 1, ['6'] = 1, ['7'] = 1, ['8'] = 1, ['9'] = 1,
    ['A'] = 1, ['B'] = 1, ['C'] = 1, ['D'] = 1, ['E'] = 1, ['F'] = 1,
    ['G'] = 1, ['H'] = 1, ['I'] = 1, ['J'] = 1, ['K'] = 1, ['L'] = 1,
    ['M'] = 1, ['N'] = 1, ['O'] = 1, ['P'] = 1, ['Q'] = 1, ['R'] = 1,
    ['S'] = 1, ['T'] = 1, ['U'] = 1, ['V'] = 1, ['W'] = 1, ['X'] = 1,
    ['Y'] = 1, ['Z'] = 1,
    ['a'] = 1, ['b'] = 1, ['c'] = 1, ['d'] = 1, ['e'] = 1, ['f'] = 1,
    ['g'] = 1, ['h'] = 1, ['i'] = 1, ['j'] = 1, ['k'] = 1, ['l'] = 1,
    ['m'] = 1, ['n'] = 1, ['o'] = 1, ['p'] = 1, ['q'] = 1, ['r'] = 1,
    ['s'] = 1, ['t'] = 1, ['u'] = 1, ['v'] = 1, ['w'] = 1, ['x'] = 1,
    ['y'] = 1, ['z'] = 1,
};

int
wreath_field_name_valid(const char *data, Py_ssize_t size)
{
    if (size == 0) {
        return 0;
    }
    for (Py_ssize_t i = 0; i < size; i++) {
        if (!wreath_field_token[(unsigned char)data[i]]) {
            return 0;
        }
    }
    return 1;
}


int
wreath_field_value_valid(const char *data, Py_ssize_t size)
{
    /* The same predicate the head parser scans with, so it gets the same
     * dispatched width: valid exactly when the run reaches the end without
     * stopping. */
    return wreath_value_run(data, (ptrdiff_t)size) == (ptrdiff_t)size;
}


/* --- request path ------------------------------------------------------- */

PyObject *
wreath_decode_request_path(const char *data, Py_ssize_t size, int *bad)
{
    /* Percent-decode (without plus-as-space), then strict UTF-8, matching the
     * pure reference. */
    char *decoded;
    Py_ssize_t out = 0;
    PyObject *path;
    *bad = 0;
    /* Almost no request path contains a percent. Finding that out costs one
     * vectorised `memchr` and skips both the scratch allocation and the
     * byte-at-a-time copy: the decoder below is only reachable when there is
     * something to decode. */
    if (memchr(data, '%', (size_t)size) == NULL) {
        path = PyUnicode_DecodeUTF8(data, size, "strict");
        if (path == NULL && PyErr_ExceptionMatches(PyExc_UnicodeDecodeError)) {
            PyErr_Clear();
            *bad = 1;
        }
        return path;
    }
    decoded = PyMem_Malloc(size ? (size_t)size : 1);
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
                int decoded_byte = (hv << 4) | lv;
                /* Encoded separators are routed differently by common proxies.
                 * Refuse rather than let an edge ACL and Wreath activate two paths. */
                if (decoded_byte == '/' || decoded_byte == '\\') {
                    PyMem_Free(decoded);
                    *bad = 1;
                    return NULL;
                }
                decoded[out++] = (char)decoded_byte;
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


/* --- shared module globals ---------------------------------------------- */
/* Borrowed from the live module; cleared by server_module_free(). */
PyObject *disconnect_error = NULL;    /* module-private _Disconnect */
PyObject *immediate_none = NULL;       /* stateless completed awaitable */


/* Cached callables for the per-request hot path. */
PyObject *task_add_done_callback = NULL;   /* unbound Task.add_done_callback */
PyObject *task_exception_fn = NULL;        /* unbound Task.exception */
/* The type those two descriptors belong to, plus their names for everything
 * else. `loop.create_task` is the loop's choice, not ours: wreath.reactor
 * returns a WreathTask, which is an asyncio.Future subclass and not a Task at
 * all, and an unbound Task descriptor refuses it. */
static PyObject *asyncio_task_type = NULL;
static PyObject *s_add_done_callback = NULL;
static PyObject *s_exception = NULL;

/* Interned key/value constants so hot dict operations skip per-call string
 * creation and hashing. */
PyObject *s_type = NULL;
PyObject *s_body = NULL;
PyObject *s_more_body = NULL;
PyObject *s_status = NULL;
PyObject *s_headers = NULL;
PyObject *s_trailers = NULL;
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


/* Deliberately no `am_send`, and the reason is worth keeping so nobody adds one
 * as an optimization again. `immediate_next` raising StopIteration is visible in
 * a profile (it is the only caller of `_PyErr_SetObject` on the request path),
 * and `am_send` looks like the fix -- return the value through a status code,
 * raise nothing. It does not work: CPython's `SEND` opcode tests
 * `PyIter_Check(receiver)` before it would consult `am_send`, so an awaitable
 * that is also an iterator always lands in `tp_iternext`. Measured directly --
 * 1000 awaits, 1000 `tp_iternext` calls, zero `am_send` calls -- and dropping
 * `tp_iternext` to force the other branch is not available either, because
 * `GET_AWAITABLE` requires `__await__` to return an iterator.
 *
 * The raise is structural to the awaitable protocol. Removing it means removing
 * the `await`, not re-slotting it: a caller that knows the native protocol never
 * suspends can call a synchronous entry point instead. */
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


/* No `am_send` here either -- see the note above `immediate_async`. */
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


/* --- a coroutine the driver already stepped, made adoptable -------------- */

/* `spawn_app_task` steps the application coroutine before deciding anything, so
 * a handler that never waits owns no asyncio Task at all. When it does wait, the
 * half-run coroutine still has to reach the loop -- and Task cannot take one,
 * because its own first step sends `None` into a coroutine that is waiting for a
 * future's result.
 *
 * This is the adapter: re-yield the value the first step already produced, then
 * be the coroutine underneath for every step after it. The Task then does
 * exactly what it would have done driving the handler from the start.
 *
 * It replaces a Python `async def` that re-awaited each value itself, which cost
 * a coroutine frame per resumption -- measured at ~7,000 instructions a
 * suspending request against the ~15,000 the Task itself costs. Its direct
 * coroutine contract is pinned by tests/test_server_continuation.py.
 *
 * Every exception is forwarded *into* the coroutine, `CancelledError` included.
 * Anything narrower silently strips cancellation from a request in flight: the
 * task dies and the query it was waiting on runs on with nobody to receive it. */
typedef struct {
    PyObject_HEAD
    PyObject *coroutine;  /* owned; the handler, already stepped once */
    PyObject *pending;    /* owned; what that step yielded, until re-yielded */
    int started;
} StartedCoroutine;


/* Raise `StopIteration(value)` the way a generator's return does.
 *
 * `PyErr_SetObject` reads a tuple as an argument list and an exception instance
 * as the exception itself, so a handler returning either would have its value
 * unpacked or swallowed. The same wrapping `value_awaitable_next` does, and for
 * the same reason. Steals `value`. */
static PyObject *
stop_iteration_with(PyObject *value)
{
    if (PyTuple_Check(value) || PyExceptionInstance_Check(value)) {
        PyObject *stop = PyObject_CallOneArg(PyExc_StopIteration, value);
        Py_DECREF(value);
        if (stop == NULL) {
            return NULL;
        }
        PyErr_SetObject(PyExc_StopIteration, stop);
        Py_DECREF(stop);
        return NULL;
    }
    PyErr_SetObject(PyExc_StopIteration, value);
    Py_DECREF(value);
    return NULL;
}


static PyObject *
started_send(PyObject *op, PyObject *value)
{
    StartedCoroutine *self = (StartedCoroutine *)op;
    if (self->started) {
        if (self->coroutine == NULL) {
            PyErr_SetString(PyExc_RuntimeError, "continuation already finished");
            return NULL;
        }
        PyObject *yielded = NULL;
        PySendResult state = PyIter_Send(self->coroutine, value, &yielded);
        if (state == PYGEN_NEXT) {
            return yielded;
        }
        if (state == PYGEN_ERROR) {
            return NULL;
        }
        /* The handler returned: the adopted coroutine is finished with, and
         * holding it past that keeps a frame alive until the Task is collected. */
        Py_CLEAR(self->coroutine);
        return stop_iteration_with(yielded);
    }
    /* The first step's value, handed on untouched: the flag asyncio's
     * `Future.__await__` set is the one the Task expects to see. */
    self->started = 1;
    PyObject *pending = self->pending;
    self->pending = NULL;
    if (pending == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "continuation has no pending value");
        return NULL;
    }
    return pending;  /* reference transferred to the caller */
}


static PyObject *
started_send_method(PyObject *op, PyObject *value)
{
    return started_send(op, value);
}


static PyObject *
started_next(PyObject *op)
{
    return started_send(op, Py_None);
}


static PyObject *
started_throw(PyObject *op, PyObject *args)
{
    StartedCoroutine *self = (StartedCoroutine *)op;
    self->started = 1;
    Py_CLEAR(self->pending);
    if (self->coroutine == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "continuation already finished");
        return NULL;
    }
    PyObject *method = PyObject_GetAttrString(self->coroutine, "throw");
    if (method == NULL) {
        return NULL;
    }
    PyObject *result = PyObject_Call(method, args, NULL);
    Py_DECREF(method);
    return result;
}


static PyObject *
started_close(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    StartedCoroutine *self = (StartedCoroutine *)op;
    Py_CLEAR(self->pending);
    if (self->coroutine == NULL) {
        Py_RETURN_NONE;
    }
    PyObject *method = PyObject_GetAttrString(self->coroutine, "close");
    if (method == NULL) {
        return NULL;
    }
    PyObject *result = PyObject_CallNoArgs(method);
    Py_DECREF(method);
    return result;
}


static int
started_traverse(PyObject *op, visitproc visit, void *arg)
{
    StartedCoroutine *self = (StartedCoroutine *)op;
    Py_VISIT(self->coroutine);
    Py_VISIT(self->pending);
    return 0;
}


static int
started_clear(PyObject *op)
{
    StartedCoroutine *self = (StartedCoroutine *)op;
    Py_CLEAR(self->coroutine);
    Py_CLEAR(self->pending);
    return 0;
}


static void
started_dealloc(PyObject *op)
{
    PyObject_GC_UnTrack(op);
    (void)started_clear(op);
    Py_TYPE(op)->tp_free(op);
}


static PyObject *
started_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    PyObject *coroutine;
    PyObject *pending;
    static char *keywords[] = {"coroutine", "awaited", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "OO:StartedCoroutine",
                                     keywords, &coroutine, &pending)) {
        return NULL;
    }
    StartedCoroutine *self = (StartedCoroutine *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->coroutine = Py_NewRef(coroutine);
    self->pending = Py_NewRef(pending);
    self->started = 0;
    return (PyObject *)self;
}


static PyMethodDef started_methods[] = {
    {"send", started_send_method, METH_O, "Resume the adopted coroutine."},
    {"throw", (PyCFunction)started_throw, METH_VARARGS,
     "Forward an exception into the adopted coroutine."},
    {"close", started_close, METH_NOARGS, "Close the adopted coroutine."},
    {NULL, NULL, 0, NULL},
};


static PyAsyncMethods started_async = {
    .am_await = PyObject_SelfIter,
};


PyTypeObject StartedCoroutineType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._server.StartedCoroutine",
    .tp_basicsize = sizeof(StartedCoroutine),
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .tp_new = started_new,
    .tp_dealloc = started_dealloc,
    .tp_traverse = started_traverse,
    .tp_clear = started_clear,
    .tp_methods = started_methods,
    .tp_as_async = &started_async,
    .tp_iter = PyObject_SelfIter,
    .tp_iternext = started_next,
};


PyObject *
wreath_started_coroutine(PyObject *coroutine, PyObject *pending)
{
    StartedCoroutine *self =
        (StartedCoroutine *)StartedCoroutineType.tp_alloc(&StartedCoroutineType, 0);
    if (self == NULL) {
        return NULL;
    }
    self->coroutine = Py_NewRef(coroutine);
    self->pending = Py_NewRef(pending);
    self->started = 0;
    return (PyObject *)self;
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
    /* `memchr` for the first byte rather than a per-byte compare: the C
     * library's is vectorised on every platform this runs on, and the head of
     * a request is the one buffer every connection walks. The candidates it
     * returns are still confirmed with `memcmp`, so the answer is unchanged. */
    for (Py_ssize_t i = start; i + needle_len <= hay_len;) {
        const char *hit = memchr(hay + i, needle[0], (size_t)(hay_len - needle_len - i + 1));
        if (hit == NULL) {
            break;
        }
        i = hit - hay;
        if (memcmp(hit, needle, (size_t)needle_len) == 0) {
            /* Leave the cursor at the match: the caller may be called again
             * before consuming, and the match must not be skipped. */
            *scan_from = i;
            return i;
        }
        i++;
    }
    Py_ssize_t resume = hay_len - (needle_len - 1);
    *scan_from = resume > 0 ? resume : 0;
    return -1;
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
    char digits[WREATH_DIGITS_MAX];
    return append_raw(buffer, digits, wreath_write_decimal(digits, value));
}


/* --- task interop --------------------------------------------------------
 *
 * `asyncio.Task.add_done_callback` and `.exception` are C descriptors bound to
 * the Task type, and calling them unbound is the cheapest way to reach a Task.
 * But the object comes from `loop.create_task`, and the loop decides what that
 * is: on `wreath.reactor.EventLoop` it is a `WreathTask`, an `asyncio.Future`
 * subclass, and the descriptor raises TypeError on sight of one.
 *
 * So: use the descriptor when it applies, and a normal method lookup when it
 * does not. Stock asyncio keeps the fast path unchanged; every other loop --
 * including Wreath's own -- gets a correct one instead of no path at all.
 */

int
wreath_task_add_done_callback(PyObject *task, PyObject *callback)
{
    PyObject *result;
    if (PyObject_TypeCheck(task, (PyTypeObject *)asyncio_task_type)) {
        PyObject *args[2] = {task, callback};
        result = PyObject_Vectorcall(task_add_done_callback, args, 2, NULL);
    }
    else {
        result = PyObject_CallMethodOneArg(task, s_add_done_callback, callback);
    }
    if (result == NULL) {
        return -1;
    }
    Py_DECREF(result);
    return 0;
}


PyObject *
wreath_task_exception(PyObject *task)
{
    if (PyObject_TypeCheck(task, (PyTypeObject *)asyncio_task_type)) {
        PyObject *args[1] = {task};
        return PyObject_Vectorcall(task_exception_fn, args, 1, NULL);
    }
    return PyObject_CallMethodNoArgs(task, s_exception);
}


/* --- module lifecycle ---------------------------------------------------- */


void
server_module_free(void *Py_UNUSED(module))
{
    wreath_request_context_fini();
    wreath_header_block_freelist_fini();
    Py_CLEAR(immediate_none);
    Py_CLEAR(task_add_done_callback);
    Py_CLEAR(task_exception_fn);
    Py_CLEAR(asyncio_task_type);
    Py_CLEAR(s_add_done_callback);
    Py_CLEAR(s_exception);
    Py_CLEAR(header_host);
    Py_CLEAR(s_type);
    Py_CLEAR(s_body);
    Py_CLEAR(s_more_body);
    Py_CLEAR(s_status);
    Py_CLEAR(s_headers);
    Py_CLEAR(s_trailers);
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
        (s_trailers = PyUnicode_InternFromString("trailers")) == NULL ||
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
        /* PyCapsule_Import walks `wreath._native._core._C_API` as attributes, so
         * a missing or half-initialised _core arrives here as AttributeError
         * rather than ImportError. Callers guard on ImportError, so report the
         * condition that actually holds and report it as the right type. The
         * original error stays as the cause, so a genuinely broken _core is
         * still diagnosable. */
        PyObject *cause = PyErr_GetRaisedException();
        PyErr_SetString(PyExc_ImportError,
                        "wreath._native._server requires the wreath._native._core "
                        "C API, which is not loaded: rebuild the extensions with "
                        "`python setup.py build_ext --inplace`");
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
    if (asyncio == NULL) return -1;
    PyObject *task_type = PyObject_GetAttrString(asyncio, "Task");
    Py_DECREF(asyncio);
    if (task_type == NULL) return -1;
    task_add_done_callback = PyObject_GetAttrString(task_type, "add_done_callback");
    task_exception_fn = PyObject_GetAttrString(task_type, "exception");
    asyncio_task_type = task_type;  /* reference kept; freed in module_free */
    s_add_done_callback = PyUnicode_InternFromString("add_done_callback");
    s_exception = PyUnicode_InternFromString("exception");
    if (task_add_done_callback == NULL || task_exception_fn == NULL
        || s_add_done_callback == NULL || s_exception == NULL) return -1;

    /* One shared extensions mapping for every scope; consumers treat scope
     * contents as read-only. */
    extensions_dict = Py_BuildValue("{s:{}}", "wreath.response");
    if (extensions_dict == NULL) {
        return -1;
    }
    return 0;
}
