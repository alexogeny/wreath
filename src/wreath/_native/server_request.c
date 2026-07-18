#include "server.h"

#include <stddef.h>  /* offsetof */

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
    return 0;
}

static void
context_dealloc(WreathRequestContext *self)
{
    PyObject_GC_UnTrack(self);
    context_clear(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
context_scope(WreathRequestContext *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *scope;
    if (self->scope != NULL) return Py_NewRef(self->scope);
    scope = PyDict_New();
    if (scope == NULL) return NULL;
    if (PyDict_SetItem(scope, s_type, self->scope_type) < 0 ||
        PyDict_SetItem(scope, k_asgi, self->asgi) < 0 ||
        PyDict_SetItem(scope, k_http_version, self->http_version) < 0 ||
        PyDict_SetItem(scope, k_method, self->method) < 0 ||
        PyDict_SetItem(scope, k_scheme, self->scheme) < 0 ||
        PyDict_SetItem(scope, k_path, self->path) < 0 ||
        PyDict_SetItem(scope, k_raw_path, self->raw_path) < 0 ||
        PyDict_SetItem(scope, k_query_string, self->query_string) < 0 ||
        PyDict_SetItem(scope, s_headers, self->headers) < 0 ||
        PyDict_SetItem(scope, k_server, self->server) < 0 ||
        PyDict_SetItem(scope, k_client, self->client) < 0 ||
        PyDict_SetItem(scope, k_root_path, self->root_path) < 0 ||
        PyDict_SetItem(scope, k_extensions, extensions_dict) < 0) {
        Py_DECREF(scope);
        return NULL;
    }
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
CONTEXT_GETTER(headers)
CONTEXT_GETTER(scheme)
CONTEXT_GETTER(http_version)
CONTEXT_GETTER(client)

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

static PyMethodDef context_methods[] = {
    {"_asgi_scope", (PyCFunction)context_scope, METH_NOARGS, NULL},
    {"_flight_stamp", (PyCFunction)context_flight_stamp, METH_FASTCALL, NULL},
    {"_flight_phase", (PyCFunction)context_flight_phase, METH_FASTCALL, NULL},
    {"_set_client", (PyCFunction)context_set_client, METH_O, NULL},
    {"_set_scheme", (PyCFunction)context_set_scheme, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};

static PyMemberDef context_members[] = {
    /* Fast route-attribution gate read by dispatch; 0 unless telemetry records. */
    {"flight", Py_T_INT, offsetof(WreathRequestContext, flight), Py_READONLY, NULL},
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

int
wreath_request_context_ready(PyObject *module)
{
    request_context_type = (PyTypeObject *)PyType_FromSpec(&context_spec);
    if (request_context_type == NULL) return -1;
    if (PyModule_AddObjectRef(
            module, "_RequestContext", (PyObject *)request_context_type) < 0)
        return -1;
    Py_DECREF(request_context_type);
    return 0;
}

int
wreath_request_context_check(PyObject *object)
{
    return PyObject_TypeCheck(object, request_context_type);
}

PyObject *
wreath_request_context_new(
    PyObject *scope_type, PyObject *asgi, PyObject *http_version,
    PyObject *method, PyObject *scheme, PyObject *path, PyObject *raw_path,
    PyObject *query_string, PyObject *headers, PyObject *server,
    PyObject *client, PyObject *root_path
)
{
    WreathRequestContext *self = (WreathRequestContext *)request_context_type->tp_alloc(
        request_context_type, 0
    );
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
    self->nfr_ctx = NULL;
    self->nfr_worker = NULL;
    self->flight = 0;
    return (PyObject *)self;
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
void
wreath_request_context_set_armed(PyObject *object)
{
    WreathRequestContext *self = (WreathRequestContext *)object;
    if (self->nfr_ctx != NULL &&
        (self->nfr_ctx->flags & WREATH_NFR_FLAG_DETAILED_ARMED) != 0 &&
        self->nfr_ctx->phase_slot >= 0) {
        self->flight = 2;
    }
}
