#include "server.h"

#include <stddef.h>  /* offsetof */
#include <string.h>

typedef struct {
    PyObject_HEAD
    PyObject *scope_type;
    PyObject *asgi;
    PyObject *http_version;
    PyObject *method;
    PyObject *scheme;
    PyObject *path;
    PyObject *raw_path;
    PyObject *query_string;
    PyObject *headers;
    PyObject *server;
    PyObject *client;
    PyObject *root_path;
    PyObject *scope;
    PyObject *policy_request_id;
    PyObject *policy_csrf_token;
    PyObject *policy_csrf_config;
    uint64_t policy_elapsed_ns;
    int policy_native;
    WreathPolicyState *policy_state; /* borrowed for this request lifetime */
    /* Native Flight Recorder route attribution. `nfr_ctx` and `nfr_worker` are
     * borrowed pointers to the owning protocol's per-request context and worker,
     * set only when telemetry is recording; `flight` is the fast Python-visible
     * flag dispatch checks to decide whether to stamp the matched route:
     * 0 = no recorder (keeps Off branch-free), 1 = recording, 2 = recording and
     * this request is armed for Detailed phase capture, so dispatch may also
     * emit `_flight_phase` markers. */
    wreath_nfr_context *nfr_ctx;
    wreath_nfr_worker *nfr_worker;
    int flight;
} WreathRequestContext;

static PyTypeObject *request_context_type = NULL;
static Py_ssize_t request_context_allocations = 0;

/* One HTTP request owns one fixed-size context.  Reuse the empty GC shell;
 * every Python reference and borrowed native pointer is cleared before it
 * enters this deliberately bounded cache. */
#define REQUEST_CONTEXT_FREELIST_CAP 64
#ifdef Py_GIL_DISABLED
static _Thread_local WreathRequestContext *
    request_context_freelist[REQUEST_CONTEXT_FREELIST_CAP];
static _Thread_local int request_context_freelist_len = 0;
#else
static WreathRequestContext *
    request_context_freelist[REQUEST_CONTEXT_FREELIST_CAP];
static int request_context_freelist_len = 0;
#endif

static int
context_traverse(WreathRequestContext *self, visitproc visit, void *arg)
{
    Py_VISIT(self->scope_type);
    Py_VISIT(self->asgi);
    Py_VISIT(self->http_version);
    Py_VISIT(self->method);
    Py_VISIT(self->scheme);
    Py_VISIT(self->path);
    Py_VISIT(self->raw_path);
    Py_VISIT(self->query_string);
    Py_VISIT(self->headers);
    Py_VISIT(self->server);
    Py_VISIT(self->client);
    Py_VISIT(self->root_path);
    Py_VISIT(self->scope);
    Py_VISIT(self->policy_request_id);
    Py_VISIT(self->policy_csrf_token);
    Py_VISIT(self->policy_csrf_config);
    return 0;
}

static int
context_clear(WreathRequestContext *self)
{
    Py_CLEAR(self->scope_type);
    Py_CLEAR(self->asgi);
    Py_CLEAR(self->http_version);
    Py_CLEAR(self->method);
    Py_CLEAR(self->scheme);
    Py_CLEAR(self->path);
    Py_CLEAR(self->raw_path);
    Py_CLEAR(self->query_string);
    Py_CLEAR(self->headers);
    Py_CLEAR(self->server);
    Py_CLEAR(self->client);
    Py_CLEAR(self->root_path);
    Py_CLEAR(self->scope);
    Py_CLEAR(self->policy_request_id);
    Py_CLEAR(self->policy_csrf_token);
    Py_CLEAR(self->policy_csrf_config);
    self->policy_state = NULL;
    return 0;
}

static void
context_dealloc(WreathRequestContext *self)
{
    PyObject_GC_UnTrack(self);
    context_clear(self);
    if (Py_TYPE(self) == request_context_type &&
        request_context_freelist_len < REQUEST_CONTEXT_FREELIST_CAP) {
        request_context_freelist[request_context_freelist_len++] = self;
    } else {
        Py_TYPE(self)->tp_free((PyObject *)self);
    }
}

static PyObject *
context_scope(WreathRequestContext *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *scope;
    PyObject *headers;
    if (self->scope != NULL) return Py_NewRef(self->scope);
    scope = PyDict_New();
    if (scope == NULL) return NULL;
    headers = wreath_headers_materialize(self->headers);
    if (headers == NULL) {
        Py_DECREF(scope);
        return NULL;
    }
    if (PyDict_SetItem(scope, s_type, self->scope_type) < 0 ||
        PyDict_SetItem(scope, k_asgi, self->asgi) < 0 ||
        PyDict_SetItem(scope, k_http_version, self->http_version) < 0 ||
        PyDict_SetItem(scope, k_method, self->method) < 0 ||
        PyDict_SetItem(scope, k_scheme, self->scheme) < 0 ||
        PyDict_SetItem(scope, k_path, self->path) < 0 ||
        PyDict_SetItem(scope, k_raw_path, self->raw_path) < 0 ||
        PyDict_SetItem(scope, k_query_string, self->query_string) < 0 ||
        PyDict_SetItem(scope, s_headers, headers) < 0 ||
        PyDict_SetItem(scope, k_server, self->server) < 0 ||
        PyDict_SetItem(scope, k_client, self->client) < 0 ||
        PyDict_SetItem(scope, k_root_path, self->root_path) < 0 ||
        PyDict_SetItem(scope, k_extensions, extensions_dict) < 0) {
        Py_DECREF(headers);
        Py_DECREF(scope);
        return NULL;
    }
    Py_DECREF(headers);
    self->scope = Py_NewRef(scope);
    return scope;
}

#define CONTEXT_GETTER(name) \
static PyObject *context_get_##name(WreathRequestContext *self, void *closure) \
{ \
    (void)closure; \
    return Py_NewRef(self->name); \
}

CONTEXT_GETTER(method)
CONTEXT_GETTER(path)
CONTEXT_GETTER(raw_path)
CONTEXT_GETTER(query_string)
CONTEXT_GETTER(scheme)
CONTEXT_GETTER(http_version)
CONTEXT_GETTER(client)

static PyObject *
context_get_headers(WreathRequestContext *self, void *closure)
{
    (void)closure;
    return wreath_headers_materialize(self->headers);
}

static PyObject *
context_get_policy_request_id(WreathRequestContext *self, void *closure)
{
    (void)closure;
    if (self->policy_request_id == NULL) Py_RETURN_NONE;
    return Py_NewRef(self->policy_request_id);
}

static PyObject *
context_get_policy_elapsed(WreathRequestContext *self, void *closure)
{
    (void)closure;
    if (self->policy_elapsed_ns == 0) Py_RETURN_NONE;
    return PyFloat_FromDouble((double)self->policy_elapsed_ns / 1000000000.0);
}

static PyObject *
context_get_policy_csrf_token(WreathRequestContext *self, void *closure)
{
    (void)closure;
    if (self->policy_state != NULL) {
        return wreath_policy_csrf_token(self->policy_state);
    }
    if (self->policy_csrf_token == NULL) Py_RETURN_NONE;
    return Py_NewRef(self->policy_csrf_token);
}

static unsigned char
ascii_lower(unsigned char value)
{
    return value >= 'A' && value <= 'Z'
        ? (unsigned char)(value + ('a' - 'A')) : value;
}

/* Extract the one built-in bearer credential without exposing the header
 * collection to Python.  This scan is deliberately here, on the native request
 * context: asking for ``context.headers`` materializes every name, value, pair,
 * and list slot, although authentication needs only this token.  The verifier
 * remains the single Python activation point.
 *
 * Authorization is not list-valued.  A duplicate returns None even when the
 * first value was usable, preserving the proxy/application ambiguity defence
 * in BearerTokenBackend's independent ASGI path. */
static PyObject *
extract_bearer_token(WreathRequestContext *self)
{
    const char *found = NULL;
    Py_ssize_t found_size = 0;
    Py_ssize_t count = wreath_headers_count(self->headers);
    if (count < 0) return NULL;
    for (Py_ssize_t i = 0; i < count; i++) {
        const char *name;
        const char *value;
        Py_ssize_t name_size;
        Py_ssize_t value_size;
        if (wreath_headers_view(self->headers, i, &name, &name_size,
                                &value, &value_size) < 0) {
            return NULL;
        }
        if (name_size != 13 || memcmp(name, "authorization", 13) != 0) {
            continue;
        }
        if (found != NULL) Py_RETURN_NONE;
        found = value;
        found_size = value_size;
    }
    if (found == NULL || found_size <= 7 || found[6] != ' ' ||
        ascii_lower((unsigned char)found[0]) != 'b' ||
        ascii_lower((unsigned char)found[1]) != 'e' ||
        ascii_lower((unsigned char)found[2]) != 'a' ||
        ascii_lower((unsigned char)found[3]) != 'r' ||
        ascii_lower((unsigned char)found[4]) != 'e' ||
        ascii_lower((unsigned char)found[5]) != 'r') {
        Py_RETURN_NONE;
    }
    return PyUnicode_DecodeLatin1(found + 7, found_size - 7, NULL);
}

static PyObject *
context_bearer_token(WreathRequestContext *self, PyObject *Py_UNUSED(ignored))
{
    return extract_bearer_token(self);
}

/* The built-in verifier is the activation boundary: scan native header spans
 * and enter Python exactly once with the token, without first returning the
 * token through a Request method and then calling it from another Python
 * frame.  An async verifier's coroutine is deliberately returned untouched;
 * dispatch owns the one necessary await. */
static PyObject *
context_bearer_verify(WreathRequestContext *self, PyObject *verifier)
{
    PyObject *token = extract_bearer_token(self);
    if (token == NULL) return NULL;
    if (token == Py_None) return token;
    PyObject *result = PyObject_CallOneArg(verifier, token);
    Py_DECREF(token);
    return result;
}

/* ProxyHeadersMiddleware overrides. The materialized scope dict, when one
 * exists, is kept in sync so both views of the request agree; before anything
 * reads the scope the update is free because there is nothing to update. */
static PyObject *
context_set_client(WreathRequestContext *self, PyObject *value)
{
    Py_INCREF(value);
    Py_SETREF(self->client, value);
    if (self->scope != NULL &&
        PyDict_SetItem(self->scope, k_client, value) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *
context_set_scheme(WreathRequestContext *self, PyObject *value)
{
    if (!PyUnicode_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "_set_scheme expects a str");
        return NULL;
    }
    Py_INCREF(value);
    Py_SETREF(self->scheme, value);
    if (self->scope != NULL &&
        PyDict_SetItem(self->scope, k_scheme, value) < 0) {
        return NULL;
    }
    Py_RETURN_NONE;
}

/* Stamp the matched route/plan onto the recorder context. Called once per
 * request from dispatch only when `flight` is set, so it never runs on Off. It
 * writes into the borrowed native context via the Flight vtable and adds no ring
 * cell of its own -- the completion cell carries the ids. */
static PyObject *
context_flight_stamp(WreathRequestContext *self, PyObject *const *args,
                     Py_ssize_t nargs)
{
    if (nargs != 2) {
        PyErr_SetString(PyExc_TypeError, "_flight_stamp expects (route_id, plan_id)");
        return NULL;
    }
    unsigned long route_id = PyLong_AsUnsignedLong(args[0]);
    if (route_id == (unsigned long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    unsigned long plan_id = PyLong_AsUnsignedLong(args[1]);
    if (plan_id == (unsigned long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    if (self->nfr_ctx != NULL && flight_capi != NULL) {
        flight_capi->context_route(self->nfr_ctx, (uint32_t)route_id,
                                   (uint32_t)plan_id);
    }
    Py_RETURN_NONE;
}

/* Record one Detailed phase from the request path. Dispatch calls this only when
 * `flight == 2` (armed), so the unarmed path never reaches here; a stale call is
 * still safe because context_phase itself no-ops without a scratch slot. Python
 * passes only a duration (a pure monotonic_ns delta, immune to which clock it
 * came from); the start offset is anchored here in the recorder's own clock so
 * offsets and the completion duration never mix clock bases. */
static PyObject *
context_flight_phase(WreathRequestContext *self, PyObject *const *args,
                     Py_ssize_t nargs)
{
    if (nargs != 4) {
        PyErr_SetString(
            PyExc_TypeError,
            "_flight_phase expects (phase_id, dependency_id, coverage, duration_ns)");
        return NULL;
    }
    unsigned long phase_id = PyLong_AsUnsignedLong(args[0]);
    if (phase_id == (unsigned long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    unsigned long dependency_id = PyLong_AsUnsignedLong(args[1]);
    if (dependency_id == (unsigned long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    unsigned long coverage = PyLong_AsUnsignedLong(args[2]);
    if (coverage == (unsigned long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    unsigned long long duration_ns = PyLong_AsUnsignedLongLong(args[3]);
    if (duration_ns == (unsigned long long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    if (self->nfr_ctx != NULL && self->nfr_worker != NULL && flight_capi != NULL) {
        uint64_t now_ns = wreath_flight_now_ns();
        uint64_t start_ns = duration_ns < now_ns ? now_ns - duration_ns : 0;
        uint64_t base_ns = self->nfr_ctx->start_ns;
        uint64_t offset_us = start_ns > base_ns ? (start_ns - base_ns) / 1000 : 0;
        uint64_t duration_us = duration_ns / 1000;
        if (offset_us > UINT32_MAX) offset_us = UINT32_MAX;
        if (duration_us > UINT32_MAX) duration_us = UINT32_MAX;
        flight_capi->context_phase(self->nfr_worker, self->nfr_ctx,
                                   (uint16_t)phase_id, (uint16_t)dependency_id,
                                   (uint8_t)coverage, (uint32_t)offset_us,
                                   (uint32_t)duration_us);
    }
    Py_RETURN_NONE;
}

/* Capture one policy-approved field from the request path into the Forensic
 * slab. Dispatch calls this only for an armed request whose compiled capture plan
 * produced a rule, so the common paths never reach here; the native
 * context_capture is deny-by-default (a no-op unless FLAG_FORENSIC_ARMED) and
 * bounds/redacts the bytes itself, so a stale call after the context is severed
 * is an inert no-op, never a stale write. `data` is any bytes-like (a header
 * value); the disposition (RAW/HASHED/MASKED/LENGTH) was decided by the plan. */
static PyObject *
context_flight_capture(WreathRequestContext *self, PyObject *const *args,
                       Py_ssize_t nargs)
{
    if (nargs != 4 && nargs != 5) {
        PyErr_SetString(
            PyExc_TypeError,
            "_flight_capture expects (field_class, descriptor_id, disposition, data"
            "[, max_bytes])");
        return NULL;
    }
    unsigned long field_class = PyLong_AsUnsignedLong(args[0]);
    if (field_class == (unsigned long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    unsigned long descriptor_id = PyLong_AsUnsignedLong(args[1]);
    if (descriptor_id == (unsigned long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    unsigned long disposition = PyLong_AsUnsignedLong(args[2]);
    if (disposition == (unsigned long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    unsigned long max_bytes = 0;
    if (nargs == 5) {
        max_bytes = PyLong_AsUnsignedLong(args[4]);
        if (max_bytes == (unsigned long)-1 && PyErr_Occurred()) {
            return NULL;
        }
    }
    if (self->nfr_ctx != NULL && self->nfr_worker != NULL && flight_capi != NULL &&
        flight_capi->context_capture != NULL) {
        Py_buffer view;
        if (PyObject_GetBuffer(args[3], &view, PyBUF_SIMPLE) < 0) {
            return NULL;
        }
        flight_capi->context_capture(self->nfr_worker, self->nfr_ctx,
                                     (uint16_t)field_class, (uint16_t)descriptor_id,
                                     (uint8_t)disposition, (const uint8_t *)view.buf,
                                     view.len, (uint32_t)max_bytes);
        PyBuffer_Release(&view);
    }
    Py_RETURN_NONE;
}

/* The owned trace/span this request carries: the incoming 128-bit trace id and
 * the *generated* server span id (a child of the incoming parent). The OTel
 * bridge uses this to parent app-created spans under the owned server span rather
 * than the incoming remote parent. Returns (trace_id_hi, trace_id_lo, span_id),
 * all zero when no recorder is attached (or Off, which leaves the context zeroed). */
static PyObject *
context_flight_server_span(WreathRequestContext *self, PyObject *Py_UNUSED(ignored))
{
    if (self->nfr_ctx == NULL) {
        return Py_BuildValue("(KKK)", 0ULL, 0ULL, 0ULL);
    }
    return Py_BuildValue("(KKK)",
                         (unsigned long long)self->nfr_ctx->trace_id_hi,
                         (unsigned long long)self->nfr_ctx->trace_id_lo,
                         (unsigned long long)self->nfr_ctx->span_id);
}

/* The recorder's own request id for this request, or 0 when no recorder context
 * is attached. Logging keys its per-request scope on this so a record joins the
 * completion the projector will assemble -- the join is by id, exactly as it is
 * for a phase batch, so the two never have to agree on anything else. */
static PyObject *
context_flight_request_id(WreathRequestContext *self, PyObject *Py_UNUSED(ignored))
{
    if (self->nfr_ctx == NULL) {
        return PyLong_FromUnsignedLongLong(0ULL);
    }
    return PyLong_FromUnsignedLongLong(
        (unsigned long long)self->nfr_ctx->request_id);
}

static PyMethodDef context_methods[] = {
    {"_asgi_scope", (PyCFunction)context_scope, METH_NOARGS, NULL},
    {"_bearer_token", (PyCFunction)context_bearer_token, METH_NOARGS, NULL},
    {"_bearer_verify", (PyCFunction)context_bearer_verify, METH_O, NULL},
    {"_flight_stamp", (PyCFunction)context_flight_stamp, METH_FASTCALL, NULL},
    {"_flight_phase", (PyCFunction)context_flight_phase, METH_FASTCALL, NULL},
    {"_flight_capture", (PyCFunction)context_flight_capture, METH_FASTCALL, NULL},
    {"_flight_server_span", (PyCFunction)context_flight_server_span, METH_NOARGS, NULL},
    {"_flight_request_id", (PyCFunction)context_flight_request_id, METH_NOARGS, NULL},
    {"_set_client", (PyCFunction)context_set_client, METH_O, NULL},
    {"_set_scheme", (PyCFunction)context_set_scheme, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};

static PyMemberDef context_members[] = {
    /* Fast route-attribution gate read by dispatch; 0 unless telemetry records. */
    {"flight", Py_T_INT, offsetof(WreathRequestContext, flight), Py_READONLY, NULL},
    {"policy_native", Py_T_INT, offsetof(WreathRequestContext, policy_native),
     Py_READONLY, NULL},
    {NULL, 0, 0, 0, NULL},
};

static PyGetSetDef context_getset[] = {
    {"method", (getter)context_get_method, NULL, NULL, NULL},
    {"path", (getter)context_get_path, NULL, NULL, NULL},
    {"raw_path", (getter)context_get_raw_path, NULL, NULL, NULL},
    {"query_string", (getter)context_get_query_string, NULL, NULL, NULL},
    {"headers", (getter)context_get_headers, NULL, NULL, NULL},
    {"scheme", (getter)context_get_scheme, NULL, NULL, NULL},
    {"http_version", (getter)context_get_http_version, NULL, NULL, NULL},
    {"client", (getter)context_get_client, NULL, NULL, NULL},
    {"policy_request_id", (getter)context_get_policy_request_id, NULL, NULL, NULL},
    {"policy_csrf_token", (getter)context_get_policy_csrf_token, NULL, NULL, NULL},
    {"policy_elapsed", (getter)context_get_policy_elapsed, NULL, NULL, NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyType_Slot context_slots[] = {
    {Py_tp_dealloc, (void *)context_dealloc},
    {Py_tp_traverse, (void *)context_traverse},
    {Py_tp_clear, (void *)context_clear},
    {Py_tp_methods, (void *)context_methods},
    {Py_tp_members, (void *)context_members},
    {Py_tp_getset, (void *)context_getset},
    {0, NULL},
};

static PyType_Spec context_spec = {
    .name = "wreath._native._server._RequestContext",
    .basicsize = sizeof(WreathRequestContext),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = context_slots,
};

static PyObject *
request_storage_counts(PyObject *module, PyObject *unused)
{
    (void)module;
    (void)unused;
    return Py_BuildValue(
        "(nn)", request_context_allocations,
        wreath_header_block_storage_allocations());
}

static PyMethodDef request_debug_methods[] = {
    {"_request_storage_counts", request_storage_counts, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

int
wreath_request_context_ready(PyObject *module)
{
    request_context_type = (PyTypeObject *)PyType_FromSpec(&context_spec);
    if (request_context_type == NULL) return -1;
    if (PyModule_AddObjectRef(
            module, "_RequestContext", (PyObject *)request_context_type) < 0)
        return -1;
    if (PyModule_AddFunctions(module, request_debug_methods) < 0) return -1;
    Py_DECREF(request_context_type);
    return 0;
}

void
wreath_request_context_fini(void)
{
    while (request_context_freelist_len > 0) {
        WreathRequestContext *context =
            request_context_freelist[--request_context_freelist_len];
        PyObject_GC_Del(context);
    }
}

int
wreath_request_context_check(PyObject *object)
{
    return PyObject_TypeCheck(object, request_context_type);
}

PyObject *
wreath_request_context_headers(PyObject *object)
{
    if (!wreath_request_context_check(object)) return NULL;
    return ((WreathRequestContext *)object)->headers;
}

/* Seed the dict-scope `_wreath_flight` slot with the recorder's request id.
 *
 * The three protocols that dispatch through a *dict* scope -- HTTP/2, HTTP/3,
 * and a WebSocket session -- have no request-context object to hang the id on,
 * and Python needs it to open a per-request log scope. They already write this
 * key (as None) to signal "a recorder is attached"; writing the id instead
 * carries the value at no extra dictionary operation.
 *
 * Python overwrites the slot with a (route_id, plan_id) tuple when it attributes
 * the route, and every completion path already requires an exact 2-tuple before
 * it stamps attribution, so an id left in place reads as "unattributed" exactly
 * as None did. Falls back to None if the id cannot be boxed. */
int
wreath_request_scope_seed_flight(PyObject *scope, const wreath_nfr_context *nfr_ctx)
{
    /* An Off worker's context_start sets `mode` and returns *without*
     * initializing the rest, so `request_id` is uninitialized stack there.
     * `mode` is the one field that is always written, and it is the same
     * discriminator `set_armed` relies on for the same reason. Reading the id
     * without this check published garbage ids on an Off recorder -- caught by
     * tests/http2/test_logging.py, which is why that test exists. */
    PyObject *value = NULL;
    if (nfr_ctx != NULL && nfr_ctx->mode != WREATH_NFR_MODE_OFF) {
        value = PyLong_FromUnsignedLongLong(
            (unsigned long long)nfr_ctx->request_id);
    }
    if (value == NULL) {
        PyErr_Clear();
        return PyDict_SetItemString(scope, "_wreath_flight", Py_None);
    }
    int rc = PyDict_SetItemString(scope, "_wreath_flight", value);
    Py_DECREF(value);
    return rc;
}

PyObject *
wreath_request_context_new(
    PyObject *scope_type, PyObject *asgi, PyObject *http_version,
    PyObject *method, PyObject *scheme, PyObject *path, PyObject *raw_path,
    PyObject *query_string, PyObject *headers, PyObject *server,
    PyObject *client, PyObject *root_path
)
{
    WreathRequestContext *self;
    if (request_context_freelist_len > 0) {
        self = request_context_freelist[--request_context_freelist_len];
        _Py_NewReference((PyObject *)self);
        PyObject_GC_Track(self);
    } else {
        self = (WreathRequestContext *)request_context_type->tp_alloc(
            request_context_type, 0
        );
        if (self != NULL) request_context_allocations++;
    }
    if (self == NULL) return NULL;
    self->scope_type = Py_NewRef(scope_type);
    self->asgi = Py_NewRef(asgi);
    self->http_version = Py_NewRef(http_version);
    self->method = Py_NewRef(method);
    self->scheme = Py_NewRef(scheme);
    self->path = Py_NewRef(path);
    self->raw_path = Py_NewRef(raw_path);
    self->query_string = Py_NewRef(query_string);
    self->headers = Py_NewRef(headers);
    self->server = Py_NewRef(server);
    self->client = Py_NewRef(client);
    self->root_path = Py_NewRef(root_path);
    self->policy_request_id = NULL;
    self->policy_csrf_token = NULL;
    self->policy_csrf_config = NULL;
    self->policy_elapsed_ns = 0;
    self->policy_native = 0;
    self->policy_state = NULL;
    self->nfr_ctx = NULL;
    self->nfr_worker = NULL;
    self->flight = 0;
    return (PyObject *)self;
}

void
wreath_request_context_set_policy(PyObject *object,
                                  const WreathPolicyState *state)
{
    if (!wreath_request_context_check(object)) return;
    WreathRequestContext *self = (WreathRequestContext *)object;
    self->policy_native = state->native;
    self->policy_state = (WreathPolicyState *)state;
    if (state->client != NULL) {
        Py_SETREF(self->client, Py_NewRef(state->client));
    }
    if (state->scheme != NULL) {
        Py_SETREF(self->scheme, Py_NewRef(state->scheme));
    }
    Py_XSETREF(self->policy_request_id, Py_XNewRef(state->request_id));
    Py_XSETREF(self->policy_csrf_token, Py_XNewRef(state->csrf_token));
    Py_XSETREF(self->policy_csrf_config, Py_XNewRef(state->csrf_config));
    self->policy_elapsed_ns = state->elapsed_ns;
}

void
wreath_request_context_update_policy(PyObject *object,
                                     const WreathPolicyState *state)
{
    wreath_request_context_set_policy(object, state);
}

/* Attach the owning protocol's recorder context and worker (borrowed) so
 * dispatch can stamp the matched route and, when armed, record phases. A NULL
 * context means telemetry is off and leaves `flight` clear, which is what keeps
 * the Off dispatch path branch-free. */
void
wreath_request_context_set_flight(PyObject *object, wreath_nfr_context *nfr_ctx,
                                  wreath_nfr_worker *nfr_worker)
{
    WreathRequestContext *self = (WreathRequestContext *)object;
    self->nfr_ctx = nfr_ctx;
    self->nfr_worker = nfr_worker;
    self->flight = nfr_ctx != NULL ? 1 : 0;
}

/* Promote `flight` to the armed state after context_start decided the sample.
 * Called only on the recording path, once the arming decision (and the scratch
 * slot it reserved) is known; a recorder in Pulse, or an unarmed Detailed
 * request, stays at 1 so dispatch never pays for phase plumbing. The ARMED flag
 * must gate this, not phase_slot alone: an Off worker's context_start returns
 * before initializing the context, so its zeroed phase_slot (0 -- a valid slot
 * index) would otherwise read as armed. */
int
wreath_request_context_set_armed(PyObject *object)
{
    WreathRequestContext *self = (WreathRequestContext *)object;
    if (self->nfr_ctx != NULL &&
        (self->nfr_ctx->flags & WREATH_NFR_FLAG_DETAILED_ARMED) != 0 &&
        self->nfr_ctx->phase_slot >= 0) {
        self->flight = 2;
        return 1;
    }
    return 0;
}

/* Null the borrowed recorder pointers once the request's completion has been
 * published. The context object itself may outlive the request (a bound
 * `_flight_phase` stored in a ContextVar escapes into spawned tasks and
 * background hooks); after this, such escaped markers are inert no-ops instead
 * of writes through pointers into a protocol that may already be gone. */
void
wreath_request_context_sever(PyObject *object)
{
    WreathRequestContext *self = (WreathRequestContext *)object;
    self->nfr_ctx = NULL;
    self->nfr_worker = NULL;
    self->flight = 0;
}
