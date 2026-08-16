/* Fixed-cell projection for WFR1 event chunks.
 *
 * Container framing stays in `_recording_format.py`: one EVNT chunk means one
 * CRC call and the rest of that reader costs about 0.35M instructions.  The
 * expensive part was crossing the interpreter 4,096 times to slice one bytes
 * object per cell (9.66M instructions in the measured 256 KiB recording).
 * This operation owns only its result tuple and makes the same bytes objects in
 * one native loop.  No view of the input escapes the call.
 */
#include "wreathcore.h"
#include "flight_schema.h"


typedef struct {
    uint64_t *slots;
    size_t capacity;
} RingIdSet;

typedef struct {
    uint64_t request_id;
    uint8_t kind;
} RingCellKey;

typedef struct {
    uint64_t sequence;
    const uint8_t *cell;
    uint8_t kind;
} RingFileCandidate;


/* Validate one known ring kind without constructing the typed Python cell that
 * `RingRecord.decode()` will construct if a caller actually asks for it.  The
 * crash reader used to materialize that object for every slot solely to throw
 * it away after validation: 8,192 live cells cost 355M retired instructions.
 * This mirrors the refusal points in `_flight_schema.py`; unknown kinds are
 * handled by the caller and are not defects. */
static int
ring_file_cell_valid(const uint8_t *cell, uint8_t kind)
{
    if (cell[0] != WREATH_NFR_SCHEMA_VERSION) return 0;
    if (kind == WREATH_NFR_KIND_PHASE) {
        return cell[2] >= 1 && cell[2] <= WREATH_NFR_PHASE_RECORDS_PER_BATCH;
    }
    if (kind == WREATH_NFR_KIND_CLIENT_FACTS) {
        return (cell[6] == 0 && cell[7] == 0) ||
               (cell[6] >= 'A' && cell[6] <= 'Z' &&
                cell[7] >= 'A' && cell[7] <= 'Z');
    }
    if (kind != WREATH_NFR_KIND_LOG) return 1;

    uint8_t declared_count = cell[26];
    uint8_t argument_bytes = cell[27];
    if (argument_bytes > WREATH_NFR_LOG_INLINE_ARG_BYTES) return 0;
    Py_ssize_t offset = 0;
    uint8_t count = 0;
    while (offset < argument_bytes) {
        if (count >= declared_count) break;
        uint8_t argument_kind = cell[32 + offset++];
        Py_ssize_t width;
        if (argument_kind == WREATH_NFR_LOG_ARG_NONE) width = 0;
        else if (argument_kind == WREATH_NFR_LOG_ARG_BOOL) width = 1;
        else if (argument_kind == WREATH_NFR_LOG_ARG_INT ||
                 argument_kind == WREATH_NFR_LOG_ARG_FLOAT ||
                 argument_kind == WREATH_NFR_LOG_ARG_HASH) width = 8;
        else if (argument_kind == WREATH_NFR_LOG_ARG_LENGTH) width = 4;
        else if (argument_kind == WREATH_NFR_LOG_ARG_STR) {
            if (offset >= argument_bytes) return 0;
            width = cell[32 + offset++];
        } else return 0;
        if (width > argument_bytes - offset) return 0;
        offset += width;
        count++;
    }
    return count == declared_count && offset == argument_bytes;
}


PyObject *
wreath_ring_file_records(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *blob, *record_type;
    Py_ssize_t header_bytes, ring_records;
    unsigned long long head, tail;
    if (!PyArg_ParseTuple(args, "OnnKKO:ring_file_records", &blob,
                          &header_bytes, &ring_records, &head, &tail,
                          &record_type)) return NULL;
    if (!PyBytes_Check(blob)) {
        PyErr_Format(PyExc_TypeError, "ring file must be bytes, not %.200s",
                     Py_TYPE(blob)->tp_name);
        return NULL;
    }
    if (header_bytes < 0 || ring_records <= 0 ||
        ring_records > (PY_SSIZE_T_MAX - header_bytes) / WREATH_NFR_CELL_SIZE ||
        PyBytes_GET_SIZE(blob) <
            header_bytes + ring_records * WREATH_NFR_CELL_SIZE) {
        PyErr_SetString(PyExc_ValueError,
                        "ring file geometry exceeds the available bytes");
        return NULL;
    }

    int inconsistent = tail > head || head - tail > (uint64_t)ring_records;
    if (tail > head) tail = head;
    uint64_t distance = head - tail;
    Py_ssize_t span = distance < (uint64_t)ring_records
        ? (Py_ssize_t)distance : ring_records;
    uint64_t first = head - (uint64_t)span;
    RingFileCandidate *candidates = span == 0 ? NULL
        : PyMem_Malloc((size_t)span * sizeof(*candidates));
    if (span != 0 && candidates == NULL) return PyErr_NoMemory();

    const uint8_t *cells =
        (const uint8_t *)PyBytes_AS_STRING(blob) + header_bytes;
    Py_ssize_t kept = 0;
    Py_ssize_t undecodable = 0;
    for (Py_ssize_t index = 0; index < span; index++) {
        uint64_t sequence = first + (uint64_t)index;
        Py_ssize_t slot = (Py_ssize_t)(sequence % (uint64_t)ring_records);
        const uint8_t *cell = cells + slot * WREATH_NFR_CELL_SIZE;
        uint8_t kind = cell[1];
        int known = kind == WREATH_NFR_KIND_COMPLETION ||
                    kind == WREATH_NFR_KIND_CORRELATION ||
                    kind == WREATH_NFR_KIND_PHASE ||
                    kind == WREATH_NFR_KIND_LOG ||
                    kind == WREATH_NFR_KIND_CLIENT_FACTS;
        if (!known) continue;
        if (!ring_file_cell_valid(cell, kind)) {
            undecodable++;
            continue;
        }
        candidates[kept++] = (RingFileCandidate){sequence, cell, kind};
        if ((index & 4095) == 4095 && PyErr_CheckSignals() < 0) {
            PyMem_Free(candidates);
            return NULL;
        }
    }

    PyObject *records = PyTuple_New(kept);
    if (records == NULL) {
        PyMem_Free(candidates);
        return NULL;
    }
    for (Py_ssize_t index = 0; index < kept; index++) {
        PyObject *values[3] = {
            PyLong_FromUnsignedLongLong(candidates[index].sequence),
            PyLong_FromUnsignedLong(candidates[index].kind),
            PyBytes_FromStringAndSize(
                (const char *)candidates[index].cell, WREATH_NFR_CELL_SIZE),
        };
        PyObject *record = NULL;
        if (values[0] != NULL && values[1] != NULL && values[2] != NULL)
            record = PyObject_Vectorcall(record_type, values, 3, NULL);
        /* complexity: allow -- exactly three final boundary values. */
        for (int value = 0; value < 3; value++) Py_XDECREF(values[value]);
        if (record == NULL) {
            Py_DECREF(records);
            PyMem_Free(candidates);
            return NULL;
        }
        PyTuple_SET_ITEM(records, index, record);
    }
    PyMem_Free(candidates);

    PyObject *undecodable_object = PyLong_FromSsize_t(undecodable);
    PyObject *tail_object = PyLong_FromUnsignedLongLong(tail);
    PyObject *inconsistent_object = PyBool_FromLong(inconsistent);
    if (undecodable_object == NULL || tail_object == NULL ||
        inconsistent_object == NULL) {
        Py_DECREF(records);
        Py_XDECREF(undecodable_object);
        Py_XDECREF(tail_object);
        Py_XDECREF(inconsistent_object);
        return NULL;
    }
    PyObject *result = PyTuple_Pack(
        4, records, undecodable_object, tail_object, inconsistent_object);
    Py_DECREF(records);
    Py_DECREF(undecodable_object);
    Py_DECREF(tail_object);
    Py_DECREF(inconsistent_object);
    return result;
}

static uint64_t
ring_id_hash(uint64_t value)
{
    value ^= value >> 30;
    value *= UINT64_C(0xbf58476d1ce4e5b9);
    value ^= value >> 27;
    value *= UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

static int
ring_id_set_init(RingIdSet *set, Py_ssize_t count)
{
    size_t capacity = 8;
    if (count < 0 || (size_t)count > SIZE_MAX / 4) {
        PyErr_NoMemory();
        return -1;
    }
    size_t required = (size_t)count * 2;
    while (capacity < required) {
        if (capacity > SIZE_MAX / 2) {
            PyErr_NoMemory();
            return -1;
        }
        capacity *= 2;
    }
    set->slots = PyMem_Calloc(capacity, sizeof(*set->slots));
    if (set->slots == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    set->capacity = capacity;
    return 0;
}

static int
ring_id_set_contains(const RingIdSet *set, uint64_t value)
{
    size_t mask = set->capacity - 1;
    size_t index = (size_t)ring_id_hash(value) & mask;
    while (set->slots[index] != 0) {
        if (set->slots[index] == value) return 1;
        index = (index + 1) & mask;
    }
    return 0;
}

static int
ring_id_set_add(RingIdSet *set, uint64_t value)
{
    size_t mask = set->capacity - 1;
    size_t index = (size_t)ring_id_hash(value) & mask;
    while (set->slots[index] != 0) {
        if (set->slots[index] == value) return 0;
        index = (index + 1) & mask;
    }
    set->slots[index] = value;
    return 1;
}

PyObject *
wreath_ring_in_flight(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *records;
    int completion_kind, log_kind;
    if (!PyArg_ParseTuple(args, "Oii:ring_in_flight", &records,
                          &completion_kind, &log_kind)) return NULL;
    PyObject *sequence = PySequence_Fast(
        records, "ring records must be an iterable");
    if (sequence == NULL) return NULL;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
    if ((size_t)count > SIZE_MAX / sizeof(RingCellKey)) {
        Py_DECREF(sequence);
        return PyErr_NoMemory();
    }
    RingCellKey *keys = count == 0 ? NULL
        : PyMem_Malloc((size_t)count * sizeof(*keys));
    RingIdSet completed = {0}, emitted = {0};
    if ((count != 0 && keys == NULL) ||
        ring_id_set_init(&completed, count) < 0 ||
        ring_id_set_init(&emitted, count) < 0) {
        PyMem_Free(keys);
        PyMem_Free(completed.slots);
        PyMem_Free(emitted.slots);
        Py_DECREF(sequence);
        if (!PyErr_Occurred()) PyErr_NoMemory();
        return NULL;
    }
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *raw = PyObject_GetAttrString(
            PySequence_Fast_GET_ITEM(sequence, i), "raw");
        if (raw == NULL) goto error;
        if (!PyBytes_Check(raw) || PyBytes_GET_SIZE(raw) < 16) {
            Py_DECREF(raw);
            PyErr_SetString(PyExc_ValueError,
                            "ring record raw data must be at least 16 bytes");
            goto error;
        }
        const uint8_t *data = (const uint8_t *)PyBytes_AS_STRING(raw);
        keys[i] = (RingCellKey){wreath_load_u64_le(data + 8), data[1]};
        Py_DECREF(raw);
        if (keys[i].kind == (uint8_t)completion_kind &&
            keys[i].request_id != 0)
            ring_id_set_add(&completed, keys[i].request_id);
        if ((i & 4095) == 4095 && PyErr_CheckSignals() < 0) goto error;
    }
    Py_ssize_t output_count = 0;
    for (Py_ssize_t i = 0; i < count; i++) {
        uint64_t request_id = keys[i].request_id;
        if (keys[i].kind == (uint8_t)log_kind && request_id != 0 &&
            !ring_id_set_contains(&completed, request_id) &&
            ring_id_set_add(&emitted, request_id))
            keys[output_count++].request_id = request_id;
    }
    PyObject *result = PyTuple_New(output_count);
    if (result == NULL) goto error;
    for (Py_ssize_t i = 0; i < output_count; i++) {
        PyObject *request_id = PyLong_FromUnsignedLongLong(keys[i].request_id);
        if (request_id == NULL) {
            Py_DECREF(result);
            goto error;
        }
        PyTuple_SET_ITEM(result, i, request_id);
    }
    PyMem_Free(keys);
    PyMem_Free(completed.slots);
    PyMem_Free(emitted.slots);
    Py_DECREF(sequence);
    return result;
error:
    PyMem_Free(keys);
    PyMem_Free(completed.slots);
    PyMem_Free(emitted.slots);
    Py_DECREF(sequence);
    return NULL;
}

PyObject *
wreath_ring_logs_for(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *records, *request_object;
    int log_kind;
    if (!PyArg_ParseTuple(args, "OOi:ring_logs_for", &records,
                          &request_object, &log_kind)) return NULL;
    uint64_t wanted = PyLong_AsUnsignedLongLong(request_object);
    if (wanted == UINT64_MAX && PyErr_Occurred()) {
        if (!PyErr_ExceptionMatches(PyExc_OverflowError)) return NULL;
        PyErr_Clear();
        return PyTuple_New(0);
    }
    PyObject *sequence = PySequence_Fast(
        records, "ring records must be an iterable");
    if (sequence == NULL) return NULL;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
    if ((size_t)count > SIZE_MAX / sizeof(PyObject *)) {
        Py_DECREF(sequence);
        return PyErr_NoMemory();
    }
    PyObject **matches = count == 0 ? NULL
        : PyMem_Malloc((size_t)count * sizeof(*matches));
    if (count != 0 && matches == NULL) {
        Py_DECREF(sequence);
        return PyErr_NoMemory();
    }
    Py_ssize_t match_count = 0;
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *record = PySequence_Fast_GET_ITEM(sequence, i);
        PyObject *raw = PyObject_GetAttrString(record, "raw");
        if (raw == NULL) goto error;
        if (!PyBytes_Check(raw) || PyBytes_GET_SIZE(raw) < 16) {
            Py_DECREF(raw);
            PyErr_SetString(PyExc_ValueError,
                            "ring record raw data must be at least 16 bytes");
            goto error;
        }
        const uint8_t *data = (const uint8_t *)PyBytes_AS_STRING(raw);
        if (data[1] == (uint8_t)log_kind &&
            wreath_load_u64_le(data + 8) == wanted)
            matches[match_count++] = record;
        Py_DECREF(raw);
        if ((i & 4095) == 4095 && PyErr_CheckSignals() < 0) goto error;
    }
    PyObject *result = PyTuple_New(match_count);
    if (result == NULL) goto error;
    for (Py_ssize_t i = 0; i < match_count; i++)
        PyTuple_SET_ITEM(result, i, Py_NewRef(matches[i]));
    PyMem_Free(matches);
    Py_DECREF(sequence);
    return result;
error:
    PyMem_Free(matches);
    Py_DECREF(sequence);
    return NULL;
}


PyObject *
wreath_recording_event_cells(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *payload;
    PyObject *error_type;
    Py_ssize_t cell_size;
    int version;
    Py_ssize_t length;
    Py_ssize_t count;
    const char *source;
    PyObject *result;

    if (!PyArg_ParseTuple(args, "OniO:recording_event_cells",
                          &payload, &cell_size, &version, &error_type)) {
        return NULL;
    }
    if (!PyBytes_Check(payload)) {
        PyErr_Format(PyExc_TypeError,
                     "recording event payload must be bytes, not %.200s",
                     Py_TYPE(payload)->tp_name);
        return NULL;
    }
    if (cell_size <= 0) {
        PyErr_Format(PyExc_ValueError,
                     "recording event cell size must be positive, not %zd",
                     cell_size);
        return NULL;
    }
    if (version < 0 || version > UINT8_MAX) {
        PyErr_Format(PyExc_ValueError,
                     "recording event schema version must fit in one byte, not %d",
                     version);
        return NULL;
    }
    if (!PyExceptionClass_Check(error_type)) {
        PyErr_SetString(PyExc_TypeError,
                        "recording event error_type must be an exception class");
        return NULL;
    }

    length = PyBytes_GET_SIZE(payload);
    if (length % cell_size != 0) {
        Py_RETURN_NONE;
    }
    count = length / cell_size;
    result = PyTuple_New(count);
    if (result == NULL) {
        return NULL;
    }
    source = PyBytes_AS_STRING(payload);
    for (Py_ssize_t index = 0; index < count; index++) {
        const char *cell = source + index * cell_size;
        PyObject *item;
        if ((uint8_t)cell[0] != (uint8_t)version) {
            Py_DECREF(result);
            PyErr_SetString(error_type,
                            "event cell has an unsupported schema version");
            return NULL;
        }
        item = PyBytes_FromStringAndSize(cell, cell_size);
        if (item == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyTuple_SET_ITEM(result, index, item);
    }
    return result;
}
