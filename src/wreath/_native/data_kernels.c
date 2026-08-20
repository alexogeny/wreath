/* Total data kernels for collection and wire-format walks. */
#include "wreathcore.h"
#include "flight_schema.h"
#include "simd.h"

#include <math.h>
#include <stdint.h>

typedef enum {
    DATA_ATTR_TARGET,
    DATA_ATTR_KIND,
    DATA_ATTR_TYPE,
    DATA_ATTR_PAYLOAD,
    DATA_ATTR_FRACTION,
    DATA_ATTR_NUMBER,
    DATA_ATTR_SEAM,
    DATA_ATTR_COORDINATE,
    DATA_ATTR_ERROR_TYPE,
    DATA_ATTR_SUBSYSTEM,
    DATA_ATTR_VALUES,
    DATA_ATTR_COUNT,
} DataAttr;

static PyObject *data_attr_names[DATA_ATTR_COUNT];

static int
data_attrs_ready(void)
{
    static const char *names[DATA_ATTR_COUNT] = {
        "target", "kind", "type", "payload", "fraction", "number", "seam",
        "coordinate", "error_type", "subsystem", "values",
    };
    for (int index = 0; index < DATA_ATTR_COUNT; index++) {
        data_attr_names[index] = PyUnicode_InternFromString(names[index]);
        if (data_attr_names[index] == NULL) {
            while (index-- != 0) Py_CLEAR(data_attr_names[index]);
            return -1;
        }
    }
    return 0;
}

static inline PyObject *
data_getattr(PyObject *object, DataAttr attribute)
{
    return PyObject_GetAttr(object, data_attr_names[attribute]);
}

typedef struct {
    Py_hash_t hash;
    PyObject *key;
    double score;
    unsigned char used;
} FusionSlot;

typedef struct {
    PyObject *key;
    double score;
} FusionRank;

typedef struct {
    double score;
    Py_ssize_t index;
} NumericRank;

PyObject *
wreath_first_duplicate(PyObject *Py_UNUSED(self), PyObject *source)
{
    PyObject *items = PySequence_Fast(source, "duplicate candidates must be a sequence");
    PyObject *seen = NULL;
    PyObject *result = NULL;
    if (items == NULL) return NULL;
    seen = PySet_New(NULL);
    if (seen == NULL) goto done;
    PyObject **values = PySequence_Fast_ITEMS(items);
    Py_ssize_t count = PySequence_Fast_GET_SIZE(items);
    for (Py_ssize_t index = 0; index < count; index++) {
        int contained = PySet_Contains(seen, values[index]);
        if (contained < 0) goto done;
        if (contained) {
            result = Py_NewRef(values[index]);
            goto done;
        }
        if (PySet_Add(seen, values[index]) < 0) goto done;
    }
    result = Py_NewRef(Py_None);

done:
    Py_XDECREF(seen);
    Py_DECREF(items);
    return result;
}

static int
data_unicode_before(PyObject *left, PyObject *right)
{
    return PyObject_RichCompareBool(left, right, Py_LT);
}

static int
data_unicode_sort(PyObject **items, PyObject **scratch, Py_ssize_t count)
{
    PyObject **source = items, **target = scratch;
    for (Py_ssize_t width = 1; width < count;) {
        for (Py_ssize_t start = 0; start < count; start += width * 2) {
            Py_ssize_t middle = start + width < count ? start + width : count;
            Py_ssize_t end = middle + width < count ? middle + width : count;
            Py_ssize_t left = start, right = middle, out = start;
            while (left < middle && right < end) {
                int before = data_unicode_before(source[right], source[left]);
                if (before < 0) return -1;
                target[out++] = before ? source[right++] : source[left++];
            }
            while (left < middle) target[out++] = source[left++];
            while (right < end) target[out++] = source[right++];
        }
        PyObject **swap = source;
        source = target;
        target = swap;
        if (width > count / 2) break;
        width *= 2;
    }
    if (source != items) memcpy(items, source, (size_t)count * sizeof(*items));
    return 0;
}

static int
data_prefix_kept(PyObject *candidate, PyObject *kept)
{
    if (kept == NULL) return 1;
    int covered = PyUnicode_Tailmatch(
        candidate, kept, 0, PyUnicode_GET_LENGTH(candidate), -1);
    return covered < 0 ? -1 : !covered;
}

PyObject *
wreath_minimal_prefixes(PyObject *Py_UNUSED(self), PyObject *source)
{
    PyObject *sequence = PySequence_Tuple(source);
    PyObject *result = NULL;
    PyObject **items = NULL, **scratch = NULL;
    if (sequence == NULL) return NULL;
    Py_ssize_t count = PyTuple_GET_SIZE(sequence);
    if ((size_t)count > SIZE_MAX / sizeof(*items)) {
        PyErr_NoMemory();
        goto done;
    }
    items = PyMem_Malloc((size_t)(count > 0 ? count : 1) * sizeof(*items));
    scratch = PyMem_Malloc((size_t)(count > 0 ? count : 1) * sizeof(*scratch));
    if (items == NULL || scratch == NULL) {
        PyErr_NoMemory();
        goto done;
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *item = PyTuple_GET_ITEM(sequence, index);
        if (!PyUnicode_Check(item)) {
            PyErr_Format(PyExc_TypeError,
                         "prefix %zd must be str, got %.200s",
                         index, Py_TYPE(item)->tp_name);
            goto done;
        }
        items[index] = item;
    }
    if (data_unicode_sort(items, scratch, count) < 0) goto done;

    Py_ssize_t selected = 0;
    PyObject *kept = NULL;
    for (Py_ssize_t index = 0; index < count; index++) {
        int retain = data_prefix_kept(items[index], kept);
        if (retain < 0) goto done;
        if (retain) {
            kept = items[index];
            scratch[selected++] = kept;
        }
    }
    result = PyTuple_New(selected);
    if (result == NULL) goto done;
    for (Py_ssize_t index = 0; index < selected; index++) {
        PyTuple_SET_ITEM(result, index, Py_NewRef(scratch[index]));
    }

done:
    PyMem_Free(items);
    PyMem_Free(scratch);
    Py_DECREF(sequence);
    return result;
}

static int
numeric_rank_ascending(const void *left_pointer, const void *right_pointer)
{
    const NumericRank *left = left_pointer;
    const NumericRank *right = right_pointer;
    if (left->score < right->score) return -1;
    if (left->score > right->score) return 1;
    return left->index < right->index ? -1 : left->index != right->index;
}

static int
numeric_rank_descending(const void *left_pointer, const void *right_pointer)
{
    return numeric_rank_ascending(right_pointer, left_pointer);
}

PyObject *
wreath_rank_indices(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *scores_source;
    Py_ssize_t offset, limit;
    int descending;
    if (!PyArg_ParseTuple(args, "Onnp:rank_indices", &scores_source,
                          &offset, &limit, &descending)) return NULL;
    if (offset < 0 || limit < 0) {
        PyErr_SetString(PyExc_ValueError, "rank offset and limit must be non-negative");
        return NULL;
    }
    PyObject *scores = PySequence_Fast(scores_source, "scores must be a sequence");
    if (scores == NULL) return NULL;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(scores);
    if ((size_t)count > SIZE_MAX / sizeof(NumericRank)) {
        Py_DECREF(scores);
        return PyErr_NoMemory();
    }
    NumericRank *ranks = count != 0
        ? PyMem_Malloc((size_t)count * sizeof(*ranks)) : NULL;
    if (count != 0 && ranks == NULL) {
        Py_DECREF(scores);
        return PyErr_NoMemory();
    }
    PyObject **items = PySequence_Fast_ITEMS(scores);
    for (Py_ssize_t index = 0; index < count; index++) {
        if (PyBool_Check(items[index]) ||
            (!PyFloat_Check(items[index]) && !PyLong_Check(items[index]))) {
            PyErr_Format(PyExc_TypeError,
                         "rank score %zd must be int or float, got %.200s",
                         index, Py_TYPE(items[index])->tp_name);
            goto rank_error;
        }
        double score = PyFloat_AsDouble(items[index]);
        if ((score == -1.0 && PyErr_Occurred()) || !isfinite(score)) {
            if (!PyErr_Occurred())
                PyErr_Format(PyExc_ValueError, "rank score %zd must be finite", index);
            goto rank_error;
        }
        ranks[index] = (NumericRank){score, index};
    }
    qsort(ranks, (size_t)count, sizeof(*ranks), descending
          ? numeric_rank_descending : numeric_rank_ascending);
    Py_ssize_t start = offset < count ? offset : count;
    Py_ssize_t available = count - start;
    Py_ssize_t output_count = limit < available ? limit : available;
    PyObject *result = PyTuple_New(output_count);
    if (result == NULL) goto rank_error;
    for (Py_ssize_t index = 0; index < output_count; index++) {
        PyObject *position = PyLong_FromSsize_t(ranks[start + index].index);
        if (position == NULL) {
            Py_DECREF(result);
            goto rank_error;
        }
        PyTuple_SET_ITEM(result, index, position);
    }
    PyMem_Free(ranks);
    Py_DECREF(scores);
    return result;

rank_error:
    PyMem_Free(ranks);
    Py_DECREF(scores);
    return NULL;
}

static void
fusion_slots_clear(FusionSlot *slots, size_t capacity)
{
    if (slots == NULL) return;
    for (size_t index = 0; index < capacity; index++)
        Py_XDECREF(slots[index].key);
    PyMem_Free(slots);
}

static int
fusion_before(const FusionRank *left, const FusionRank *right)
{
    if (left->score != right->score) return left->score > right->score;
    return PyObject_RichCompareBool(left->key, right->key, Py_LT);
}

static int
fusion_sort(FusionRank *items, FusionRank *scratch, size_t count)
{
    for (size_t width = 1; width < count;) {
        for (size_t start = 0; start < count; start += width * 2) {
            size_t middle = start + width < count ? start + width : count;
            size_t end = middle + width < count ? middle + width : count;
            size_t left = start, right = middle, out = start;
            while (left < middle && right < end) {
                int before = fusion_before(&items[right], &items[left]);
                if (before < 0) return -1;
                scratch[out++] = before ? items[right++] : items[left++];
            }
            while (left < middle) scratch[out++] = items[left++];
            while (right < end) scratch[out++] = items[right++];
        }
        memcpy(items, scratch, count * sizeof(FusionRank));
        if (width > count / 2) break;
        width *= 2;
    }
    return 0;
}

PyObject *
wreath_fused_order(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *rankings_obj;
    long k;
    if (!PyArg_ParseTuple(args, "Ol:fused_order", &rankings_obj, &k)) return NULL;
    PyObject *rankings = PySequence_Fast(rankings_obj, "rankings must be a sequence");
    if (rankings == NULL) return NULL;
    PyObject **ranking_items = PySequence_Fast_ITEMS(rankings);
    Py_ssize_t ranking_count = PySequence_Fast_GET_SIZE(rankings);
    size_t cells = 0;
    for (Py_ssize_t arm = 0; arm < ranking_count; arm++) {
        PyObject *ranking = PySequence_Fast(
            ranking_items[arm], "each ranking must be a sequence");
        if (ranking == NULL) goto error_rankings;
        Py_ssize_t count = PySequence_Fast_GET_SIZE(ranking);
        Py_DECREF(ranking);
        if (count < 0 || (size_t)count > SIZE_MAX - cells) {
            PyErr_NoMemory();
            goto error_rankings;
        }
        cells += (size_t)count;
    }
    size_t capacity = 8;
    while (capacity < cells && capacity <= SIZE_MAX / 2) capacity *= 2;
    if (capacity < cells || capacity > SIZE_MAX / 2 ||
        capacity * 2 > SIZE_MAX / sizeof(FusionSlot)) {
        PyErr_NoMemory();
        goto error_rankings;
    }
    capacity *= 2;
    FusionSlot *slots = PyMem_Calloc(capacity, sizeof(FusionSlot));
    if (slots == NULL) {
        PyErr_NoMemory();
        goto error_rankings;
    }
    size_t used = 0;
    for (Py_ssize_t arm = 0; arm < ranking_count; arm++) {
        PyObject *ranking = PySequence_Fast(
            ranking_items[arm], "each ranking must be a sequence");
        if (ranking == NULL) goto error;
        PyObject **keys = PySequence_Fast_ITEMS(ranking);
        Py_ssize_t count = PySequence_Fast_GET_SIZE(ranking);
        for (Py_ssize_t index = 0; index < count; index++) {
            Py_hash_t hash = PyObject_Hash(keys[index]);
            if (hash == -1 && PyErr_Occurred()) {
                Py_DECREF(ranking); goto error;
            }
            size_t slot_index = (size_t)hash & (capacity - 1);
            for (;;) {
                FusionSlot *slot = &slots[slot_index];
                if (!slot->used) {
                    slot->used = 1;
                    slot->hash = hash;
                    slot->key = Py_NewRef(keys[index]);
                    slot->score = 1.0 / ((double)k + (double)index + 1.0);
                    used++;
                    break;
                }
                if (slot->hash == hash) {
                    int equal = slot->key == keys[index] ? 1
                        : PyObject_RichCompareBool(slot->key, keys[index], Py_EQ);
                    if (equal < 0) { Py_DECREF(ranking); goto error; }
                    if (equal) {
                        slot->score += 1.0 / ((double)k + (double)index + 1.0);
                        break;
                    }
                }
                slot_index = (slot_index + 1) & (capacity - 1);
            }
        }
        Py_DECREF(ranking);
    }
    FusionRank *ordered = PyMem_Malloc((used > 0 ? used : 1) * sizeof(FusionRank));
    FusionRank *scratch = PyMem_Malloc((used > 0 ? used : 1) * sizeof(FusionRank));
    if (ordered == NULL || scratch == NULL) {
        PyMem_Free(ordered); PyMem_Free(scratch); PyErr_NoMemory(); goto error;
    }
    size_t position = 0;
    for (size_t index = 0; index < capacity; index++) {
        if (slots[index].used)
            ordered[position++] = (FusionRank){slots[index].key, slots[index].score};
    }
    if (fusion_sort(ordered, scratch, used) < 0) {
        PyMem_Free(ordered); PyMem_Free(scratch); goto error;
    }
    PyObject *result = PyList_New((Py_ssize_t)used);
    if (result == NULL) {
        PyMem_Free(ordered); PyMem_Free(scratch); goto error;
    }
    for (size_t index = 0; index < used; index++)
        PyList_SET_ITEM(result, (Py_ssize_t)index, Py_NewRef(ordered[index].key));
    PyMem_Free(ordered); PyMem_Free(scratch);
    fusion_slots_clear(slots, capacity);
    Py_DECREF(rankings);
    return result;
error:
    fusion_slots_clear(slots, capacity);
error_rankings:
    Py_DECREF(rankings);
    return NULL;
}


typedef struct {
    PyObject *value;
    unsigned char state;
} ArgumentActiveSlot;

typedef struct {
    Py_ssize_t remaining;
    Py_ssize_t max_depth;
    ArgumentActiveSlot *active;
    size_t active_capacity;
    PyObject *reason;
    WreathBytesWriter writer;
} ArgumentWriter;

static size_t
argument_active_index(PyObject *value, size_t capacity)
{
    uintptr_t pointer = (uintptr_t)value;
    pointer >>= 4;
    pointer *= UINT64_C(11400714819323198485);
    return (size_t)pointer & (capacity - 1);
}

static int
argument_active_add(ArgumentWriter *state, PyObject *value)
{
    size_t slot = argument_active_index(value, state->active_capacity);
    size_t deleted = SIZE_MAX;
    for (size_t scanned = 0; scanned < state->active_capacity; scanned++) {
        if (state->active[slot].state == 0) break;
        if (state->active[slot].state == 1 && state->active[slot].value == value)
            return 1;
        if (state->active[slot].state == 2 && deleted == SIZE_MAX) deleted = slot;
        slot = (slot + 1) & (state->active_capacity - 1);
    }
    if (deleted != SIZE_MAX) slot = deleted;
    state->active[slot].value = value;
    state->active[slot].state = 1;
    return 0;
}

static void
argument_active_remove(ArgumentWriter *state, PyObject *value)
{
    size_t slot = argument_active_index(value, state->active_capacity);
    for (size_t scanned = 0; scanned < state->active_capacity; scanned++) {
        if (state->active[slot].state == 0) return;
        if (state->active[slot].state == 1 && state->active[slot].value == value) {
            state->active[slot].value = NULL;
            state->active[slot].state = 2;
            return;
        }
        slot = (slot + 1) & (state->active_capacity - 1);
    }
}

static int
argument_refuse(ArgumentWriter *state, const char *message)
{
    state->reason = PyUnicode_FromString(message);
    return state->reason == NULL ? -1 : 1;
}

static int
argument_write_value(ArgumentWriter *state, PyObject *value, Py_ssize_t depth)
{
    if (value == Py_None || PyUnicode_Check(value) || PyBool_Check(value) ||
        PyLong_Check(value))
        return wreath_json_write_value(&state->writer, value, (int)depth);
    if (PyFloat_Check(value)) {
        double number = PyFloat_AsDouble(value);
        if (number == -1.0 && PyErr_Occurred()) return -1;
        if (!isfinite(number))
            return argument_refuse(state, "a non-finite number has no JSON form");
        return wreath_json_write_value(&state->writer, value, (int)depth);
    }
    if (depth >= state->max_depth) {
        state->reason = PyUnicode_FromFormat(
            "nested deeper than the %zd-level limit", state->max_depth);
        return state->reason == NULL ? -1 : 1;
    }
    int container = PyList_Check(value) || PyTuple_Check(value) || PyDict_Check(value);
    if (!container) {
        state->reason = PyUnicode_FromFormat(
            "unsupported type %s", Py_TYPE(value)->tp_name);
        return state->reason == NULL ? -1 : 1;
    }
    if (argument_active_add(state, value))
        return argument_refuse(state, "contains a cycle");
    if (PyList_Check(value) || PyTuple_Check(value)) {
        PyObject *sequence = PySequence_Fast(value, "expected list or tuple");
        if (sequence == NULL) {
            argument_active_remove(state, value);
            return -1;
        }
        Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
        int result = wreath_writer_byte(&state->writer, '[');
        PyObject **items = PySequence_Fast_ITEMS(sequence);
        for (Py_ssize_t index = 0; result == 0 && index < count; index++) {
            if (--state->remaining < 0) {
                result = argument_refuse(
                    state, "more fields than the policy's max_fields allows");
                break;
            }
            if (index && wreath_writer_byte(&state->writer, ',') < 0) {
                result = -1;
                break;
            }
            result = argument_write_value(state, items[index], depth + 1);
        }
        if (result == 0) result = wreath_writer_byte(&state->writer, ']');
        Py_DECREF(sequence);
        argument_active_remove(state, value);
        return result;
    }
    int result = wreath_writer_byte(&state->writer, '{');
    Py_ssize_t cursor = 0;
    PyObject *key, *item;
    int first = 1;
    while (result == 0 && PyDict_Next(value, &cursor, &key, &item)) {
        if (!PyUnicode_Check(key)) {
            state->reason = PyUnicode_FromFormat(
                "a mapping keyed by %s has no JSON form", Py_TYPE(key)->tp_name);
            result = state->reason == NULL ? -1 : 1;
            break;
        }
        if (--state->remaining < 0) {
            result = argument_refuse(
                state, "more fields than the policy's max_fields allows");
            break;
        }
        if ((!first && wreath_writer_byte(&state->writer, ',') < 0) ||
            wreath_json_write_string(&state->writer, key) < 0 ||
            wreath_writer_byte(&state->writer, ':') < 0) {
            result = -1;
            break;
        }
        first = 0;
        result = argument_write_value(state, item, depth + 1);
    }
    if (result == 0) result = wreath_writer_byte(&state->writer, '}');
    argument_active_remove(state, value);
    return result;
}

static PyObject *
argument_finish(WreathBytesWriter *writer)
{
    PyObject *bytes = wreath_writer_finish(writer);
    if (bytes == NULL) return NULL;
    PyObject *result = PyUnicode_DecodeUTF8(
        PyBytes_AS_STRING(bytes), PyBytes_GET_SIZE(bytes), "strict");
    Py_DECREF(bytes);
    return result;
}

PyObject *
wreath_normalise_argument(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *value;
    Py_ssize_t max_fields, max_depth, max_bytes;
    if (!PyArg_ParseTuple(args, "Onnn:normalise_argument", &value, &max_fields,
                          &max_depth, &max_bytes)) return NULL;
    size_t active_capacity = 8;
    size_t active_needed = max_depth > 0 ? (size_t)max_depth : 1;
    if (active_needed > SIZE_MAX / 2) {
        PyErr_NoMemory();
        return NULL;
    }
    while (active_capacity < active_needed * 2) {
        if (active_capacity > SIZE_MAX / 2) {
            PyErr_NoMemory();
            return NULL;
        }
        active_capacity *= 2;
    }
    ArgumentActiveSlot *active = PyMem_Calloc(active_capacity, sizeof(*active));
    if (active == NULL) return PyErr_NoMemory();
    ArgumentWriter state = {
        max_fields, max_depth, active, active_capacity, NULL,
        {NULL, NULL, 0, 0},
    };
    if (wreath_writer_init(&state.writer, 256) < 0) {
        PyMem_Free(active);
        return NULL;
    }
    int result = wreath_writer_write(&state.writer, "{\"value\":", 9);
    if (result == 0) result = argument_write_value(&state, value, 0);
    if (result == 0) result = wreath_writer_byte(&state.writer, '}');
    PyMem_Free(active);
    if (result < 0) {
        Py_XDECREF(state.reason);
        Py_XDECREF(state.writer.bytes);
        return NULL;
    }
    if (result == 0 && state.writer.len > max_bytes) {
        state.reason = PyUnicode_FromFormat("over the %zd-byte argument budget", max_bytes);
        if (state.reason == NULL) { Py_DECREF(state.writer.bytes); return NULL; }
        result = 1;
    }
    if (result == 0) return argument_finish(&state.writer);
    Py_DECREF(state.writer.bytes);
    if (wreath_writer_init(&state.writer, 96) < 0) { Py_DECREF(state.reason); return NULL; }
    int failed = wreath_writer_write(&state.writer, "{\"withheld\":", 12) < 0 ||
        wreath_json_write_string(&state.writer, state.reason) < 0 ||
        wreath_writer_byte(&state.writer, '}') < 0;
    Py_DECREF(state.reason);
    if (failed) { Py_XDECREF(state.writer.bytes); return NULL; }
    return argument_finish(&state.writer);
}


static int
object_unsigned(PyObject *owner, const char *name, uint64_t maximum,
                uint64_t *out)
{
    PyObject *value = PyObject_GetAttrString(owner, name);
    if (value == NULL) return -1;
    unsigned long long number = PyLong_AsUnsignedLongLong(value);
    Py_DECREF(value);
    if (number == (unsigned long long)-1 && PyErr_Occurred()) return -1;
    if (number > maximum) {
        PyErr_Format(PyExc_OverflowError, "%s is outside its wire range", name);
        return -1;
    }
    *out = (uint64_t)number;
    return 0;
}

static int
object_signed(PyObject *owner, const char *name, int64_t minimum,
              int64_t maximum, int64_t *out)
{
    PyObject *value = PyObject_GetAttrString(owner, name);
    if (value == NULL) return -1;
    long long number = PyLong_AsLongLong(value);
    Py_DECREF(value);
    if (number == -1 && PyErr_Occurred()) return -1;
    if (number < minimum || number > maximum) {
        PyErr_Format(PyExc_OverflowError, "%s is outside its wire range", name);
        return -1;
    }
    *out = (int64_t)number;
    return 0;
}

static PyObject *
decode_address(const uint8_t *data, Py_ssize_t length, Py_ssize_t *offset,
               PyObject *error_type)
{
    if (*offset > length - 4) {
        PyErr_SetString(error_type, "transport address is truncated");
        return NULL;
    }
    uint16_t host_length = wreath_load_u16_le(data + *offset);
    uint16_t port = wreath_load_u16_le(data + *offset + 2);
    *offset += 4;
    if (*offset > length - host_length) {
        PyErr_SetString(error_type, "transport address host is truncated");
        return NULL;
    }
    PyObject *host = PyUnicode_DecodeUTF8((const char *)data + *offset,
                                          host_length, "strict");
    *offset += host_length;
    PyObject *port_obj = host == NULL ? NULL : PyLong_FromUnsignedLong(port);
    return wreath_tuple2_from_owned(host, port_obj);
}

PyObject *
wreath_transport_decode_parts(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *head, *body, *segment_type, *error_type;
    if (!PyArg_ParseTuple(args, "O!O!OO:transport_decode_parts", &PyBytes_Type,
                          &head, &PyBytes_Type, &body, &segment_type,
                          &error_type)) return NULL;
    Py_ssize_t head_length = PyBytes_GET_SIZE(head);
    if (head_length < 16) {
        PyErr_SetString(error_type, "transport recording HEAD chunk is truncated");
        return NULL;
    }
    const uint8_t *head_data = (const uint8_t *)PyBytes_AS_STRING(head);
    uint64_t build_id = wreath_load_u64_le(head_data);
    Py_ssize_t head_offset = 16;
    PyObject *peer = decode_address(head_data, head_length, &head_offset, error_type);
    PyObject *sock = peer == NULL ? NULL
        : decode_address(head_data, head_length, &head_offset, error_type);
    if (sock == NULL) {
        Py_XDECREF(peer);
        return NULL;
    }
    Py_ssize_t body_length = PyBytes_GET_SIZE(body);
    if (body_length < 4) {
        PyErr_SetString(error_type, "transport recording SEGS chunk is truncated");
        goto transport_decode_error;
    }
    const uint8_t *body_data = (const uint8_t *)PyBytes_AS_STRING(body);
    uint32_t count = wreath_load_u32_le(body_data);
    if ((uint64_t)count > (uint64_t)(body_length - 4) / 13U) {
        PyErr_SetString(error_type, "transport recording segment count exceeds its data");
        goto transport_decode_error;
    }
    PyObject *segments = PyTuple_New((Py_ssize_t)count);
    if (segments == NULL) goto transport_decode_error;
    Py_ssize_t offset = 4;
    for (uint32_t index = 0; index < count; index++) {
        if (offset > body_length - 13) {
            PyErr_SetString(error_type, "transport segment header is truncated");
            Py_DECREF(segments);
            goto transport_decode_error;
        }
        uint64_t arrival = wreath_load_u64_le(body_data + offset);
        uint8_t kind = body_data[offset + 8];
        uint32_t data_length = wreath_load_u32_le(body_data + offset + 9);
        offset += 13;
        if ((uint64_t)data_length > (uint64_t)(body_length - offset)) {
            PyErr_SetString(error_type, "transport segment payload is truncated");
            Py_DECREF(segments);
            goto transport_decode_error;
        }
        PyObject *arrival_obj = PyLong_FromUnsignedLongLong(arrival);
        PyObject *kind_obj = PyLong_FromUnsignedLong(kind);
        PyObject *payload = PyBytes_FromStringAndSize(
            (const char *)body_data + offset, data_length);
        PyObject *segment = arrival_obj != NULL && kind_obj != NULL && payload != NULL
            ? PyObject_CallFunctionObjArgs(segment_type, arrival_obj, kind_obj,
                                           payload, NULL) : NULL;
        Py_XDECREF(arrival_obj); Py_XDECREF(kind_obj); Py_XDECREF(payload);
        if (segment == NULL) {
            Py_DECREF(segments);
            goto transport_decode_error;
        }
        PyTuple_SET_ITEM(segments, index, segment);
        offset += data_length;
    }
    PyObject *build = PyLong_FromUnsignedLongLong(build_id);
    PyObject *result = build == NULL ? NULL
        : PyTuple_Pack(4, segments, peer, sock, build);
    Py_XDECREF(build); Py_DECREF(segments); Py_DECREF(peer); Py_DECREF(sock);
    return result;
transport_decode_error:
    Py_DECREF(peer); Py_DECREF(sock);
    return NULL;
}


typedef struct {
    int64_t seam;
    int64_t coordinate;
    PyObject *target_obj;
    PyObject *kind_obj;
    const char *target;
    Py_ssize_t target_length;
    const char *kind;
    Py_ssize_t kind_length;
} AdapterWire;

PyObject *
wreath_fault_encode_parts(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *faults_obj, *adapters_obj;
    if (!PyArg_ParseTuple(args, "OO:fault_encode_parts", &faults_obj,
                          &adapters_obj)) return NULL;
    PyObject *faults = PySequence_Fast(faults_obj, "faults must be a sequence");
    PyObject *adapters = faults == NULL ? NULL
        : PySequence_Fast(adapters_obj, "adapter faults must be a sequence");
    if (adapters == NULL) {
        Py_XDECREF(faults);
        return NULL;
    }
    Py_ssize_t fault_count = PySequence_Fast_GET_SIZE(faults);
    if ((uint64_t)fault_count > UINT32_MAX) {
        PyErr_SetString(PyExc_OverflowError, "too many transport faults");
        goto fault_encode_error;
    }
    PyObject *body = PyBytes_FromStringAndSize(NULL, 4 + fault_count * 9);
    if (body == NULL) goto fault_encode_error;
    uint8_t *cursor = (uint8_t *)PyBytes_AS_STRING(body);
    wreath_store_u32_le(cursor, (uint32_t)fault_count);
    cursor += 4;
    PyObject **fault_items = PySequence_Fast_ITEMS(faults);
    for (Py_ssize_t index = 0; index < fault_count; index++) {
        uint64_t kind, value;
        int64_t segment;
        if (object_unsigned(fault_items[index], "kind", UINT8_MAX, &kind) < 0 ||
            object_signed(fault_items[index], "segment_index", INT32_MIN,
                          INT32_MAX, &segment) < 0 ||
            object_unsigned(fault_items[index], "value", UINT32_MAX, &value) < 0) {
            Py_DECREF(body);
            goto fault_encode_error;
        }
        cursor[0] = (uint8_t)kind;
        wreath_store_u32_le(cursor + 1, (uint32_t)(int32_t)segment);
        wreath_store_u32_le(cursor + 5, (uint32_t)value);
        cursor += 9;
    }
    Py_ssize_t adapter_count = PySequence_Fast_GET_SIZE(adapters);
    if ((uint64_t)adapter_count > UINT32_MAX) {
        Py_DECREF(body);
        PyErr_SetString(PyExc_OverflowError, "too many adapter faults");
        goto fault_encode_error;
    }
    PyObject *adapt = Py_None;
    Py_INCREF(adapt);
    if (adapter_count > 0) {
        AdapterWire *wire = PyMem_Calloc((size_t)adapter_count, sizeof(AdapterWire));
        if (wire == NULL) {
            Py_DECREF(body); Py_DECREF(adapt);
            PyErr_NoMemory();
            goto fault_encode_error;
        }
        Py_ssize_t total = 4;
        PyObject **adapter_items = PySequence_Fast_ITEMS(adapters);
        for (Py_ssize_t index = 0; index < adapter_count; index++) {
            PyObject *target = data_getattr(adapter_items[index], DATA_ATTR_TARGET);
            PyObject *kind = target == NULL ? NULL
                : data_getattr(adapter_items[index], DATA_ATTR_KIND);
            if (kind == NULL ||
                object_signed(adapter_items[index], "seam", 0, UINT8_MAX,
                              &wire[index].seam) < 0 ||
                object_signed(adapter_items[index], "coordinate", INT32_MIN,
                              INT32_MAX, &wire[index].coordinate) < 0) {
                Py_XDECREF(target); Py_XDECREF(kind);
                for (Py_ssize_t prior = 0; prior < index; prior++) {
                    Py_DECREF(wire[prior].target_obj);
                    Py_DECREF(wire[prior].kind_obj);
                }
                PyMem_Free(wire); Py_DECREF(body); Py_DECREF(adapt);
                goto fault_encode_error;
            }
            wire[index].target_obj = target;
            wire[index].kind_obj = kind;
            wire[index].target = PyUnicode_AsUTF8AndSize(
                target, &wire[index].target_length);
            wire[index].kind = wire[index].target == NULL ? NULL
                : PyUnicode_AsUTF8AndSize(kind, &wire[index].kind_length);
            if (wire[index].kind == NULL || wire[index].target_length > UINT16_MAX ||
                wire[index].kind_length > UINT16_MAX ||
                total > PY_SSIZE_T_MAX - 9 - wire[index].target_length -
                            wire[index].kind_length) {
                if (!PyErr_Occurred())
                    PyErr_SetString(PyExc_OverflowError,
                                    "adapter fault string exceeds its wire range");
                for (Py_ssize_t prior = 0; prior <= index; prior++) {
                    Py_DECREF(wire[prior].target_obj);
                    Py_DECREF(wire[prior].kind_obj);
                }
                PyMem_Free(wire); Py_DECREF(body); Py_DECREF(adapt);
                goto fault_encode_error;
            }
            total += 9 + wire[index].target_length + wire[index].kind_length;
        }
        Py_SETREF(adapt, PyBytes_FromStringAndSize(NULL, total));
        if (adapt == NULL) {
            for (Py_ssize_t index = 0; index < adapter_count; index++) {
                Py_DECREF(wire[index].target_obj);
                Py_DECREF(wire[index].kind_obj);
            }
            PyMem_Free(wire); Py_DECREF(body);
            goto fault_encode_error;
        }
        cursor = (uint8_t *)PyBytes_AS_STRING(adapt);
        wreath_store_u32_le(cursor, (uint32_t)adapter_count);
        cursor += 4;
        for (Py_ssize_t index = 0; index < adapter_count; index++) {
            cursor[0] = (uint8_t)wire[index].seam;
            wreath_store_u32_le(cursor + 1, (uint32_t)(int32_t)wire[index].coordinate);
            wreath_store_u16_le(cursor + 5, (uint16_t)wire[index].target_length);
            memcpy(cursor + 7, wire[index].target, (size_t)wire[index].target_length);
            cursor += 7 + wire[index].target_length;
            wreath_store_u16_le(cursor, (uint16_t)wire[index].kind_length);
            memcpy(cursor + 2, wire[index].kind, (size_t)wire[index].kind_length);
            cursor += 2 + wire[index].kind_length;
        }
        for (Py_ssize_t index = 0; index < adapter_count; index++) {
            Py_DECREF(wire[index].target_obj);
            Py_DECREF(wire[index].kind_obj);
        }
        PyMem_Free(wire);
    }
    PyObject *result = wreath_tuple2_from_owned(body, adapt);
    Py_DECREF(faults); Py_DECREF(adapters);
    return result;
fault_encode_error:
    Py_DECREF(faults); Py_DECREF(adapters);
    return NULL;
}

static int
decode_wire_string(const uint8_t *data, Py_ssize_t length, Py_ssize_t *offset,
                   PyObject *error_type, PyObject **out)
{
    if (*offset > length - 2) {
        PyErr_SetString(error_type, "adapter fault string length is truncated");
        return -1;
    }
    uint16_t size = wreath_load_u16_le(data + *offset);
    *offset += 2;
    if (*offset > length - size) {
        PyErr_SetString(error_type, "adapter fault string is truncated");
        return -1;
    }
    *out = PyUnicode_DecodeUTF8((const char *)data + *offset, size, "strict");
    *offset += size;
    return *out == NULL ? -1 : 0;
}

PyObject *
wreath_fault_decode_parts(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *body, *adapt, *fault_type, *adapter_type, *error_type;
    if (!PyArg_ParseTuple(args, "O!OOOO:fault_decode_parts", &PyBytes_Type,
                          &body, &adapt, &fault_type, &adapter_type,
                          &error_type)) return NULL;
    Py_ssize_t length = PyBytes_GET_SIZE(body);
    if (length < 4) {
        PyErr_SetString(error_type, "fault schedule FALT chunk is truncated");
        return NULL;
    }
    const uint8_t *data = (const uint8_t *)PyBytes_AS_STRING(body);
    uint32_t count = wreath_load_u32_le(data);
    if ((uint64_t)count > (uint64_t)(length - 4) / 9U) {
        PyErr_SetString(error_type, "fault schedule count exceeds its data");
        return NULL;
    }
    PyObject *faults = PyTuple_New(count);
    if (faults == NULL) return NULL;
    Py_ssize_t offset = 4;
    for (uint32_t index = 0; index < count; index++, offset += 9) {
        PyObject *kind = PyLong_FromUnsignedLong(data[offset]);
        PyObject *segment = PyLong_FromLong(
            (long)(int32_t)wreath_load_u32_le(data + offset + 1));
        PyObject *value = PyLong_FromUnsignedLong(
            wreath_load_u32_le(data + offset + 5));
        PyObject *fault = kind != NULL && segment != NULL && value != NULL
            ? PyObject_CallFunctionObjArgs(fault_type, kind, segment, value, NULL)
            : NULL;
        Py_XDECREF(kind); Py_XDECREF(segment); Py_XDECREF(value);
        if (fault == NULL) {
            Py_DECREF(faults);
            return NULL;
        }
        PyTuple_SET_ITEM(faults, index, fault);
    }
    PyObject *adapter_faults = PyTuple_New(0);
    if (adapter_faults == NULL) {
        Py_DECREF(faults);
        return NULL;
    }
    if (adapt != Py_None) {
        if (!PyBytes_Check(adapt)) {
            PyErr_SetString(PyExc_TypeError, "ADPT chunk must be bytes or None");
            goto fault_decode_error;
        }
        length = PyBytes_GET_SIZE(adapt);
        if (length < 4) {
            PyErr_SetString(error_type, "fault schedule ADPT chunk is truncated");
            goto fault_decode_error;
        }
        data = (const uint8_t *)PyBytes_AS_STRING(adapt);
        uint32_t adapter_count = wreath_load_u32_le(data);
        Py_SETREF(adapter_faults, PyTuple_New(adapter_count));
        if (adapter_faults == NULL) {
            Py_DECREF(faults);
            return NULL;
        }
        offset = 4;
        for (uint32_t index = 0; index < adapter_count; index++) {
            if (offset > length - 5) {
                PyErr_SetString(error_type, "adapter fault header is truncated");
                goto fault_decode_error;
            }
            PyObject *seam = PyLong_FromUnsignedLong(data[offset]);
            PyObject *coordinate = PyLong_FromLong(
                (long)(int32_t)wreath_load_u32_le(data + offset + 1));
            offset += 5;
            PyObject *target = NULL, *kind = NULL;
            if (seam == NULL || coordinate == NULL ||
                decode_wire_string(data, length, &offset, error_type, &target) < 0 ||
                decode_wire_string(data, length, &offset, error_type, &kind) < 0) {
                Py_XDECREF(seam); Py_XDECREF(coordinate);
                Py_XDECREF(target); Py_XDECREF(kind);
                goto fault_decode_error;
            }
            PyObject *fault = PyObject_CallFunctionObjArgs(
                adapter_type, seam, target, kind, coordinate, NULL);
            Py_DECREF(seam); Py_DECREF(coordinate); Py_DECREF(target); Py_DECREF(kind);
            if (fault == NULL) goto fault_decode_error;
            PyTuple_SET_ITEM(adapter_faults, index, fault);
        }
    }
    return wreath_tuple2_from_owned(faults, adapter_faults);
fault_decode_error:
    Py_DECREF(faults); Py_DECREF(adapter_faults);
    return NULL;
}


static int
dns_skip_name(const uint8_t *data, Py_ssize_t length, Py_ssize_t *offset)
{
    for (;;) {
        if (*offset >= length) {
            PyErr_SetString(PyExc_ValueError, "truncated DNS name");
            return -1;
        }
        uint8_t size = data[(*offset)++];
        if (size == 0) return 0;
        if ((size & 0xc0U) == 0xc0U) {
            if (*offset >= length) {
                PyErr_SetString(PyExc_ValueError, "truncated DNS compression pointer");
                return -1;
            }
            (*offset)++;
            return 0;
        }
        if (size > 63 || *offset > length - size) {
            PyErr_SetString(PyExc_ValueError, "invalid DNS label");
            return -1;
        }
        *offset += size;
    }
}

PyObject *
wreath_dns_parse_txt(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *response;
    unsigned long query_id;
    if (!PyArg_ParseTuple(args, "O!k:dns_parse_txt", &PyBytes_Type, &response,
                          &query_id)) return NULL;
    Py_ssize_t length = PyBytes_GET_SIZE(response);
    const uint8_t *data = (const uint8_t *)PyBytes_AS_STRING(response);
    if (length < 12) {
        PyErr_SetString(PyExc_ValueError, "DNS response shorter than a header");
        return NULL;
    }
    uint16_t ident = wreath_load_u16_be(data);
    uint16_t flags = wreath_load_u16_be(data + 2);
    uint16_t questions = wreath_load_u16_be(data + 4);
    uint16_t answers = wreath_load_u16_be(data + 6);
    if (ident != (query_id & UINT16_MAX)) {
        PyErr_SetString(PyExc_ValueError, "DNS response id does not match the query");
        return NULL;
    }
    unsigned int rcode = flags & 15U;
    if (rcode == 3) return PyTuple_New(0);
    if (rcode != 0) {
        PyErr_Format(PyExc_ValueError, "DNS server returned rcode %u", rcode);
        return NULL;
    }
    Py_ssize_t offset = 12;
    for (uint16_t index = 0; index < questions; index++) {
        if (dns_skip_name(data, length, &offset) < 0) return NULL;
        if (offset > length - 4) {
            PyErr_SetString(PyExc_ValueError, "truncated DNS question");
            return NULL;
        }
        offset += 4;
    }
    PyObject **found = PyMem_Calloc(answers > 0 ? answers : 1, sizeof(PyObject *));
    if (found == NULL) return PyErr_NoMemory();
    uint16_t found_count = 0;
    for (uint16_t index = 0; index < answers; index++) {
        if (dns_skip_name(data, length, &offset) < 0) goto dns_error;
        if (offset > length - 10) {
            PyErr_SetString(PyExc_ValueError, "truncated DNS answer");
            goto dns_error;
        }
        uint16_t type = wreath_load_u16_be(data + offset);
        uint16_t data_length = wreath_load_u16_be(data + offset + 8);
        offset += 10;
        if (offset > length - data_length) {
            PyErr_SetString(PyExc_ValueError, "truncated DNS rdata");
            goto dns_error;
        }
        Py_ssize_t end = offset + data_length;
        if (type == 16) {
            char *text = PyMem_Malloc(data_length > 0 ? data_length : 1);
            if (text == NULL) { PyErr_NoMemory(); goto dns_error; }
            Py_ssize_t text_length = 0;
            while (offset < end) {
                uint8_t size = data[offset++];
                Py_ssize_t available = end - offset;
                Py_ssize_t take = size < available ? size : available;
                memcpy(text + text_length, data + offset, (size_t)take);
                text_length += take;
                offset += size;
            }
            found[found_count] = PyUnicode_DecodeUTF8(text, text_length, "replace");
            PyMem_Free(text);
            if (found[found_count] == NULL) goto dns_error;
            found_count++;
        }
        offset = end;
    }
    PyObject *result = PyTuple_New(found_count);
    if (result == NULL) goto dns_error;
    for (uint16_t index = 0; index < found_count; index++)
        PyTuple_SET_ITEM(result, index, found[index]);
    PyMem_Free(found);
    return result;
dns_error:
    for (uint16_t index = 0; index < found_count; index++) Py_DECREF(found[index]);
    PyMem_Free(found);
    return NULL;
}


static uint64_t
siphash_rotl(uint64_t value, unsigned int shift)
{
    return (value << shift) | (value >> (64U - shift));
}

static void
siphash_round(uint64_t *v0, uint64_t *v1, uint64_t *v2, uint64_t *v3)
{
    *v0 += *v1;
    *v1 = siphash_rotl(*v1, 13) ^ *v0;
    *v0 = siphash_rotl(*v0, 32);
    *v2 += *v3;
    *v3 = siphash_rotl(*v3, 16) ^ *v2;
    *v0 += *v3;
    *v3 = siphash_rotl(*v3, 21) ^ *v0;
    *v2 += *v1;
    *v1 = siphash_rotl(*v1, 17) ^ *v2;
    *v2 = siphash_rotl(*v2, 32);
}

static uint64_t
siphash24_data(const uint8_t *data, size_t length, uint64_t k0, uint64_t k1)
{
    uint64_t v0 = UINT64_C(0x736f6d6570736575) ^ k0;
    uint64_t v1 = UINT64_C(0x646f72616e646f6d) ^ k1;
    uint64_t v2 = UINT64_C(0x6c7967656e657261) ^ k0;
    uint64_t v3 = UINT64_C(0x7465646279746573) ^ k1;
    size_t offset = 0;
    while (length - offset >= 8) {
        uint64_t word = wreath_load_u64_le(data + offset);
        v3 ^= word;
        siphash_round(&v0, &v1, &v2, &v3);
        siphash_round(&v0, &v1, &v2, &v3);
        v0 ^= word;
        offset += 8;
    }
    uint64_t tail = (uint64_t)(length & 0xffU) << 56;
    for (size_t index = 0; index < length - offset; index++)
        tail |= (uint64_t)data[offset + index] << (8U * index);
    v3 ^= tail;
    siphash_round(&v0, &v1, &v2, &v3);
    siphash_round(&v0, &v1, &v2, &v3);
    v0 ^= tail;
    v2 ^= UINT64_C(0xff);
    for (int index = 0; index < 4; index++)
        siphash_round(&v0, &v1, &v2, &v3);
    return v0 ^ v1 ^ v2 ^ v3;
}

PyObject *
wreath_siphash24(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer view;
    unsigned long long k0, k1;
    if (!PyArg_ParseTuple(args, "y*KK:siphash24", &view, &k0, &k1)) return NULL;
    const uint8_t *data = view.buf;
    size_t length = (size_t)view.len;
    uint64_t result = siphash24_data(data, length, (uint64_t)k0, (uint64_t)k1);
    PyBuffer_Release(&view);
    return PyLong_FromUnsignedLongLong(result);
}

/* --- request-scoped structured-log buffer --------------------------------
 *
 * Successful requests throw DEBUG/TRACE history away.  Keeping that history
 * as LogArg and LogCell objects made the discarded case pay for public object
 * materialization.  This object instead owns a bounded array of wire cells;
 * only finish(promoted=True) creates bytes for the Python sink boundary. */

typedef struct {
    uint8_t *cursor;
    uint8_t *end;
    uint64_t k0;
    uint64_t k1;
    uint16_t flags;
    uint32_t mismatches;
    uint8_t count;
} DataLogPack;

typedef struct {
    PyObject_HEAD
    wreath_nfr_log_cell *records;
    uint64_t request_id;
    Py_ssize_t budget;
    Py_ssize_t count;
    uint64_t dropped;
    int promoted;
} DataLogBuffer;

static int
data_log_fixed(DataLogPack *state, uint8_t tag, const void *payload, size_t width)
{
    if ((size_t)(state->end - state->cursor) < 1U + width) return 0;
    *state->cursor++ = tag;
    if (width != 0) {
        memcpy(state->cursor, payload, width);
        state->cursor += width;
    }
    return 1;
}

static int
data_log_text(DataLogPack *state, const char *data, Py_ssize_t length)
{
    size_t budget = (size_t)(state->end - state->cursor);
    if (budget < 3) return 0;
    size_t limit = budget - 2;
    if (limit > UINT8_MAX) limit = UINT8_MAX;
    size_t take = (size_t)length;
    if (take > limit) {
        take = limit;
        while (take > 0 && (((const uint8_t *)data)[take] & 0xC0U) == 0x80U)
            take--;
        state->flags |= WREATH_NFR_LOG_FLAG_TRUNCATED;
    }
    *state->cursor++ = WREATH_NFR_LOG_ARG_STR;
    *state->cursor++ = (uint8_t)take;
    memcpy(state->cursor, data, take);
    state->cursor += take;
    return 1;
}

static int
data_log_bytes(PyObject *value, const char **data, Py_ssize_t *length,
               PyObject **owner)
{
    *owner = NULL;
    if (PyBytes_Check(value)) {
        *data = PyBytes_AS_STRING(value);
        *length = PyBytes_GET_SIZE(value);
        return 1;
    }
    PyObject *text = PyUnicode_Check(value) ? Py_NewRef(value) : PyObject_Str(value);
    if (text == NULL) return 0;
    const char *encoded = PyUnicode_AsUTF8AndSize(text, length);
    if (encoded != NULL) {
        *data = encoded;
        *owner = text;
        return 1;
    }
    PyErr_Clear();
    PyObject *replacement = PyUnicode_AsEncodedString(text, "utf-8", "replace");
    Py_DECREF(text);
    if (replacement == NULL) return 0;
    *data = PyBytes_AS_STRING(replacement);
    *length = PyBytes_GET_SIZE(replacement);
    *owner = replacement;
    return 1;
}

static int
data_log_argument(DataLogPack *state, uint8_t spec, PyObject *value)
{
    uint8_t declared = WREATH_NFR_LOG_SPEC_TYPE(spec);
    uint8_t disposition = WREATH_NFR_LOG_SPEC_DISPOSITION(spec);
    if (disposition == WREATH_NFR_CAPTURE_HASHED) {
        const char *data;
        Py_ssize_t length;
        PyObject *owner;
        if (!data_log_bytes(value, &data, &length, &owner)) return -1;
        uint64_t digest = siphash24_data(
            (const uint8_t *)data, (size_t)length, state->k0, state->k1);
        Py_XDECREF(owner);
        state->flags |= WREATH_NFR_LOG_FLAG_REDACTED;
        return data_log_fixed(
            state, WREATH_NFR_LOG_ARG_HASH, &digest, sizeof(digest));
    }
    if (disposition == WREATH_NFR_CAPTURE_MASKED ||
        disposition == WREATH_NFR_CAPTURE_LENGTH) {
        const char *data;
        Py_ssize_t length;
        PyObject *owner;
        if (!data_log_bytes(value, &data, &length, &owner)) return -1;
        Py_XDECREF(owner);
        uint32_t retained = length > (Py_ssize_t)UINT32_MAX
            ? UINT32_MAX : (uint32_t)length;
        state->flags |= WREATH_NFR_LOG_FLAG_REDACTED;
        return data_log_fixed(
            state, WREATH_NFR_LOG_ARG_LENGTH, &retained, sizeof(retained));
    }
    if (value == Py_None) {
        if (declared != WREATH_NFR_LOG_SPEC_NONE) state->mismatches++;
        return data_log_fixed(state, WREATH_NFR_LOG_ARG_NONE, NULL, 0);
    }
    if (declared == WREATH_NFR_LOG_SPEC_BOOL && PyBool_Check(value)) {
        uint8_t flag = value == Py_True;
        return data_log_fixed(state, WREATH_NFR_LOG_ARG_BOOL, &flag, 1);
    }
    if (declared == WREATH_NFR_LOG_SPEC_INT &&
        PyLong_Check(value) && !PyBool_Check(value)) {
        int overflow = 0;
        long long number = PyLong_AsLongLongAndOverflow(value, &overflow);
        if (number == -1 && PyErr_Occurred()) return -1;
        if (overflow == 0) {
            int64_t packed = (int64_t)number;
            return data_log_fixed(
                state, WREATH_NFR_LOG_ARG_INT, &packed, sizeof(packed));
        }
    }
    else if (declared == WREATH_NFR_LOG_SPEC_FLOAT && !PyBool_Check(value) &&
             (PyFloat_Check(value) || PyLong_Check(value))) {
        double number = PyFloat_AsDouble(value);
        if (number == -1.0 && PyErr_Occurred()) PyErr_Clear();
        else return data_log_fixed(
            state, WREATH_NFR_LOG_ARG_FLOAT, &number, sizeof(number));
    }
    else if (declared == WREATH_NFR_LOG_SPEC_STR && PyUnicode_Check(value)) {
        const char *data;
        Py_ssize_t length;
        PyObject *owner;
        if (!data_log_bytes(value, &data, &length, &owner)) return -1;
        int packed = data_log_text(state, data, length);
        Py_XDECREF(owner);
        return packed;
    }
    else if (declared == WREATH_NFR_LOG_SPEC_BYTES && PyBytes_Check(value)) {
        PyObject *text = PyUnicode_DecodeUTF8(
            PyBytes_AS_STRING(value), PyBytes_GET_SIZE(value), "replace");
        if (text == NULL) return -1;
        Py_ssize_t length;
        const char *data = PyUnicode_AsUTF8AndSize(text, &length);
        int packed = data == NULL ? -1 : data_log_text(state, data, length);
        Py_DECREF(text);
        return packed;
    }
    state->mismatches++;
    return data_log_fixed(state, WREATH_NFR_LOG_ARG_NONE, NULL, 0);
}

static int
data_log_pack_cell(wreath_nfr_log_cell *cell, uint64_t request_id,
                   uint32_t site_id, uint8_t severity, uint16_t flags,
                   uint32_t dropped, const uint8_t *specs, Py_ssize_t spec_count,
                   PyObject *values, uint64_t k0, uint64_t k1,
                   uint32_t *mismatches)
{
    memset(cell, 0, sizeof(*cell));
    cell->schema_version = WREATH_NFR_SCHEMA_VERSION;
    cell->kind = WREATH_NFR_KIND_LOG;
    cell->site_id = site_id;
    cell->request_id = request_id;
    cell->severity = severity;
    cell->dropped_siblings = dropped;
    DataLogPack state = {
        .cursor = cell->args,
        .end = cell->args + WREATH_NFR_LOG_INLINE_ARG_BYTES,
        .k0 = k0,
        .k1 = k1,
        .flags = flags,
        .mismatches = 0,
        .count = 0,
    };
    Py_ssize_t value_count = PyTuple_GET_SIZE(values);
    for (Py_ssize_t index = 0; index < spec_count; index++) {
        if (index >= WREATH_NFR_LOG_MAX_ARGS) {
            state.flags |= WREATH_NFR_LOG_FLAG_TRUNCATED;
            break;
        }
        PyObject *value = index < value_count ? PyTuple_GET_ITEM(values, index) : Py_None;
        int packed = data_log_argument(&state, specs[index], value);
        if (packed < 0) return -1;
        if (packed == 0) {
            state.flags |= WREATH_NFR_LOG_FLAG_TRUNCATED;
            break;
        }
        state.count++;
    }
    cell->flags = state.flags;
    cell->arg_count = state.count;
    cell->arg_bytes = (uint8_t)(state.cursor - cell->args);
    *mismatches = state.mismatches;
    return 0;
}

static int
data_log_buffer_init(PyObject *object, PyObject *args, PyObject *kwargs)
{
    static char *names[] = {"request_id", "budget", NULL};
    DataLogBuffer *self = (DataLogBuffer *)object;
    unsigned long long request_id;
    Py_ssize_t budget = 64;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "K|n:LogBuffer", names,
                                     &request_id, &budget)) return -1;
    if (budget < 0) {
        PyErr_SetString(PyExc_ValueError, "budget must not be negative");
        return -1;
    }
    if ((size_t)budget > SIZE_MAX / sizeof(wreath_nfr_log_cell)) {
        PyErr_NoMemory();
        return -1;
    }
    PyMem_Free(self->records);
    self->records = NULL;
    self->request_id = (uint64_t)request_id;
    self->budget = budget;
    self->count = 0;
    self->dropped = 0;
    self->promoted = 0;
    self->records = budget == 0 ? NULL
        : PyMem_Malloc((size_t)budget * sizeof(wreath_nfr_log_cell));
    if (budget != 0 && self->records == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    return 0;
}

static void
data_log_buffer_dealloc(PyObject *object)
{
    DataLogBuffer *self = (DataLogBuffer *)object;
    PyMem_Free(self->records);
    Py_TYPE(object)->tp_free(object);
}

static PyObject *
data_log_buffer_add_cell(PyObject *object, PyObject *arg)
{
    DataLogBuffer *self = (DataLogBuffer *)object;
    Py_buffer view;
    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) return NULL;
    if (view.len != WREATH_NFR_CELL_SIZE) {
        PyBuffer_Release(&view);
        PyErr_Format(PyExc_ValueError, "a log cell is exactly %d bytes, got %zd",
                     WREATH_NFR_CELL_SIZE, view.len);
        return NULL;
    }
    const uint8_t *source = view.buf;
    if (source[0] != WREATH_NFR_SCHEMA_VERSION ||
        source[1] != WREATH_NFR_KIND_LOG) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "LogBuffer accepts only log cells");
        return NULL;
    }
    if (self->count >= self->budget) self->dropped++;
    else memcpy(&self->records[self->count++], source, WREATH_NFR_CELL_SIZE);
    PyBuffer_Release(&view);
    Py_RETURN_NONE;
}

static PyObject *
data_log_buffer_add_values(PyObject *object, PyObject *const *args, Py_ssize_t nargs)
{
    DataLogBuffer *self = (DataLogBuffer *)object;
    if (nargs != 8) {
        PyErr_Format(PyExc_TypeError, "add_values() takes 8 arguments, got %zd", nargs);
        return NULL;
    }
    unsigned long site = PyLong_AsUnsignedLong(args[0]);
    long severity = PyLong_AsLong(args[1]);
    unsigned long long k0 = PyLong_AsUnsignedLongLong(args[4]);
    unsigned long long k1 = PyLong_AsUnsignedLongLong(args[5]);
    unsigned long flags = PyLong_AsUnsignedLong(args[6]);
    unsigned long dropped = PyLong_AsUnsignedLong(args[7]);
    if (PyErr_Occurred()) return NULL;
    if (!PyBytes_Check(args[2]) || !PyTuple_Check(args[3])) {
        PyErr_SetString(PyExc_TypeError, "specs must be bytes and values must be a tuple");
        return NULL;
    }
    wreath_nfr_log_cell local;
    uint32_t mismatches;
    if (data_log_pack_cell(
            &local, self->request_id, (uint32_t)site, (uint8_t)severity,
            (uint16_t)flags, (uint32_t)dropped,
            (const uint8_t *)PyBytes_AS_STRING(args[2]), PyBytes_GET_SIZE(args[2]),
            args[3], (uint64_t)k0, (uint64_t)k1, &mismatches) < 0) return NULL;
    if (self->count >= self->budget) self->dropped++;
    else self->records[self->count++] = local;
    return PyLong_FromUnsignedLong(mismatches);
}

static PyObject *
data_log_buffer_promote(PyObject *object, PyObject *Py_UNUSED(ignored))
{
    ((DataLogBuffer *)object)->promoted = 1;
    Py_RETURN_NONE;
}

static PyObject *
data_log_buffer_finish(PyObject *object, PyObject *arg)
{
    DataLogBuffer *self = (DataLogBuffer *)object;
    int promoted = PyObject_IsTrue(arg);
    if (promoted < 0) return NULL;
    Py_ssize_t count = self->count;
    self->count = 0;
    if (!promoted && !self->promoted) return PyTuple_New(0);
    PyObject *result = PyTuple_New(count);
    if (result == NULL) return NULL;
    for (Py_ssize_t index = 0; index < count; index++) {
        wreath_nfr_log_cell cell = self->records[index];
        cell.request_id = self->request_id;
        cell.flags |= WREATH_NFR_LOG_FLAG_PROMOTED;
        PyObject *encoded = PyBytes_FromStringAndSize(
            (const char *)&cell, WREATH_NFR_CELL_SIZE);
        if (encoded == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyTuple_SET_ITEM(result, index, encoded);
    }
    return result;
}

static PyObject *
data_log_buffer_get_request_id(PyObject *object, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(((DataLogBuffer *)object)->request_id);
}

static PyObject *
data_log_buffer_get_held(PyObject *object, void *Py_UNUSED(closure))
{
    return PyLong_FromSsize_t(((DataLogBuffer *)object)->count);
}

static PyObject *
data_log_buffer_get_dropped(PyObject *object, void *Py_UNUSED(closure))
{
    return PyLong_FromUnsignedLongLong(((DataLogBuffer *)object)->dropped);
}

static PyObject *
data_log_buffer_get_promoted(PyObject *object, void *Py_UNUSED(closure))
{
    return PyBool_FromLong(((DataLogBuffer *)object)->promoted);
}

static PyMethodDef data_log_buffer_methods[] = {
    {"add_cell", data_log_buffer_add_cell, METH_O, "Retain one encoded log cell."},
    {"add_values", (PyCFunction)(void (*)(void))data_log_buffer_add_values,
     METH_FASTCALL, "Pack values directly into the request-owned buffer."},
    {"promote", data_log_buffer_promote, METH_NOARGS, "Mark this buffer promoted."},
    {"finish", data_log_buffer_finish, METH_O, "Discard or materialize held cells."},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef data_log_buffer_getset[] = {
    {"request_id", data_log_buffer_get_request_id, NULL, NULL, NULL},
    {"held", data_log_buffer_get_held, NULL, NULL, NULL},
    {"dropped", data_log_buffer_get_dropped, NULL, NULL, NULL},
    {"promoted", data_log_buffer_get_promoted, NULL, NULL, NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyType_Slot data_log_buffer_slots[] = {
    {Py_tp_init, data_log_buffer_init},
    {Py_tp_dealloc, data_log_buffer_dealloc},
    {Py_tp_methods, data_log_buffer_methods},
    {Py_tp_getset, data_log_buffer_getset},
    {0, NULL},
};

static PyType_Spec data_log_buffer_spec = {
    .name = "wreath._native._core.LogBuffer",
    .basicsize = sizeof(DataLogBuffer),
    .flags = Py_TPFLAGS_DEFAULT,
    .slots = data_log_buffer_slots,
};

int
wreath_register_data_kernels(PyObject *module)
{
    if (data_attrs_ready() < 0) return -1;
    PyObject *type = PyType_FromSpec(&data_log_buffer_spec);
    if (type == NULL) return -1;
    if (PyModule_AddObject(module, "LogBuffer", type) < 0) {
        Py_DECREF(type);
        return -1;
    }
    return 0;
}

static int
data_object_long(PyObject *owner, DataAttr attribute, long long *value)
{
    PyObject *object = data_getattr(owner, attribute);
    if (object == NULL) return -1;
    *value = PyLong_AsLongLong(object);
    Py_DECREF(object);
    return *value == -1 && PyErr_Occurred() ? -1 : 0;
}

PyObject *
wreath_log_cell_encode(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *request_obj, *site_obj, *severity_obj, *offset_obj, *worker_obj,
             *arguments_obj, *flags_obj, *dropped_obj;
    if (!PyArg_ParseTuple(args, "OOOOOOOO:log_cell_encode", &request_obj,
                          &site_obj, &severity_obj, &offset_obj, &worker_obj,
                          &arguments_obj, &flags_obj, &dropped_obj)) return NULL;
    uint64_t request = (uint64_t)PyLong_AsUnsignedLongLongMask(request_obj);
    uint64_t site = (uint64_t)PyLong_AsUnsignedLongLongMask(site_obj);
    uint64_t severity = (uint64_t)PyLong_AsUnsignedLongLongMask(severity_obj);
    uint64_t offset_ms = (uint64_t)PyLong_AsUnsignedLongLongMask(offset_obj);
    uint64_t worker = (uint64_t)PyLong_AsUnsignedLongLongMask(worker_obj);
    uint64_t flags = (uint64_t)PyLong_AsUnsignedLongLongMask(flags_obj);
    uint64_t dropped = (uint64_t)PyLong_AsUnsignedLongLongMask(dropped_obj);
    if (PyErr_Occurred()) return NULL;
    PyObject *arguments = PySequence_Fast(arguments_obj,
                                          "log arguments must be a sequence");
    if (arguments == NULL) return NULL;
    uint8_t packed[WREATH_NFR_LOG_INLINE_ARG_BYTES] = {0};
    size_t used = 0;
    uint8_t count = 0;
    Py_ssize_t argument_count = PySequence_Fast_GET_SIZE(arguments);
    PyObject **items = PySequence_Fast_ITEMS(arguments);
    for (Py_ssize_t index = 0; index < argument_count; index++) {
        if (index >= WREATH_NFR_LOG_MAX_ARGS) {
            flags |= WREATH_NFR_LOG_FLAG_TRUNCATED;
            break;
        }
        PyObject *arg = items[index];
        long long kind_value;
        if (data_object_long(arg, DATA_ATTR_TYPE, &kind_value) < 0)
            goto log_encode_error;
        if (kind_value < WREATH_NFR_LOG_ARG_NONE ||
            kind_value > WREATH_NFR_LOG_ARG_LENGTH) {
            PyErr_Format(PyExc_ValueError, "unknown log argument type %lld", kind_value);
            goto log_encode_error;
        }
        uint8_t kind = (uint8_t)kind_value;
        size_t remaining = WREATH_NFR_LOG_INLINE_ARG_BYTES - used;
        size_t width = 1;
        if (kind == WREATH_NFR_LOG_ARG_BOOL) width = 2;
        else if (kind == WREATH_NFR_LOG_ARG_INT ||
                 kind == WREATH_NFR_LOG_ARG_FLOAT ||
                 kind == WREATH_NFR_LOG_ARG_HASH) width = 9;
        else if (kind == WREATH_NFR_LOG_ARG_LENGTH) width = 5;
        if (kind != WREATH_NFR_LOG_ARG_STR && width > remaining) {
            flags |= WREATH_NFR_LOG_FLAG_TRUNCATED;
            break;
        }
        packed[used++] = kind;
        if (kind == WREATH_NFR_LOG_ARG_NONE) {
            count++;
            continue;
        }
        if (kind == WREATH_NFR_LOG_ARG_STR) {
            PyObject *payload_obj = data_getattr(arg, DATA_ATTR_PAYLOAD);
            if (payload_obj == NULL) goto log_encode_error;
            PyObject *payload = PyBytes_FromObject(payload_obj);
            Py_DECREF(payload_obj);
            if (payload == NULL) goto log_encode_error;
            Py_ssize_t length = PyBytes_GET_SIZE(payload);
            if (remaining < 3) {
                Py_DECREF(payload);
                used--;
                flags |= WREATH_NFR_LOG_FLAG_TRUNCATED;
                break;
            }
            Py_ssize_t take = length;
            Py_ssize_t limit = (Py_ssize_t)remaining - 2;
            if (limit > UINT8_MAX) limit = UINT8_MAX;
            if (take > limit) {
                take = limit;
                const uint8_t *raw = (const uint8_t *)PyBytes_AS_STRING(payload);
                while (take > 0 && (raw[take] & 0xc0U) == 0x80U) take--;
                flags |= WREATH_NFR_LOG_FLAG_TRUNCATED;
            }
            packed[used++] = (uint8_t)take;
            memcpy(packed + used, PyBytes_AS_STRING(payload), (size_t)take);
            used += (size_t)take;
            Py_DECREF(payload);
            count++;
            continue;
        }
        if (kind == WREATH_NFR_LOG_ARG_FLOAT) {
            PyObject *fraction = data_getattr(arg, DATA_ATTR_FRACTION);
            double number = fraction == NULL ? -1.0 : PyFloat_AsDouble(fraction);
            Py_XDECREF(fraction);
            if (number == -1.0 && PyErr_Occurred()) goto log_encode_error;
            uint64_t bits;
            memcpy(&bits, &number, sizeof(bits));
            wreath_store_u64_le(packed + used, bits);
            used += 8;
        } else {
            PyObject *number = data_getattr(arg, DATA_ATTR_NUMBER);
            if (number == NULL) goto log_encode_error;
            if (kind == WREATH_NFR_LOG_ARG_BOOL) {
                int truth = PyObject_IsTrue(number);
                Py_DECREF(number);
                if (truth < 0) goto log_encode_error;
                packed[used++] = truth ? 1 : 0;
            } else if (kind == WREATH_NFR_LOG_ARG_INT) {
                long long value = PyLong_AsLongLong(number);
                Py_DECREF(number);
                if (value == -1 && PyErr_Occurred()) goto log_encode_error;
                wreath_store_u64_le(packed + used, (uint64_t)value);
                used += 8;
            } else if (kind == WREATH_NFR_LOG_ARG_HASH) {
                unsigned long long value = PyLong_AsUnsignedLongLong(number);
                Py_DECREF(number);
                if (value == (unsigned long long)-1 && PyErr_Occurred())
                    goto log_encode_error;
                wreath_store_u64_le(packed + used, value);
                used += 8;
                flags |= WREATH_NFR_LOG_FLAG_REDACTED;
            } else {
                unsigned long value = PyLong_AsUnsignedLong(number);
                Py_DECREF(number);
                if (value == (unsigned long)-1 && PyErr_Occurred())
                    goto log_encode_error;
                wreath_store_u32_le(packed + used, (uint32_t)value);
                used += 4;
                flags |= WREATH_NFR_LOG_FLAG_REDACTED;
            }
        }
        count++;
    }
    PyObject *result = PyBytes_FromStringAndSize(NULL, WREATH_NFR_CELL_SIZE);
    if (result == NULL) goto log_encode_error;
    uint8_t *data = (uint8_t *)PyBytes_AS_STRING(result);
    memset(data, 0, WREATH_NFR_CELL_SIZE);
    data[0] = WREATH_NFR_SCHEMA_VERSION;
    data[1] = WREATH_NFR_KIND_LOG;
    wreath_store_u16_le(data + 2, (uint16_t)flags);
    wreath_store_u32_le(data + 4, (uint32_t)site);
    wreath_store_u64_le(data + 8, request);
    wreath_store_u32_le(data + 16, (uint32_t)offset_ms);
    wreath_store_u32_le(data + 20, (uint32_t)dropped);
    data[24] = (uint8_t)severity;
    data[25] = (uint8_t)worker;
    data[26] = count;
    data[27] = (uint8_t)used;
    memcpy(data + 32, packed, used);
    Py_DECREF(arguments);
    return result;
log_encode_error:
    Py_DECREF(arguments);
    return NULL;
}

static PyObject *
log_arg_materialize(PyObject *arg_type, PyObject *kind_type, uint8_t kind,
                    const uint8_t *data, Py_ssize_t size)
{
    PyObject *kind_obj = PyObject_CallFunction(kind_type, "i", kind);
    if (kind_obj == NULL) return NULL;
    PyObject *values[4] = {kind_obj, NULL, NULL, NULL};
    Py_ssize_t count = 1;
    if (kind == WREATH_NFR_LOG_ARG_BOOL) {
        values[1] = PyLong_FromLong(data[0] ? 1 : 0); count = 2;
    } else if (kind == WREATH_NFR_LOG_ARG_INT) {
        values[1] = PyLong_FromLongLong((int64_t)wreath_load_u64_le(data)); count = 2;
    } else if (kind == WREATH_NFR_LOG_ARG_FLOAT) {
        uint64_t bits = wreath_load_u64_le(data);
        double number;
        memcpy(&number, &bits, sizeof(number));
        values[1] = PyLong_FromLong(0);
        values[2] = PyFloat_FromDouble(number); count = 3;
    } else if (kind == WREATH_NFR_LOG_ARG_HASH) {
        values[1] = PyLong_FromUnsignedLongLong(wreath_load_u64_le(data)); count = 2;
    } else if (kind == WREATH_NFR_LOG_ARG_LENGTH) {
        values[1] = PyLong_FromUnsignedLong(wreath_load_u32_le(data)); count = 2;
    } else if (kind == WREATH_NFR_LOG_ARG_STR) {
        values[1] = PyLong_FromLong(0);
        values[2] = PyFloat_FromDouble(0.0);
        values[3] = PyBytes_FromStringAndSize((const char *)data, size); count = 4;
    }
    PyObject *result = NULL;
    for (Py_ssize_t index = 1; index < count; index++)
        if (values[index] == NULL) goto log_arg_done;
    result = PyObject_Vectorcall(arg_type, values, (size_t)count, NULL);
log_arg_done:
    for (Py_ssize_t index = 0; index < count; index++) Py_XDECREF(values[index]);
    return result;
}

PyObject *
wreath_log_cell_decode(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer view;
    PyObject *error_type, *arg_type, *kind_type, *severity_type, *cell_type;
    if (!PyArg_ParseTuple(args, "y*OOOOO:log_cell_decode", &view, &error_type,
                          &arg_type, &kind_type, &severity_type, &cell_type)) return NULL;
    const uint8_t *data = view.buf;
    if (view.len < WREATH_NFR_CELL_SIZE) {
        PyErr_Format(error_type, "log cell needs %d bytes, got %zd",
                     WREATH_NFR_CELL_SIZE, view.len);
        goto log_decode_error;
    }
    if (data[0] != WREATH_NFR_SCHEMA_VERSION) {
        PyErr_Format(error_type, "unsupported schema version %u", data[0]);
        goto log_decode_error;
    }
    if (data[1] != WREATH_NFR_KIND_LOG) {
        PyErr_Format(error_type, "expected log kind, got %u", data[1]);
        goto log_decode_error;
    }
    uint8_t arg_count = data[26], arg_bytes = data[27];
    if (arg_bytes > WREATH_NFR_LOG_INLINE_ARG_BYTES) {
        PyErr_Format(error_type,
                     "log cell declares %u argument bytes, but a cell holds at most %d",
                     arg_bytes, WREATH_NFR_LOG_INLINE_ARG_BYTES);
        goto log_decode_error;
    }
    PyObject *decoded = PyTuple_New(arg_count);
    if (decoded == NULL) goto log_decode_error;
    Py_ssize_t offset = 0;
    uint8_t decoded_count = 0;
    while (offset < arg_bytes) {
        if (decoded_count >= arg_count) break;
        uint8_t kind = data[32 + offset++];
        Py_ssize_t width;
        if (kind == WREATH_NFR_LOG_ARG_NONE) width = 0;
        else if (kind == WREATH_NFR_LOG_ARG_BOOL) width = 1;
        else if (kind == WREATH_NFR_LOG_ARG_INT ||
                 kind == WREATH_NFR_LOG_ARG_FLOAT ||
                 kind == WREATH_NFR_LOG_ARG_HASH) width = 8;
        else if (kind == WREATH_NFR_LOG_ARG_LENGTH) width = 4;
        else if (kind == WREATH_NFR_LOG_ARG_STR) {
            if (offset >= arg_bytes) {
                PyErr_SetString(error_type,
                                "log cell truncated reading an argument length");
                Py_DECREF(decoded); goto log_decode_error;
            }
            width = data[32 + offset++];
        } else {
            PyErr_Format(error_type, "unknown log argument type %u", kind);
            Py_DECREF(decoded); goto log_decode_error;
        }
        if (width > arg_bytes - offset) {
            PyErr_SetString(error_type,
                            "log cell truncated reading an argument payload");
            Py_DECREF(decoded); goto log_decode_error;
        }
        PyObject *item = log_arg_materialize(
            arg_type, kind_type, kind, data + 32 + offset, width);
        if (item == NULL) { Py_DECREF(decoded); goto log_decode_error; }
        PyTuple_SET_ITEM(decoded, decoded_count++, item);
        offset += width;
    }
    if (decoded_count != arg_count || offset != arg_bytes) {
        PyErr_Format(error_type,
                     "log cell declares %u arguments but its payload holds %u",
                     arg_count, decoded_count);
        Py_DECREF(decoded); goto log_decode_error;
    }
    uint8_t severity = data[24];
    int known_severity = severity == 1 || severity == 5 || severity == 9 ||
                         severity == 13 || severity == 17 || severity == 21;
    PyObject *values[8] = {
        PyLong_FromUnsignedLongLong(wreath_load_u64_le(data + 8)),
        PyLong_FromUnsignedLong(wreath_load_u32_le(data + 4)),
        known_severity ? PyObject_CallFunction(severity_type, "i", severity)
                       : PyLong_FromUnsignedLong(severity),
        PyLong_FromUnsignedLong(wreath_load_u32_le(data + 16)),
        PyLong_FromUnsignedLong(data[25]), decoded,
        PyLong_FromUnsignedLong(wreath_load_u16_le(data + 2)),
        PyLong_FromUnsignedLong(wreath_load_u32_le(data + 20))
    };
    PyObject *result = NULL;
    for (int index = 0; index < 8; index++)
        if (values[index] == NULL) goto log_values_done;
    result = PyObject_Vectorcall(cell_type, values, 8, NULL);
log_values_done:
    for (int index = 0; index < 8; index++) Py_XDECREF(values[index]);
    PyBuffer_Release(&view);
    return result;
log_decode_error:
    PyBuffer_Release(&view);
    return NULL;
}

PyObject *
wreath_capture_slab_decode(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer view;
    PyObject *error_type, *field_type, *class_type, *disposition_type, *slab_type;
    if (!PyArg_ParseTuple(args, "y*OOOOO:capture_slab_decode", &view, &error_type,
                          &field_type, &class_type, &disposition_type,
                          &slab_type)) return NULL;
    const uint8_t *data = view.buf;
    if (view.len < WREATH_NFR_CAPTURE_SLAB_HEADER_SIZE) {
        PyErr_Format(error_type, "capture slab needs %d bytes, got %zd",
                     WREATH_NFR_CAPTURE_SLAB_HEADER_SIZE, view.len);
        goto capture_error;
    }
    uint32_t used = wreath_load_u32_le(data + 8);
    uint16_t count = wreath_load_u16_le(data + 12);
    if (data[14] != WREATH_NFR_SCHEMA_VERSION) {
        PyErr_Format(error_type, "unsupported schema version %u", data[14]);
        goto capture_error;
    }
    if (data[15] != WREATH_NFR_KIND_CAPTURE) {
        PyErr_Format(error_type, "expected capture kind, got %u", data[15]);
        goto capture_error;
    }
    if (used > (uint64_t)view.len) {
        PyErr_SetString(error_type, "capture slab used_bytes exceeds the buffer");
        goto capture_error;
    }
    PyObject *fields = PyTuple_New(count);
    if (fields == NULL) goto capture_error;
    Py_ssize_t offset = WREATH_NFR_CAPTURE_SLAB_HEADER_SIZE;
    for (uint16_t index = 0; index < count; index++) {
        if (offset > (Py_ssize_t)used - WREATH_NFR_CAPTURE_FIELD_HEADER_SIZE) {
            PyErr_SetString(error_type,
                            "capture slab truncated reading a field header");
            Py_DECREF(fields); goto capture_error;
        }
        uint16_t field_class = wreath_load_u16_le(data + offset);
        uint16_t descriptor = wreath_load_u16_le(data + offset + 2);
        uint8_t disposition = data[offset + 4];
        uint16_t stored = wreath_load_u16_le(data + offset + 6);
        uint32_t original = wreath_load_u32_le(data + offset + 8);
        offset += WREATH_NFR_CAPTURE_FIELD_HEADER_SIZE;
        if (stored > used - (uint32_t)offset) {
            PyErr_SetString(error_type,
                            "capture slab truncated reading a field payload");
            Py_DECREF(fields); goto capture_error;
        }
        unsigned int class_value = field_class <= 9 ? field_class : 0;
        unsigned int disposition_value = disposition <= 3 ? disposition : 3;
        PyObject *values[5] = {
            PyObject_CallFunction(class_type, "I", class_value),
            PyLong_FromUnsignedLong(descriptor),
            PyObject_CallFunction(disposition_type, "I", disposition_value),
            PyLong_FromUnsignedLong(original),
            PyBytes_FromStringAndSize((const char *)data + offset, stored)
        };
        PyObject *field = NULL;
        for (int cell = 0; cell < 5; cell++)
            if (values[cell] == NULL) goto capture_values_done;
        field = PyObject_Vectorcall(field_type, values, 5, NULL);
capture_values_done:
        for (int cell = 0; cell < 5; cell++) Py_XDECREF(values[cell]);
        if (field == NULL) { Py_DECREF(fields); goto capture_error; }
        PyTuple_SET_ITEM(fields, index, field);
        offset += (stored + 3U) & ~3U;
    }
    PyObject *values[4] = {
        PyLong_FromUnsignedLongLong(wreath_load_u64_le(data)), fields,
        PyLong_FromUnsignedLong(data[16]), PyLong_FromUnsignedLong(data[17])
    };
    PyObject *result = NULL;
    for (int index = 0; index < 4; index++)
        if (values[index] == NULL) goto capture_result_done;
    result = PyObject_Vectorcall(slab_type, values, 4, NULL);
capture_result_done:
    for (int index = 0; index < 4; index++) Py_XDECREF(values[index]);
    PyBuffer_Release(&view);
    return result;
capture_error:
    PyBuffer_Release(&view);
    return NULL;
}


static int
data_writer_u32(WreathBytesWriter *writer, uint32_t value)
{
    uint8_t bytes[4];
    wreath_store_u32_le(bytes, value);
    return wreath_writer_write(writer, (const char *)bytes, 4);
}

static int
data_writer_text(WreathBytesWriter *writer, PyObject *text)
{
    Py_ssize_t length;
    const char *utf8 = PyUnicode_AsUTF8AndSize(text, &length);
    if (utf8 == NULL) return -1;
    if ((uint64_t)length > UINT32_MAX) {
        PyErr_SetString(PyExc_OverflowError,
                        "attempt recording field is too long to encode");
        return -1;
    }
    return data_writer_u32(writer, (uint32_t)length) < 0 ||
           wreath_writer_write(writer, utf8, length) < 0 ? -1 : 0;
}

PyObject *
wreath_step_encode(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *position_obj, *boundaries_obj, *completed_obj, *compensations_obj;
    PyObject *texts[9];
    if (!PyArg_UnpackTuple(
            args, "step_encode", 13, 13, &position_obj, &boundaries_obj,
            &completed_obj, &compensations_obj, &texts[0], &texts[1], &texts[2],
            &texts[3], &texts[4], &texts[5], &texts[6], &texts[7], &texts[8]))
        return NULL;
    long position = PyLong_AsLong(position_obj);
    unsigned long completed = PyLong_AsUnsignedLong(completed_obj);
    if (PyErr_Occurred() || position < INT32_MIN || position > INT32_MAX ||
        completed > UINT32_MAX) return NULL;
    PyObject *boundaries = PySequence_Fast(boundaries_obj,
                                            "boundaries must be a sequence");
    PyObject *compensations = boundaries == NULL ? NULL
        : PySequence_Fast(compensations_obj,
                          "compensations must be a sequence");
    if (compensations == NULL) { Py_XDECREF(boundaries); return NULL; }
    Py_ssize_t boundary_count = PySequence_Fast_GET_SIZE(boundaries);
    Py_ssize_t compensation_count = PySequence_Fast_GET_SIZE(compensations);
    if ((uint64_t)boundary_count > UINT32_MAX ||
        (uint64_t)compensation_count > UINT32_MAX) {
        PyErr_SetString(PyExc_OverflowError,
                        "workflow-step record has too many entries");
        goto step_encode_error;
    }
    WreathBytesWriter writer;
    if (wreath_writer_init(&writer, 512) < 0) goto step_encode_error;
    if (wreath_writer_write(&writer, "WFS1\x01\0\0\0", 8) < 0 ||
        data_writer_u32(&writer, 0) < 0 ||
        data_writer_u32(&writer, (uint32_t)(int32_t)position) < 0 ||
        data_writer_u32(&writer, (uint32_t)boundary_count) < 0 ||
        data_writer_u32(&writer, (uint32_t)compensation_count) < 0 ||
        data_writer_u32(&writer, (uint32_t)completed) < 0) goto step_writer_error;
    for (int index = 0; index < 9; index++)
        if (data_writer_text(&writer, texts[index]) < 0) goto step_writer_error;
    PyObject **boundary_items = PySequence_Fast_ITEMS(boundaries);
    for (Py_ssize_t index = 0; index < boundary_count; index++) {
        PyObject *event = boundary_items[index];
        PyObject *seam_obj = data_getattr(event, DATA_ATTR_SEAM);
        PyObject *coordinate_obj = seam_obj == NULL ? NULL
            : data_getattr(event, DATA_ATTR_COORDINATE);
        PyObject *target = coordinate_obj == NULL ? NULL
            : data_getattr(event, DATA_ATTR_TARGET);
        PyObject *failure = target == NULL ? NULL
            : data_getattr(event, DATA_ATTR_ERROR_TYPE);
        if (failure == NULL) {
            Py_XDECREF(seam_obj); Py_XDECREF(coordinate_obj);
            Py_XDECREF(target); goto step_writer_error;
        }
        unsigned long seam = PyLong_AsUnsignedLong(seam_obj);
        long coordinate = PyLong_AsLong(coordinate_obj);
        Py_DECREF(seam_obj); Py_DECREF(coordinate_obj);
        int failed = PyErr_Occurred() || seam > UINT8_MAX ||
            coordinate < INT32_MIN || coordinate > INT32_MAX ||
            wreath_writer_byte(&writer, (char)seam) < 0 ||
            data_writer_u32(&writer, (uint32_t)(int32_t)coordinate) < 0 ||
            data_writer_text(&writer, target) < 0 ||
            data_writer_text(&writer, failure) < 0;
        Py_DECREF(target); Py_DECREF(failure);
        if (failed) goto step_writer_error;
    }
    PyObject **compensation_items = PySequence_Fast_ITEMS(compensations);
    for (Py_ssize_t index = 0; index < compensation_count; index++) {
        PyObject *pair = PySequence_Fast(compensation_items[index],
                                         "a compensation must be a pair");
        if (pair == NULL) goto step_writer_error;
        if (PySequence_Fast_GET_SIZE(pair) != 2) {
            PyErr_SetString(PyExc_ValueError, "a compensation must be a pair");
            Py_DECREF(pair); goto step_writer_error;
        }
        int failed = data_writer_text(
                &writer, PySequence_Fast_GET_ITEM(pair, 0)) < 0 ||
            data_writer_text(&writer, PySequence_Fast_GET_ITEM(pair, 1)) < 0;
        Py_DECREF(pair);
        if (failed) goto step_writer_error;
    }
    if ((uint64_t)writer.len > UINT32_MAX) {
        PyErr_SetString(PyExc_OverflowError,
                        "workflow-step recording is too long to encode");
        goto step_writer_error;
    }
    wreath_store_u32_le((uint8_t *)writer.buf + 8, (uint32_t)writer.len);
    Py_DECREF(boundaries); Py_DECREF(compensations);
    return wreath_writer_finish(&writer);
step_writer_error:
    Py_XDECREF(writer.bytes);
step_encode_error:
    Py_DECREF(boundaries); Py_DECREF(compensations);
    return NULL;
}

static PyObject *
data_read_text(const uint8_t *data, Py_ssize_t length, Py_ssize_t *offset,
               PyObject *error_type)
{
    if (*offset > length - 4) {
        PyErr_SetString(error_type,
                        "attempt recording is truncated inside a text field");
        return NULL;
    }
    uint32_t size = wreath_load_u32_le(data + *offset);
    *offset += 4;
    if ((uint64_t)size > (uint64_t)(length - *offset)) {
        PyErr_Format(error_type,
                     "attempt recording is truncated: a field declares %u bytes and only %zd remain",
                     size, length - *offset);
        return NULL;
    }
    PyObject *text = PyUnicode_DecodeUTF8(
        (const char *)data + *offset, size, "strict");
    *offset += size;
    return text;
}

PyObject *
wreath_step_decode(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer view;
    PyObject *error_type, *boundary_type, *record_type;
    if (!PyArg_ParseTuple(args, "y*OOO:step_decode", &view, &error_type,
                          &boundary_type, &record_type)) return NULL;
    const uint8_t *data = view.buf;
    Py_ssize_t length = view.len;
    if (length < 12) {
        PyErr_Format(error_type,
                     "workflow-step recording is truncated: %zd bytes is shorter than its 12-byte header",
                     length);
        goto step_decode_error;
    }
    if (memcmp(data, "WFS1", 4) != 0) {
        PyObject *magic = PyBytes_FromStringAndSize((const char *)data, 4);
        if (magic != NULL)
            PyErr_Format(error_type,
                         "not a workflow-step recording: bad record magic %R", magic);
        Py_XDECREF(magic);
        goto step_decode_error;
    }
    if (data[4] != 1) {
        PyErr_Format(error_type, "unsupported workflow-step record version %u",
                     data[4]);
        goto step_decode_error;
    }
    if (data[5] & 1) {
        PyErr_SetString(error_type,
                        "workflow-step recording is chunked across records; it is refused rather than joined, because a partially assembled step reports fewer compensations than the saga ran and reads as complete");
        goto step_decode_error;
    }
    uint32_t total = wreath_load_u32_le(data + 8);
    if ((uint64_t)total != (uint64_t)length) {
        PyErr_Format(error_type,
                     "workflow-step recording is truncated: it declares %u bytes and holds %zd",
                     total, length);
        goto step_decode_error;
    }
    if (length < 28) {
        PyErr_SetString(error_type,
                        "workflow-step recording is truncated inside its fixed fields");
        goto step_decode_error;
    }
    int32_t position = (int32_t)wreath_load_u32_le(data + 12);
    uint32_t boundary_count = wreath_load_u32_le(data + 16);
    uint32_t compensation_count = wreath_load_u32_le(data + 20);
    uint32_t completed = wreath_load_u32_le(data + 24);
    Py_ssize_t offset = 28;
    PyObject *texts[9] = {0};
    for (int index = 0; index < 9; index++) {
        texts[index] = data_read_text(data, length, &offset, error_type);
        if (texts[index] == NULL) goto step_objects_error;
    }
    PyObject *boundaries = PyTuple_New(boundary_count);
    if (boundaries == NULL) goto step_objects_error;
    for (uint32_t index = 0; index < boundary_count; index++) {
        if (offset > length - 5) {
            PyErr_SetString(error_type,
                            "attempt recording is truncated inside a boundary");
            Py_DECREF(boundaries); goto step_objects_error;
        }
        PyObject *target = NULL, *failure = NULL;
        PyObject *values[4] = {
            PyLong_FromUnsignedLong(data[offset]), NULL,
            PyLong_FromLong((int32_t)wreath_load_u32_le(data + offset + 1)), NULL
        };
        offset += 5;
        target = data_read_text(data, length, &offset, error_type);
        failure = target == NULL ? NULL
            : data_read_text(data, length, &offset, error_type);
        values[1] = target; values[3] = failure;
        PyObject *event = NULL;
        for (int cell = 0; cell < 4; cell++)
            if (values[cell] == NULL) goto step_boundary_done;
        event = PyObject_Vectorcall(boundary_type, values, 4, NULL);
step_boundary_done:
        for (int cell = 0; cell < 4; cell++) Py_XDECREF(values[cell]);
        if (event == NULL) { Py_DECREF(boundaries); goto step_objects_error; }
        PyTuple_SET_ITEM(boundaries, index, event);
    }
    PyObject *compensations = PyTuple_New(compensation_count);
    if (compensations == NULL) { Py_DECREF(boundaries); goto step_objects_error; }
    for (uint32_t index = 0; index < compensation_count; index++) {
        PyObject *name = data_read_text(data, length, &offset, error_type);
        PyObject *state = name == NULL ? NULL
            : data_read_text(data, length, &offset, error_type);
        PyObject *pair = wreath_tuple2_from_owned(name, state);
        if (pair == NULL) {
            Py_DECREF(boundaries); Py_DECREF(compensations);
            goto step_objects_error;
        }
        PyTuple_SET_ITEM(compensations, index, pair);
    }
    PyObject *values[13] = {
        Py_NewRef(texts[0]), Py_NewRef(texts[1]), Py_NewRef(texts[2]),
        PyLong_FromLong(position), Py_NewRef(texts[3]), Py_NewRef(texts[4]),
        Py_NewRef(texts[5]), boundaries, Py_NewRef(texts[6]),
        Py_NewRef(texts[7]), Py_NewRef(texts[8]),
        PyLong_FromUnsignedLong(completed), compensations
    };
    PyObject *result = NULL;
    for (int index = 0; index < 13; index++)
        if (values[index] == NULL) goto step_result_done;
    result = PyObject_Vectorcall(record_type, values, 13, NULL);
step_result_done:
    for (int index = 0; index < 13; index++) Py_XDECREF(values[index]);
    for (int index = 0; index < 9; index++) Py_DECREF(texts[index]);
    PyBuffer_Release(&view);
    return result;
step_objects_error:
    for (int index = 0; index < 9; index++) Py_XDECREF(texts[index]);
step_decode_error:
    PyBuffer_Release(&view);
    return NULL;
}

typedef struct {
    int sign;
    uint32_t *limbs;
    size_t used;
    size_t capacity;
} DataInteger;

typedef struct {
    char *key;
    size_t key_length;
    uint64_t hash;
    DataInteger value;
} MetricEntry;

typedef struct {
    MetricEntry *entries;
    size_t count;
    size_t capacity;
    size_t *slots;
    size_t slot_capacity;
} MetricTable;

static void
data_integer_clear(DataInteger *value)
{
    PyMem_Free(value->limbs);
    memset(value, 0, sizeof(*value));
}

static int
data_integer_reserve(DataInteger *value, size_t capacity)
{
    if (capacity <= value->capacity) return 0;
    if (capacity > SIZE_MAX / sizeof(uint32_t)) {
        PyErr_NoMemory();
        return -1;
    }
    uint32_t *grown = PyMem_Realloc(value->limbs, capacity * sizeof(uint32_t));
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    value->limbs = grown;
    value->capacity = capacity;
    return 0;
}

static void
data_integer_normalize(DataInteger *value)
{
    while (value->used && value->limbs[value->used - 1] == 0) value->used--;
    if (!value->used) value->sign = 0;
}

static int
data_integer_from_python(DataInteger *out, PyObject *object)
{
    if (!PyLong_Check(object)) {
        PyErr_Format(PyExc_TypeError,
                     "counter value must be int, got %.200s",
                     Py_TYPE(object)->tp_name);
        return -1;
    }
    int sign;
    if (PyLong_GetSign(object, &sign) < 0) return -1;
    if (sign == 0) return 0;
    Py_ssize_t byte_count = PyLong_AsNativeBytes(
        object, NULL, 0, Py_ASNATIVEBYTES_LITTLE_ENDIAN);
    if (byte_count < 0 && PyErr_Occurred()) return -1;
    uint8_t *bytes = PyMem_Malloc((size_t)byte_count);
    if (bytes == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    Py_ssize_t copied = PyLong_AsNativeBytes(
        object, bytes, byte_count, Py_ASNATIVEBYTES_LITTLE_ENDIAN);
    if (copied < 0 && PyErr_Occurred()) {
        PyMem_Free(bytes);
        return -1;
    }
    if (sign < 0) {
        uint16_t carry = 1;
        for (Py_ssize_t index = 0; index < byte_count; index++) {
            uint16_t cell = (uint16_t)(uint8_t)~bytes[index] + carry;
            bytes[index] = (uint8_t)cell;
            carry = cell >> 8;
        }
    }
    while (byte_count && bytes[byte_count - 1] == 0) byte_count--;
    size_t limbs = ((size_t)byte_count + 3) / 4;
    if (data_integer_reserve(out, limbs) < 0) {
        PyMem_Free(bytes);
        return -1;
    }
    memset(out->limbs, 0, limbs * sizeof(uint32_t));
    for (Py_ssize_t index = 0; index < byte_count; index++)
        out->limbs[(size_t)index / 4] |=
            (uint32_t)bytes[index] << (8U * ((size_t)index & 3U));
    PyMem_Free(bytes);
    out->used = limbs;
    out->sign = sign;
    data_integer_normalize(out);
    return 0;
}

static int
data_integer_compare_magnitude(const DataInteger *left,
                               const DataInteger *right)
{
    if (left->used != right->used) return left->used > right->used ? 1 : -1;
    for (size_t index = left->used; index-- > 0;) {
        if (left->limbs[index] != right->limbs[index])
            return left->limbs[index] > right->limbs[index] ? 1 : -1;
    }
    return 0;
}

static int
data_integer_add_magnitude(DataInteger *left, const DataInteger *right)
{
    size_t longest = left->used > right->used ? left->used : right->used;
    if (data_integer_reserve(left, longest + 1) < 0) return -1;
    uint64_t carry = 0;
    for (size_t index = 0; index < longest; index++) {
        uint64_t cell = carry;
        if (index < left->used) cell += left->limbs[index];
        if (index < right->used) cell += right->limbs[index];
        left->limbs[index] = (uint32_t)cell;
        carry = cell >> 32;
    }
    left->used = longest;
    if (carry) left->limbs[left->used++] = (uint32_t)carry;
    return 0;
}

static void
data_integer_subtract_magnitude(DataInteger *larger, const DataInteger *smaller)
{
    uint64_t borrow = 0;
    for (size_t index = 0; index < larger->used; index++) {
        uint64_t subtrahend = borrow;
        if (index < smaller->used) subtrahend += smaller->limbs[index];
        uint64_t cell = larger->limbs[index];
        larger->limbs[index] = (uint32_t)(cell - subtrahend);
        borrow = cell < subtrahend;
    }
    data_integer_normalize(larger);
}

static int
data_integer_add(DataInteger *left, const DataInteger *right)
{
    if (right->sign == 0) return 0;
    if (left->sign == 0) {
        if (data_integer_reserve(left, right->used) < 0) return -1;
        memcpy(left->limbs, right->limbs, right->used * sizeof(uint32_t));
        left->used = right->used;
        left->sign = right->sign;
        return 0;
    }
    if (left->sign == right->sign)
        return data_integer_add_magnitude(left, right);
    int order = data_integer_compare_magnitude(left, right);
    if (order == 0) {
        left->used = 0;
        left->sign = 0;
        return 0;
    }
    if (order > 0) {
        data_integer_subtract_magnitude(left, right);
        return 0;
    }
    DataInteger difference = {0};
    if (data_integer_reserve(&difference, right->used) < 0) return -1;
    memcpy(difference.limbs, right->limbs, right->used * sizeof(uint32_t));
    difference.used = right->used;
    difference.sign = right->sign;
    data_integer_subtract_magnitude(&difference, left);
    data_integer_clear(left);
    *left = difference;
    return 0;
}

static PyObject *
data_integer_to_python(const DataInteger *value)
{
    if (value->sign == 0) return PyLong_FromLong(0);
    size_t byte_count = value->used * sizeof(uint32_t);
    if (value->sign > 0)
        return PyLong_FromUnsignedNativeBytes(
            value->limbs, byte_count, Py_ASNATIVEBYTES_LITTLE_ENDIAN);
    if (byte_count == SIZE_MAX) {
        PyErr_NoMemory();
        return NULL;
    }
    byte_count++;
    uint8_t *bytes = PyMem_Calloc(byte_count, 1);
    if (bytes == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    memcpy(bytes, value->limbs, byte_count - 1);
    uint16_t carry = 1;
    for (size_t index = 0; index < byte_count; index++) {
        uint16_t cell = (uint16_t)(uint8_t)~bytes[index] + carry;
        bytes[index] = (uint8_t)cell;
        carry = cell >> 8;
    }
    PyObject *result = PyLong_FromNativeBytes(
        bytes, byte_count, Py_ASNATIVEBYTES_LITTLE_ENDIAN);
    PyMem_Free(bytes);
    return result;
}

static uint64_t
metric_hash(const char *data, size_t length)
{
    uint64_t hash = UINT64_C(1469598103934665603);
    for (size_t index = 0; index < length; index++) {
        hash ^= (uint8_t)data[index];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}

static void
metric_table_clear(MetricTable *table)
{
    for (size_t index = 0; index < table->count; index++) {
        PyMem_Free(table->entries[index].key);
        data_integer_clear(&table->entries[index].value);
    }
    PyMem_Free(table->entries);
    PyMem_Free(table->slots);
    memset(table, 0, sizeof(*table));
}

static int
metric_table_rehash(MetricTable *table, size_t capacity)
{
    size_t *slots = PyMem_Malloc(capacity * sizeof(size_t));
    if (slots == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    for (size_t index = 0; index < capacity; index++) slots[index] = SIZE_MAX;
    for (size_t index = 0; index < table->count; index++) {
        size_t slot = (size_t)table->entries[index].hash & (capacity - 1);
        while (slots[slot] != SIZE_MAX) slot = (slot + 1) & (capacity - 1);
        slots[slot] = index;
    }
    PyMem_Free(table->slots);
    table->slots = slots;
    table->slot_capacity = capacity;
    return 0;
}

static int
metric_table_add(MetricTable *table, char *key, size_t key_length,
                 DataInteger *value)
{
    if (table->slot_capacity == 0 && metric_table_rehash(table, 16) < 0)
        goto add_error;
    if ((table->count + 1) * 10 >= table->slot_capacity * 7 &&
        metric_table_rehash(table, table->slot_capacity * 2) < 0) goto add_error;
    uint64_t hash = metric_hash(key, key_length);
    size_t slot = (size_t)hash & (table->slot_capacity - 1);
    while (table->slots[slot] != SIZE_MAX) {
        MetricEntry *entry = &table->entries[table->slots[slot]];
        if (entry->hash == hash && entry->key_length == key_length &&
            memcmp(entry->key, key, key_length) == 0) {
            int result = data_integer_add(&entry->value, value);
            PyMem_Free(key);
            data_integer_clear(value);
            return result;
        }
        slot = (slot + 1) & (table->slot_capacity - 1);
    }
    if (table->count == table->capacity) {
        size_t capacity = table->capacity ? table->capacity * 2 : 16;
        MetricEntry *entries = PyMem_Realloc(
            table->entries, capacity * sizeof(MetricEntry));
        if (entries == NULL) {
            PyErr_NoMemory();
            goto add_error;
        }
        table->entries = entries;
        table->capacity = capacity;
    }
    size_t index = table->count++;
    table->entries[index] = (MetricEntry){key, key_length, hash, *value};
    memset(value, 0, sizeof(*value));
    table->slots[slot] = index;
    return 0;
add_error:
    PyMem_Free(key);
    data_integer_clear(value);
    return -1;
}

static int
metric_text(PyObject *object, PyObject **owner, const char **data, Py_ssize_t *length)
{
    *owner = PyObject_Str(object);
    if (*owner == NULL) return -1;
    *data = PyUnicode_AsUTF8AndSize(*owner, length);
    if (*data == NULL) {
        Py_CLEAR(*owner);
        return -1;
    }
    return 0;
}

static int
metric_add_pair(MetricTable *table, const char *namespace_data,
                Py_ssize_t namespace_length, const char *subsystem_data,
                Py_ssize_t subsystem_length, PyObject *name_object,
                PyObject *value_object)
{
    PyObject *name_owner = NULL;
    const char *name_data;
    Py_ssize_t name_length;
    if (metric_text(name_object, &name_owner, &name_data, &name_length) < 0)
        return -1;
    size_t key_length = (size_t)namespace_length + (size_t)subsystem_length +
                        (size_t)name_length + 2;
    char *key = PyMem_Malloc(key_length);
    if (key == NULL) {
        Py_DECREF(name_owner);
        PyErr_NoMemory();
        return -1;
    }
    char *cursor = key;
    memcpy(cursor, namespace_data, (size_t)namespace_length);
    cursor += namespace_length; *cursor++ = '_';
    memcpy(cursor, subsystem_data, (size_t)subsystem_length);
    cursor += subsystem_length; *cursor++ = '_';
    memcpy(cursor, name_data, (size_t)name_length);
    Py_DECREF(name_owner);
    DataInteger value = {0};
    if (data_integer_from_python(&value, value_object) < 0) {
        PyMem_Free(key);
        return -1;
    }
    return metric_table_add(table, key, key_length, &value);
}

PyObject *
wreath_metrics_flatten(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *readings_object, *namespace_object;
    if (!PyArg_ParseTuple(args, "OO:metrics_flatten", &readings_object,
                          &namespace_object)) return NULL;
    PyObject *namespace_owner = NULL;
    const char *namespace_data;
    Py_ssize_t namespace_length;
    if (metric_text(namespace_object, &namespace_owner, &namespace_data,
                    &namespace_length) < 0) return NULL;
    PyObject *readings = PyObject_GetIter(readings_object);
    if (readings == NULL) {
        Py_DECREF(namespace_owner);
        return NULL;
    }
    MetricTable table = {0};
    PyObject *reading;
    while ((reading = PyIter_Next(readings)) != NULL) {
        PyObject *subsystem_object = data_getattr(reading, DATA_ATTR_SUBSYSTEM);
        PyObject *values = subsystem_object == NULL ? NULL
            : data_getattr(reading, DATA_ATTR_VALUES);
        Py_DECREF(reading);
        if (values == NULL) {
            Py_XDECREF(subsystem_object);
            goto metric_error;
        }
        PyObject *subsystem_owner = NULL;
        const char *subsystem_data;
        Py_ssize_t subsystem_length;
        if (metric_text(subsystem_object, &subsystem_owner, &subsystem_data,
                        &subsystem_length) < 0) {
            Py_DECREF(subsystem_object); Py_DECREF(values);
            goto metric_error;
        }
        Py_DECREF(subsystem_object);
        if (PyDict_CheckExact(values)) {
            Py_ssize_t position = 0;
            PyObject *name, *value;
            while (PyDict_Next(values, &position, &name, &value)) {
                if (metric_add_pair(&table, namespace_data, namespace_length,
                                    subsystem_data, subsystem_length,
                                    name, value) < 0) {
                    Py_DECREF(subsystem_owner); Py_DECREF(values);
                    goto metric_error;
                }
            }
        } else {
            PyObject *items = PyMapping_Items(values);
            if (items == NULL) {
                Py_DECREF(subsystem_owner); Py_DECREF(values);
                goto metric_error;
            }
            Py_ssize_t count = PyList_GET_SIZE(items);
            for (Py_ssize_t index = 0; index < count; index++) {
                PyObject *pair = PyList_GET_ITEM(items, index);
                if (metric_add_pair(&table, namespace_data, namespace_length,
                                    subsystem_data, subsystem_length,
                                    PyTuple_GET_ITEM(pair, 0),
                                    PyTuple_GET_ITEM(pair, 1)) < 0) {
                    Py_DECREF(items); Py_DECREF(subsystem_owner); Py_DECREF(values);
                    goto metric_error;
                }
            }
            Py_DECREF(items);
        }
        Py_DECREF(subsystem_owner);
        Py_DECREF(values);
    }
    if (PyErr_Occurred()) goto metric_error;
    Py_DECREF(readings);
    Py_DECREF(namespace_owner);
    PyObject *result = PyDict_New();
    if (result == NULL) goto metric_table_error;
    for (size_t index = 0; index < table.count; index++) {
        MetricEntry *entry = &table.entries[index];
        PyObject *key = PyUnicode_DecodeUTF8(entry->key, (Py_ssize_t)entry->key_length,
                                             "strict");
        PyObject *value = key == NULL ? NULL : data_integer_to_python(&entry->value);
        if (value == NULL || PyDict_SetItem(result, key, value) < 0) {
            Py_XDECREF(key); Py_XDECREF(value); Py_DECREF(result);
            goto metric_table_error;
        }
        Py_DECREF(key); Py_DECREF(value);
    }
    metric_table_clear(&table);
    return result;
metric_error:
    Py_DECREF(readings);
    Py_DECREF(namespace_owner);
metric_table_error:
    metric_table_clear(&table);
    return NULL;
}

static void
locale_trim(const Py_UCS4 *text, Py_ssize_t *start, Py_ssize_t *end)
{
    while (*start < *end && Py_UNICODE_ISSPACE(text[*start])) (*start)++;
    while (*end > *start && Py_UNICODE_ISSPACE(text[*end - 1])) (*end)--;
}

static int
locale_ascii_equal(const Py_UCS4 *text, Py_ssize_t start, Py_ssize_t end,
                   const char *expected)
{
    size_t length = strlen(expected);
    if ((size_t)(end - start) != length) return 0;
    for (size_t index = 0; index < length; index++) {
        Py_UCS4 cell = text[start + (Py_ssize_t)index];
        if (cell >= 'A' && cell <= 'Z') cell += 'a' - 'A';
        if (cell != (uint8_t)expected[index]) return 0;
    }
    return 1;
}

static int
locale_quality(const Py_UCS4 *text, Py_ssize_t start, Py_ssize_t end,
               double *result)
{
    locale_trim(text, &start, &end);
    if (start == end) return 0;
    int negative = 0;
    if (text[start] == '+' || text[start] == '-') {
        negative = text[start] == '-';
        if (++start == end) return 0;
    }
    if (locale_ascii_equal(text, start, end, "nan")) {
        *result = NAN;
        return 1;
    }
    if (locale_ascii_equal(text, start, end, "inf") ||
        locale_ascii_equal(text, start, end, "infinity")) {
        *result = negative ? -INFINITY : INFINITY;
        return 1;
    }
    double value = 0.0;
    int digits = 0;
    while (start < end && text[start] >= '0' && text[start] <= '9') {
        value = value * 10.0 + (double)(text[start++] - '0');
        digits++;
    }
    if (start < end && text[start] == '.') {
        start++;
        double place = 0.1;
        while (start < end && text[start] >= '0' && text[start] <= '9') {
            value += (double)(text[start++] - '0') * place;
            place *= 0.1;
            digits++;
        }
    }
    if (!digits) return 0;
    int exponent = 0, exponent_negative = 0, exponent_digits = 0;
    if (start < end && (text[start] == 'e' || text[start] == 'E')) {
        start++;
        if (start < end && (text[start] == '+' || text[start] == '-')) {
            exponent_negative = text[start] == '-';
            start++;
        }
        while (start < end && text[start] >= '0' && text[start] <= '9') {
            if (exponent < 10000) exponent = exponent * 10 + (int)(text[start] - '0');
            start++;
            exponent_digits++;
        }
        if (!exponent_digits) return 0;
    }
    if (start != end) return 0;
    if (exponent) value *= pow(10.0, exponent_negative ? -exponent : exponent);
    *result = negative ? -value : value;
    return 1;
}

PyObject *
wreath_locale_preference(PyObject *Py_UNUSED(self), PyObject *header)
{
    if (!PyUnicode_Check(header)) {
        PyErr_Format(PyExc_TypeError, "Accept-Language must be str, got %.200s",
                     Py_TYPE(header)->tp_name);
        return NULL;
    }
    Py_ssize_t length = PyUnicode_GetLength(header);
    Py_UCS4 *text = PyUnicode_AsUCS4Copy(header);
    if (text == NULL) return NULL;
    int found = 0;
    double best_quality = 0.0;
    Py_ssize_t best_start = 0, best_end = 0;
    Py_ssize_t part_start = 0;
    while (part_start <= length) {
        Py_ssize_t part_end = part_start;
        while (part_end < length && text[part_end] != ',') part_end++;
        Py_ssize_t start = part_start, end = part_end;
        locale_trim(text, &start, &end);
        Py_ssize_t separator = start;
        while (separator < end && text[separator] != ';') separator++;
        Py_ssize_t tag_start = start, tag_end = separator;
        locale_trim(text, &tag_start, &tag_end);
        if (tag_start < tag_end &&
            !(tag_end - tag_start == 1 && text[tag_start] == '*')) {
            double quality = 1.0;
            Py_ssize_t parameter_start = separator < end ? separator + 1 : end;
            while (parameter_start <= end) {
                Py_ssize_t parameter_end = parameter_start;
                while (parameter_end < end && text[parameter_end] != ';')
                    parameter_end++;
                Py_ssize_t equals = parameter_start;
                while (equals < parameter_end && text[equals] != '=') equals++;
                Py_ssize_t key_start = parameter_start, key_end = equals;
                locale_trim(text, &key_start, &key_end);
                if (locale_ascii_equal(text, key_start, key_end, "q")) {
                    double parsed;
                    if (equals == parameter_end ||
                        !locale_quality(text, equals + 1, parameter_end, &parsed))
                        quality = 0.0;
                    else
                        quality = parsed;
                    break;
                }
                if (parameter_end == end) break;
                parameter_start = parameter_end + 1;
            }
            if (!found || quality > best_quality) {
                found = 1;
                best_quality = quality;
                best_start = tag_start;
                best_end = tag_end;
            }
        }
        if (part_end == length) break;
        part_start = part_end + 1;
    }
    PyMem_Free(text);
    return found ? PyUnicode_Substring(header, best_start, best_end)
                 : PyUnicode_FromString("en");
}

typedef struct {
    PyObject *original;
    Py_UCS4 *text;
    Py_ssize_t length;
    int refused;
} LocaleOffer;

static int
locale_tag_equal(const Py_UCS4 *left, Py_ssize_t left_start, Py_ssize_t left_end,
                 const Py_UCS4 *right, Py_ssize_t right_length)
{
    if (left_end - left_start != right_length) return 0;
    for (Py_ssize_t index = 0; index < right_length; index++) {
        Py_UCS4 a = left[left_start + index], b = right[index];
        if (a >= 'A' && a <= 'Z') a += 'a' - 'A';
        if (b >= 'A' && b <= 'Z') b += 'a' - 'A';
        if (a != b) return 0;
    }
    return 1;
}

static int
locale_primary_equal(const Py_UCS4 *left, Py_ssize_t left_start, Py_ssize_t left_end,
                     const Py_UCS4 *right, Py_ssize_t right_length)
{
    Py_ssize_t left_length = 0, right_primary = 0;
    while (left_start + left_length < left_end && left[left_start + left_length] != '-')
        left_length++;
    while (right_primary < right_length && right[right_primary] != '-') right_primary++;
    return locale_tag_equal(left, left_start, left_start + left_length,
                            right, right_primary);
}

PyObject *
wreath_select_language(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *header, *offered_object;
    if (!PyArg_ParseTuple(args, "OO:select_language", &header, &offered_object)) return NULL;
    if (!PyUnicode_Check(header)) {
        PyErr_Format(PyExc_TypeError, "Accept-Language must be str, got %.200s",
                     Py_TYPE(header)->tp_name);
        return NULL;
    }
    PyObject *offered = PySequence_Fast(offered_object,
                                        "offered languages must be a sequence");
    if (offered == NULL) return NULL;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(offered);
    if (count == 0) {
        Py_DECREF(offered);
        PyErr_SetString(PyExc_ValueError,
                        "offered must contain at least one language");
        return NULL;
    }
    LocaleOffer *offers = PyMem_Calloc((size_t)count, sizeof(LocaleOffer));
    if (offers == NULL) {
        Py_DECREF(offered);
        return PyErr_NoMemory();
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *item = PySequence_Fast_GET_ITEM(offered, index);
        if (!PyUnicode_Check(item)) {
            PyErr_Format(PyExc_TypeError, "offered language must be str, got %.200s",
                         Py_TYPE(item)->tp_name);
            goto language_error;
        }
        offers[index].original = item;
        offers[index].length = PyUnicode_GetLength(item);
        offers[index].text = PyUnicode_AsUCS4Copy(item);
        if (offers[index].text == NULL) goto language_error;
    }
    Py_ssize_t length = PyUnicode_GetLength(header);
    Py_UCS4 *text = PyUnicode_AsUCS4Copy(header);
    if (text == NULL) goto language_error;
    Py_ssize_t best = -1;
    double best_quality = 0.0, wildcard_quality = 0.0;
    Py_ssize_t part_start = 0;
    while (part_start <= length) {
        Py_ssize_t part_end = part_start;
        while (part_end < length && text[part_end] != ',') part_end++;
        Py_ssize_t start = part_start, end = part_end;
        locale_trim(text, &start, &end);
        Py_ssize_t separator = start;
        while (separator < end && text[separator] != ';') separator++;
        Py_ssize_t tag_start = start, tag_end = separator;
        locale_trim(text, &tag_start, &tag_end);
        double quality = 1.0;
        Py_ssize_t parameter_start = separator < end ? separator + 1 : end;
        while (parameter_start <= end) {
            Py_ssize_t parameter_end = parameter_start;
            while (parameter_end < end && text[parameter_end] != ';') parameter_end++;
            Py_ssize_t equals = parameter_start;
            while (equals < parameter_end && text[equals] != '=') equals++;
            Py_ssize_t key_start = parameter_start, key_end = equals;
            locale_trim(text, &key_start, &key_end);
            if (locale_ascii_equal(text, key_start, key_end, "q")) {
                double parsed;
                if (equals == parameter_end ||
                    !locale_quality(text, equals + 1, parameter_end, &parsed))
                    quality = 0.0;
                else
                    quality = parsed;
                break;
            }
            if (parameter_end == end) break;
            parameter_start = parameter_end + 1;
        }
        if (tag_end - tag_start == 1 && text[tag_start] == '*') {
            if (quality > wildcard_quality) wildcard_quality = quality;
        } else if (tag_start < tag_end) {
            Py_ssize_t match = -1;
            for (Py_ssize_t index = 0; index < count; index++) {
                if (locale_tag_equal(text, tag_start, tag_end,
                                     offers[index].text, offers[index].length)) {
                    match = index;
                    break;
                }
            }
            if (match < 0) {
                for (Py_ssize_t index = 0; index < count; index++) {
                    if (locale_primary_equal(text, tag_start, tag_end,
                                             offers[index].text,
                                             offers[index].length)) {
                        match = index;
                        break;
                    }
                }
            }
            if (match >= 0) {
                if (quality <= 0.0)
                    offers[match].refused = 1;
                else if (quality > best_quality) {
                    best = match;
                    best_quality = quality;
                }
            }
        }
        if (part_end == length) break;
        part_start = part_end + 1;
    }
    PyMem_Free(text);
    Py_ssize_t selected = best;
    if (selected < 0 && wildcard_quality > 0.0) {
        for (Py_ssize_t index = 0; index < count; index++) {
            if (!offers[index].refused) {
                selected = index;
                break;
            }
        }
    }
    if (selected < 0) selected = 0;
    PyObject *result = offers[selected].original;
    Py_INCREF(result);
    for (Py_ssize_t index = 0; index < count; index++) PyMem_Free(offers[index].text);
    PyMem_Free(offers);
    Py_DECREF(offered);
    return result;
language_error:
    for (Py_ssize_t index = 0; index < count; index++) PyMem_Free(offers[index].text);
    PyMem_Free(offers);
    Py_DECREF(offered);
    return NULL;
}

static int
host_port_valid(const char *text, size_t length)
{
    if (length == 0) return 1;
    unsigned value = 0;
    for (size_t index = 0; index < length; index++) {
        if (text[index] < '0' || text[index] > '9') return 0;
        value = value * 10U + (unsigned)(text[index] - '0');
        if (value > 65535U) return 0;
    }
    return 1;
}

static int
host_ipv4_words(const char *text, size_t length, uint16_t *first,
                uint16_t *second)
{
    unsigned octets[4] = {0};
    size_t offset = 0;
    for (size_t part = 0; part < 4; part++) {
        if (offset == length || text[offset] < '0' || text[offset] > '9') return 0;
        unsigned value = 0;
        size_t digits = 0;
        while (offset < length && text[offset] >= '0' && text[offset] <= '9') {
            value = value * 10U + (unsigned)(text[offset++] - '0');
            if (++digits > 3 || value > 255U) return 0;
        }
        octets[part] = value;
        if (part < 3) {
            if (offset == length || text[offset++] != '.') return 0;
        } else if (offset != length) {
            return 0;
        }
    }
    *first = (uint16_t)((octets[0] << 8) | octets[1]);
    *second = (uint16_t)((octets[2] << 8) | octets[3]);
    return 1;
}

static int
host_ipv6_parse(const char *text, size_t length, uint16_t words[8],
                size_t *scope_at)
{
    size_t address_length = length;
    *scope_at = length;
    for (size_t index = 0; index < length; index++) {
        if (text[index] != '%') continue;
        if (index == 0 || index + 1 == length || *scope_at != length) return 0;
        *scope_at = index;
        address_length = index;
    }
    size_t offset = 0, count = 0;
    int compressed = -1;
    if (address_length >= 2 && text[0] == ':' && text[1] == ':') {
        compressed = 0;
        offset = 2;
        if (offset == address_length) goto ipv6_finish;
    } else if (address_length == 0 || text[0] == ':') {
        return 0;
    }
    while (offset < address_length) {
        if (count >= 8) return 0;
        size_t token_start = offset;
        while (offset < address_length && text[offset] != ':') offset++;
        size_t token_length = offset - token_start;
        if (token_length == 0) return 0;
        int dotted = 0;
        for (size_t index = token_start; index < offset; index++)
            if (text[index] == '.') dotted = 1;
        if (dotted) {
            if (count > 6 || offset != address_length ||
                !host_ipv4_words(text + token_start, token_length,
                                 &words[count], &words[count + 1])) return 0;
            count += 2;
        } else {
            if (token_length > 4) return 0;
            unsigned value = 0;
            for (size_t index = token_start; index < offset; index++) {
                char cell = text[index];
                unsigned digit;
                if (cell >= '0' && cell <= '9') digit = (unsigned)(cell - '0');
                else if (cell >= 'a' && cell <= 'f') digit = (unsigned)(cell - 'a' + 10);
                else return 0;
                value = (value << 4) | digit;
            }
            words[count++] = (uint16_t)value;
        }
        if (offset == address_length) break;
        offset++;
        if (offset < address_length && text[offset] == ':') {
            if (compressed >= 0) return 0;
            compressed = (int)count;
            offset++;
            if (offset == address_length) break;
        }
    }
ipv6_finish:
    if (compressed < 0) return count == 8;
    if (count >= 8) return 0;
    size_t suffix = count - (size_t)compressed;
    memmove(words + 8 - suffix, words + compressed, suffix * sizeof(uint16_t));
    memset(words + compressed, 0, (8 - count) * sizeof(uint16_t));
    return 1;
}

static size_t
host_hex(char *out, uint16_t value)
{
    static const char digits[] = "0123456789abcdef";
    char reversed[4];
    size_t count = 0;
    do {
        reversed[count++] = digits[value & 15U];
        value >>= 4;
    } while (value);
    for (size_t index = 0; index < count; index++)
        out[index] = reversed[count - index - 1];
    return count;
}

static size_t
host_ipv6_format(char *out, const uint16_t words[8])
{
    if (words[0] == 0 && words[1] == 0 && words[2] == 0 && words[3] == 0 &&
        words[4] == 0 && words[5] == 0xffffU) {
        uint8_t octets[4] = {
            (uint8_t)(words[6] >> 8), (uint8_t)words[6],
            (uint8_t)(words[7] >> 8), (uint8_t)words[7]
        };
        size_t used = 0;
        memcpy(out, "::ffff:", 7); used = 7;
        for (size_t index = 0; index < 4; index++) {
            unsigned value = octets[index];
            if (value >= 100) out[used++] = (char)('0' + value / 100);
            if (value >= 10) out[used++] = (char)('0' + (value / 10) % 10);
            out[used++] = (char)('0' + value % 10);
            if (index != 3) out[used++] = '.';
        }
        return used;
    }
    size_t best_start = 8, best_length = 0;
    for (size_t start = 0; start < 8;) {
        if (words[start] != 0) { start++; continue; }
        size_t end = start + 1;
        while (end < 8 && words[end] == 0) end++;
        if (end - start > best_length) {
            best_start = start;
            best_length = end - start;
        }
        start = end;
    }
    if (best_length < 2) { best_start = 8; best_length = 0; }
    size_t used = 0;
    for (size_t index = 0; index < 8;) {
        if (index == best_start) {
            out[used++] = ':'; out[used++] = ':';
            index += best_length;
            continue;
        }
        if (used && out[used - 1] != ':') out[used++] = ':';
        used += host_hex(out + used, words[index++]);
    }
    return used;
}

PyObject *
wreath_normalize_host(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *value;
    int pattern;
    if (!PyArg_ParseTuple(args, "Up:normalize_host", &value, &pattern)) return NULL;
    Py_ssize_t unicode_length = PyUnicode_GetLength(value);
    Py_UCS4 *unicode = PyUnicode_AsUCS4Copy(value);
    if (unicode == NULL) return NULL;
    Py_ssize_t start = 0, end = unicode_length;
    locale_trim(unicode, &start, &end);
    if (start == end) { PyMem_Free(unicode); Py_RETURN_NONE; }
    size_t length = (size_t)(end - start);
    char *text = PyMem_Malloc(length + 1);
    if (text == NULL) {
        PyMem_Free(unicode); PyErr_NoMemory(); return NULL;
    }
    for (size_t index = 0; index < length; index++) {
        Py_UCS4 cell = unicode[start + (Py_ssize_t)index];
        if (cell > 0x7fU) {
            PyMem_Free(unicode); PyMem_Free(text); Py_RETURN_NONE;
        }
        if (cell >= 'A' && cell <= 'Z') cell += 'a' - 'A';
        text[index] = (char)cell;
    }
    PyMem_Free(unicode);
    text[length] = '\0';
    if (text[0] == '[') {
        size_t close = 1;
        while (close < length && text[close] != ']') close++;
        if (close == length ||
            (close + 1 < length &&
             (text[close + 1] != ':' ||
              !host_port_valid(text + close + 2, length - close - 2)))) {
            PyMem_Free(text); Py_RETURN_NONE;
        }
        uint16_t words[8] = {0};
        size_t scope_at;
        if (!host_ipv6_parse(text + 1, close - 1, words, &scope_at)) {
            PyMem_Free(text); Py_RETURN_NONE;
        }
        char rendered[192];
        size_t used = 0;
        rendered[used++] = '[';
        used += host_ipv6_format(rendered + used, words);
        if (scope_at < close - 1) {
            size_t scope_length = close - 1 - scope_at;
            memcpy(rendered + used, text + 1 + scope_at, scope_length);
            used += scope_length;
        }
        rendered[used++] = ']';
        PyMem_Free(text);
        return PyUnicode_DecodeASCII(rendered, (Py_ssize_t)used, "strict");
    }
    size_t host_length = 0;
    while (host_length < length && text[host_length] != ':') {
        if (text[host_length] == '[' || text[host_length] == ']') {
            PyMem_Free(text); Py_RETURN_NONE;
        }
        host_length++;
    }
    if (host_length < length &&
        !host_port_valid(text + host_length + 1, length - host_length - 1)) {
        PyMem_Free(text); Py_RETURN_NONE;
    }
    if (pattern && host_length == 1 && text[0] == '*') {
        PyObject *result = PyUnicode_FromString("*");
        PyMem_Free(text); return result;
    }
    size_t candidate = pattern && host_length >= 2 && text[0] == '*' && text[1] == '.'
        ? 2 : 0;
    if (candidate == host_length || text[candidate] == '.') {
        PyMem_Free(text); Py_RETURN_NONE;
    }
    for (size_t index = candidate; index < host_length; index++) {
        char cell = text[index];
        if (!((cell >= 'a' && cell <= 'z') || (cell >= '0' && cell <= '9') ||
              cell == '.' || cell == '_' || cell == '-')) {
            PyMem_Free(text); Py_RETURN_NONE;
        }
    }
    PyObject *result = PyUnicode_DecodeASCII(text, (Py_ssize_t)host_length, "strict");
    PyMem_Free(text);
    return result;
}

typedef struct {
    Py_ssize_t start;
    Py_ssize_t end;
    double quality;
    Py_ssize_t rank;
    Py_ssize_t order;
} AcceptRange;

static int
accept_range_before(const AcceptRange *left, const AcceptRange *right)
{
    if (left->quality > right->quality) return 1;
    if (left->quality < right->quality) return 0;
    return left->rank > right->rank;
}

static int
accept_compare(const void *left_pointer, const void *right_pointer)
{
    const AcceptRange *left = left_pointer;
    const AcceptRange *right = right_pointer;
    if (accept_range_before(left, right)) return -1;
    if (accept_range_before(right, left)) return 1;
    return left->order < right->order ? -1 : left->order != right->order;
}

static int
accept_parse(PyObject *header, Py_UCS4 **text_out, AcceptRange **ranges_out,
             size_t *count_out)
{
    *text_out = NULL; *ranges_out = NULL; *count_out = 0;
    if (header == Py_None) return 0;
    if (!PyUnicode_Check(header)) {
        PyErr_Format(PyExc_TypeError, "Accept must be str or None, got %.200s",
                     Py_TYPE(header)->tp_name);
        return -1;
    }
    Py_ssize_t length = PyUnicode_GetLength(header);
    if (length == 0) return 0;
    Py_UCS4 *text = PyUnicode_AsUCS4Copy(header);
    if (text == NULL) return -1;
    AcceptRange *ranges = PyMem_Malloc(
        ((size_t)length + 1) * sizeof(AcceptRange));
    if (ranges == NULL) {
        PyMem_Free(text); PyErr_NoMemory(); return -1;
    }
    for (Py_ssize_t index = 0; index < length; index++)
        text[index] = Py_UNICODE_TOLOWER(text[index]);
    size_t count = 0;
    Py_ssize_t part_start = 0, part_index = 0;
    while (part_start <= length) {
        Py_ssize_t part_end = part_start;
        while (part_end < length && text[part_end] != ',') part_end++;
        Py_ssize_t start = part_start, end = part_end;
        locale_trim(text, &start, &end);
        Py_ssize_t separator = start;
        while (separator < end && text[separator] != ';') separator++;
        Py_ssize_t media_start = start, media_end = separator;
        locale_trim(text, &media_start, &media_end);
        if (media_start < media_end) {
            double quality = 1.0;
            Py_ssize_t parameter_start = separator < end ? separator + 1 : end;
            while (parameter_start <= end) {
                Py_ssize_t parameter_end = parameter_start;
                while (parameter_end < end && text[parameter_end] != ';')
                    parameter_end++;
                Py_ssize_t equals = parameter_start;
                while (equals < parameter_end && text[equals] != '=') equals++;
                Py_ssize_t key_start = parameter_start, key_end = equals;
                locale_trim(text, &key_start, &key_end);
                if (locale_ascii_equal(text, key_start, key_end, "q")) {
                    double parsed;
                    if (equals == parameter_end ||
                        !locale_quality(text, equals + 1, parameter_end, &parsed))
                        quality = 0.0;
                    else
                        quality = parsed;
                }
                if (parameter_end == end) break;
                parameter_start = parameter_end + 1;
            }
            int has_star = 0;
            for (Py_ssize_t index = media_start; index < media_end; index++)
                if (text[index] == '*') has_star = 1;
            int specificity = !has_star ? 2 :
                !(media_end - media_start == 3 && text[media_start] == '*' &&
                  text[media_start + 1] == '/' && text[media_start + 2] == '*');
            ranges[count++] = (AcceptRange){
                media_start, media_end, quality,
                (Py_ssize_t)specificity * 1000 - part_index,
                part_index,
            };
        }
        part_index++;
        if (part_end == length) break;
        part_start = part_end + 1;
    }
    qsort(ranges, count, sizeof(*ranges), accept_compare);
    *text_out = text; *ranges_out = ranges; *count_out = count;
    return 0;
}

typedef struct {
    const Py_UCS4 *text;
    Py_ssize_t length;
    Py_ssize_t value;
    uint64_t hash;
    unsigned char used;
} AcceptTextSlot;

static uint64_t
accept_text_hash(const Py_UCS4 *text, Py_ssize_t length)
{
    uint64_t hash = UINT64_C(1469598103934665603);
    for (Py_ssize_t index = 0; index < length; index++) {
        hash ^= (uint64_t)text[index];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}

static AcceptTextSlot *
accept_table_new(size_t expected, size_t *capacity_out)
{
    size_t capacity = 8;
    if (expected > SIZE_MAX / 2) {
        PyErr_NoMemory();
        return NULL;
    }
    size_t wanted = expected * 2;
    while (capacity < wanted) {
        if (capacity > SIZE_MAX / 2) {
            PyErr_NoMemory();
            return NULL;
        }
        capacity *= 2;
    }
    if (capacity > SIZE_MAX / sizeof(AcceptTextSlot)) {
        PyErr_NoMemory();
        return NULL;
    }
    AcceptTextSlot *table = PyMem_Calloc(capacity, sizeof(*table));
    if (table == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
    *capacity_out = capacity;
    return table;
}

static Py_ssize_t
accept_table_lookup(const AcceptTextSlot *table, size_t capacity,
                    const Py_UCS4 *text, Py_ssize_t length)
{
    uint64_t hash = accept_text_hash(text, length);
    size_t slot = (size_t)hash & (capacity - 1);
    while (table[slot].used) {
        if (table[slot].hash == hash && table[slot].length == length &&
            memcmp(table[slot].text, text,
                   (size_t)length * sizeof(Py_UCS4)) == 0)
            return table[slot].value;
        slot = (slot + 1) & (capacity - 1);
    }
    return -1;
}

static void
accept_table_insert_first(AcceptTextSlot *table, size_t capacity,
                          const Py_UCS4 *text, Py_ssize_t length,
                          Py_ssize_t value)
{
    uint64_t hash = accept_text_hash(text, length);
    size_t slot = (size_t)hash & (capacity - 1);
    while (table[slot].used) {
        if (table[slot].hash == hash && table[slot].length == length &&
            memcmp(table[slot].text, text,
                   (size_t)length * sizeof(Py_UCS4)) == 0)
            return;
        slot = (slot + 1) & (capacity - 1);
    }
    table[slot] = (AcceptTextSlot){text, length, value, hash, 1};
}

/* 0 is a global wildcard, 1 a type wildcard, and 2 an exact media type. */
static int
accept_range_kind(const Py_UCS4 *text, Py_ssize_t length)
{
    if ((length == 1 && text[0] == '*') ||
        (length == 3 && text[0] == '*' && text[1] == '/' && text[2] == '*'))
        return 0;
    if (length >= 2 && text[length - 2] == '/' && text[length - 1] == '*')
        return 1;
    return 2;
}

PyObject *
wreath_parse_accept(PyObject *Py_UNUSED(self), PyObject *header)
{
    Py_UCS4 *text;
    AcceptRange *ranges;
    size_t count;
    if (accept_parse(header, &text, &ranges, &count) < 0) return NULL;
    PyObject *result = PyList_New((Py_ssize_t)count);
    if (result == NULL) goto accept_result_error;
    for (size_t index = 0; index < count; index++) {
        AcceptRange *range = &ranges[index];
        PyObject *media = PyUnicode_FromKindAndData(
            PyUnicode_4BYTE_KIND, text + range->start, range->end - range->start);
        PyObject *quality = media == NULL ? NULL : PyFloat_FromDouble(range->quality);
        PyObject *pair = wreath_tuple2_from_owned(media, quality);
        if (pair == NULL) {
            Py_DECREF(result); result = NULL; goto accept_result_error;
        }
        PyList_SET_ITEM(result, (Py_ssize_t)index, pair);
    }
accept_result_error:
    PyMem_Free(text); PyMem_Free(ranges);
    return result;
}

PyObject *
wreath_negotiate_media(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *header, *offers_object;
    if (!PyArg_ParseTuple(args, "OO:negotiate_media", &header, &offers_object))
        return NULL;
    PyObject *offers = PySequence_Fast(offers_object, "media offers must be a sequence");
    if (offers == NULL) return NULL;
    Py_ssize_t offer_count = PySequence_Fast_GET_SIZE(offers);
    if (offer_count == 0) { Py_DECREF(offers); Py_RETURN_NONE; }
    Py_UCS4 *text;
    AcceptRange *ranges;
    size_t count;
    if (accept_parse(header, &text, &ranges, &count) < 0) {
        Py_DECREF(offers); return NULL;
    }
    if (count == 0) {
        PyMem_Free(text); PyMem_Free(ranges); Py_DECREF(offers);
        return PyLong_FromLong(0);
    }
    Py_UCS4 **offer_text = PyMem_Calloc((size_t)offer_count, sizeof(Py_UCS4 *));
    Py_ssize_t *offer_lengths = PyMem_Malloc((size_t)offer_count * sizeof(Py_ssize_t));
    if (offer_text == NULL || offer_lengths == NULL) {
        PyMem_Free(offer_text); PyMem_Free(offer_lengths);
        PyMem_Free(text); PyMem_Free(ranges); Py_DECREF(offers);
        PyErr_NoMemory(); return NULL;
    }
    PyObject **items = PySequence_Fast_ITEMS(offers);
    for (Py_ssize_t index = 0; index < offer_count; index++) {
        if (!PyUnicode_Check(items[index])) {
            PyErr_SetString(PyExc_TypeError, "media offers must be strings");
            goto negotiate_error;
        }
        offer_lengths[index] = PyUnicode_GetLength(items[index]);
        offer_text[index] = PyUnicode_AsUCS4Copy(items[index]);
        if (offer_text[index] == NULL) goto negotiate_error;
        for (Py_ssize_t cell = 0; cell < offer_lengths[index]; cell++)
            offer_text[index][cell] = Py_UNICODE_TOLOWER(offer_text[index][cell]);
    }

    size_t denied_exact_capacity = 0, denied_type_capacity = 0;
    size_t offered_exact_capacity = 0, offered_type_capacity = 0;
    AcceptTextSlot *denied_exact = accept_table_new(count, &denied_exact_capacity);
    AcceptTextSlot *denied_type = accept_table_new(count, &denied_type_capacity);
    AcceptTextSlot *offered_exact = accept_table_new(
        (size_t)offer_count, &offered_exact_capacity);
    AcceptTextSlot *offered_type = accept_table_new(
        (size_t)offer_count, &offered_type_capacity);
    if (denied_exact == NULL || denied_type == NULL ||
        offered_exact == NULL || offered_type == NULL) {
        PyMem_Free(denied_exact); PyMem_Free(denied_type);
        PyMem_Free(offered_exact); PyMem_Free(offered_type);
        goto negotiate_error;
    }
    int deny_all = 0;
    for (size_t range_index = 0; range_index < count; range_index++) {
        AcceptRange *range = &ranges[range_index];
        if (range->quality > 0.0) continue;
        const Py_UCS4 *range_text = text + range->start;
        Py_ssize_t range_length = range->end - range->start;
        int kind = accept_range_kind(range_text, range_length);
        if (kind == 0) deny_all = 1;
        else if (kind == 1)
            accept_table_insert_first(denied_type, denied_type_capacity,
                                      range_text, range_length - 2, 0);
        else
            accept_table_insert_first(denied_exact, denied_exact_capacity,
                                      range_text, range_length, 0);
    }
    Py_ssize_t first_eligible = -1;
    for (Py_ssize_t offer = 0; offer < offer_count; offer++) {
        Py_ssize_t slash = 0;
        while (slash < offer_lengths[offer] && offer_text[offer][slash] != '/')
            slash++;
        int excluded = deny_all ||
            accept_table_lookup(denied_exact, denied_exact_capacity,
                                offer_text[offer], offer_lengths[offer]) >= 0 ||
            (slash < offer_lengths[offer] &&
             accept_table_lookup(denied_type, denied_type_capacity,
                                 offer_text[offer], slash) >= 0);
        if (excluded) continue;
        if (first_eligible < 0) first_eligible = offer;
        accept_table_insert_first(offered_exact, offered_exact_capacity,
                                  offer_text[offer], offer_lengths[offer], offer);
        if (slash < offer_lengths[offer])
            accept_table_insert_first(offered_type, offered_type_capacity,
                                      offer_text[offer], slash, offer);
    }
    Py_ssize_t selected = -1;
    for (size_t range_index = 0; range_index < count && selected < 0; range_index++) {
        AcceptRange *range = &ranges[range_index];
        if (!(range->quality > 0.0)) continue;
        const Py_UCS4 *range_text = text + range->start;
        Py_ssize_t range_length = range->end - range->start;
        int kind = accept_range_kind(range_text, range_length);
        if (kind == 0) selected = first_eligible;
        else if (kind == 1)
            selected = accept_table_lookup(offered_type, offered_type_capacity,
                                           range_text, range_length - 2);
        else
            selected = accept_table_lookup(offered_exact, offered_exact_capacity,
                                           range_text, range_length);
    }
    PyMem_Free(denied_exact); PyMem_Free(denied_type);
    PyMem_Free(offered_exact); PyMem_Free(offered_type);
    for (Py_ssize_t index = 0; index < offer_count; index++) PyMem_Free(offer_text[index]);
    PyMem_Free(offer_text); PyMem_Free(offer_lengths);
    PyMem_Free(text); PyMem_Free(ranges); Py_DECREF(offers);
    return selected < 0 ? Py_NewRef(Py_None) : PyLong_FromSsize_t(selected);
negotiate_error:
    for (Py_ssize_t index = 0; index < offer_count; index++) PyMem_Free(offer_text[index]);
    PyMem_Free(offer_text); PyMem_Free(offer_lengths);
    PyMem_Free(text); PyMem_Free(ranges); Py_DECREF(offers);
    return NULL;
}

PyObject *
wreath_bearer_token(PyObject *Py_UNUSED(self), PyObject *headers_object)
{
    PyObject *headers = PySequence_Fast(
        headers_object, "request headers must be a sequence");
    if (headers == NULL) return NULL;
    PyObject **items = PySequence_Fast_ITEMS(headers);
    Py_ssize_t count = PySequence_Fast_GET_SIZE(headers);
    PyObject *authorization = NULL;
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *pair = items[index];
        PyObject *name = NULL, *value = NULL;
        if (PyTuple_Check(pair) && PyTuple_GET_SIZE(pair) == 2) {
            name = PyTuple_GET_ITEM(pair, 0); value = PyTuple_GET_ITEM(pair, 1);
        } else if (PyList_Check(pair) && PyList_GET_SIZE(pair) == 2) {
            name = PyList_GET_ITEM(pair, 0); value = PyList_GET_ITEM(pair, 1);
        }
        if (name == NULL || !PyBytes_Check(name) || !PyBytes_Check(value)) {
            PyErr_SetString(PyExc_TypeError,
                            "request headers must be (bytes, bytes) pairs");
            Py_DECREF(headers);
            return NULL;
        }
        if (PyBytes_GET_SIZE(name) != 13 ||
            memcmp(PyBytes_AS_STRING(name), "authorization", 13) != 0) continue;
        if (authorization != NULL) {
            Py_DECREF(headers);
            Py_RETURN_NONE;
        }
        authorization = value;
    }
    if (authorization == NULL) {
        Py_DECREF(headers);
        Py_RETURN_NONE;
    }
    const char *value = PyBytes_AS_STRING(authorization);
    Py_ssize_t length = PyBytes_GET_SIZE(authorization);
    const char *space = memchr(value, ' ', (size_t)length);
    if (space == NULL || space == value || space + 1 == value + length ||
        space - value != 6) {
        Py_DECREF(headers);
        Py_RETURN_NONE;
    }
    static const char bearer[] = "bearer";
    for (size_t index = 0; index < 6; index++) {
        char cell = value[index];
        if (cell >= 'A' && cell <= 'Z') cell += 'a' - 'A';
        if (cell != bearer[index]) {
            Py_DECREF(headers);
            Py_RETURN_NONE;
        }
    }
    PyObject *result = PyUnicode_DecodeLatin1(
        space + 1, length - (Py_ssize_t)(space + 1 - value), NULL);
    Py_DECREF(headers);
    return result;
}

static int
header_name_is(const char *value, Py_ssize_t length, const char *expected)
{
    size_t expected_length = strlen(expected);
    if ((size_t)length != expected_length) return 0;
    for (size_t index = 0; index < expected_length; index++) {
        char cell = value[index];
        if (cell >= 'A' && cell <= 'Z') cell += 'a' - 'A';
        if (cell != expected[index]) return 0;
    }
    return 1;
}

static int
header_contains_ci(const char *value, Py_ssize_t length, const char *needle)
{
    size_t needle_length = strlen(needle);
    if ((size_t)length < needle_length) return 0;
    for (size_t start = 0; start <= (size_t)length - needle_length; start++) {
        size_t index = 0;
        while (index < needle_length) {
            char cell = value[start + index];
            if (cell >= 'A' && cell <= 'Z') cell += 'a' - 'A';
            if (cell != needle[index]) break;
            index++;
        }
        if (index == needle_length) return 1;
    }
    return 0;
}

PyObject *
wreath_cacheable_headers(PyObject *Py_UNUSED(self), PyObject *headers_object)
{
    PyObject *headers = PySequence_Fast(
        headers_object, "response headers must be a sequence");
    if (headers == NULL) return NULL;
    PyObject **items = PySequence_Fast_ITEMS(headers);
    Py_ssize_t count = PySequence_Fast_GET_SIZE(headers);
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *pair = items[index];
        PyObject *name = NULL, *value = NULL;
        if (PyTuple_Check(pair) && PyTuple_GET_SIZE(pair) == 2) {
            name = PyTuple_GET_ITEM(pair, 0); value = PyTuple_GET_ITEM(pair, 1);
        } else if (PyList_Check(pair) && PyList_GET_SIZE(pair) == 2) {
            name = PyList_GET_ITEM(pair, 0); value = PyList_GET_ITEM(pair, 1);
        }
        if (name == NULL || !PyBytes_Check(name) || !PyBytes_Check(value)) {
            PyErr_SetString(PyExc_TypeError,
                            "response headers must be (bytes, bytes) pairs");
            Py_DECREF(headers);
            return NULL;
        }
        const char *name_data = PyBytes_AS_STRING(name);
        Py_ssize_t name_length = PyBytes_GET_SIZE(name);
        const char *value_data = PyBytes_AS_STRING(value);
        Py_ssize_t value_length = PyBytes_GET_SIZE(value);
        if (header_name_is(name_data, name_length, "set-cookie")) {
            Py_DECREF(headers); Py_RETURN_FALSE;
        }
        if (header_name_is(name_data, name_length, "vary")) {
            Py_ssize_t start = 0, end = value_length;
            while (start < end && (value_data[start] == ' ' || value_data[start] == '\t' ||
                   value_data[start] == '\n' || value_data[start] == '\r' ||
                   value_data[start] == '\v' || value_data[start] == '\f')) start++;
            while (end > start && (value_data[end - 1] == ' ' || value_data[end - 1] == '\t' ||
                   value_data[end - 1] == '\n' || value_data[end - 1] == '\r' ||
                   value_data[end - 1] == '\v' || value_data[end - 1] == '\f')) end--;
            if (start != end) { Py_DECREF(headers); Py_RETURN_FALSE; }
        }
        if (header_name_is(name_data, name_length, "cache-control") &&
            (header_contains_ci(value_data, value_length, "no-store") ||
             header_contains_ci(value_data, value_length, "private"))) {
            Py_DECREF(headers); Py_RETURN_FALSE;
        }
    }
    Py_DECREF(headers);
    Py_RETURN_TRUE;
}

typedef struct {
    uint64_t hash[8];
    uint64_t count[2];
    uint8_t block[128];
    size_t used;
} DataBlake2b;

static const uint64_t data_blake2b_iv[8] = {
    UINT64_C(0x6a09e667f3bcc908), UINT64_C(0xbb67ae8584caa73b),
    UINT64_C(0x3c6ef372fe94f82b), UINT64_C(0xa54ff53a5f1d36f1),
    UINT64_C(0x510e527fade682d1), UINT64_C(0x9b05688c2b3e6c1f),
    UINT64_C(0x1f83d9abfb41bd6b), UINT64_C(0x5be0cd19137e2179),
};

static const uint8_t data_blake2b_sigma[12][16] = {
    {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15},
    {14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3},
    {11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4},
    {7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8},
    {9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13},
    {2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9},
    {12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11},
    {13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10},
    {6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5},
    {10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0},
    {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15},
    {14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3},
};

static uint64_t
data_load64(const uint8_t *source)
{
    uint64_t value = 0;
    for (unsigned shift = 0; shift < 64; shift += 8)
        value |= (uint64_t)*source++ << shift;
    return value;
}

static uint64_t
data_rotr64(uint64_t value, unsigned shift)
{
    return (value >> shift) | (value << (64 - shift));
}

static void
data_blake2b_mix(uint64_t state[16], unsigned a, unsigned b, unsigned c,
                 unsigned d, uint64_t x, uint64_t y)
{
    state[a] = state[a] + state[b] + x;
    state[d] = data_rotr64(state[d] ^ state[a], 32);
    state[c] += state[d];
    state[b] = data_rotr64(state[b] ^ state[c], 24);
    state[a] = state[a] + state[b] + y;
    state[d] = data_rotr64(state[d] ^ state[a], 16);
    state[c] += state[d];
    state[b] = data_rotr64(state[b] ^ state[c], 63);
}

static void
data_blake2b_compress(DataBlake2b *state, int final)
{
    uint64_t message[16];
    uint64_t work[16];
    for (size_t index = 0; index < 16; index++)
        message[index] = data_load64(state->block + index * 8);
    for (size_t index = 0; index < 8; index++) {
        work[index] = state->hash[index];
        work[index + 8] = data_blake2b_iv[index];
    }
    work[12] ^= state->count[0];
    work[13] ^= state->count[1];
    if (final) work[14] = ~work[14];

    for (size_t round = 0; round < 12; round++) {
        const uint8_t *order = data_blake2b_sigma[round];
        data_blake2b_mix(work, 0, 4, 8, 12,
                         message[order[0]], message[order[1]]);
        data_blake2b_mix(work, 1, 5, 9, 13,
                         message[order[2]], message[order[3]]);
        data_blake2b_mix(work, 2, 6, 10, 14,
                         message[order[4]], message[order[5]]);
        data_blake2b_mix(work, 3, 7, 11, 15,
                         message[order[6]], message[order[7]]);
        data_blake2b_mix(work, 0, 5, 10, 15,
                         message[order[8]], message[order[9]]);
        data_blake2b_mix(work, 1, 6, 11, 12,
                         message[order[10]], message[order[11]]);
        data_blake2b_mix(work, 2, 7, 8, 13,
                         message[order[12]], message[order[13]]);
        data_blake2b_mix(work, 3, 4, 9, 14,
                         message[order[14]], message[order[15]]);
    }
    for (size_t index = 0; index < 8; index++)
        state->hash[index] ^= work[index] ^ work[index + 8];
}

static void
data_blake2b_count(DataBlake2b *state, uint64_t amount)
{
    uint64_t previous = state->count[0];
    state->count[0] += amount;
    if (state->count[0] < previous) state->count[1]++;
}

static void
data_blake2b_update(DataBlake2b *state, const uint8_t *input, size_t length)
{
    size_t available = sizeof(state->block) - state->used;
    if (length > available) {
        memcpy(state->block + state->used, input, available);
        data_blake2b_count(state, sizeof(state->block));
        data_blake2b_compress(state, 0);
        input += available;
        length -= available;
        state->used = 0;
        while (length > sizeof(state->block)) {
            memcpy(state->block, input, sizeof(state->block));
            data_blake2b_count(state, sizeof(state->block));
            data_blake2b_compress(state, 0);
            input += sizeof(state->block);
            length -= sizeof(state->block);
        }
    }
    memcpy(state->block + state->used, input, length);
    state->used += length;
}

static void
data_blake2b_finish(DataBlake2b *state, uint8_t digest[16])
{
    data_blake2b_count(state, (uint64_t)state->used);
    memset(state->block + state->used, 0, sizeof(state->block) - state->used);
    data_blake2b_compress(state, 1);
    for (size_t index = 0; index < 16; index++)
        digest[index] = (uint8_t)(state->hash[index / 8] >> (8 * (index % 8)));
}

typedef struct {
    uint8_t *data;
    size_t length;
    size_t capacity;
} DataSyncPayload;

static void
data_sync_payload_clear(DataSyncPayload *payload)
{
    PyMem_Free(payload->data);
    *payload = (DataSyncPayload){0};
}

static int
data_sync_payload_append(DataSyncPayload *payload,
                         const uint8_t *source, size_t length)
{
    if (length > SIZE_MAX - payload->length) {
        PyErr_NoMemory();
        return -1;
    }
    size_t needed = payload->length + length;
    if (needed > payload->capacity) {
        size_t capacity = payload->capacity != 0 ? payload->capacity : 128;
        while (capacity < needed) {
            if (capacity > SIZE_MAX / 2) {
                capacity = needed;
                break;
            }
            capacity *= 2;
        }
        uint8_t *grown = PyMem_Realloc(payload->data, capacity);
        if (grown == NULL) {
            PyErr_NoMemory();
            return -1;
        }
        payload->data = grown;
        payload->capacity = capacity;
    }
    memcpy(payload->data + payload->length, source, length);
    payload->length = needed;
    return 0;
}

static int
data_sync_payload_build(PyObject *values, DataSyncPayload *payload)
{
    static const uint8_t separator = 0;
    PyObject *keys = PySequence_List(values);
    if (keys == NULL) return -1;
    if (PyList_Sort(keys) < 0) {
        Py_DECREF(keys);
        return -1;
    }

    PyObject **items = PySequence_Fast_ITEMS(keys);
    Py_ssize_t count = PyList_GET_SIZE(keys);
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *name_bytes = PyUnicode_AsEncodedString(
            items[index], "utf-8", "strict");
        if (name_bytes == NULL) goto error;
        if (data_sync_payload_append(
                payload, (const uint8_t *)PyBytes_AS_STRING(name_bytes),
                (size_t)PyBytes_GET_SIZE(name_bytes)) < 0 ||
            data_sync_payload_append(payload, &separator, 1) < 0) {
            Py_DECREF(name_bytes);
            goto error;
        }
        Py_DECREF(name_bytes);

        PyObject *value = PyObject_GetItem(values, items[index]);
        if (value == NULL) goto error;
        PyObject *representation = PyObject_Repr(value);
        Py_DECREF(value);
        if (representation == NULL) goto error;
        PyObject *representation_bytes = PyUnicode_AsEncodedString(
            representation, "utf-8", "replace");
        Py_DECREF(representation);
        if (representation_bytes == NULL) goto error;
        if (data_sync_payload_append(
                payload,
                (const uint8_t *)PyBytes_AS_STRING(representation_bytes),
                (size_t)PyBytes_GET_SIZE(representation_bytes)) < 0 ||
            data_sync_payload_append(payload, &separator, 1) < 0) {
            Py_DECREF(representation_bytes);
            goto error;
        }
        Py_DECREF(representation_bytes);
    }
    Py_DECREF(keys);
    return 0;

error:
    Py_DECREF(keys);
    return -1;
}

static void
data_sync_digest_payload(const DataSyncPayload *payload, uint8_t digest[16])
{
    DataBlake2b state = {0};
    memcpy(state.hash, data_blake2b_iv, sizeof(state.hash));
    state.hash[0] ^= UINT64_C(0x01010000) ^ UINT64_C(16);
    if (payload->length != 0)
        data_blake2b_update(&state, payload->data, payload->length);
    data_blake2b_finish(&state, digest);
}

#if defined(WREATH_HAVE_AVX2)
#define DATA_BLAKE2B4_ROTR(value, shift) _mm256_or_si256(                    \
    _mm256_srli_epi64((value), (shift)),                                    \
    _mm256_slli_epi64((value), 64 - (shift)))

WREATH_TARGET_AVX2 static inline void
data_blake2b4_mix(__m256i state[16], unsigned a, unsigned b, unsigned c,
                  unsigned d, __m256i x, __m256i y)
{
    state[a] = _mm256_add_epi64(_mm256_add_epi64(state[a], state[b]), x);
    state[d] = DATA_BLAKE2B4_ROTR(_mm256_xor_si256(state[d], state[a]), 32);
    state[c] = _mm256_add_epi64(state[c], state[d]);
    state[b] = DATA_BLAKE2B4_ROTR(_mm256_xor_si256(state[b], state[c]), 24);
    state[a] = _mm256_add_epi64(_mm256_add_epi64(state[a], state[b]), y);
    state[d] = DATA_BLAKE2B4_ROTR(_mm256_xor_si256(state[d], state[a]), 16);
    state[c] = _mm256_add_epi64(state[c], state[d]);
    state[b] = DATA_BLAKE2B4_ROTR(_mm256_xor_si256(state[b], state[c]), 63);
}

/* Four independent messages occupy the four uint64 lanes.  Python mappings
 * have already been materialized into operation-owned byte buffers before
 * this point; the compression rounds touch only native state. */
WREATH_TARGET_AVX2 static void
data_sync_digest4(const DataSyncPayload payloads[4], uint8_t digests[4][16])
{
    __m256i hash[8];
    size_t block_counts[4];
    size_t max_blocks = 1;
    for (unsigned lane = 0; lane < 4; lane++) {
        block_counts[lane] = payloads[lane].length / 128 +
                             (payloads[lane].length % 128 != 0);
        if (block_counts[lane] == 0) block_counts[lane] = 1;
        if (block_counts[lane] > max_blocks) max_blocks = block_counts[lane];
    }
    for (unsigned index = 0; index < 8; index++) {
        uint64_t initial = data_blake2b_iv[index];
        if (index == 0) initial ^= UINT64_C(0x01010000) ^ UINT64_C(16);
        hash[index] = _mm256_set1_epi64x((long long)initial);
    }

    for (size_t block_index = 0; block_index < max_blocks; block_index++) {
        uint8_t blocks[4][128] = {{0}};
        uint64_t active_values[4] = {0};
        uint64_t count_values[4] = {0};
        uint64_t final_values[4] = {0};
        for (unsigned lane = 0; lane < 4; lane++) {
            if (block_index >= block_counts[lane]) continue;
            size_t offset = block_index * 128;
            size_t remaining = payloads[lane].length - offset;
            size_t take = remaining < 128 ? remaining : 128;
            if (take != 0)
                memcpy(blocks[lane], payloads[lane].data + offset, take);
            active_values[lane] = UINT64_MAX;
            count_values[lane] = (uint64_t)(offset + take);
            if (block_index + 1 == block_counts[lane])
                final_values[lane] = UINT64_MAX;
        }

        __m256i message[16];
        __m256i work[16];
        for (unsigned index = 0; index < 16; index++) {
            message[index] = _mm256_set_epi64x(
                (long long)data_load64(blocks[3] + index * 8),
                (long long)data_load64(blocks[2] + index * 8),
                (long long)data_load64(blocks[1] + index * 8),
                (long long)data_load64(blocks[0] + index * 8));
        }
        for (unsigned index = 0; index < 8; index++) {
            work[index] = hash[index];
            work[index + 8] = _mm256_set1_epi64x(
                (long long)data_blake2b_iv[index]);
        }
        work[12] = _mm256_xor_si256(
            work[12], _mm256_loadu_si256((const __m256i *)count_values));
        work[14] = _mm256_xor_si256(
            work[14], _mm256_loadu_si256((const __m256i *)final_values));

        for (unsigned round = 0; round < 12; round++) {
            const uint8_t *order = data_blake2b_sigma[round];
            data_blake2b4_mix(work, 0, 4, 8, 12,
                              message[order[0]], message[order[1]]);
            data_blake2b4_mix(work, 1, 5, 9, 13,
                              message[order[2]], message[order[3]]);
            data_blake2b4_mix(work, 2, 6, 10, 14,
                              message[order[4]], message[order[5]]);
            data_blake2b4_mix(work, 3, 7, 11, 15,
                              message[order[6]], message[order[7]]);
            data_blake2b4_mix(work, 0, 5, 10, 15,
                              message[order[8]], message[order[9]]);
            data_blake2b4_mix(work, 1, 6, 11, 12,
                              message[order[10]], message[order[11]]);
            data_blake2b4_mix(work, 2, 7, 8, 13,
                              message[order[12]], message[order[13]]);
            data_blake2b4_mix(work, 3, 4, 9, 14,
                              message[order[14]], message[order[15]]);
        }
        __m256i active = _mm256_loadu_si256((const __m256i *)active_values);
        for (unsigned index = 0; index < 8; index++) {
            __m256i updated = _mm256_xor_si256(
                hash[index], _mm256_xor_si256(work[index], work[index + 8]));
            hash[index] = _mm256_blendv_epi8(hash[index], updated, active);
        }
    }

    uint64_t words[2][4];
    _mm256_storeu_si256((__m256i *)words[0], hash[0]);
    _mm256_storeu_si256((__m256i *)words[1], hash[1]);
    for (unsigned lane = 0; lane < 4; lane++)
        for (unsigned index = 0; index < 16; index++)
            digests[lane][index] = (uint8_t)(
                words[index / 8][lane] >> (8 * (index % 8)));
}
#undef DATA_BLAKE2B4_ROTR
#endif

static int
data_sync_digest(PyObject *values, uint8_t digest[16])
{
    DataSyncPayload payload = {0};
    if (data_sync_payload_build(values, &payload) < 0) {
        data_sync_payload_clear(&payload);
        return -1;
    }
    data_sync_digest_payload(&payload, digest);
    data_sync_payload_clear(&payload);
    return 0;
}

PyObject *
wreath_sync_version(PyObject *Py_UNUSED(self), PyObject *values)
{
    static const char hexadecimal[] = "0123456789abcdef";
    uint8_t digest[16];
    char encoded[32];
    if (data_sync_digest(values, digest) < 0) return NULL;
    for (size_t index = 0; index < sizeof(digest); index++) {
        encoded[index * 2] = hexadecimal[digest[index] >> 4];
        encoded[index * 2 + 1] = hexadecimal[digest[index] & 15];
    }
    return PyUnicode_FromStringAndSize(encoded, (Py_ssize_t)sizeof(encoded));
}

typedef struct {
    PyObject *key;
    Py_hash_t hash;
    uint8_t digest[16];
} DataSyncEntry;

typedef struct {
    Py_ssize_t count;
    DataSyncEntry *entries;
} DataSyncState;

#define DATA_SYNC_STATE_CAPSULE "wreath.sync.state"

static DataSyncState *
data_sync_state_from(PyObject *capsule)
{
    return PyCapsule_GetPointer(capsule, DATA_SYNC_STATE_CAPSULE);
}

static void
data_sync_state_free(DataSyncState *state)
{
    if (state == NULL) return;
    for (Py_ssize_t index = 0; index < state->count; index++)
        Py_XDECREF(state->entries[index].key);
    PyMem_Free(state->entries);
    PyMem_Free(state);
}

static void
data_sync_state_destroy(PyObject *capsule)
{
    DataSyncState *state = data_sync_state_from(capsule);
    if (state == NULL) {
        PyErr_Clear();
        return;
    }
    data_sync_state_free(state);
}

static DataSyncState *
data_sync_state_build(PyObject *source)
{
    PyObject *rows = PySequence_Fast(source, "sync rows must be a sequence");
    DataSyncState *state = NULL;
    if (rows == NULL) return NULL;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(rows);
    if ((size_t)count > SIZE_MAX / sizeof(DataSyncEntry)) {
        Py_DECREF(rows);
        PyErr_NoMemory();
        return NULL;
    }
    state = PyMem_Calloc(1, sizeof(*state));
    if (state == NULL) goto memory;
    state->entries = count == 0 ? NULL : PyMem_Calloc(
        (size_t)count, sizeof(*state->entries));
    if (count != 0 && state->entries == NULL) goto memory;
    state->count = count;
    for (Py_ssize_t base = 0; base < count; base += 4) {
        DataSyncPayload payloads[4] = {{0}};
        Py_ssize_t lanes = count - base < 4 ? count - base : 4;
        int failed = 0;
        for (Py_ssize_t lane = 0; lane < lanes; lane++) {
            Py_ssize_t index = base + lane;
            PyObject *row = PySequence_Fast_GET_ITEM(rows, index);
            PyObject *key;
            PyObject *values;
            if (!PyDict_Check(row)) {
                PyErr_Format(PyExc_TypeError,
                             "sync row %zd must be a materialized mapping", index);
                failed = 1;
                break;
            }
            key = PyDict_GetItemString(row, "key");
            values = PyDict_GetItemString(row, "values");
            if (key == NULL || values == NULL || !PyMapping_Check(values)) {
                PyErr_Format(PyExc_TypeError,
                             "sync row %zd needs key and mapping values", index);
                failed = 1;
                break;
            }
            state->entries[index].hash = PyObject_Hash(key);
            if (state->entries[index].hash == -1 ||
                data_sync_payload_build(values, &payloads[lane]) < 0) {
                failed = 1;
                break;
            }
            state->entries[index].key = Py_NewRef(key);
        }
        if (!failed) {
#if defined(WREATH_HAVE_AVX2)
            if (lanes == 4 && wreath_simd_has_avx2()) {
                uint8_t digests[4][16];
                data_sync_digest4(payloads, digests);
                for (Py_ssize_t lane = 0; lane < 4; lane++)
                    memcpy(state->entries[base + lane].digest,
                           digests[lane], 16);
            }
            else
#endif
            {
                for (Py_ssize_t lane = 0; lane < lanes; lane++)
                    data_sync_digest_payload(
                        &payloads[lane], state->entries[base + lane].digest);
            }
        }
        for (Py_ssize_t lane = 0; lane < 4; lane++)
            data_sync_payload_clear(&payloads[lane]);
        if (failed) goto error;
    }
    Py_DECREF(rows);
    return state;

memory:
    PyErr_NoMemory();
error:
    Py_DECREF(rows);
    data_sync_state_free(state);
    return NULL;
}

PyObject *
wreath_sync_state(PyObject *Py_UNUSED(self), PyObject *rows)
{
    DataSyncState *state = data_sync_state_build(rows);
    PyObject *capsule;
    if (state == NULL) return NULL;
    capsule = PyCapsule_New(
        state, DATA_SYNC_STATE_CAPSULE, data_sync_state_destroy);
    if (capsule == NULL) data_sync_state_free(state);
    return capsule;
}

static Py_ssize_t
data_sync_table_size(Py_ssize_t count)
{
    Py_ssize_t size = 8;
    if (count > PY_SSIZE_T_MAX / 2) return -1;
    while (size < count * 2) {
        if (size > PY_SSIZE_T_MAX / 2) return -1;
        size *= 2;
    }
    return size;
}

static Py_ssize_t
data_sync_lookup(DataSyncState *state, Py_ssize_t *slots, Py_ssize_t size,
                 PyObject *key, Py_hash_t hash)
{
    size_t slot = (size_t)hash & ((size_t)size - 1);
    for (;;) {
        Py_ssize_t index = slots[slot];
        if (index < 0) return -1;
        if (state->entries[index].hash == hash) {
            int equal = PyObject_RichCompareBool(
                state->entries[index].key, key, Py_EQ);
            if (equal < 0) return -2;
            if (equal) return index;
        }
        slot = (slot + 1) & ((size_t)size - 1);
    }
}

PyObject *
wreath_sync_state_diff(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *old_capsule, *source;
    PyObject *rows = NULL, *new_capsule = NULL, *upserted = NULL;
    PyObject *removed = NULL, *removed_tuple = NULL, *result = NULL;
    DataSyncState *old_state, *new_state = NULL;
    Py_ssize_t *slots = NULL;
    unsigned char *seen = NULL;
    if (!PyArg_ParseTuple(args, "OO:sync_state_diff", &old_capsule, &source))
        return NULL;
    old_state = data_sync_state_from(old_capsule);
    if (old_state == NULL) return NULL;
    rows = PySequence_Fast(source, "sync rows must be a sequence");
    if (rows == NULL) goto error;
    new_state = data_sync_state_build(rows);
    if (new_state == NULL) goto error;
    Py_ssize_t table_size = data_sync_table_size(old_state->count);
    if (table_size < 0) goto memory;
    slots = PyMem_Malloc((size_t)table_size * sizeof(*slots));
    seen = PyMem_Calloc(
        (size_t)(old_state->count > 0 ? old_state->count : 1), sizeof(*seen));
    if (slots == NULL || seen == NULL) goto memory;
    for (Py_ssize_t slot = 0; slot < table_size; slot++) slots[slot] = -1;
    for (Py_ssize_t index = 0; index < old_state->count; index++) {
        size_t slot = (size_t)old_state->entries[index].hash &
                      ((size_t)table_size - 1);
        while (slots[slot] >= 0) slot = (slot + 1) & ((size_t)table_size - 1);
        slots[slot] = index;
    }
    upserted = PyList_New(0);
    removed = PyList_New(0);
    if (upserted == NULL || removed == NULL) goto error;
    for (Py_ssize_t index = 0; index < new_state->count; index++) {
        DataSyncEntry *entry = &new_state->entries[index];
        Py_ssize_t previous = data_sync_lookup(
            old_state, slots, table_size, entry->key, entry->hash);
        if (previous == -2) goto error;
        if (previous >= 0) seen[previous] = 1;
        if ((previous < 0 || memcmp(
                 old_state->entries[previous].digest, entry->digest, 16) != 0) &&
            PyList_Append(upserted, PySequence_Fast_GET_ITEM(rows, index)) < 0)
            goto error;
    }
    for (Py_ssize_t index = 0; index < old_state->count; index++)
        if (!seen[index] && PyList_Append(
                removed, old_state->entries[index].key) < 0) goto error;
    if (PyList_Sort(removed) < 0) goto error;
    new_capsule = PyCapsule_New(
        new_state, DATA_SYNC_STATE_CAPSULE, data_sync_state_destroy);
    if (new_capsule == NULL) goto error;
    new_state = NULL;
    removed_tuple = PyList_AsTuple(removed);
    if (removed_tuple == NULL) goto error;
    PyObject *upserted_tuple = PyList_AsTuple(upserted);
    if (upserted_tuple == NULL) goto error;
    result = PyTuple_Pack(3, new_capsule, upserted_tuple, removed_tuple);
    Py_DECREF(upserted_tuple);

error:
    PyMem_Free(slots);
    PyMem_Free(seen);
    data_sync_state_free(new_state);
    Py_XDECREF(rows);
    Py_XDECREF(new_capsule);
    Py_XDECREF(upserted);
    Py_XDECREF(removed);
    Py_XDECREF(removed_tuple);
    return result;

memory:
    PyErr_NoMemory();
    goto error;
}

PyObject *
wreath_sync_state_keys(PyObject *Py_UNUSED(self), PyObject *capsule)
{
    DataSyncState *state = data_sync_state_from(capsule);
    PyObject *keys, *result;
    if (state == NULL) return NULL;
    keys = PySet_New(NULL);
    if (keys == NULL) return NULL;
    for (Py_ssize_t index = 0; index < state->count; index++)
        if (PySet_Add(keys, state->entries[index].key) < 0) {
            Py_DECREF(keys);
            return NULL;
        }
    result = PyFrozenSet_New(keys);
    Py_DECREF(keys);
    return result;
}

PyObject *
wreath_sync_state_size(PyObject *Py_UNUSED(self), PyObject *capsule)
{
    DataSyncState *state = data_sync_state_from(capsule);
    if (state == NULL) return NULL;
    return PyLong_FromSsize_t(state->count);
}
