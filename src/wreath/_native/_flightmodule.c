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

/* `PyUnicode_FSConverter` that also accepts None, for an optional path.
 *
 * `ring_path=None` is the normal way to say "no forensic ring", and callers
 * build kwargs without branching on it, so the converter has to take it. The
 * plain FS converter rejects None, which would make `ring_path=None` a
 * TypeError -- the one spelling every caller reaches for first. */
static int
path_or_none(PyObject *object, void *address)
{
    PyObject **target = (PyObject **)address;
    if (object == Py_None) {
        *target = NULL;
        return 1;
    }
    return PyUnicode_FSConverter(object, target);
}

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
                             "capture_hash_key", "ring_path", NULL};
    unsigned int mode, worker_id = 0, ring_records = 16384, active_requests = 2048,
                       histogram_count = 1, phase_slots = 256, capture_slabs = 0,
                       slab_bytes = 65536;
    int completion_summaries = 1;
    double detailed_sample_rate = 0.0;
    unsigned long long detailed_slow_us = 0;
    PyObject *hash_key = NULL;
    PyObject *ring_path_obj = NULL;
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "I|IIIIpdIKIIOO&:Recorder", kwlist,
                                     &mode, &worker_id, &ring_records, &active_requests,
                                     &histogram_count, &completion_summaries,
                                     &detailed_sample_rate, &phase_slots,
                                     &detailed_slow_us, &capture_slabs, &slab_bytes,
                                     &hash_key, path_or_none, &ring_path_obj)) {
        return NULL;
    }
    if (detailed_sample_rate < 0.0 || detailed_sample_rate > 1.0) {
        PyErr_SetString(PyExc_ValueError, "detailed_sample_rate must be in [0, 1]");
        Py_XDECREF(ring_path_obj);
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
            Py_XDECREF(ring_path_obj);
            return NULL;
        }
    }
    /* threshold = round(rate * 2^32): a 32-bit draw < threshold arms. rate 1.0
     * yields 2^32 so every draw (< 2^32) arms; rate 0.0 arms none. */
    uint64_t detailed_sample_threshold =
        (uint64_t)(detailed_sample_rate * 4294967296.0 + 0.5);
    RecorderObject *self = (RecorderObject *)type->tp_alloc(type, 0);
    if (self == NULL) {
        Py_XDECREF(ring_path_obj);
        return NULL;
    }
    const char *ring_path =
        ring_path_obj == NULL ? NULL : PyBytes_AS_STRING(ring_path_obj);
    self->worker = wreath_nfr_worker_new((uint8_t)mode, worker_id, ring_records,
                                         active_requests, histogram_count,
                                         completion_summaries,
                                         detailed_sample_threshold, phase_slots,
                                         detailed_slow_us, capture_slabs, slab_bytes,
                                         hash_key0, hash_key1, ring_path);
    Py_XDECREF(ring_path_obj);
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

/* Publish one pre-packed log cell into the ring.
 *
 * The buffer must be exactly one cell and must carry the log kind byte: this is
 * the one place Python can put bytes directly into the ring, so it refuses
 * anything that is not a log record rather than trusting the caller. A
 * completion forged from Python would corrupt the projector's assembly, and
 * "the caller would not do that" is not a check.
 *
 * Returns True when published, False when the ring was full -- a full ring is a
 * counted drop, not an error, exactly as it is for a completion. */
static PyObject *
recorder_publish_log(RecorderObject *self, PyObject *arg)
{
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    if (view.len != WREATH_NFR_CELL_SIZE) {
        PyBuffer_Release(&view);
        PyErr_Format(PyExc_ValueError,
                     "a log cell is exactly %d bytes, got %zd",
                     WREATH_NFR_CELL_SIZE, view.len);
        return NULL;
    }
    const unsigned char *bytes = (const unsigned char *)view.buf;
    if (bytes[0] != WREATH_NFR_SCHEMA_VERSION || bytes[1] != WREATH_NFR_KIND_LOG) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError,
                        "publish_log accepts only a current-schema log cell");
        return NULL;
    }
    int published = wreath_nfr_publish_cell(self->worker, view.buf);
    PyBuffer_Release(&view);
    return PyBool_FromLong(published);
}

/* --- the native log emitter ----------------------------------------------- */
/*
 * `wreath_nfr_log`: pack one record straight into a cell and publish it, with
 * no LogArg, no LogCell, and no `struct.pack` in between. This is the piece
 * stages 1-6 deliberately deferred; `_flight_schema.py` still holds the layout,
 * and `_logsite.pack_value` / `LogCell.encode` -- which run when there is no
 * ring, when a record is buffered, or when the caller is not the loop -- are
 * held byte for byte to this by tests/test_logging_native_parity.py.
 *
 * What made the swap mechanical rather than a redesign is what stage 1 built
 * for it: a dense `site_id`, argument types declared at the call site, and a
 * level check that precedes marshalling. The declared types arrive here already
 * flattened into a spec blob -- one byte per field, `(type << 4) | disposition`
 * -- so packing branches on a small integer instead of on `isinstance`.
 *
 * Everything here mirrors `pack_value` and `_encode_log_arg` exactly, including
 * the parts that look like details: bool is checked before int because bool is
 * an int subclass; a value that fails its declared type packs as `none` and is
 * *counted*, never raised, because a log call that can break the request that
 * made it is worse than a log line reading `?`; a clipped string backs off to a
 * UTF-8 boundary, because a record that raises on read is the one failure a log
 * line must never have.
 */

/* One argument's packing cursor over a cell's inline area. The fingerprint key
 * travels here rather than being read off the worker: the Python packer hashes
 * with the site registry's key, and the two must agree byte for byte. */
typedef struct {
    uint8_t *cursor;
    uint8_t *end;
    uint64_t k0;
    uint64_t k1;
    uint16_t flags;
    uint32_t mismatches;
    uint8_t count;
} log_pack_state;

/* Write `tag` and `width` payload bytes if they fit. Returns 0 when they do
 * not, which is the caller's signal to stop and flag truncation. */
static int
log_pack_fixed(log_pack_state *st, uint8_t tag, const void *payload, size_t width)
{
    if ((size_t)(st->end - st->cursor) < 1u + width) {
        return 0;
    }
    *st->cursor++ = tag;
    if (width != 0) {
        memcpy(st->cursor, payload, width);
        st->cursor += width;
    }
    return 1;
}

/* Write a STR argument, clipping to the remaining budget and to 255 bytes.
 * Mirrors `_clip_utf8`: the cut backs off over continuation bytes so the
 * payload still decodes. */
static int
log_pack_text(log_pack_state *st, const char *data, Py_ssize_t len)
{
    size_t budget = (size_t)(st->end - st->cursor);
    if (budget < 3) {  /* tag + length + at least one byte */
        return 0;
    }
    size_t limit = budget - 2;
    if (limit > 0xFF) {
        limit = 0xFF;
    }
    size_t take = (size_t)len;
    if (take > limit) {
        take = limit;
        while (take > 0 && (((const uint8_t *)data)[take] & 0xC0) == 0x80) {
            take--;
        }
        st->flags |= WREATH_NFR_LOG_FLAG_TRUNCATED;
    }
    *st->cursor++ = WREATH_NFR_LOG_ARG_STR;
    *st->cursor++ = (uint8_t)take;
    memcpy(st->cursor, data, take);
    st->cursor += take;
    return 1;
}

/* The UTF-8 bytes of a value, as `_as_bytes` produces them: bytes verbatim,
 * anything else through `str()`. Returns a borrowed pointer valid while
 * *owner is held; the caller must Py_XDECREF *owner. */
static int
log_value_bytes(PyObject *value, const char **out, Py_ssize_t *len, PyObject **owner)
{
    *owner = NULL;
    if (PyBytes_Check(value)) {
        *out = PyBytes_AS_STRING(value);
        *len = PyBytes_GET_SIZE(value);
        return 1;
    }
    PyObject *text = PyUnicode_Check(value) ? Py_NewRef(value) : PyObject_Str(value);
    if (text == NULL) {
        return 0;
    }
    /* The fast path refuses lone surrogates; Python's `.encode("utf-8",
     * "replace")` does not, so fall back to it rather than differing. */
    const char *utf8 = PyUnicode_AsUTF8AndSize(text, len);
    if (utf8 != NULL) {
        *out = utf8;
        *owner = text;
        return 1;
    }
    PyErr_Clear();
    PyObject *encoded = PyUnicode_AsEncodedString(text, "utf-8", "replace");
    Py_DECREF(text);
    if (encoded == NULL) {
        return 0;
    }
    *out = PyBytes_AS_STRING(encoded);
    *len = PyBytes_GET_SIZE(encoded);
    *owner = encoded;
    return 1;
}

/* Pack one argument. Returns 1 on success, 0 when the cell had no room left
 * (the caller flags truncation and stops), -1 with an exception set.
 *
 * A value that fails its declared type packs as `none` and bumps `mismatches`;
 * only a genuine interpreter error (a failing `__str__`, a MemoryError) returns
 * -1. That split is the contract `pack_value` documents. */
static int
log_pack_argument(log_pack_state *st, uint8_t spec, PyObject *value)
{
    const uint8_t declared = WREATH_NFR_LOG_SPEC_TYPE(spec);
    const uint8_t disposition = WREATH_NFR_LOG_SPEC_DISPOSITION(spec);

    if (disposition == WREATH_NFR_CAPTURE_HASHED) {
        const char *data = NULL;
        Py_ssize_t len = 0;
        PyObject *owner = NULL;
        if (!log_value_bytes(value, &data, &len, &owner)) {
            return -1;
        }
        uint64_t digest =
            wreath_nfr_fingerprint(data, (size_t)len, st->k0, st->k1);
        Py_XDECREF(owner);
        st->flags |= WREATH_NFR_LOG_FLAG_REDACTED;
        return log_pack_fixed(st, WREATH_NFR_LOG_ARG_HASH, &digest, sizeof(digest));
    }
    if (disposition == WREATH_NFR_CAPTURE_MASKED
        || disposition == WREATH_NFR_CAPTURE_LENGTH) {
        const char *data = NULL;
        Py_ssize_t len = 0;
        PyObject *owner = NULL;
        if (!log_value_bytes(value, &data, &len, &owner)) {
            return -1;
        }
        Py_XDECREF(owner);
        uint32_t original = len > (Py_ssize_t)UINT32_MAX ? UINT32_MAX : (uint32_t)len;
        st->flags |= WREATH_NFR_LOG_FLAG_REDACTED;
        return log_pack_fixed(st, WREATH_NFR_LOG_ARG_LENGTH, &original,
                              sizeof(original));
    }

    if (value == Py_None) {
        if (declared != WREATH_NFR_LOG_SPEC_NONE) {
            st->mismatches++;
        }
        return log_pack_fixed(st, WREATH_NFR_LOG_ARG_NONE, NULL, 0);
    }

    switch (declared) {
    case WREATH_NFR_LOG_SPEC_BOOL: {
        /* Checked before INT deliberately: bool is an int subclass, and a
         * `True` packed as an integer reads back as 1 with no way to tell. */
        if (!PyBool_Check(value)) {
            break;
        }
        uint8_t flag = (value == Py_True) ? 1u : 0u;
        return log_pack_fixed(st, WREATH_NFR_LOG_ARG_BOOL, &flag, sizeof(flag));
    }
    case WREATH_NFR_LOG_SPEC_INT: {
        if (!PyLong_Check(value) || PyBool_Check(value)) {
            break;
        }
        int overflow = 0;
        long long number = PyLong_AsLongLongAndOverflow(value, &overflow);
        if (number == -1 && PyErr_Occurred()) {
            return -1;
        }
        if (overflow != 0) {
            /* Wider than the int64 slot. A mismatch, not an exception: the
             * pure packer used to reach `struct.pack` and raise here, out of
             * the sink and into whatever made the log call. */
            break;
        }
        int64_t packed = (int64_t)number;
        return log_pack_fixed(st, WREATH_NFR_LOG_ARG_INT, &packed, sizeof(packed));
    }
    case WREATH_NFR_LOG_SPEC_FLOAT: {
        if (PyBool_Check(value) || !(PyFloat_Check(value) || PyLong_Check(value))) {
            break;
        }
        double number = PyFloat_AsDouble(value);
        if (number == -1.0 && PyErr_Occurred()) {
            /* An int too wide to become a double. The pure packer's
             * `float(value)` raises the same OverflowError, and neither should
             * reach the caller, so it is a mismatch here too. */
            PyErr_Clear();
            break;
        }
        return log_pack_fixed(st, WREATH_NFR_LOG_ARG_FLOAT, &number, sizeof(number));
    }
    case WREATH_NFR_LOG_SPEC_STR: {
        if (!PyUnicode_Check(value)) {
            break;
        }
        const char *data = NULL;
        Py_ssize_t len = 0;
        PyObject *owner = NULL;
        if (!log_value_bytes(value, &data, &len, &owner)) {
            return -1;
        }
        int packed = log_pack_text(st, data, len);
        Py_XDECREF(owner);
        return packed;
    }
    case WREATH_NFR_LOG_SPEC_BYTES: {
        if (!PyBytes_Check(value)) {
            break;
        }
        /* The pure packer stores `value.decode("utf-8", "replace")`, so the
         * bytes are round-tripped through the same replacement rather than
         * copied: a byte string that is not valid UTF-8 must produce the same
         * U+FFFD sequence on both paths. */
        PyObject *text = PyUnicode_DecodeUTF8(PyBytes_AS_STRING(value),
                                              PyBytes_GET_SIZE(value), "replace");
        if (text == NULL) {
            return -1;
        }
        Py_ssize_t len = 0;
        const char *data = PyUnicode_AsUTF8AndSize(text, &len);
        if (data == NULL) {
            Py_DECREF(text);
            return -1;
        }
        int packed = log_pack_text(st, data, len);
        Py_DECREF(text);
        return packed;
    }
    default:
        break;
    }

    /* Fell through: the value is not what the site declared. */
    st->mismatches++;
    return log_pack_fixed(st, WREATH_NFR_LOG_ARG_NONE, NULL, 0);
}

/* Recorder.log(site_id, severity, request_id, flags, dropped_siblings,
 *              specs, values, k0, k1) -> int
 *
 * Packs and publishes one record. The return value carries two answers in one
 * int so the hot path allocates no tuple: bit 0 is whether the record reached
 * the ring, and the remaining bits are how many arguments failed their declared
 * type. The caller folds the latter into `SiteCounters.type_mismatch`.
 *
 * METH_FASTCALL, not METH_VARARGS: this is the request path, and building an
 * argument tuple per record is exactly the kind of cost this whole function
 * exists to remove. */
static PyObject *
recorder_log(RecorderObject *self, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 9) {
        PyErr_Format(PyExc_TypeError, "log() takes 9 arguments, got %zd", nargs);
        return NULL;
    }
    if (self->worker == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "recorder is closed");
        return NULL;
    }
    unsigned long site_id = PyLong_AsUnsignedLong(args[0]);
    if (site_id == (unsigned long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    long severity = PyLong_AsLong(args[1]);
    if (severity == -1 && PyErr_Occurred()) {
        return NULL;
    }
    unsigned long long request_id = PyLong_AsUnsignedLongLong(args[2]);
    if (request_id == (unsigned long long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    long flags = PyLong_AsLong(args[3]);
    if (flags == -1 && PyErr_Occurred()) {
        return NULL;
    }
    unsigned long dropped = PyLong_AsUnsignedLong(args[4]);
    if (dropped == (unsigned long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    if (!PyBytes_Check(args[5])) {
        PyErr_SetString(PyExc_TypeError, "specs must be bytes");
        return NULL;
    }
    if (!PyTuple_Check(args[6])) {
        PyErr_SetString(PyExc_TypeError, "values must be a tuple");
        return NULL;
    }
    unsigned long long k0 = PyLong_AsUnsignedLongLong(args[7]);
    if (k0 == (unsigned long long)-1 && PyErr_Occurred()) {
        return NULL;
    }
    unsigned long long k1 = PyLong_AsUnsignedLongLong(args[8]);
    if (k1 == (unsigned long long)-1 && PyErr_Occurred()) {
        return NULL;
    }

    const uint8_t *specs = (const uint8_t *)PyBytes_AS_STRING(args[5]);
    Py_ssize_t spec_count = PyBytes_GET_SIZE(args[5]);
    Py_ssize_t value_count = PyTuple_GET_SIZE(args[6]);

    wreath_nfr_log_cell cell;
    memset(&cell, 0, sizeof(cell));
    cell.schema_version = WREATH_NFR_SCHEMA_VERSION;
    cell.kind = WREATH_NFR_KIND_LOG;
    cell.site_id = (uint32_t)site_id;
    cell.request_id = (uint64_t)request_id;
    cell.severity = (uint8_t)severity;
    cell.dropped_siblings = (uint32_t)dropped;

    log_pack_state st = {
        .cursor = cell.args,
        .end = cell.args + WREATH_NFR_LOG_INLINE_ARG_BYTES,
        .k0 = (uint64_t)k0,
        .k1 = (uint64_t)k1,
        .flags = (uint16_t)flags,
        .mismatches = 0,
        .count = 0,
    };

    for (Py_ssize_t index = 0; index < spec_count; index++) {
        if (index >= WREATH_NFR_LOG_MAX_ARGS) {
            st.flags |= WREATH_NFR_LOG_FLAG_TRUNCATED;
            break;
        }
        /* Fewer values than the site declares: the missing ones pack as `none`,
         * exactly as the Python emitter pads them. The arity mismatch itself is
         * counted by the caller, which is the only side that knows the site. */
        PyObject *value = index < value_count ? PyTuple_GET_ITEM(args[6], index)
                                              : Py_None;
        int packed = log_pack_argument(&st, specs[index], value);
        if (packed < 0) {
            return NULL;
        }
        if (packed == 0) {
            st.flags |= WREATH_NFR_LOG_FLAG_TRUNCATED;
            break;
        }
        st.count++;
    }

    cell.flags = st.flags;
    cell.arg_count = st.count;
    cell.arg_bytes = (uint8_t)(st.cursor - cell.args);
    int published = wreath_nfr_publish_cell(self->worker, &cell);
    return PyLong_FromUnsignedLong(((unsigned long)st.mismatches << 1)
                                   | (unsigned long)(published != 0));
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
    if (slab_bytes > (uint64_t)PY_SSIZE_T_MAX) {
        return PyErr_NoMemory();
    }
    PyObject *result = PyList_New(max_slabs);
    if (result == NULL) {
        return NULL;
    }
    Py_ssize_t drained = 0;
    for (; drained < max_slabs; drained++) {
        PyObject *slab = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)slab_bytes);
        if (slab == NULL) {
            Py_SET_SIZE(result, drained);
            Py_DECREF(result);
            return NULL;
        }
        uint32_t length = 0;
        if (wreath_nfr_capture_drain(
                self->worker, (uint8_t *)PyBytes_AS_STRING(slab), &length, 1) == 0) {
            Py_DECREF(slab);
            break;
        }
        if (_PyBytes_Resize(&slab, length) < 0) {
            Py_SET_SIZE(result, drained);
            Py_DECREF(result);
            return NULL;
        }
        PyList_SET_ITEM(result, drained, slab);
    }
    Py_SET_SIZE(result, drained);
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

/* (epoch_mono_ns, epoch_unix_ns): the clock calibration the projector uses to
 * map a completion's end_offset_ms to Unix time. */
static PyObject *
recorder_get_clock_calibration(RecorderObject *self, void *c)
{
    (void)c;
    return Py_BuildValue("(KK)",
                         (unsigned long long)wreath_nfr_worker_epoch_mono_ns(self->worker),
                         (unsigned long long)wreath_nfr_worker_epoch_unix_ns(self->worker));
}

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
    {"publish_log", (PyCFunction)recorder_publish_log, METH_O, NULL},
    {"log", (PyCFunction)(void (*)(void))recorder_log, METH_FASTCALL, NULL},
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
    {"clock_calibration", (getter)recorder_get_clock_calibration, NULL, NULL, NULL},
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
