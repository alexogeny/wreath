/* wreath._native._flight — Native Flight Recorder extension.
 *
 * Exposes a Recorder object wrapping one native worker (ring, counters,
 * histograms, active table, phase scratch) plus a _Request handle for driving
 * the start/route/phase/end lifecycle. The server extension drives the same
 * worker on the request path by resolving the versioned C capsule this module
 * exports; tests drive it directly through the same handle.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include "flight.h"

typedef struct {
    PyObject_HEAD
    wreath_nfr_worker *worker;
} RecorderObject;

typedef struct {
    PyObject_HEAD
    RecorderObject *recorder;  /* strong ref */
    wreath_nfr_context ctx;
    int finished;
} RequestObject;

static PyTypeObject RecorderType;
static PyTypeObject RequestType;

/* --- _Request ------------------------------------------------------------- */

static void
request_dealloc(RequestObject *self)
{
    if (!self->finished && self->recorder != NULL && self->recorder->worker != NULL) {
        /* Dropped without finishing: model cancellation/teardown. */
        wreath_nfr_context_abandon(self->recorder->worker, &self->ctx);
    }
    Py_XDECREF(self->recorder);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
request_route(RequestObject *self, PyObject *args)
{
    unsigned int route_id, plan_id;
    if (!PyArg_ParseTuple(args, "II:route", &route_id, &plan_id)) {
        return NULL;
    }
    wreath_nfr_context_route(&self->ctx, route_id, plan_id);
    Py_RETURN_NONE;
}

static PyObject *
request_finish(RequestObject *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"now_ns", "status", "terminal", "error_class",
                             "bytes_in", "bytes_out", NULL};
    unsigned long long now_ns, bytes_in = 0, bytes_out = 0;
    unsigned int status = 0, terminal = 0, error_class = 0;
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "K|IIIKK:finish", kwlist, &now_ns,
                                     &status, &terminal, &error_class, &bytes_in,
                                     &bytes_out)) {
        return NULL;
    }
    if (self->finished) {
        PyErr_SetString(PyExc_RuntimeError, "request already finished");
        return NULL;
    }
    wreath_nfr_context_end(self->recorder->worker, &self->ctx, now_ns, status,
                           (uint8_t)terminal, (uint8_t)error_class, bytes_in,
                           bytes_out);
    self->finished = 1;
    Py_RETURN_NONE;
}

static PyObject *
request_phase(RequestObject *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"phase_id", "dependency_id", "coverage",
                             "start_offset_us", "duration_us", NULL};
    unsigned int phase_id, dependency_id = 0, coverage = 0, start_offset_us = 0,
                 duration_us = 0;
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "I|IIII:phase", kwlist, &phase_id,
                                     &dependency_id, &coverage, &start_offset_us,
                                     &duration_us)) {
        return NULL;
    }
    wreath_nfr_context_phase(self->recorder->worker, &self->ctx, (uint16_t)phase_id,
                             (uint16_t)dependency_id, (uint8_t)coverage,
                             start_offset_us, duration_us);
    Py_RETURN_NONE;
}

static PyObject *
request_phase_count(RequestObject *self, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLong(self->ctx.phase_count);
}

static PyObject *
request_capture(RequestObject *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"field_class", "descriptor_id", "disposition", "data",
                             "max_bytes", NULL};
    unsigned int field_class, descriptor_id = 0, disposition = 0, max_bytes = 0;
    Py_buffer view = {0};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "I|IIy*I:capture", kwlist,
                                     &field_class, &descriptor_id, &disposition,
                                     &view, &max_bytes)) {
        return NULL;
    }
    wreath_nfr_context_capture(self->recorder->worker, &self->ctx,
                               (uint16_t)field_class, (uint16_t)descriptor_id,
                               (uint8_t)disposition, (const uint8_t *)view.buf,
                               view.len, max_bytes);
    if (view.obj != NULL) {
        PyBuffer_Release(&view);
    }
    Py_RETURN_NONE;
}

static PyObject *
request_get_capture_slot(RequestObject *self, void *Py_UNUSED(closure))
{
    return PyLong_FromLong(self->ctx.capture_slot);
}

static PyObject *
request_propagate(RequestObject *self, PyObject *arg)
{
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    wreath_nfr_context_propagate(self->recorder->worker, &self->ctx,
                                 (const uint8_t *)view.buf, view.len);
    PyBuffer_Release(&view);
    Py_RETURN_NONE;
}

static PyObject *
request_abandon(RequestObject *self, PyObject *Py_UNUSED(ignored))
{
    if (!self->finished) {
        wreath_nfr_context_abandon(self->recorder->worker, &self->ctx);
        self->finished = 1;
    }
    Py_RETURN_NONE;
}

static PyObject *
request_get_slot(RequestObject *self, void *Py_UNUSED(closure))
{
    return PyLong_FromLong(self->ctx.active_slot);
}

static PyObject *
request_get_request_id(RequestObject *self, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(self->ctx.request_id);
}

static PyMethodDef request_methods[] = {
    {"route", (PyCFunction)request_route, METH_VARARGS, NULL},
    {"phase", (PyCFunction)request_phase, METH_VARARGS | METH_KEYWORDS, NULL},
    {"capture", (PyCFunction)request_capture, METH_VARARGS | METH_KEYWORDS, NULL},
    {"propagate", (PyCFunction)request_propagate, METH_O, NULL},
    {"finish", (PyCFunction)request_finish, METH_VARARGS | METH_KEYWORDS, NULL},
    {"abandon", (PyCFunction)request_abandon, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef request_getset[] = {
    {"active_slot", (getter)request_get_slot, NULL, NULL, NULL},
    {"request_id", (getter)request_get_request_id, NULL, NULL, NULL},
    {"phase_count", (getter)request_phase_count, NULL, NULL, NULL},
    {"capture_slot", (getter)request_get_capture_slot, NULL, NULL, NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject RequestType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._flight._Request",
    .tp_basicsize = sizeof(RequestObject),
    .tp_dealloc = (destructor)request_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_methods = request_methods,
    .tp_getset = request_getset,
};

/* --- Recorder ------------------------------------------------------------- */

static PyObject *
recorder_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"mode", "worker_id", "ring_records", "active_requests",
                             "histogram_count", "completion_summaries",
                             "detailed_sample_rate", "phase_slots",
                             "detailed_slow_us", "capture_slabs", "slab_bytes",
                             "capture_hash_key", NULL};
    unsigned int mode, worker_id = 0, ring_records = 16384, active_requests = 2048,
                       histogram_count = 1, phase_slots = 256, capture_slabs = 0,
                       slab_bytes = 65536;
    int completion_summaries = 1;
    double detailed_sample_rate = 0.0;
    unsigned long long detailed_slow_us = 0;
    PyObject *hash_key = NULL;
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "I|IIIIpdIKIIO:Recorder", kwlist,
                                     &mode, &worker_id, &ring_records, &active_requests,
                                     &histogram_count, &completion_summaries,
                                     &detailed_sample_rate, &phase_slots,
                                     &detailed_slow_us, &capture_slabs, &slab_bytes,
                                     &hash_key)) {
        return NULL;
    }
    if (detailed_sample_rate < 0.0 || detailed_sample_rate > 1.0) {
        PyErr_SetString(PyExc_ValueError, "detailed_sample_rate must be in [0, 1]");
        return NULL;
    }
    /* An optional (k0, k1) key makes HASHED capture reproducible for tests; the
     * default draws a process-local key from the CSPRNG inside the worker. */
    uint64_t hash_key0 = 0, hash_key1 = 0;
    if (hash_key != NULL && hash_key != Py_None) {
        if (!PyTuple_Check(hash_key) ||
            !PyArg_ParseTuple(hash_key, "KK", &hash_key0, &hash_key1)) {
            PyErr_SetString(PyExc_TypeError,
                            "capture_hash_key must be a (k0, k1) tuple");
            return NULL;
        }
    }
    /* threshold = round(rate * 2^32): a 32-bit draw < threshold arms. rate 1.0
     * yields 2^32 so every draw (< 2^32) arms; rate 0.0 arms none. */
    uint64_t detailed_sample_threshold =
        (uint64_t)(detailed_sample_rate * 4294967296.0 + 0.5);
    RecorderObject *self = (RecorderObject *)type->tp_alloc(type, 0);
    if (self == NULL) {
        return NULL;
    }
    self->worker = wreath_nfr_worker_new((uint8_t)mode, worker_id, ring_records,
                                         active_requests, histogram_count,
                                         completion_summaries,
                                         detailed_sample_threshold, phase_slots,
                                         detailed_slow_us, capture_slabs, slab_bytes,
                                         hash_key0, hash_key1);
    if (self->worker == NULL) {
        Py_DECREF(self);
        return NULL;
    }
    return (PyObject *)self;
}

static void
recorder_dealloc(RecorderObject *self)
{
    wreath_nfr_worker_free(self->worker);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
recorder_begin(RecorderObject *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"connection_id", "protocol", "start_ns", NULL};
    unsigned long long connection_id = 0, start_ns = 0;
    unsigned int protocol = 0;
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|KIK:begin", kwlist, &connection_id,
                                     &protocol, &start_ns)) {
        return NULL;
    }
    RequestObject *req = (RequestObject *)RequestType.tp_alloc(&RequestType, 0);
    if (req == NULL) {
        return NULL;
    }
    req->recorder = (RecorderObject *)Py_NewRef((PyObject *)self);
    req->finished = 0;
    wreath_nfr_context_start(self->worker, &req->ctx, connection_id,
                             (uint8_t)protocol, start_ns);
    return (PyObject *)req;
}

/* Convenience: run a whole request (start, route, end) in one call for
 * throughput/completion tests. */
static PyObject *
recorder_record(RecorderObject *self, PyObject *args, PyObject *kwds)
{
    static char *kwlist[] = {"start_ns", "end_ns", "connection_id", "protocol",
                             "route_id", "plan_id", "status", "terminal",
                             "error_class", "bytes_in", "bytes_out", NULL};
    unsigned long long start_ns = 0, end_ns = 0, connection_id = 0, bytes_in = 0,
                       bytes_out = 0;
    unsigned int protocol = 0, route_id = 0, plan_id = 0, status = 0, terminal = 0,
                 error_class = 0;
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "KK|KIIIIIIKK:record", kwlist,
                                     &start_ns, &end_ns, &connection_id, &protocol,
                                     &route_id, &plan_id, &status, &terminal,
                                     &error_class, &bytes_in, &bytes_out)) {
        return NULL;
    }
    wreath_nfr_context ctx;
    wreath_nfr_context_start(self->worker, &ctx, connection_id, (uint8_t)protocol,
                             start_ns);
    wreath_nfr_context_route(&ctx, route_id, plan_id);
    wreath_nfr_context_end(self->worker, &ctx, end_ns, status, (uint8_t)terminal,
                           (uint8_t)error_class, bytes_in, bytes_out);
    Py_RETURN_NONE;
}

static PyObject *
recorder_drain(RecorderObject *self, PyObject *args)
{
    Py_ssize_t max_cells = 4096;
    if (!PyArg_ParseTuple(args, "|n:drain", &max_cells)) {
        return NULL;
    }
    if (max_cells <= 0) {
        return PyBytes_FromStringAndSize(NULL, 0);
    }
    PyObject *buffer = PyBytes_FromStringAndSize(NULL, max_cells * WREATH_NFR_CELL_SIZE);
    if (buffer == NULL) {
        return NULL;
    }
    Py_ssize_t drained =
        wreath_nfr_ring_drain(self->worker, (uint8_t *)PyBytes_AS_STRING(buffer),
                              max_cells);
    if (_PyBytes_Resize(&buffer, drained * WREATH_NFR_CELL_SIZE) < 0) {
        return NULL;
    }
    return buffer;
}

/* Drain committed forensic capture slabs: a list of bytes, each one slab's
 * used_bytes (self-identifying header + typed field records). The sink and tests
 * consume this; each drained slab is returned to the free pool. */
static PyObject *
recorder_drain_captures(RecorderObject *self, PyObject *args)
{
    Py_ssize_t max_slabs = 256;
    if (!PyArg_ParseTuple(args, "|n:drain_captures", &max_slabs)) {
        return NULL;
    }
    uint64_t capacity = wreath_nfr_capture_capacity(self->worker);
    if (capacity == 0 || max_slabs <= 0) {
        return PyList_New(0);
    }
    if ((uint64_t)max_slabs > capacity) {
        max_slabs = (Py_ssize_t)capacity;  /* at most `capacity` are ever committed */
    }
    uint64_t slab_bytes = wreath_nfr_capture_slab_bytes(self->worker);
    uint8_t *buffer = PyMem_Malloc((size_t)max_slabs * (size_t)slab_bytes);
    uint32_t *lengths = PyMem_Malloc((size_t)max_slabs * sizeof(uint32_t));
    if (buffer == NULL || lengths == NULL) {
        PyMem_Free(buffer);
        PyMem_Free(lengths);
        return PyErr_NoMemory();
    }
    Py_ssize_t drained =
        wreath_nfr_capture_drain(self->worker, buffer, lengths, max_slabs);
    PyObject *result = PyList_New(drained);
    if (result == NULL) {
        PyMem_Free(buffer);
        PyMem_Free(lengths);
        return NULL;
    }
    for (Py_ssize_t i = 0; i < drained; i++) {
        PyObject *slab = PyBytes_FromStringAndSize(
            (const char *)(buffer + (size_t)i * (size_t)slab_bytes), lengths[i]);
        if (slab == NULL) {
            Py_DECREF(result);
            PyMem_Free(buffer);
            PyMem_Free(lengths);
            return NULL;
        }
        PyList_SET_ITEM(result, i, slab);
    }
    PyMem_Free(buffer);
    PyMem_Free(lengths);
    return result;
}

static PyObject *
recorder_loss(RecorderObject *self, PyObject *arg)
{
    long reason = PyLong_AsLong(arg);
    if (reason == -1 && PyErr_Occurred()) {
        return NULL;
    }
    return PyLong_FromUnsignedLongLong(wreath_nfr_loss(self->worker, (int)reason));
}

/* A capsule wrapping this recorder's worker pointer, for the server extension to
 * resolve and drive on the request path. The capsule holds no ownership: the
 * Recorder object outlives every connection that references it. */
static PyObject *
recorder_worker_capsule(RecorderObject *self, PyObject *Py_UNUSED(ignored))
{
    return PyCapsule_New(self->worker, WREATH_FLIGHT_WORKER_CAPSULE, NULL);
}

static PyObject *
recorder_histogram(RecorderObject *self, PyObject *Py_UNUSED(ignored))
{
    uint64_t buckets[WREATH_NFR_HISTOGRAM_BUCKETS];
    wreath_nfr_histogram_global(self->worker, buckets);
    PyObject *tuple = PyTuple_New(WREATH_NFR_HISTOGRAM_BUCKETS);
    if (tuple == NULL) {
        return NULL;
    }
    for (int i = 0; i < WREATH_NFR_HISTOGRAM_BUCKETS; i++) {
        PyObject *value = PyLong_FromUnsignedLongLong(buckets[i]);
        if (value == NULL) {
            Py_DECREF(tuple);
            return NULL;
        }
        PyTuple_SET_ITEM(tuple, i, value);
    }
    return tuple;
}

#define RECORDER_U64_GETTER(name, expr)                                          \
    static PyObject *recorder_get_##name(RecorderObject *self, void *c)          \
    {                                                                            \
        (void)c;                                                                 \
        return PyLong_FromUnsignedLongLong(expr);                                \
    }

RECORDER_U64_GETTER(requests, wreath_nfr_counter_requests(self->worker))
RECORDER_U64_GETTER(completions, wreath_nfr_counter_completions(self->worker))
RECORDER_U64_GETTER(active_count, wreath_nfr_active_count(self->worker))
RECORDER_U64_GETTER(ring_occupancy, wreath_nfr_ring_occupancy(self->worker))
RECORDER_U64_GETTER(ring_high_water, wreath_nfr_ring_high_water(self->worker))
RECORDER_U64_GETTER(phase_capacity, wreath_nfr_phase_capacity(self->worker))
RECORDER_U64_GETTER(phase_in_use, wreath_nfr_phase_in_use(self->worker))
RECORDER_U64_GETTER(phase_high_water, wreath_nfr_phase_high_water(self->worker))
RECORDER_U64_GETTER(capture_capacity, wreath_nfr_capture_capacity(self->worker))
RECORDER_U64_GETTER(capture_slab_bytes, wreath_nfr_capture_slab_bytes(self->worker))
RECORDER_U64_GETTER(capture_in_use, wreath_nfr_capture_in_use(self->worker))
RECORDER_U64_GETTER(capture_high_water, wreath_nfr_capture_high_water(self->worker))
RECORDER_U64_GETTER(capture_committed, wreath_nfr_capture_committed(self->worker))

static PyObject *
recorder_get_mode(RecorderObject *self, void *Py_UNUSED(closure))
{
    return PyLong_FromLong(wreath_nfr_worker_mode(self->worker));
}

/* Snapshot the active table for the Inspector: a list of
 * (request_id, start_ns, protocol, route_id) tuples for in-use slots. */
static PyObject *
recorder_active_snapshot(RecorderObject *self, PyObject *Py_UNUSED(ignored))
{
    uint64_t capacity = wreath_nfr_active_capacity(self->worker);
    if (capacity == 0) {
        return PyList_New(0);
    }
    wreath_nfr_active_entry *rows =
        PyMem_Malloc((size_t)capacity * sizeof(wreath_nfr_active_entry));
    if (rows == NULL) {
        return PyErr_NoMemory();
    }
    uint32_t written =
        wreath_nfr_active_snapshot(self->worker, rows, (uint32_t)capacity);
    PyObject *result = PyList_New(written);
    if (result == NULL) {
        PyMem_Free(rows);
        return NULL;
    }
    for (uint32_t i = 0; i < written; i++) {
        PyObject *row = Py_BuildValue(
            "(KKBI)", (unsigned long long)rows[i].request_id,
            (unsigned long long)rows[i].start_ns, rows[i].protocol,
            rows[i].route_id);
        if (row == NULL) {
            Py_DECREF(result);
            PyMem_Free(rows);
            return NULL;
        }
        PyList_SET_ITEM(result, i, row);
    }
    PyMem_Free(rows);
    return result;
}

static PyMethodDef recorder_methods[] = {
    {"active_snapshot", (PyCFunction)recorder_active_snapshot, METH_NOARGS, NULL},
    {"begin", (PyCFunction)recorder_begin, METH_VARARGS | METH_KEYWORDS, NULL},
    {"record", (PyCFunction)recorder_record, METH_VARARGS | METH_KEYWORDS, NULL},
    {"drain", (PyCFunction)recorder_drain, METH_VARARGS, NULL},
    {"drain_captures", (PyCFunction)recorder_drain_captures, METH_VARARGS, NULL},
    {"worker_capsule", (PyCFunction)recorder_worker_capsule, METH_NOARGS, NULL},
    {"loss", (PyCFunction)recorder_loss, METH_O, NULL},
    {"histogram", (PyCFunction)recorder_histogram, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef recorder_getset[] = {
    {"mode", (getter)recorder_get_mode, NULL, NULL, NULL},
    {"requests", (getter)recorder_get_requests, NULL, NULL, NULL},
    {"completions", (getter)recorder_get_completions, NULL, NULL, NULL},
    {"active_count", (getter)recorder_get_active_count, NULL, NULL, NULL},
    {"ring_occupancy", (getter)recorder_get_ring_occupancy, NULL, NULL, NULL},
    {"ring_high_water", (getter)recorder_get_ring_high_water, NULL, NULL, NULL},
    {"phase_capacity", (getter)recorder_get_phase_capacity, NULL, NULL, NULL},
    {"phase_in_use", (getter)recorder_get_phase_in_use, NULL, NULL, NULL},
    {"phase_high_water", (getter)recorder_get_phase_high_water, NULL, NULL, NULL},
    {"capture_capacity", (getter)recorder_get_capture_capacity, NULL, NULL, NULL},
    {"capture_slab_bytes", (getter)recorder_get_capture_slab_bytes, NULL, NULL, NULL},
    {"capture_in_use", (getter)recorder_get_capture_in_use, NULL, NULL, NULL},
    {"capture_high_water", (getter)recorder_get_capture_high_water, NULL, NULL, NULL},
    {"capture_committed", (getter)recorder_get_capture_committed, NULL, NULL, NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject RecorderType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "wreath._native._flight.Recorder",
    .tp_basicsize = sizeof(RecorderObject),
    .tp_dealloc = (destructor)recorder_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = recorder_new,
    .tp_methods = recorder_methods,
    .tp_getset = recorder_getset,
};

/* --- capsule -------------------------------------------------------------- */

static WreathFlightCAPI capi = {
    .version = WREATH_FLIGHT_CAPI_VERSION,
    .context_start = wreath_nfr_context_start,
    .context_route = wreath_nfr_context_route,
    .context_propagate = wreath_nfr_context_propagate,
    .context_end = wreath_nfr_context_end,
    .context_abandon = wreath_nfr_context_abandon,
    .worker_mode = wreath_nfr_worker_mode,
    .context_phase = wreath_nfr_context_phase,
    .context_capture = wreath_nfr_context_capture,
};

/* --- module --------------------------------------------------------------- */

/* Direct access to the strict W3C parser for corpus/fuzz tests. Returns
 * (trace_hi, trace_lo, parent_span, sampled) or None for malformed input. */
static PyObject *
py_parse_traceparent(PyObject *Py_UNUSED(self), PyObject *arg)
{
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    uint64_t hi, lo, parent;
    uint8_t sampled;
    int rc = wreath_nfr_parse_traceparent((const uint8_t *)view.buf, view.len, &hi,
                                          &lo, &parent, &sampled);
    PyBuffer_Release(&view);
    if (rc < 0) {
        Py_RETURN_NONE;
    }
    return Py_BuildValue("(KKKN)", (unsigned long long)hi, (unsigned long long)lo,
                         (unsigned long long)parent, PyBool_FromLong(sampled));
}

static PyMethodDef flight_methods[] = {
    {"parse_traceparent", py_parse_traceparent, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};

static int
add_int(PyObject *module, const char *name, long value)
{
    return PyModule_AddIntConstant(module, name, value);
}

static PyModuleDef flight_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_flight",
    .m_doc = "Native Flight Recorder (Stage 1 native core).",
    .m_size = 0,
    .m_methods = flight_methods,
};

PyMODINIT_FUNC
PyInit__flight(void)
{
    if (PyType_Ready(&RecorderType) < 0 || PyType_Ready(&RequestType) < 0) {
        return NULL;
    }
    PyObject *module = PyModule_Create(&flight_module);
    if (module == NULL) {
        return NULL;
    }
    if (PyModule_AddObjectRef(module, "Recorder", (PyObject *)&RecorderType) < 0) {
        goto error;
    }

    if (add_int(module, "MODE_OFF", WREATH_NFR_MODE_OFF) < 0 ||
        add_int(module, "MODE_PULSE", WREATH_NFR_MODE_PULSE) < 0 ||
        add_int(module, "MODE_DETAILED", WREATH_NFR_MODE_DETAILED) < 0 ||
        add_int(module, "MODE_FORENSIC", WREATH_NFR_MODE_FORENSIC) < 0 ||
        add_int(module, "PROTO_HTTP1", WREATH_NFR_PROTO_HTTP1) < 0 ||
        add_int(module, "PROTO_HTTP2", WREATH_NFR_PROTO_HTTP2) < 0 ||
        add_int(module, "PROTO_HTTP3", WREATH_NFR_PROTO_HTTP3) < 0 ||
        add_int(module, "CELL_SIZE", WREATH_NFR_CELL_SIZE) < 0 ||
        add_int(module, "HISTOGRAM_BUCKETS", WREATH_NFR_HISTOGRAM_BUCKETS) < 0 ||
        add_int(module, "LOSS_RING_FULL", WREATH_NFR_LOSS_RING_FULL) < 0 ||
        add_int(module, "LOSS_ACTIVE_TABLE_FULL", WREATH_NFR_LOSS_ACTIVE_TABLE_FULL) < 0 ||
        add_int(module, "LOSS_CAPTURE_POOL_FULL", WREATH_NFR_LOSS_CAPTURE_POOL_FULL) < 0 ||
        add_int(module, "LOSS_BODY_TRUNCATED", WREATH_NFR_LOSS_BODY_TRUNCATED) < 0 ||
        add_int(module, "CAP_RAW", WREATH_NFR_CAP_RAW) < 0 ||
        add_int(module, "CAP_HASHED", WREATH_NFR_CAP_HASHED) < 0 ||
        add_int(module, "CAP_MASKED", WREATH_NFR_CAP_MASKED) < 0 ||
        add_int(module, "CAP_LENGTH", WREATH_NFR_CAP_LENGTH) < 0) {
        goto error;
    }

    PyObject *capsule = PyCapsule_New(&capi, WREATH_FLIGHT_CAPI_NAME, NULL);
    if (capsule == NULL || PyModule_AddObject(module, "_C_API", capsule) < 0) {
        Py_XDECREF(capsule);
        goto error;
    }
    return module;

error:
    Py_DECREF(module);
    return NULL;
}
