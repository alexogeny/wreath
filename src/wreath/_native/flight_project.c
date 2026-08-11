/* Flight-cell batch dispatch, independent of the mmap recorder extension. */
#include "wreathcore.h"

#define FLIGHT_CELL_BYTES 64
#define FLIGHT_COMPLETION 1
#define FLIGHT_CORRELATION 2
#define FLIGHT_PHASE 3
#define FLIGHT_LOG 6

typedef struct {
    PyObject_HEAD
    PyObject *config;
    Py_ssize_t cycle;
    unsigned char completion[FLIGHT_CELL_BYTES];
    unsigned char correlation[FLIGHT_CELL_BYTES];
    unsigned char *phases;
    unsigned char *logs;
    Py_ssize_t phase_count;
    Py_ssize_t phase_capacity;
    Py_ssize_t log_count;
    Py_ssize_t log_capacity;
    unsigned int has_completion : 1;
    unsigned int has_correlation : 1;
} FlightAssembly;

static PyObject *
flight_enum(PyObject *type, unsigned long value)
{
    PyObject *number = PyLong_FromUnsignedLong(value);
    PyObject *result;
    if (number == NULL) return NULL;
    result = PyObject_CallOneArg(type, number);
    Py_DECREF(number);
    return result;
}

static PyObject *
flight_slot(PyObject *projector, PyObject *pending, PyObject *assembly_type,
            PyObject *config, PyObject *cycle, Py_ssize_t max_pending,
            PyObject *request_id)
{
    PyObject *entry = NULL;
    if (PyDict_GetItemRef(pending, request_id, &entry) < 0) return NULL;
    if (entry == NULL) {
        if (PyDict_GET_SIZE(pending) >= max_pending) {
            PyObject *ignored = PyObject_CallMethod(
                projector, "_evict_oldest_pending", NULL);
            if (ignored == NULL) return NULL;
            Py_DECREF(ignored);
        }
        entry = PyObject_CallOneArg(assembly_type, config);
        if (entry == NULL || PyDict_SetItem(pending, request_id, entry) < 0) {
            Py_XDECREF(entry);
            return NULL;
        }
    }
    if (!Py_IS_TYPE(entry, (PyTypeObject *)assembly_type)) {
        PyErr_SetString(PyExc_TypeError,
                        "flight assembly factory returned the wrong native type");
        Py_DECREF(entry);
        return NULL;
    }
    {
        Py_ssize_t value = PyLong_AsSsize_t(cycle);
        if (value == -1 && PyErr_Occurred()) {
            Py_DECREF(entry);
            return NULL;
        }
        ((FlightAssembly *)entry)->cycle = value;
    }
    return entry;
}

static PyObject *
flight_completion_cell(const unsigned char *cell, PyObject *cell_type,
                       PyObject *protocol_type, PyObject *terminal_type)
{
    unsigned int protocol_value = cell[56] <= 4 ? cell[56] : 0;
    unsigned int terminal_value = cell[57] <= 5 ? cell[57] : 0;
    PyObject *protocol = NULL;
    PyObject *terminal = NULL;
    PyObject *result = NULL;
    protocol = flight_enum(protocol_type, protocol_value);
    terminal = flight_enum(terminal_type, terminal_value);
    if (protocol == NULL || terminal == NULL) goto done;
    result = PyObject_CallFunction(
        cell_type, "KKIIKIKKOOiiiI",
        (unsigned long long)wreath_load_u64_le(cell + 8),
        (unsigned long long)wreath_load_u64_le(cell + 16),
        wreath_load_u32_le(cell + 24), wreath_load_u32_le(cell + 28),
        (unsigned long long)wreath_load_u64_le(cell + 32),
        wreath_load_u32_le(cell + 4),
        (unsigned long long)wreath_load_u64_le(cell + 40),
        (unsigned long long)wreath_load_u64_le(cell + 48),
        protocol, terminal, (int)cell[58], (int)cell[59],
        (int)wreath_load_u16_le(cell + 2), wreath_load_u32_le(cell + 60));
done:
    Py_XDECREF(terminal);
    Py_XDECREF(protocol);
    return result;
}

static int
flight_ingest_completion(PyObject *projector, PyObject *pending,
                         PyObject *assembly_type, PyObject *config, PyObject *cycle,
                         Py_ssize_t max_pending, const unsigned char *cell)
{
    PyObject *request_id = PyLong_FromUnsignedLongLong(
        wreath_load_u64_le(cell + 8));
    PyObject *entry = NULL;
    if (request_id == NULL) return -1;
    entry = flight_slot(projector, pending, assembly_type, config, cycle,
                        max_pending, request_id);
    if (entry == NULL) goto error;
    FlightAssembly *assembly = (FlightAssembly *)entry;
    memcpy(assembly->completion, cell, FLIGHT_CELL_BYTES);
    assembly->has_completion = 1;
    Py_DECREF(entry);
    Py_DECREF(request_id);
    return 0;
error:
    Py_XDECREF(entry);
    Py_DECREF(request_id);
    return -1;
}

static PyObject *
flight_correlation_cell(const unsigned char *cell, PyObject *cell_type)
{
    PyObject *hi = PyLong_FromUnsignedLongLong(wreath_load_u64_le(cell + 16));
    PyObject *lo = PyLong_FromUnsignedLongLong(wreath_load_u64_le(cell + 24));
    PyObject *shift = PyLong_FromLong(64);
    PyObject *upper = NULL, *trace = NULL, *result = NULL;
    if (hi == NULL || lo == NULL || shift == NULL) goto done;
    upper = PyNumber_Lshift(hi, shift);
    if (upper == NULL) goto done;
    trace = PyNumber_Or(upper, lo);
    if (trace == NULL) goto done;
    result = PyObject_CallFunction(
        cell_type, "KOKKI",
        (unsigned long long)wreath_load_u64_le(cell + 8), trace,
        (unsigned long long)wreath_load_u64_le(cell + 40),
        (unsigned long long)wreath_load_u64_le(cell + 32),
        wreath_load_u16_le(cell + 2));
done:
    Py_XDECREF(trace); Py_XDECREF(upper); Py_XDECREF(shift);
    Py_XDECREF(lo); Py_XDECREF(hi);
    return result;
}

static int
flight_ingest_correlation(PyObject *projector, PyObject *pending,
                          PyObject *assembly_type, PyObject *config, PyObject *cycle,
                          Py_ssize_t max_pending, const unsigned char *cell)
{
    PyObject *request_id = PyLong_FromUnsignedLongLong(wreath_load_u64_le(cell + 8));
    PyObject *entry = NULL;
    if (request_id == NULL) return -1;
    entry = flight_slot(projector, pending, assembly_type, config, cycle,
                        max_pending, request_id);
    if (entry == NULL) goto error;
    FlightAssembly *assembly = (FlightAssembly *)entry;
    memcpy(assembly->correlation, cell, FLIGHT_CELL_BYTES);
    assembly->has_correlation = 1;
    Py_DECREF(entry); Py_DECREF(request_id);
    return 0;
error:
    Py_XDECREF(entry); Py_DECREF(request_id);
    return -1;
}

static PyObject *
flight_phase_record(const unsigned char *raw, PyObject *record_type,
                    PyObject *kind_type, PyObject *coverage_type)
{
    unsigned long kind_value = wreath_load_u16_le(raw);
    unsigned long coverage_value = raw[4];
    PyObject *kind = flight_enum(kind_type, kind_value <= 15 ? kind_value : 0);
    PyObject *coverage = flight_enum(
        coverage_type, coverage_value <= 3 ? coverage_value : 3);
    PyObject *result = NULL;
    if (kind == NULL || coverage == NULL) goto done;
    result = PyObject_CallFunction(
        record_type, "OIIIOI", kind, wreath_load_u32_le(raw + 12),
        wreath_load_u32_le(raw + 8), wreath_load_u16_le(raw + 2), coverage,
        (unsigned int)raw[5]);
done:
    Py_XDECREF(coverage); Py_XDECREF(kind);
    return result;
}

static int
flight_reserve(unsigned char **items, Py_ssize_t *capacity,
               Py_ssize_t needed, Py_ssize_t width)
{
    Py_ssize_t next = *capacity ? *capacity : 4;
    unsigned char *grown;
    if (needed <= *capacity) return 0;
    while (next < needed) {
        if (next > PY_SSIZE_T_MAX / 2) {
            PyErr_NoMemory();
            return -1;
        }
        next *= 2;
    }
    if (next > PY_SSIZE_T_MAX / width) {
        PyErr_NoMemory();
        return -1;
    }
    grown = PyMem_Realloc(*items, (size_t)(next * width));
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    *items = grown;
    *capacity = next;
    return 0;
}

static int
flight_ingest_phase(PyObject *projector, PyObject *pending,
                    PyObject *assembly_type, PyObject *config, PyObject *cycle,
                    Py_ssize_t max_pending, const unsigned char *cell)
{
    unsigned int count = cell[2];
    PyObject *request_id = NULL, *entry = NULL;
    if (count < 1 || count > 3) return 1;
    request_id = PyLong_FromUnsignedLongLong(wreath_load_u64_le(cell + 8));
    if (request_id == NULL) return -1;
    entry = flight_slot(projector, pending, assembly_type, config, cycle,
                        max_pending, request_id);
    if (entry == NULL) goto error;
    FlightAssembly *assembly = (FlightAssembly *)entry;
    if (assembly->phase_count > PY_SSIZE_T_MAX - count) {
        PyErr_NoMemory();
        goto error;
    }
    if (flight_reserve(&assembly->phases, &assembly->phase_capacity,
                       assembly->phase_count + count, 16) < 0) goto error;
    memcpy(assembly->phases + assembly->phase_count * 16,
           cell + 16, (size_t)count * 16);
    assembly->phase_count += count;
    Py_DECREF(entry); Py_DECREF(request_id);
    return 0;
error:
    Py_XDECREF(entry); Py_XDECREF(request_id);
    return -1;
}

static PyObject *
flight_log_arg(const unsigned char *raw, unsigned int tag, Py_ssize_t width,
               PyObject *arg_type, PyObject *tag_type)
{
    PyObject *kind = flight_enum(tag_type, tag);
    PyObject *number = NULL, *fraction = NULL, *payload = NULL, *result = NULL;
    if (kind == NULL) return NULL;
    number = PyLong_FromLong(0);
    fraction = PyFloat_FromDouble(0.0);
    payload = PyBytes_FromStringAndSize(NULL, 0);
    if (number == NULL || fraction == NULL || payload == NULL) goto done;
    if (tag == 1) {
        Py_SETREF(number, PyLong_FromLong(raw[0] != 0));
    }
    else if (tag == 2) {
        Py_SETREF(number, PyLong_FromLongLong((long long)wreath_load_u64_le(raw)));
    }
    else if (tag == 3) {
        uint64_t bits = wreath_load_u64_le(raw);
        double value;
        memcpy(&value, &bits, sizeof(value));
        Py_SETREF(fraction, PyFloat_FromDouble(value));
    }
    else if (tag == 4) {
        Py_SETREF(payload, PyBytes_FromStringAndSize((const char *)raw, width));
    }
    else if (tag == 5) {
        Py_SETREF(number, PyLong_FromUnsignedLongLong(wreath_load_u64_le(raw)));
    }
    else if (tag == 6) {
        Py_SETREF(number, PyLong_FromUnsignedLong(wreath_load_u32_le(raw)));
    }
    if (number == NULL || fraction == NULL || payload == NULL) goto done;
    result = PyObject_CallFunctionObjArgs(
        arg_type, kind, number, fraction, payload, NULL);
done:
    Py_XDECREF(payload); Py_XDECREF(fraction); Py_XDECREF(number); Py_DECREF(kind);
    return result;
}

static PyObject *
flight_log_cell(const unsigned char *cell, PyObject *cell_type,
                PyObject *arg_type, PyObject *tag_type, PyObject *severity_type)
{
    unsigned int declared = cell[26];
    unsigned int byte_count = cell[27];
    unsigned int severity_value = cell[24];
    const unsigned char *blob = cell + 32;
    Py_ssize_t offset = 0;
    PyObject *items = NULL, *severity = NULL, *result = NULL;
    if (byte_count > 32) return Py_NewRef(Py_None);
    items = PyList_New(0);
    if (items == NULL) return NULL;
    while (offset < byte_count) {
        unsigned int tag = blob[offset++];
        Py_ssize_t width;
        PyObject *item;
        if (tag > 6) goto malformed;
        if (tag == 4) {
            if (offset >= byte_count) goto malformed;
            width = blob[offset++];
        }
        else {
            static const unsigned char widths[7] = {0, 1, 8, 8, 0, 8, 4};
            width = widths[tag];
        }
        if (width > byte_count - offset) goto malformed;
        item = flight_log_arg(blob + offset, tag, width, arg_type, tag_type);
        if (item == NULL) goto error;
        if (PyList_Append(items, item) < 0) {
            Py_DECREF(item);
            goto error;
        }
        Py_DECREF(item);
        offset += width;
    }
    if ((unsigned int)PyList_GET_SIZE(items) != declared) goto malformed;
    {
        PyObject *tuple = PyList_AsTuple(items);
        if (tuple == NULL) goto error;
        if (severity_value == 1 || severity_value == 5 || severity_value == 9 ||
            severity_value == 13 || severity_value == 17 || severity_value == 21)
            severity = flight_enum(severity_type, severity_value);
        else
            severity = PyLong_FromUnsignedLong(severity_value);
        if (severity != NULL) {
            result = PyObject_CallFunction(
                cell_type, "KIOIIOII",
                (unsigned long long)wreath_load_u64_le(cell + 8),
                wreath_load_u32_le(cell + 4), severity,
                wreath_load_u32_le(cell + 16), (unsigned int)cell[25], tuple,
                wreath_load_u16_le(cell + 2), wreath_load_u32_le(cell + 20));
        }
        Py_DECREF(tuple);
    }
    Py_XDECREF(severity); Py_DECREF(items);
    return result;
malformed:
    Py_DECREF(items);
    return Py_NewRef(Py_None);
error:
    Py_XDECREF(severity); Py_DECREF(items);
    return NULL;
}

static int
flight_log_valid(const unsigned char *cell)
{
    static const unsigned char widths[7] = {0, 1, 8, 8, 0, 8, 4};
    unsigned int declared = cell[26];
    unsigned int byte_count = cell[27];
    const unsigned char *blob = cell + 32;
    unsigned int offset = 0;
    unsigned int count = 0;
    if (byte_count > 32) return 0;
    while (offset < byte_count) {
        unsigned int tag = blob[offset++];
        unsigned int width;
        if (tag > 6) return 0;
        if (tag == 4) {
            if (offset >= byte_count) return 0;
            width = blob[offset++];
        }
        else {
            width = widths[tag];
        }
        if (width > byte_count - offset) return 0;
        offset += width;
        count++;
    }
    return count == declared;
}

static PyObject *
flight_assembly_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"config", NULL};
    PyObject *config;
    FlightAssembly *self;
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "O!:FlightAssembly", keywords, &PyTuple_Type, &config)) {
        return NULL;
    }
    if (PyTuple_GET_SIZE(config) != 14) {
        PyErr_SetString(PyExc_ValueError,
                        "flight assembly config must have fourteen items");
        return NULL;
    }
    self = (FlightAssembly *)type->tp_alloc(type, 0);
    if (self == NULL) return NULL;
    self->config = Py_NewRef(config);
    self->cycle = -1;
    self->phases = NULL;
    self->logs = NULL;
    self->phase_count = 0;
    self->phase_capacity = 0;
    self->log_count = 0;
    self->log_capacity = 0;
    self->has_completion = 0;
    self->has_correlation = 0;
    return (PyObject *)self;
}

static int
flight_assembly_traverse(FlightAssembly *self, visitproc visit, void *arg)
{
    Py_VISIT(self->config);
    return 0;
}

static int
flight_assembly_clear(FlightAssembly *self)
{
    Py_CLEAR(self->config);
    return 0;
}

static void
flight_assembly_dealloc(FlightAssembly *self)
{
    PyObject_GC_UnTrack(self);
    PyMem_Free(self->phases);
    PyMem_Free(self->logs);
    flight_assembly_clear(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
flight_assembly_get_cycle(FlightAssembly *self, void *Py_UNUSED(closure))
{
    return PyLong_FromSsize_t(self->cycle);
}

static int
flight_assembly_set_cycle(FlightAssembly *self, PyObject *value,
                          void *Py_UNUSED(closure))
{
    Py_ssize_t cycle;
    if (value == NULL) {
        PyErr_SetString(PyExc_TypeError, "flight assembly cycle cannot be deleted");
        return -1;
    }
    cycle = PyLong_AsSsize_t(value);
    if (cycle == -1 && PyErr_Occurred()) return -1;
    self->cycle = cycle;
    return 0;
}

static PyObject *
flight_assembly_get_completion(FlightAssembly *self, void *Py_UNUSED(closure))
{
    if (!self->has_completion) return Py_NewRef(Py_None);
    return flight_completion_cell(
        self->completion, PyTuple_GET_ITEM(self->config, 0),
        PyTuple_GET_ITEM(self->config, 6), PyTuple_GET_ITEM(self->config, 7));
}

static PyObject *
flight_assembly_get_correlation(FlightAssembly *self, void *Py_UNUSED(closure))
{
    if (!self->has_correlation) return Py_NewRef(Py_None);
    return flight_correlation_cell(
        self->correlation, PyTuple_GET_ITEM(self->config, 1));
}

static PyObject *
flight_assembly_get_phases(FlightAssembly *self, void *Py_UNUSED(closure))
{
    PyObject *result = PyList_New(self->phase_count);
    if (result == NULL) return NULL;
    for (Py_ssize_t index = 0; index < self->phase_count; index++) {
        PyObject *record = flight_phase_record(
            self->phases + index * 16, PyTuple_GET_ITEM(self->config, 8),
            PyTuple_GET_ITEM(self->config, 9), PyTuple_GET_ITEM(self->config, 10));
        if (record == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyList_SET_ITEM(result, index, record);
    }
    return result;
}

static PyObject *
flight_assembly_get_logs(FlightAssembly *self, void *Py_UNUSED(closure))
{
    PyObject *result = PyList_New(self->log_count);
    if (result == NULL) return NULL;
    for (Py_ssize_t index = 0; index < self->log_count; index++) {
        PyObject *record = flight_log_cell(
            self->logs + index * FLIGHT_CELL_BYTES,
            PyTuple_GET_ITEM(self->config, 3), PyTuple_GET_ITEM(self->config, 11),
            PyTuple_GET_ITEM(self->config, 12), PyTuple_GET_ITEM(self->config, 13));
        if (record == NULL || record == Py_None) {
            Py_XDECREF(record);
            Py_DECREF(result);
            if (!PyErr_Occurred()) {
                PyErr_SetString(PyExc_RuntimeError,
                                "stored flight log cell became malformed");
            }
            return NULL;
        }
        PyList_SET_ITEM(result, index, record);
    }
    return result;
}

static PyGetSetDef flight_assembly_getset[] = {
    {"cycle", (getter)flight_assembly_get_cycle,
     (setter)flight_assembly_set_cycle, NULL, NULL},
    {"completion", (getter)flight_assembly_get_completion, NULL, NULL, NULL},
    {"correlation", (getter)flight_assembly_get_correlation, NULL, NULL, NULL},
    {"phases", (getter)flight_assembly_get_phases, NULL, NULL, NULL},
    {"logs", (getter)flight_assembly_get_logs, NULL, NULL, NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyType_Slot flight_assembly_slots[] = {
    {Py_tp_new, flight_assembly_new},
    {Py_tp_dealloc, flight_assembly_dealloc},
    {Py_tp_traverse, flight_assembly_traverse},
    {Py_tp_clear, flight_assembly_clear},
    {Py_tp_getset, flight_assembly_getset},
    {0, NULL},
};

static PyType_Spec flight_assembly_spec = {
    .name = "wreath._native._core.FlightAssembly",
    .basicsize = sizeof(FlightAssembly),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = flight_assembly_slots,
};

int
wreath_register_flight_project(PyObject *module)
{
    PyObject *type = PyType_FromSpec(&flight_assembly_spec);
    if (type == NULL) return -1;
    if (PyModule_AddObject(module, "FlightAssembly", type) < 0) {
        Py_DECREF(type);
        return -1;
    }
    return 0;
}

static int
flight_ingest_log(PyObject *projector, PyObject *pending,
                  PyObject *assembly_type, PyObject *config, PyObject *cycle,
                  Py_ssize_t max_pending, const unsigned char *cell,
                  PyObject *cell_type, PyObject *arg_type,
                  PyObject *tag_type, PyObject *severity_type)
{
    PyObject *decoded = NULL;
    PyObject *request_id = NULL, *entry = NULL;
    if (!flight_log_valid(cell)) return 1;
    if (wreath_load_u64_le(cell + 8) == 0) {
        decoded = flight_log_cell(
            cell, cell_type, arg_type, tag_type, severity_type);
        if (decoded == NULL) return -1;
        if (decoded == Py_None) { Py_DECREF(decoded); return 1; }
        PyObject *ignored = PyObject_CallMethod(
            projector, "_emit_standalone_log", "O", decoded);
        Py_DECREF(decoded);
        if (ignored == NULL) return -1;
        Py_DECREF(ignored);
        return 0;
    }
    request_id = PyLong_FromUnsignedLongLong(wreath_load_u64_le(cell + 8));
    if (request_id == NULL) goto error;
    entry = flight_slot(projector, pending, assembly_type, config, cycle,
                        max_pending, request_id);
    if (entry == NULL) goto error;
    FlightAssembly *assembly = (FlightAssembly *)entry;
    if (assembly->log_count == PY_SSIZE_T_MAX) {
        PyErr_NoMemory();
        goto error;
    }
    if (flight_reserve(&assembly->logs, &assembly->log_capacity,
                       assembly->log_count + 1, FLIGHT_CELL_BYTES) < 0) goto error;
    memcpy(assembly->logs + assembly->log_count * FLIGHT_CELL_BYTES,
           cell, FLIGHT_CELL_BYTES);
    assembly->log_count++;
    Py_DECREF(entry); Py_DECREF(request_id);
    return 0;
error:
    Py_XDECREF(entry); Py_XDECREF(request_id); Py_XDECREF(decoded);
    return -1;
}

PyObject *
wreath_flight_project_cells(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *projector, *config;
    Py_buffer view;
    PyObject *pending = NULL;
    PyObject *cycle = NULL;
    PyObject *max_pending_object = NULL;
    Py_ssize_t max_pending;
    unsigned long errors = 0;
    if (!PyArg_ParseTuple(args, "Oy*O!:flight_project_cells", &projector, &view,
                          &PyTuple_Type, &config)) return NULL;
    if (PyTuple_GET_SIZE(config) != 14) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "flight projection config must have fourteen items");
        return NULL;
    }
    pending = PyObject_GetAttrString(projector, "_pending");
    cycle = PyObject_GetAttrString(projector, "_cycle");
    max_pending_object = PyObject_GetAttrString(projector, "_max_pending");
    if (pending == NULL || cycle == NULL || max_pending_object == NULL) goto error;
    max_pending = PyLong_AsSsize_t(max_pending_object);
    if (max_pending < 0 && PyErr_Occurred()) goto error;
    for (Py_ssize_t offset = 0; offset + FLIGHT_CELL_BYTES <= view.len;
         offset += FLIGHT_CELL_BYTES) {
        const unsigned char *cell = (const unsigned char *)view.buf + offset;
        int result;
        if (cell[0] != 1) {
            errors++;
            continue;
        }
        switch (cell[1]) {
        case FLIGHT_COMPLETION:
            if (flight_ingest_completion(
                    projector, pending, PyTuple_GET_ITEM(config, 5), config, cycle,
                    max_pending, cell) < 0) goto error;
            break;
        case FLIGHT_CORRELATION:
            if (flight_ingest_correlation(
                    projector, pending, PyTuple_GET_ITEM(config, 5), config, cycle,
                    max_pending, cell) < 0) goto error;
            break;
        case FLIGHT_PHASE:
            result = flight_ingest_phase(
                projector, pending, PyTuple_GET_ITEM(config, 5), config, cycle,
                max_pending, cell);
            if (result < 0) goto error;
            errors += result;
            break;
        case FLIGHT_LOG:
            result = flight_ingest_log(
                projector, pending, PyTuple_GET_ITEM(config, 5), config, cycle,
                max_pending, cell, PyTuple_GET_ITEM(config, 3),
                PyTuple_GET_ITEM(config, 11), PyTuple_GET_ITEM(config, 12),
                PyTuple_GET_ITEM(config, 13));
            if (result < 0) goto error;
            errors += result;
            break;
        default:
            break;
        }
    }
    Py_DECREF(max_pending_object);
    Py_DECREF(cycle);
    Py_DECREF(pending);
    PyBuffer_Release(&view);
    return PyLong_FromUnsignedLong(errors);
error:
    Py_XDECREF(max_pending_object);
    Py_XDECREF(cycle);
    Py_XDECREF(pending);
    PyBuffer_Release(&view);
    return NULL;
}

static int
flight_increment(PyObject *loss, const char *name)
{
    PyObject *value = PyObject_GetAttrString(loss, name);
    PyObject *one = NULL;
    PyObject *next;
    int result;
    if (value == NULL) return -1;
    one = PyLong_FromLong(1);
    if (one == NULL) {
        Py_DECREF(value);
        return -1;
    }
    next = PyNumber_Add(value, one);
    Py_DECREF(one);
    Py_DECREF(value);
    if (next == NULL) return -1;
    result = PyObject_SetAttrString(loss, name, next);
    Py_DECREF(next);
    return result;
}

static int
flight_count_orphans(PyObject *entry, PyObject *loss)
{
    PyObject *correlation = PyObject_GetAttrString(entry, "correlation");
    PyObject *phases = PyObject_GetAttrString(entry, "phases");
    PyObject *logs = PyObject_GetAttrString(entry, "logs");
    int phase_truth, log_truth;
    if (correlation == NULL || phases == NULL || logs == NULL) goto error;
    phase_truth = PyObject_IsTrue(phases);
    log_truth = PyObject_IsTrue(logs);
    if (phase_truth < 0 || log_truth < 0) goto error;
    if (correlation != Py_None && flight_increment(loss, "orphan_correlation") < 0)
        goto error;
    if (phase_truth && flight_increment(loss, "orphan_phase") < 0) goto error;
    if (log_truth && flight_increment(loss, "orphan_log") < 0) goto error;
    Py_DECREF(logs); Py_DECREF(phases); Py_DECREF(correlation);
    return 0;
error:
    Py_XDECREF(logs); Py_XDECREF(phases); Py_XDECREF(correlation);
    return -1;
}

PyObject *
wreath_flight_settle(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *projector, *pending, *loss, *keys = NULL;
    PyObject *finalize = NULL;
    Py_ssize_t cycle;
    Py_ssize_t emitted = 0;
    if (!PyArg_ParseTuple(args, "OOOn:flight_settle", &projector, &pending,
                          &loss, &cycle)) return NULL;
    if (!PyDict_Check(pending)) {
        PyErr_SetString(PyExc_TypeError, "flight pending table must be a dict");
        return NULL;
    }
    keys = PyDict_Keys(pending);
    finalize = PyObject_GetAttrString(projector, "_finalize");
    if (keys == NULL || finalize == NULL) goto error;
    for (Py_ssize_t index = 0; index < PyList_GET_SIZE(keys); index++) {
        PyObject *request_id = PyList_GET_ITEM(keys, index);
        PyObject *entry = NULL, *entry_cycle = NULL, *completion = NULL;
        Py_ssize_t seen;
        if (PyDict_GetItemRef(pending, request_id, &entry) < 0) goto error;
        if (entry == NULL) continue;
        entry_cycle = PyObject_GetAttrString(entry, "cycle");
        if (entry_cycle == NULL) { Py_DECREF(entry); goto error; }
        seen = PyLong_AsSsize_t(entry_cycle);
        Py_DECREF(entry_cycle);
        if (seen == -1 && PyErr_Occurred()) { Py_DECREF(entry); goto error; }
        if (seen >= cycle) { Py_DECREF(entry); continue; }
        completion = PyObject_GetAttrString(entry, "completion");
        if (completion == NULL) { Py_DECREF(entry); goto error; }
        if (completion != Py_None) {
            PyObject *ignored = PyObject_CallFunctionObjArgs(
                finalize, request_id, entry, NULL);
            Py_DECREF(completion);
            Py_DECREF(entry);
            if (ignored == NULL) goto error;
            Py_DECREF(ignored);
            emitted++;
            continue;
        }
        Py_DECREF(completion);
        if (flight_count_orphans(entry, loss) < 0 ||
            PyDict_DelItem(pending, request_id) < 0) {
            Py_DECREF(entry);
            goto error;
        }
        Py_DECREF(entry);
    }
    Py_DECREF(finalize);
    Py_DECREF(keys);
    return PyLong_FromSsize_t(emitted);
error:
    Py_XDECREF(finalize);
    Py_XDECREF(keys);
    return NULL;
}

PyObject *
wreath_flight_evict_pending(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *pending, *loss;
    PyObject *oldest_key = NULL, *oldest_entry = NULL;
    Py_ssize_t position = 0;
    PyObject *key, *entry;
    Py_ssize_t oldest_cycle = PY_SSIZE_T_MAX;
    if (!PyArg_ParseTuple(args, "OO:flight_evict_pending", &pending, &loss)) return NULL;
    if (!PyDict_Check(pending) || PyDict_Size(pending) == 0) {
        PyErr_SetString(PyExc_ValueError, "cannot evict from an empty flight pending table");
        return NULL;
    }
    while (PyDict_Next(pending, &position, &key, &entry)) {
        PyObject *cycle_object = PyObject_GetAttrString(entry, "cycle");
        Py_ssize_t cycle;
        if (cycle_object == NULL) goto error;
        cycle = PyLong_AsSsize_t(cycle_object);
        Py_DECREF(cycle_object);
        if (cycle == -1 && PyErr_Occurred()) goto error;
        if (cycle < oldest_cycle) {
            oldest_cycle = cycle;
            Py_XSETREF(oldest_key, Py_NewRef(key));
            Py_XSETREF(oldest_entry, Py_NewRef(entry));
        }
    }
    if (PyDict_DelItem(pending, oldest_key) < 0) goto error;
    {
        PyObject *completion = PyObject_GetAttrString(oldest_entry, "completion");
        if (completion == NULL) goto error;
        if (completion != Py_None) {
            Py_DECREF(completion);
            if (flight_increment(loss, "pending_evicted") < 0) goto error;
        }
        else {
            Py_DECREF(completion);
            if (flight_count_orphans(oldest_entry, loss) < 0) goto error;
        }
    }
    Py_DECREF(oldest_entry); Py_DECREF(oldest_key);
    Py_RETURN_NONE;
error:
    Py_XDECREF(oldest_entry); Py_XDECREF(oldest_key);
    return NULL;
}
