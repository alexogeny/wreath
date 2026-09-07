/* Request correlation and timing primitives.
 *
 * Both functions exist to keep untrusted bytes out of places that concatenate
 * without escaping: a request id is echoed into a response header and (later)
 * into access logs and trace attributes, so it is validated against a strict
 * charset rather than sanitized. Rejecting is cheaper and safer than rewriting;
 * the caller mints a fresh id instead.
 */
#include "wreathcore.h"
#include "bytes_writer.h"

#include <cpython/longintrepr.h>
#include <math.h>
#include <stdint.h>

typedef enum {
    OBS_SUBSYSTEM,
    OBS_INSTANCE,
    OBS_VALUES,
    OBS_ROUTE_ID,
    OBS_COUNT,
    OBS_ERRORS,
    OBS_DURATION_US_SUM,
    OBS_DURATION_US_MAX,
    OBS_BUCKETS,
    OBS_ROUTES,
    OBS_ASSEMBLED,
    OBS_PENDING,
    OBS_LOSS,
    OBS_NAME,
    OBS_GAUGES,
    OBS_ATTR_COUNT,
} ObservabilityAttr;

static PyObject *observability_attr_names[OBS_ATTR_COUNT];

int
wreath_observability_ready(void)
{
    static const char *names[OBS_ATTR_COUNT] = {
        "subsystem", "instance", "values", "route_id", "count", "errors",
        "duration_us_sum", "duration_us_max", "buckets", "routes",
        "assembled", "pending", "loss", "name", "gauges",
    };
    for (int index = 0; index < OBS_ATTR_COUNT; index++) {
        observability_attr_names[index] = PyUnicode_InternFromString(names[index]);
        if (observability_attr_names[index] == NULL) {
            while (index-- != 0) Py_CLEAR(observability_attr_names[index]);
            return -1;
        }
    }
    return 0;
}

static inline PyObject *
observability_getattr(PyObject *object, ObservabilityAttr attribute)
{
    return PyObject_GetAttr(object, observability_attr_names[attribute]);
}

/* Unreserved characters only: enough for UUIDs, W3C trace ids, and ULIDs, while
 * excluding every byte with meaning in a header, a log line, or a shell. */
static inline int
request_id_char(uint8_t c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
           (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
}

PyObject *
wreath_request_id_valid(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer view;
    Py_ssize_t max_len;
    const uint8_t *data;
    int ok = 1;

    if (!PyArg_ParseTuple(args, "y*n:request_id_valid", &view, &max_len)) {
        return NULL;
    }
    data = view.buf;
    if (view.len == 0 || view.len > max_len) {
        ok = 0;
    }
    else {
        for (Py_ssize_t i = 0; i < view.len; i++) {
            if (!request_id_char(data[i])) {
                ok = 0;
                break;
            }
        }
    }
    PyBuffer_Release(&view);
    return PyBool_FromLong(ok);
}

/* Format one Server-Timing metric: seconds in, "name;dur=12.345" out.
 *
 * The header is expressed in milliseconds with three decimals, which is the
 * resolution browsers display and enough to keep sub-microsecond handlers from
 * rendering as a flat zero. */
PyObject *
wreath_format_server_timing(PyObject *Py_UNUSED(self), PyObject *args)
{
    Py_buffer name;
    double seconds;
    char buf[128];
    int written;

    if (!PyArg_ParseTuple(args, "y*d:format_server_timing", &name, &seconds)) {
        return NULL;
    }
    /* The name is a configured constant validated by the caller; the bound here
     * only guarantees the snprintf below cannot be truncated into a surprise. */
    if (name.len < 1 || name.len > 64) {
        PyBuffer_Release(&name);
        PyErr_SetString(PyExc_ValueError, "metric name must be 1-64 bytes");
        return NULL;
    }
    written = snprintf(
        buf, sizeof(buf), "%.*s;dur=%.3f", (int)name.len, (const char *)name.buf,
        seconds * 1000.0);
    PyBuffer_Release(&name);
    if (written < 0 || (size_t)written >= sizeof(buf)) {
        PyErr_SetString(PyExc_ValueError, "server-timing metric did not fit");
        return NULL;
    }
    return PyBytes_FromStringAndSize(buf, written);
}

typedef struct {
    char *labels;
    Py_ssize_t labels_length;
    PyObject *count;
    PyObject *errors;
    PyObject *duration_sum;
    PyObject *duration_max;
    PyObject *buckets;
} PromRoute;

static void
prom_routes_clear(PromRoute *routes, Py_ssize_t count)
{
    if (routes == NULL) return;
    for (Py_ssize_t index = 0; index < count; index++) {
        PyMem_Free(routes[index].labels);
        Py_XDECREF(routes[index].buckets);
        Py_XDECREF(routes[index].duration_max);
        Py_XDECREF(routes[index].duration_sum);
        Py_XDECREF(routes[index].errors);
        Py_XDECREF(routes[index].count);
    }
    PyMem_Free(routes);
}

typedef struct {
    char *data;
    Py_ssize_t length;
    Py_ssize_t capacity;
} PromLabelBuffer;

static void
prom_label_buffer_clear(PromLabelBuffer *buffer)
{
    PyMem_Free(buffer->data);
    buffer->data = NULL;
    buffer->length = 0;
    buffer->capacity = 0;
}

static int
prom_label_buffer_reserve(PromLabelBuffer *buffer, Py_ssize_t extra)
{
    if (extra < 0 || buffer->length > PY_SSIZE_T_MAX - extra) {
        PyErr_NoMemory();
        return -1;
    }
    Py_ssize_t needed = buffer->length + extra;
    if (needed <= buffer->capacity) return 0;
    Py_ssize_t capacity = buffer->capacity == 0 ? 64 : buffer->capacity;
    while (capacity < needed) {
        if (capacity > PY_SSIZE_T_MAX / 2) {
            capacity = needed;
            break;
        }
        capacity *= 2;
    }
    char *grown = PyMem_Realloc(buffer->data, (size_t)capacity);
    if (grown == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    buffer->data = grown;
    buffer->capacity = capacity;
    return 0;
}

static int
prom_label_buffer_write(PromLabelBuffer *buffer, const char *data,
                        Py_ssize_t length)
{
    if (prom_label_buffer_reserve(buffer, length) < 0) return -1;
    memcpy(buffer->data + buffer->length, data, (size_t)length);
    buffer->length += length;
    return 0;
}

static int
prom_label_buffer_byte(PromLabelBuffer *buffer, char value)
{
    if (prom_label_buffer_reserve(buffer, 1) < 0) return -1;
    buffer->data[buffer->length++] = value;
    return 0;
}

static int
prom_label_buffer_value(PromLabelBuffer *buffer, const char *data,
                        Py_ssize_t length)
{
    Py_ssize_t start = 0;
    for (Py_ssize_t index = 0; index < length; index++) {
        const char *escape;
        if (data[index] == '\\') escape = "\\\\";
        else if (data[index] == '"') escape = "\\\"";
        else if (data[index] == '\n') escape = "\\n";
        else continue;
        if (prom_label_buffer_write(buffer, data + start, index - start) < 0 ||
            prom_label_buffer_write(buffer, escape, 2) < 0) return -1;
        start = index + 1;
    }
    return prom_label_buffer_write(buffer, data + start, length - start);
}

static inline int
prom_label_char(Py_UCS4 character)
{
    return (character >= 'a' && character <= 'z') ||
           (character >= 'A' && character <= 'Z') ||
           (character >= '0' && character <= '9') || character == '_';
}

static inline int
prom_label_lead(Py_UCS4 character)
{
    return (character >= 'a' && character <= 'z') ||
           (character >= 'A' && character <= 'Z') || character == '_';
}

static int
prom_label_buffer_name(PromLabelBuffer *buffer, PyObject *name)
{
    if (!PyUnicode_Check(name)) {
        PyErr_SetString(PyExc_TypeError, "Prometheus label names must be strings");
        return -1;
    }
    Py_ssize_t length = PyUnicode_GetLength(name);
    if (length < 0) return -1;
    if (length == 0) return prom_label_buffer_byte(buffer, '_');
    Py_UCS4 first = PyUnicode_ReadChar(name, 0);
    if (first == (Py_UCS4)-1 && PyErr_Occurred()) return -1;
    if (!prom_label_lead(first) && prom_label_buffer_byte(buffer, '_') < 0)
        return -1;
    for (Py_ssize_t index = 0; index < length; index++) {
        Py_UCS4 character = PyUnicode_ReadChar(name, index);
        if (character == (Py_UCS4)-1 && PyErr_Occurred()) return -1;
        if (prom_label_buffer_byte(
                buffer, prom_label_char(character) ? (char)character : '_') < 0)
            return -1;
    }
    return 0;
}

static int
prom_label_buffer_item(PromLabelBuffer *buffer, PyObject *key, PyObject *value,
                       int first)
{
    PyObject *text = NULL;
    const char *data;
    Py_ssize_t length;
    if (!first && prom_label_buffer_byte(buffer, ',') < 0) return -1;
    if (prom_label_buffer_name(buffer, key) < 0 ||
        prom_label_buffer_write(buffer, "=\"", 2) < 0) return -1;
    text = PyObject_Str(value);
    if (text == NULL) return -1;
    data = PyUnicode_AsUTF8AndSize(text, &length);
    if (data == NULL || prom_label_buffer_value(buffer, data, length) < 0 ||
        prom_label_buffer_byte(buffer, '"') < 0) {
        Py_DECREF(text);
        return -1;
    }
    Py_DECREF(text);
    return 0;
}

static int
prom_route_default_labels(PromRoute *route, PyObject *route_id)
{
    PromLabelBuffer buffer = {0};
    PyObject *text = PyObject_Str(route_id);
    const char *data;
    Py_ssize_t length;
    if (text == NULL) return -1;
    data = PyUnicode_AsUTF8AndSize(text, &length);
    if (data == NULL ||
        prom_label_buffer_write(&buffer, "route_id=\"", 10) < 0 ||
        prom_label_buffer_value(&buffer, data, length) < 0 ||
        prom_label_buffer_byte(&buffer, '"') < 0) {
        Py_DECREF(text);
        prom_label_buffer_clear(&buffer);
        return -1;
    }
    Py_DECREF(text);
    route->labels = buffer.data;
    route->labels_length = buffer.length;
    return 0;
}

static int
prom_route_custom_labels(PromRoute *route, PyObject *labels)
{
    PromLabelBuffer buffer = {0};
    PyObject *iterator = PyObject_GetIter(labels);
    PyObject *key;
    int first = 1;
    if (iterator == NULL) return -1;
    while ((key = PyIter_Next(iterator)) != NULL) {
        PyObject *value = PyObject_GetItem(labels, key);
        if (value == NULL ||
            prom_label_buffer_item(&buffer, key, value, first) < 0) {
            Py_XDECREF(value);
            Py_DECREF(key);
            Py_DECREF(iterator);
            prom_label_buffer_clear(&buffer);
            return -1;
        }
        first = 0;
        Py_DECREF(value);
        Py_DECREF(key);
    }
    Py_DECREF(iterator);
    if (PyErr_Occurred()) {
        prom_label_buffer_clear(&buffer);
        return -1;
    }
    route->labels = buffer.data;
    route->labels_length = buffer.length;
    return 0;
}

static int
prom_route_labels(PromRoute *route, PyObject *route_id,
                  PyObject *resolver, int resolver_is_mapping)
{
    PyObject *labels;
    int truth;
    if (resolver == Py_None) return prom_route_default_labels(route, route_id);
    labels = resolver_is_mapping
        ? PyObject_CallMethod(resolver, "get", "O", route_id)
        : PyObject_CallOneArg(resolver, route_id);
    if (labels == NULL) return -1;
    truth = PyObject_IsTrue(labels);
    if (truth < 0) {
        Py_DECREF(labels);
        return -1;
    }
    if (!truth) {
        Py_DECREF(labels);
        return prom_route_default_labels(route, route_id);
    }
    int result = prom_route_custom_labels(route, labels);
    Py_DECREF(labels);
    return result;
}

static PyObject *
prom_number(PyObject *value)
{
    if (PyBool_Check(value))
        return PyUnicode_FromString(value == Py_True ? "1" : "0");
    if (PyLong_Check(value)) return PyObject_Str(value);
    if (PyFloat_Check(value)) {
        double number = PyFloat_AS_DOUBLE(value);
        double whole;
        if (isinf(number)) return PyUnicode_FromString(number > 0 ? "+Inf" : "-Inf");
        if (isnan(number)) return PyUnicode_FromString("NaN");
        if (modf(number, &whole) == 0.0 && fabs(number) < 1e16) {
            PyObject *integer = PyLong_FromDouble(number);
            PyObject *text;
            if (integer == NULL) return NULL;
            text = PyObject_Str(integer);
            Py_DECREF(integer);
            return text;
        }
        return PyObject_Repr(value);
    }
    return PyObject_Str(value);
}

static PyObject *
prom_double(double value)
{
    PyObject *number = PyFloat_FromDouble(value);
    PyObject *result;
    if (number == NULL) return NULL;
    result = prom_number(number);
    Py_DECREF(number);
    return result;
}

static int
prom_write_unicode(WreathBytesWriter *writer, PyObject *text)
{
    const char *data;
    Py_ssize_t length;
    data = PyUnicode_AsUTF8AndSize(text, &length);
    if (data == NULL) return -1;
    return wreath_writer_write(writer, data, length);
}

static int
prom_write_label_value(WreathBytesWriter *writer, const char *data,
                       Py_ssize_t length)
{
    Py_ssize_t start = 0;
    for (Py_ssize_t index = 0; index < length; index++) {
        const char *escape;
        if (data[index] == '\\') escape = "\\\\";
        else if (data[index] == '"') escape = "\\\"";
        else if (data[index] == '\n') escape = "\\n";
        else continue;
        if (wreath_writer_write(writer, data + start, index - start) < 0 ||
            wreath_writer_write(writer, escape, 2) < 0) return -1;
        start = index + 1;
    }
    return wreath_writer_write(writer, data + start, length - start);
}

static int
prom_write_sample_head(WreathBytesWriter *writer,
                       const char *name, Py_ssize_t name_length,
                       const char *name_suffix, const PromRoute *route,
                       PyObject *bound)
{
    if (writer->len != 0 && wreath_writer_byte(writer, '\n') < 0) return -1;
    if (wreath_writer_write(writer, name, name_length) < 0 ||
        (name_suffix[0] != '\0' && wreath_writer_write(
            writer, name_suffix, (Py_ssize_t)strlen(name_suffix)) < 0) ||
        wreath_writer_byte(writer, '{') < 0 ||
        wreath_writer_write(writer, route->labels, route->labels_length) < 0)
        return -1;
    if (bound != NULL) {
        if (wreath_writer_write(writer, ",le=\"", 5) < 0 ||
            prom_write_unicode(writer, bound) < 0) return -1;
    }
    else if (wreath_writer_byte(writer, '}') < 0 ||
             wreath_writer_byte(writer, ' ') < 0) return -1;
    if (bound != NULL) return wreath_writer_write(writer, "\"} ", 3);
    return 0;
}

static int
prom_write_sample(WreathBytesWriter *writer,
                  const char *name, Py_ssize_t name_length,
                  const char *name_suffix, const PromRoute *route,
                  PyObject *bound, PyObject *value)
{
    return prom_write_sample_head(
        writer, name, name_length, name_suffix, route, bound) < 0
        ? -1 : prom_write_unicode(writer, value);
}

static int
prom_write_uint64(WreathBytesWriter *writer, uint64_t value)
{
    char reversed[20], digits[20];
    Py_ssize_t count = 0;
    do {
        reversed[count++] = (char)('0' + value % 10U);
        value /= 10U;
    } while (value != 0);
    for (Py_ssize_t index = 0; index < count; index++)
        digits[index] = reversed[count - index - 1];
    return wreath_writer_write(writer, digits, count);
}

static int
prom_write_sample_uint64(WreathBytesWriter *writer,
                         const char *name, Py_ssize_t name_length,
                         const char *name_suffix, const PromRoute *route,
                         PyObject *bound, uint64_t value)
{
    return prom_write_sample_head(
        writer, name, name_length, name_suffix, route, bound) < 0
        ? -1 : prom_write_uint64(writer, value);
}

static int
prom_as_uint64(PyObject *number, uint64_t *value)
{
    if (!PyLong_Check(number)) return 0;
    if (PyUnstable_Long_IsCompact((PyLongObject *)number)) {
        Py_ssize_t compact = PyUnstable_Long_CompactValue((PyLongObject *)number);
        if (compact < 0) return 0;
        *value = (uint64_t)compact;
        return 1;
    }
    *value = PyLong_AsUnsignedLongLong(number);
    if (!PyErr_Occurred()) return 1;
    PyErr_Clear();
    return 0;
}

static int
prom_write_sample_number(WreathBytesWriter *writer,
                         const char *name, Py_ssize_t name_length,
                         const char *name_suffix, const PromRoute *route,
                         PyObject *bound, PyObject *number)
{
    uint64_t value;
    if (prom_as_uint64(number, &value))
        return prom_write_sample_uint64(
            writer, name, name_length, name_suffix, route, bound, value);
    PyObject *text = prom_number(number);
    int result;
    if (text == NULL) return -1;
    result = prom_write_sample(
        writer, name, name_length, name_suffix, route, bound, text);
    Py_DECREF(text);
    return result;
}

static PyObject *
prom_finish_block(WreathBytesWriter *writer)
{
    PyObject *bytes = wreath_writer_finish(writer);
    PyObject *text;
    if (bytes == NULL) return NULL;
    text = PyUnicode_DecodeUTF8(
        PyBytes_AS_STRING(bytes), PyBytes_GET_SIZE(bytes), "strict");
    Py_DECREF(bytes);
    return text;
}

static inline int
prom_metric_char(Py_UCS4 character)
{
    return (character >= 'a' && character <= 'z') ||
           (character >= 'A' && character <= 'Z') ||
           (character >= '0' && character <= '9') ||
           character == '_' || character == ':';
}

static inline int
prom_metric_lead(Py_UCS4 character)
{
    return (character >= 'a' && character <= 'z') ||
           (character >= 'A' && character <= 'Z') ||
           character == '_' || character == ':';
}

static PyObject *
prom_sanitize_metric(PyObject *text)
{
    Py_ssize_t length = PyUnicode_GetLength(text);
    if (length < 0) return NULL;
    char *clean = PyMem_Malloc((size_t)length + 1);
    if (clean == NULL) return PyErr_NoMemory();
    for (Py_ssize_t index = 0; index < length; index++) {
        Py_UCS4 character = PyUnicode_ReadChar(text, index);
        if (character == (Py_UCS4)-1 && PyErr_Occurred()) {
            PyMem_Free(clean);
            return NULL;
        }
        clean[index] = prom_metric_char(character) ? (char)character : '_';
    }
    Py_ssize_t prefix = length != 0 && !prom_metric_lead((unsigned char)clean[0]);
    PyObject *result;
    if (prefix) {
        char *prefixed = PyMem_Malloc((size_t)length + 1);
        if (prefixed == NULL) {
            PyMem_Free(clean);
            return PyErr_NoMemory();
        }
        prefixed[0] = '_';
        memcpy(prefixed + 1, clean, (size_t)length);
        result = PyUnicode_FromStringAndSize(prefixed, length + 1);
        PyMem_Free(prefixed);
    }
    else {
        result = PyUnicode_FromStringAndSize(clean, length);
    }
    PyMem_Free(clean);
    return result;
}

static int
prometheus_counter_sample(PyObject *groups, PyObject *namespace,
                          PyObject *subsystem, PyObject *instance,
                          PyObject *raw_name, PyObject *raw_number)
{
    PyObject *name_text = PyObject_Str(raw_name);
    PyObject *name = name_text != NULL
        ? prom_sanitize_metric(name_text) : NULL;
    PyObject *family = name != NULL
        ? PyUnicode_FromFormat("%U_%U_%U", namespace, subsystem, name)
        : NULL;
    PyObject *number = family != NULL ? PyNumber_Long(raw_number) : NULL;
    PyObject *samples = NULL;
    PyObject *inserted_samples = NULL;
    PyObject *sample = NULL;
    int status = -1;
    Py_XDECREF(name_text);
    Py_XDECREF(name);
    if (number == NULL) goto done;
    samples = PyDict_GetItemWithError(groups, family);
    if (samples == NULL) {
        if (PyErr_Occurred()) goto done;
        inserted_samples = PyList_New(0);
        if (inserted_samples == NULL ||
            PyDict_SetItem(groups, family, inserted_samples) < 0) goto done;
        samples = inserted_samples;
    }
    sample = wreath_tuple2_from_owned(Py_NewRef(instance), number);
    number = NULL;
    if (sample == NULL || PyList_Append(samples, sample) < 0) goto done;
    status = 0;

done:
    Py_XDECREF(sample);
    Py_XDECREF(inserted_samples);
    Py_XDECREF(number);
    Py_XDECREF(family);
    return status;
}

PyObject *
wreath_prometheus_counter_block(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *readings_object, *namespace;
    PyObject *readings = NULL, *groups = NULL;
    WreathBytesWriter writer = {0};
    if (!PyArg_ParseTuple(args, "OU:prometheus_counter_block",
                          &readings_object, &namespace)) return NULL;
    readings = PySequence_Fast(
        readings_object, "Prometheus counters must be a sequence");
    if (readings == NULL) return NULL;
    groups = PyDict_New();
    if (groups == NULL) goto error;

    Py_ssize_t reading_count = PySequence_Fast_GET_SIZE(readings);
    for (Py_ssize_t reading_index = 0;
         reading_index < reading_count; reading_index++) {
        PyObject *reading = PySequence_Fast_GET_ITEM(readings, reading_index);
        PyObject *subsystem_value = observability_getattr(reading, OBS_SUBSYSTEM);
        PyObject *instance_value = observability_getattr(reading, OBS_INSTANCE);
        PyObject *values = observability_getattr(reading, OBS_VALUES);
        PyObject *subsystem_text = NULL, *subsystem = NULL, *instance = NULL;
        PyObject *items = NULL;
        if (subsystem_value == NULL || instance_value == NULL || values == NULL)
            goto reading_error;
        subsystem_text = PyObject_Str(subsystem_value);
        instance = PyObject_Str(instance_value);
        if (subsystem_text == NULL || instance == NULL) goto reading_error;
        subsystem = prom_sanitize_metric(subsystem_text);
        if (subsystem == NULL) goto reading_error;
        if (PyDict_CheckExact(values)) {
            Py_ssize_t position = 0;
            PyObject *raw_name;
            PyObject *raw_number;
            while (PyDict_Next(values, &position, &raw_name, &raw_number)) {
                if (prometheus_counter_sample(
                    groups, namespace, subsystem, instance,
                    raw_name, raw_number) < 0) goto reading_error;
            }
        }
        else {
            items = PyMapping_Items(values);
            if (items == NULL) goto reading_error;
            Py_ssize_t item_count = PyList_GET_SIZE(items);
            for (Py_ssize_t item_index = 0; item_index < item_count; item_index++) {
                PyObject *item = PyList_GET_ITEM(items, item_index);
                if (prometheus_counter_sample(
                    groups, namespace, subsystem, instance,
                    PyTuple_GET_ITEM(item, 0), PyTuple_GET_ITEM(item, 1)) < 0)
                    goto reading_error;
            }
        }
        Py_XDECREF(items);
        items = NULL;
        Py_DECREF(subsystem);
        Py_DECREF(subsystem_text);
        Py_DECREF(instance);
        Py_DECREF(values);
        Py_DECREF(instance_value);
        Py_DECREF(subsystem_value);
        if ((reading_index & 63) == 63 && PyErr_CheckSignals() < 0) goto error;
        continue;

reading_error:
        Py_XDECREF(items);
        Py_XDECREF(subsystem);
        Py_XDECREF(subsystem_text);
        Py_XDECREF(instance);
        Py_XDECREF(values);
        Py_XDECREF(instance_value);
        Py_XDECREF(subsystem_value);
        goto error;
    }

    Py_ssize_t group_count = PyDict_Size(groups);
    Py_ssize_t capacity = group_count > (PY_SSIZE_T_MAX - 64) / 128
        ? 256 : group_count * 128 + 64;
    if (wreath_writer_init(&writer, capacity) < 0) goto error;
    Py_ssize_t position = 0;
    PyObject *family, *samples;
    while (PyDict_Next(groups, &position, &family, &samples)) {
        if (writer.len != 0 && wreath_writer_byte(&writer, '\n') < 0) goto error;
        if (wreath_writer_write(&writer, "# HELP ", 7) < 0 ||
            prom_write_unicode(&writer, family) < 0 ||
            wreath_writer_write(
                &writer, " Reported by the subsystem that owns it.\n# TYPE ",
                (Py_ssize_t)sizeof(
                    " Reported by the subsystem that owns it.\n# TYPE ") - 1) < 0 ||
            prom_write_unicode(&writer, family) < 0 ||
            wreath_writer_write(&writer, " gauge", 6) < 0) goto error;
        Py_ssize_t sample_count = PyList_GET_SIZE(samples);
        for (Py_ssize_t index = 0; index < sample_count; index++) {
            PyObject *sample = PyList_GET_ITEM(samples, index);
            PyObject *instance = PyTuple_GET_ITEM(sample, 0);
            PyObject *number = PyTuple_GET_ITEM(sample, 1);
            Py_ssize_t instance_length;
            const char *instance_data = PyUnicode_AsUTF8AndSize(
                instance, &instance_length);
            PyObject *number_text = instance_data != NULL
                ? prom_number(number) : NULL;
            if (number_text == NULL || wreath_writer_byte(&writer, '\n') < 0 ||
                prom_write_unicode(&writer, family) < 0 ||
                wreath_writer_write(&writer, "{instance=\"", 11) < 0 ||
                prom_write_label_value(
                    &writer, instance_data, instance_length) < 0 ||
                wreath_writer_write(&writer, "\"} ", 3) < 0 ||
                prom_write_unicode(&writer, number_text) < 0) {
                Py_XDECREF(number_text);
                goto error;
            }
            Py_DECREF(number_text);
        }
    }
    Py_DECREF(groups);
    Py_DECREF(readings);
    return prom_finish_block(&writer);

error:
    Py_XDECREF(writer.bytes);
    Py_XDECREF(groups);
    Py_XDECREF(readings);
    return NULL;
}

PyObject *
wreath_prometheus_route_blocks(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *routes_object, *names, *resolver = Py_None;
    int resolver_is_mapping = 0;
    PyObject *routes = NULL, *groups = NULL;
    PromRoute *plan = NULL;
    WreathBytesWriter writers[4] = {{0}};
    const char *name_data[4];
    Py_ssize_t name_lengths[4];
    Py_ssize_t count = 0;
    if (!PyArg_ParseTuple(args, "OO!|Op:prometheus_route_blocks", &routes_object,
                          &PyTuple_Type, &names, &resolver,
                          &resolver_is_mapping)) return NULL;
    if (PyTuple_GET_SIZE(names) != 4) {
        PyErr_SetString(PyExc_ValueError,
                        "Prometheus route names must contain four strings");
        return NULL;
    }
    for (int group = 0; group < 4; group++) {
        name_data[group] = PyUnicode_AsUTF8AndSize(
            PyTuple_GET_ITEM(names, group), &name_lengths[group]);
        if (name_data[group] == NULL) return NULL;
    }
    routes = PySequence_Fast(routes_object, "Prometheus routes must be a sequence");
    if (routes == NULL) return NULL;
    count = PySequence_Fast_GET_SIZE(routes);
    if (count != 0) {
        plan = PyMem_Calloc((size_t)count, sizeof(*plan));
        if (plan == NULL) { PyErr_NoMemory(); goto error; }
    }
    for (int group = 0; group < 4; group++) {
        Py_ssize_t per_route = group == 2 ? 256 : 64;
        Py_ssize_t capacity = count > (PY_SSIZE_T_MAX - 64) / per_route
            ? 256 : count * per_route + 64;
        if (wreath_writer_init(&writers[group], capacity) < 0) goto error;
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *route = PySequence_Fast_GET_ITEM(routes, index);
        PyObject *route_id = observability_getattr(route, OBS_ROUTE_ID);
        if (route_id == NULL) goto error;
        if (prom_route_labels(
                &plan[index], route_id, resolver, resolver_is_mapping) < 0) {
            Py_DECREF(route_id);
            goto error;
        }
        Py_DECREF(route_id);
        plan[index].count = observability_getattr(route, OBS_COUNT);
        plan[index].errors = observability_getattr(route, OBS_ERRORS);
        plan[index].duration_sum = observability_getattr(route, OBS_DURATION_US_SUM);
        plan[index].duration_max = observability_getattr(route, OBS_DURATION_US_MAX);
        plan[index].buckets = observability_getattr(route, OBS_BUCKETS);
        if (plan[index].count == NULL || plan[index].errors == NULL ||
            plan[index].duration_sum == NULL ||
            plan[index].duration_max == NULL || plan[index].buckets == NULL) goto error;
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        if (prom_write_sample_number(
                &writers[0], name_data[0], name_lengths[0], "",
                &plan[index], NULL, plan[index].count) < 0) goto error;
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        if (prom_write_sample_number(
                &writers[1], name_data[1], name_lengths[1], "",
                &plan[index], NULL, plan[index].errors) < 0) goto error;
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *buckets = PySequence_Fast(
            plan[index].buckets, "Prometheus buckets must be a sequence");
        uint64_t bucket_values[64], cumulative_fast = 0;
        Py_ssize_t bucket_count;
        int fast_buckets = 1;
        if (buckets == NULL) goto error;
        bucket_count = PySequence_Fast_GET_SIZE(buckets);
        if (bucket_count > 64) bucket_count = 64;
        for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++) {
            PyObject *amount = PySequence_Fast_GET_ITEM(buckets, bucket);
            uint64_t value;
            if (!prom_as_uint64(amount, &value)) { fast_buckets = 0; break; }
            if (value > UINT64_MAX - cumulative_fast) {
                fast_buckets = 0;
                break;
            }
            bucket_values[bucket] = value;
            cumulative_fast += value;
        }
        if (fast_buckets) {
            uint64_t cumulative = 0;
            for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++) {
                if (bucket_values[bucket] != 0) {
                    PyObject *bound;
                    cumulative += bucket_values[bucket];
                    bound = prom_double(ldexp(1.0, (int)bucket + 1) / 1000000.0);
                    if (bound == NULL || prom_write_sample_uint64(
                            &writers[2], name_data[2], name_lengths[2], "_bucket",
                            &plan[index], bound, cumulative) < 0) {
                        Py_XDECREF(bound); Py_DECREF(buckets); goto error;
                    }
                    Py_DECREF(bound);
                } else {
                    cumulative += bucket_values[bucket];
                }
            }
            {
                PyObject *bound = PyUnicode_FromString("+Inf");
                if (bound == NULL || prom_write_sample_uint64(
                        &writers[2], name_data[2], name_lengths[2], "_bucket",
                        &plan[index], bound, cumulative) < 0) {
                    Py_XDECREF(bound); Py_DECREF(buckets); goto error;
                }
                Py_DECREF(bound);
            }
            {
                double sum = PyFloat_AsDouble(plan[index].duration_sum);
                PyObject *sum_value = PyErr_Occurred() ? NULL : prom_double(sum / 1000000.0);
                if (sum_value == NULL || prom_write_sample(
                        &writers[2], name_data[2], name_lengths[2], "_sum",
                        &plan[index], NULL, sum_value) < 0 ||
                    prom_write_sample_uint64(
                        &writers[2], name_data[2], name_lengths[2], "_count",
                        &plan[index], NULL, cumulative) < 0) {
                    Py_XDECREF(sum_value); Py_DECREF(buckets); goto error;
                }
                Py_DECREF(sum_value);
            }
            Py_DECREF(buckets);
            continue;
        }
        PyObject *cumulative = PyLong_FromLong(0);
        if (cumulative == NULL) { Py_DECREF(buckets); goto error; }
        for (Py_ssize_t bucket = 0; bucket < bucket_count; bucket++) {
            PyObject *amount = PySequence_Fast_GET_ITEM(buckets, bucket);
            PyObject *next = PyNumber_Add(cumulative, amount);
            int truth;
            if (next == NULL) { Py_DECREF(cumulative); Py_DECREF(buckets); goto error; }
            Py_SETREF(cumulative, next);
            truth = PyObject_IsTrue(amount);
            if (truth < 0) { Py_DECREF(cumulative); Py_DECREF(buckets); goto error; }
            if (truth) {
                PyObject *bound = prom_double(ldexp(1.0, (int)bucket + 1) / 1000000.0);
                PyObject *value = prom_number(cumulative);
                if (bound == NULL || value == NULL || prom_write_sample(
                        &writers[2], name_data[2], name_lengths[2], "_bucket",
                        &plan[index], bound, value) < 0) {
                    Py_XDECREF(value); Py_XDECREF(bound);
                    Py_DECREF(cumulative); Py_DECREF(buckets); goto error;
                }
                Py_DECREF(value); Py_DECREF(bound);
            }
        }
        {
            PyObject *bound = PyUnicode_FromString("+Inf");
            PyObject *value = prom_number(cumulative);
            if (bound == NULL || value == NULL || prom_write_sample(
                    &writers[2], name_data[2], name_lengths[2], "_bucket",
                    &plan[index], bound, value) < 0) {
                Py_XDECREF(value); Py_XDECREF(bound);
                Py_DECREF(cumulative); Py_DECREF(buckets); goto error;
            }
            Py_DECREF(value); Py_DECREF(bound);
        }
        {
            double sum = PyFloat_AsDouble(plan[index].duration_sum);
            PyObject *sum_value = PyErr_Occurred() ? NULL : prom_double(sum / 1000000.0);
            PyObject *count_value = prom_number(cumulative);
            if (sum_value == NULL || count_value == NULL || prom_write_sample(
                    &writers[2], name_data[2], name_lengths[2], "_sum",
                    &plan[index], NULL, sum_value) < 0 || prom_write_sample(
                    &writers[2], name_data[2], name_lengths[2], "_count",
                    &plan[index], NULL, count_value) < 0) {
                Py_XDECREF(count_value); Py_XDECREF(sum_value);
                Py_DECREF(cumulative); Py_DECREF(buckets); goto error;
            }
            Py_DECREF(count_value); Py_DECREF(sum_value);
        }
        Py_DECREF(cumulative); Py_DECREF(buckets);
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        double maximum = PyFloat_AsDouble(plan[index].duration_max);
        PyObject *value = PyErr_Occurred() ? NULL : prom_double(maximum / 1000000.0);
        if (value == NULL || prom_write_sample(
                &writers[3], name_data[3], name_lengths[3], "",
                &plan[index], NULL, value) < 0) {
            Py_XDECREF(value);
            goto error;
        }
        Py_DECREF(value);
    }
    groups = PyTuple_New(4);
    if (groups == NULL) goto error;
    for (int group = 0; group < 4; group++) {
        PyObject *block = prom_finish_block(&writers[group]);
        if (block == NULL) goto error;
        PyTuple_SET_ITEM(groups, group, block);
    }
    prom_routes_clear(plan, count);
    Py_DECREF(routes);
    return groups;
error:
    for (int group = 0; group < 4; group++)
        Py_XDECREF(writers[group].bytes);
    prom_routes_clear(plan, count);
    Py_XDECREF(groups);
    Py_XDECREF(routes);
    return NULL;
}

static int
prom_write_family(WreathBytesWriter *writer, PyObject *family,
                  const char *kind, const char *help)
{
    if (writer->len != 0 && wreath_writer_byte(writer, '\n') < 0) return -1;
    return wreath_writer_write(writer, "# HELP ", 7) < 0 ||
           prom_write_unicode(writer, family) < 0 ||
           wreath_writer_byte(writer, ' ') < 0 ||
           wreath_writer_write(writer, help, (Py_ssize_t)strlen(help)) < 0 ||
           wreath_writer_write(writer, "\n# TYPE ", 8) < 0 ||
           prom_write_unicode(writer, family) < 0 ||
           wreath_writer_byte(writer, ' ') < 0 ||
           wreath_writer_write(writer, kind, (Py_ssize_t)strlen(kind)) < 0
        ? -1 : 0;
}

static int
prom_write_number_value(WreathBytesWriter *writer, PyObject *value)
{
    uint64_t compact;
    if (prom_as_uint64(value, &compact))
        return prom_write_uint64(writer, compact);
    PyObject *number = PyNumber_Long(value);
    PyObject *text;
    int result;
    if (number == NULL) return -1;
    text = prom_number(number);
    Py_DECREF(number);
    if (text == NULL) return -1;
    result = prom_write_unicode(writer, text);
    Py_DECREF(text);
    return result;
}

static int
prom_write_plain_number(WreathBytesWriter *writer, PyObject *value)
{
    return wreath_writer_byte(writer, ' ') < 0
        ? -1 : prom_write_number_value(writer, value);
}

static PyObject *
prom_optional_number(PyObject *object, const char *name)
{
    PyObject *value = NULL;
    int found = object != Py_None
        ? PyObject_GetOptionalAttrString(object, name, &value) : 0;
    if (found < 0) return NULL;
    if (!found) return PyLong_FromLong(0);
    PyObject *number = PyNumber_Long(value);
    Py_DECREF(value);
    return number;
}

static int
prom_write_reason_sample(WreathBytesWriter *writer, PyObject *sample,
                         const char *reason, PyObject *value)
{
    return wreath_writer_byte(writer, '\n') < 0 ||
           prom_write_unicode(writer, sample) < 0 ||
           wreath_writer_write(writer, "{reason=\"", 9) < 0 ||
           prom_write_label_value(
               writer, reason, (Py_ssize_t)strlen(reason)) < 0 ||
           wreath_writer_write(writer, "\"} ", 3) < 0 ||
           prom_write_number_value(writer, value) < 0
        ? -1 : 0;
}

static int
prom_write_reason_value(WreathBytesWriter *writer, PyObject *sample,
                        PyObject *reason, PyObject *value)
{
    PyObject *name = NULL, *text = NULL, *lower = NULL;
    int found = PyObject_GetOptionalAttrString(reason, "name", &name);
    const char *data;
    Py_ssize_t length;
    int result;
    if (found < 0) return -1;
    if (found) text = PyObject_Str(name);
    else text = PyObject_Str(reason);
    Py_XDECREF(name);
    if (text == NULL) return -1;
    lower = PyObject_CallMethod(text, "lower", NULL);
    Py_DECREF(text);
    if (lower == NULL) return -1;
    data = PyUnicode_AsUTF8AndSize(lower, &length);
    if (data == NULL || wreath_writer_byte(writer, '\n') < 0 ||
        prom_write_unicode(writer, sample) < 0 ||
        wreath_writer_write(writer, "{reason=\"", 9) < 0 ||
        prom_write_label_value(writer, data, length) < 0 ||
        wreath_writer_write(writer, "\"} ", 3) < 0) {
        Py_DECREF(lower);
        return -1;
    }
    Py_DECREF(lower);
    result = prom_write_number_value(writer, value);
    return result;
}

PyObject *
wreath_prometheus_global_block(PyObject *Py_UNUSED(self), PyObject *args)
{
    static const char *loss_fields[] = {
        "orphan_phase", "orphan_correlation", "pending_evicted",
        "decode_error", "export_error", "recent_evicted",
    };
    PyObject *snapshot, *recorder_loss, *names;
    PyObject *assembled = NULL, *pending = NULL, *loss = NULL;
    PyObject *recorder_items = NULL;
    WreathBytesWriter writer = {0};
    if (!PyArg_ParseTuple(args, "OOO!:prometheus_global_block", &snapshot,
                          &recorder_loss, &PyTuple_Type, &names)) return NULL;
    if (PyTuple_GET_SIZE(names) != 7) {
        PyErr_SetString(PyExc_ValueError,
                        "Prometheus global names must contain seven strings");
        return NULL;
    }
    assembled = prom_optional_number(snapshot, "assembled");
    pending = prom_optional_number(snapshot, "pending");
    if (assembled == NULL || pending == NULL ||
        PyObject_GetOptionalAttrString(snapshot, "loss", &loss) < 0) goto error;
    if (loss == NULL) loss = Py_NewRef(Py_None);
    if (wreath_writer_init(&writer, 1024) < 0) goto error;
    PyObject *assembled_family = PyTuple_GET_ITEM(names, 0);
    PyObject *assembled_sample = PyTuple_GET_ITEM(names, 1);
    PyObject *pending_name = PyTuple_GET_ITEM(names, 2);
    PyObject *projector_family = PyTuple_GET_ITEM(names, 3);
    PyObject *projector_sample = PyTuple_GET_ITEM(names, 4);
    PyObject *recorder_family = PyTuple_GET_ITEM(names, 5);
    PyObject *recorder_sample = PyTuple_GET_ITEM(names, 6);
    if (prom_write_family(
            &writer, assembled_family, "counter",
            "Total request traces the projector has finalized.") < 0 ||
        wreath_writer_byte(&writer, '\n') < 0 ||
        prom_write_unicode(&writer, assembled_sample) < 0 ||
        prom_write_plain_number(&writer, assembled) < 0 ||
        prom_write_family(
            &writer, pending_name, "gauge",
            "Completions awaiting their trailing correlation/phase cells.") < 0 ||
        wreath_writer_byte(&writer, '\n') < 0 ||
        prom_write_unicode(&writer, pending_name) < 0 ||
        prom_write_plain_number(&writer, pending) < 0 ||
        prom_write_family(
            &writer, projector_family, "counter",
            "Telemetry items the projector dropped, by reason.") < 0)
        goto error;
    for (size_t index = 0; index < sizeof(loss_fields) / sizeof(loss_fields[0]);
         index++) {
        PyObject *value = prom_optional_number(loss, loss_fields[index]);
        if (value == NULL || prom_write_reason_sample(
                &writer, projector_sample, loss_fields[index], value) < 0) {
            Py_XDECREF(value);
            goto error;
        }
        Py_DECREF(value);
    }
    if (recorder_loss != Py_None) {
        if (prom_write_family(
                &writer, recorder_family, "counter",
                "Items the recorder dropped before the projector saw them, by reason.") < 0)
            goto error;
        recorder_items = PyMapping_Items(recorder_loss);
        if (recorder_items == NULL) goto error;
        Py_ssize_t count = PyList_GET_SIZE(recorder_items);
        for (Py_ssize_t index = 0; index < count; index++) {
            PyObject *item = PyList_GET_ITEM(recorder_items, index);
            if (prom_write_reason_value(
                    &writer, recorder_sample, PyTuple_GET_ITEM(item, 0),
                    PyTuple_GET_ITEM(item, 1)) < 0) goto error;
        }
    }
    Py_XDECREF(recorder_items);
    Py_DECREF(loss);
    Py_DECREF(pending);
    Py_DECREF(assembled);
    return prom_finish_block(&writer);
error:
    Py_XDECREF(writer.bytes);
    Py_XDECREF(recorder_items);
    Py_XDECREF(loss);
    Py_XDECREF(pending);
    Py_XDECREF(assembled);
    return NULL;
}

static int
prom_append_block(WreathBytesWriter *writer, PyObject *block)
{
    if (!PyUnicode_Check(block)) {
        PyErr_SetString(PyExc_TypeError, "Prometheus block must be str");
        return -1;
    }
    if (PyUnicode_GetLength(block) == 0) return 0;
    return wreath_writer_byte(writer, '\n') < 0 ||
           prom_write_unicode(writer, block) < 0 ? -1 : 0;
}

/* Join the independently useful exposition kernels in one native-owned output
 * buffer.  The blocks are already final boundary text; keeping their framing
 * here removes the Python Writer/list/f-string graph without coupling route,
 * recorder or subsystem declarations to one another. */
PyObject *
wreath_prometheus_document(PyObject *Py_UNUSED(self), PyObject *args)
{
    static const char *kinds[4] = {"counter", "counter", "histogram", "gauge"};
    static const char *helps[4] = {
        "Requests finalized by the flight projector, by route.",
        "Failed requests (non-OK terminal, 5xx, or promoted), by route.",
        "Request duration in seconds (base-2 log buckets), by route.",
        "Maximum observed request duration in seconds, by route.",
    };
    PyObject *route_blocks, *global_block, *counter_block, *families;
    int openmetrics;
    WreathBytesWriter writer = {0};
    if (!PyArg_ParseTuple(
            args, "O!UUO!p:prometheus_document", &PyTuple_Type, &route_blocks,
            &global_block, &counter_block, &PyTuple_Type, &families,
            &openmetrics)) return NULL;
    if (PyTuple_GET_SIZE(route_blocks) != 4 || PyTuple_GET_SIZE(families) != 4) {
        PyErr_SetString(PyExc_ValueError,
                        "Prometheus document needs four route blocks and families");
        return NULL;
    }
    if (wreath_writer_init(&writer, 4096) < 0) return NULL;
    for (Py_ssize_t index = 0; index < 4; index++) {
        PyObject *family = PyTuple_GET_ITEM(families, index);
        if (!PyUnicode_Check(family)) {
            PyErr_SetString(PyExc_TypeError, "Prometheus family name must be str");
            goto error;
        }
        if (prom_write_family(&writer, family, kinds[index], helps[index]) < 0 ||
            prom_append_block(&writer, PyTuple_GET_ITEM(route_blocks, index)) < 0)
            goto error;
    }
    if (prom_append_block(&writer, global_block) < 0 ||
        prom_append_block(&writer, counter_block) < 0 ||
        wreath_writer_byte(&writer, '\n') < 0) goto error;
    if (openmetrics && wreath_writer_write(&writer, "# EOF\n", 6) < 0) goto error;
    return prom_finish_block(&writer);
error:
    Py_XDECREF(writer.bytes);
    return NULL;
}

static inline int
statsd_metric_char(Py_UCS4 character)
{
    return (character >= 'a' && character <= 'z') ||
           (character >= 'A' && character <= 'Z') ||
           (character >= '0' && character <= '9') ||
           character == '.' || character == '_' || character == '-';
}

static inline int
statsd_tag_char(Py_UCS4 character)
{
    return character != ',' && character != '|' && character != '#' &&
           character != ':' && !Py_UNICODE_ISSPACE(character);
}

static int
statsd_write_codepoint(WreathBytesWriter *writer, Py_UCS4 character)
{
    char encoded[4];
    Py_ssize_t length;
    if (character <= 0x7ff) {
        encoded[0] = (char)(0xc0 | (character >> 6));
        encoded[1] = (char)(0x80 | (character & 0x3f));
        length = 2;
    }
    else if (character <= 0xffff && !(character >= 0xd800 && character <= 0xdfff)) {
        encoded[0] = (char)(0xe0 | (character >> 12));
        encoded[1] = (char)(0x80 | ((character >> 6) & 0x3f));
        encoded[2] = (char)(0x80 | (character & 0x3f));
        length = 3;
    }
    else if (character <= 0x10ffff) {
        encoded[0] = (char)(0xf0 | (character >> 18));
        encoded[1] = (char)(0x80 | ((character >> 12) & 0x3f));
        encoded[2] = (char)(0x80 | ((character >> 6) & 0x3f));
        encoded[3] = (char)(0x80 | (character & 0x3f));
        length = 4;
    }
    else {
        PyErr_SetString(PyExc_UnicodeEncodeError, "invalid metric tag character");
        return -1;
    }
    return wreath_writer_write(writer, encoded, length);
}

static int
statsd_write_sanitized(WreathBytesWriter *writer, PyObject *text, int tag)
{
    if (!PyUnicode_Check(text)) {
        PyErr_Format(PyExc_TypeError, "metric component must be str, got %s",
                     Py_TYPE(text)->tp_name);
        return -1;
    }
    Py_ssize_t length = PyUnicode_GetLength(text);
    if (length < 0) return -1;
    for (Py_ssize_t index = 0; index < length; index++) {
        Py_UCS4 character = PyUnicode_ReadChar(text, index);
        if (character == (Py_UCS4)-1 && PyErr_Occurred()) return -1;
        if (tag ? statsd_tag_char(character) : statsd_metric_char(character)) {
            if (character < 128) {
                if (wreath_writer_byte(writer, (char)character) < 0) return -1;
            }
            else if (statsd_write_codepoint(writer, character) < 0) return -1;
        }
        else if (wreath_writer_byte(writer, '_') < 0) return -1;
    }
    return 0;
}

static PyObject *
statsd_number(PyObject *value)
{
    if (PyBool_Check(value))
        return PyUnicode_FromString(value == Py_True ? "1" : "0");
    if (PyFloat_Check(value)) {
        double number = PyFloat_AS_DOUBLE(value);
        if (isfinite(number) && floor(number) == number) {
            PyObject *integer = PyLong_FromDouble(number);
            PyObject *text;
            if (integer == NULL) return NULL;
            text = PyObject_Str(integer);
            Py_DECREF(integer);
            return text;
        }
        return PyObject_Repr(value);
    }
    if (PyLong_Check(value)) return PyObject_Str(value);
    PyObject *integer = PyNumber_Long(value);
    if (integer == NULL) return NULL;
    PyObject *text = PyObject_Str(integer);
    Py_DECREF(integer);
    return text;
}

static PyObject *
statsd_default_labels(PyObject *route_id)
{
    PyObject *labels = PyDict_New();
    PyObject *key = PyUnicode_FromString("route_id");
    PyObject *value = PyObject_Str(route_id);
    if (labels == NULL || key == NULL || value == NULL ||
        PyDict_SetItem(labels, key, value) < 0) {
        Py_XDECREF(value);
        Py_XDECREF(key);
        Py_XDECREF(labels);
        return NULL;
    }
    Py_DECREF(value);
    Py_DECREF(key);
    return labels;
}

static PyObject *
statsd_route_labels(PyObject *resolver, PyObject *route_id)
{
    if (resolver == Py_None) return statsd_default_labels(route_id);
    int mapping = PyObject_HasAttrString(resolver, "get");
    if (mapping < 0) return NULL;
    PyObject *resolved = mapping
        ? PyObject_CallMethod(resolver, "get", "O", route_id)
        : PyObject_CallOneArg(resolver, route_id);
    if (resolved == NULL) return NULL;
    int truth = PyObject_IsTrue(resolved);
    if (truth <= 0) {
        Py_DECREF(resolved);
        return truth < 0 ? NULL : statsd_default_labels(route_id);
    }
    PyObject *items = PyMapping_Items(resolved);
    Py_DECREF(resolved);
    if (items == NULL) return NULL;
    PyObject *labels = PyDict_New();
    if (labels == NULL) {
        Py_DECREF(items);
        return NULL;
    }
    Py_ssize_t count = PyList_GET_SIZE(items);
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *item = PyList_GET_ITEM(items, index);
        PyObject *key = PyTuple_GET_ITEM(item, 0);
        PyObject *value = PyObject_Str(PyTuple_GET_ITEM(item, 1));
        if (value == NULL || PyDict_SetItem(labels, key, value) < 0) {
            Py_XDECREF(value);
            Py_DECREF(labels);
            Py_DECREF(items);
            return NULL;
        }
        Py_DECREF(value);
    }
    Py_DECREF(items);
    return labels;
}

#define METRIC_DELTA_CAPSULE "wreath.metric_delta_state"

typedef struct {
    uint64_t hash;
    uint64_t identity;
    char *name;
    Py_ssize_t name_length;
    union {
        uint64_t integer;
        double real;
        PyObject *object;
    } value;
    unsigned char kind;
    unsigned char representation;
} MetricDeltaEntry;

typedef struct {
    PyMutex mutex;
    MetricDeltaEntry *entries;
    size_t capacity;
    size_t size;
} MetricDeltaState;

static uint64_t
metric_delta_mix(uint64_t value)
{
    value ^= value >> 30;
    value *= UINT64_C(0xbf58476d1ce4e5b9);
    value ^= value >> 27;
    value *= UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

static uint64_t
metric_delta_hash(unsigned char kind, uint64_t identity,
                  const char *name, Py_ssize_t name_length)
{
    uint64_t hash = metric_delta_mix(identity ^ ((uint64_t)kind << 56));
    for (Py_ssize_t index = 0; index < name_length; index++) {
        hash ^= (unsigned char)name[index];
        hash *= UINT64_C(1099511628211);
    }
    return hash != 0 ? hash : UINT64_C(1);
}

static int
metric_delta_key_equal(const MetricDeltaEntry *entry, unsigned char kind,
                       uint64_t identity, const char *name,
                       Py_ssize_t name_length, uint64_t hash)
{
    return entry->hash == hash && entry->kind == kind &&
           entry->identity == identity && entry->name_length == name_length &&
           (name_length == 0 || memcmp(entry->name, name, (size_t)name_length) == 0);
}

static MetricDeltaEntry *
metric_delta_slot(MetricDeltaEntry *entries, size_t capacity,
                  unsigned char kind, uint64_t identity,
                  const char *name, Py_ssize_t name_length, uint64_t hash)
{
    size_t index = (size_t)hash & (capacity - 1);
    for (;;) {
        MetricDeltaEntry *entry = &entries[index];
        if (entry->hash == 0 || metric_delta_key_equal(
                entry, kind, identity, name, name_length, hash)) return entry;
        index = (index + 1) & (capacity - 1);
    }
}

static int
metric_delta_grow(MetricDeltaState *state)
{
    size_t capacity = state->capacity == 0 ? 64 : state->capacity * 2;
    if (capacity < state->capacity || capacity > SIZE_MAX / sizeof(MetricDeltaEntry)) {
        PyErr_NoMemory();
        return -1;
    }
    MetricDeltaEntry *entries = PyMem_Calloc(capacity, sizeof(*entries));
    if (entries == NULL) return PyErr_NoMemory(), -1;
    for (size_t index = 0; index < state->capacity; index++) {
        MetricDeltaEntry *old = &state->entries[index];
        if (old->hash == 0) continue;
        MetricDeltaEntry *next = metric_delta_slot(
            entries, capacity, old->kind, old->identity,
            old->name, old->name_length, old->hash);
        *next = *old;
    }
    PyMem_Free(state->entries);
    state->entries = entries;
    state->capacity = capacity;
    return 0;
}

static MetricDeltaEntry *
metric_delta_get(MetricDeltaState *state, unsigned char kind,
                 uint64_t identity, const char *name, Py_ssize_t name_length,
                 int representation, int *created)
{
    if (state->capacity == 0 ||
        (state->size + 1) * 10 >= state->capacity * 7) {
        if (metric_delta_grow(state) < 0) return NULL;
    }
    uint64_t hash = metric_delta_hash(kind, identity, name, name_length);
    MetricDeltaEntry *entry = metric_delta_slot(
        state->entries, state->capacity, kind, identity, name, name_length, hash);
    *created = entry->hash == 0;
    if (entry->hash == 0) {
        char *copy = name_length != 0 ? PyMem_Malloc((size_t)name_length) : NULL;
        if (name_length != 0 && copy == NULL) return PyErr_NoMemory(), NULL;
        if (name_length != 0) memcpy(copy, name, (size_t)name_length);
        *entry = (MetricDeltaEntry){
            .hash = hash,
            .identity = identity,
            .name = copy,
            .name_length = name_length,
            .kind = kind,
            .representation = (unsigned char)representation,
        };
        state->size++;
    }
    else if (entry->representation != (unsigned char)representation) {
        PyErr_SetString(PyExc_TypeError, "metric changed numeric representation");
        return NULL;
    }
    return entry;
}

static int
metric_delta_u64(MetricDeltaState *state, unsigned char kind,
                 uint64_t identity, const char *name, Py_ssize_t name_length,
                 uint64_t current, uint64_t *result)
{
    PyMutex_Lock(&state->mutex);
    int created;
    MetricDeltaEntry *entry = metric_delta_get(
        state, kind, identity, name, name_length, 0, &created);
    if (entry == NULL) {
        PyMutex_Unlock(&state->mutex);
        return -1;
    }
    *result = created || current < entry->value.integer
        ? current : current - entry->value.integer;
    entry->value.integer = current;
    PyMutex_Unlock(&state->mutex);
    return 0;
}

static int
metric_delta_double(MetricDeltaState *state, unsigned char kind,
                    uint64_t identity, double current, double *result)
{
    PyMutex_Lock(&state->mutex);
    int created;
    MetricDeltaEntry *entry = metric_delta_get(
        state, kind, identity, NULL, 0, 1, &created);
    if (entry == NULL) {
        PyMutex_Unlock(&state->mutex);
        return -1;
    }
    *result = created || current < entry->value.real
        ? current : current - entry->value.real;
    entry->value.real = current;
    PyMutex_Unlock(&state->mutex);
    return 0;
}

static int
metric_delta_pyint(MetricDeltaState *state, unsigned char kind,
                   const char *name, Py_ssize_t name_length,
                   PyObject *current, PyObject **result)
{
    PyMutex_Lock(&state->mutex);
    int created;
    MetricDeltaEntry *entry = metric_delta_get(
        state, kind, 0, name, name_length, 3, &created);
    if (entry == NULL) {
        PyMutex_Unlock(&state->mutex);
        return -1;
    }
    int reset = created ? 1 : PyObject_RichCompareBool(
        current, entry->value.object, Py_LT);
    if (reset < 0) {
        PyMutex_Unlock(&state->mutex);
        return -1;
    }
    PyObject *emitted = reset ? Py_NewRef(current)
        : PyNumber_Subtract(current, entry->value.object);
    if (emitted == NULL) {
        PyMutex_Unlock(&state->mutex);
        return -1;
    }
    Py_XSETREF(entry->value.object, Py_NewRef(current));
    *result = emitted;
    PyMutex_Unlock(&state->mutex);
    return 0;
}

static void
metric_delta_free(PyObject *capsule)
{
    MetricDeltaState *state = PyCapsule_GetPointer(capsule, METRIC_DELTA_CAPSULE);
    if (state == NULL) {
        PyErr_Clear();
        return;
    }
    for (size_t index = 0; index < state->capacity; index++) {
        MetricDeltaEntry *entry = &state->entries[index];
        if (entry->hash != 0 && entry->representation == 3)
            Py_XDECREF(entry->value.object);
    }
    for (size_t index = 0; index < state->capacity; index++)
        PyMem_Free(state->entries[index].name);
    PyMem_Free(state->entries);
    PyMem_Free(state);
}

PyObject *
wreath_metric_delta_state(PyObject *Py_UNUSED(self), PyObject *Py_UNUSED(arg))
{
    MetricDeltaState *state = PyMem_Calloc(1, sizeof(*state));
    if (state == NULL) return PyErr_NoMemory();
    PyObject *capsule = PyCapsule_New(
        state, METRIC_DELTA_CAPSULE, metric_delta_free);
    if (capsule == NULL) PyMem_Free(state);
    return capsule;
}

static MetricDeltaState *
metric_delta_state(PyObject *capsule)
{
    return PyCapsule_GetPointer(capsule, METRIC_DELTA_CAPSULE);
}

static PyObject *
statsd_delta(MetricDeltaState *state, unsigned char kind,
             PyObject *identity, PyObject *current)
{
    uint64_t key = 0;
    const char *name = NULL;
    Py_ssize_t name_length = 0;
    if (identity != NULL) {
        if (kind == 20) {
            name = PyUnicode_AsUTF8AndSize(identity, &name_length);
            if (name == NULL) {
                Py_DECREF(current);
                return NULL;
            }
        }
        else {
            key = PyLong_AsUnsignedLongLong(identity);
            if (PyErr_Occurred()) {
                Py_DECREF(current);
                return NULL;
            }
        }
    }
    if (PyFloat_Check(current)) {
        double delta;
        if (metric_delta_double(
                state, kind, key, PyFloat_AS_DOUBLE(current), &delta) < 0) {
            Py_DECREF(current);
            return NULL;
        }
        Py_DECREF(current);
        return PyFloat_FromDouble(delta);
    }
    uint64_t value = PyLong_AsUnsignedLongLong(current), delta;
    Py_DECREF(current);
    if (PyErr_Occurred() || metric_delta_u64(
            state, kind, key, name, name_length, value, &delta) < 0) return NULL;
    return PyLong_FromUnsignedLongLong(delta);
}

typedef struct {
    PyObject *out;
    WreathBytesWriter packet;
    Py_ssize_t max_packet_bytes;
    Py_ssize_t packet_size;
    Py_ssize_t line_count;
    int packets;
} StatsdSink;

static int
statsd_sink_init(StatsdSink *sink, int packets, Py_ssize_t max_packet_bytes)
{
    *sink = (StatsdSink){
        .out = PyList_New(0),
        .max_packet_bytes = max_packet_bytes,
        .packets = packets,
    };
    if (sink->out == NULL) return -1;
    if (packets && wreath_writer_init(&sink->packet, max_packet_bytes) < 0) {
        Py_CLEAR(sink->out);
        return -1;
    }
    return 0;
}

static int
statsd_sink_flush_packet(StatsdSink *sink)
{
    if (sink->packet.len == 0) return 0;
    PyObject *packet = wreath_writer_finish(&sink->packet);
    if (packet == NULL) return -1;
    int appended = PyList_Append(sink->out, packet);
    Py_DECREF(packet);
    if (appended < 0) return -1;
    sink->packet_size = 0;
    return wreath_writer_init(&sink->packet, sink->max_packet_bytes);
}

static int
statsd_sink_line(StatsdSink *sink, WreathBytesWriter *line)
{
    if (!sink->packets) {
        PyObject *encoded = wreath_writer_finish(line);
        if (encoded == NULL) return -1;
        PyObject *text = PyUnicode_DecodeUTF8(
            PyBytes_AS_STRING(encoded), PyBytes_GET_SIZE(encoded), "strict");
        Py_DECREF(encoded);
        if (text == NULL) return -1;
        int appended = PyList_Append(sink->out, text);
        Py_DECREF(text);
        if (appended < 0) return -1;
        sink->line_count++;
        return 0;
    }

    if (line->len == PY_SSIZE_T_MAX) return PyErr_NoMemory(), -1;
    Py_ssize_t budget = line->len + 1;
    if (sink->packet.len != 0 &&
        (sink->packet_size >= sink->max_packet_bytes ||
         budget > sink->max_packet_bytes - sink->packet_size)) {
        if (statsd_sink_flush_packet(sink) < 0) return -1;
    }
    if ((sink->packet.len != 0 && wreath_writer_byte(&sink->packet, '\n') < 0) ||
        wreath_writer_write(&sink->packet, line->buf, line->len) < 0) return -1;
    Py_CLEAR(line->bytes);
    line->buf = NULL;
    line->len = line->cap = 0;
    sink->packet_size += budget;
    sink->line_count++;
    return 0;
}

static void
statsd_sink_clear(StatsdSink *sink)
{
    Py_XDECREF(sink->packet.bytes);
    Py_XDECREF(sink->out);
    *sink = (StatsdSink){0};
}

static int
statsd_emit_parts(StatsdSink *sink, PyObject *prefix, int dogstatsd,
                  PyObject *static_tags, const char *fixed_name,
                  PyObject *name_prefix, PyObject *name_suffix,
                  PyObject *value, char kind, PyObject *labels)
{
    WreathBytesWriter writer = {0};
    PyObject *number = NULL, *merged = NULL, *items = NULL;
    if (wreath_writer_init(&writer, 96) < 0 ||
        statsd_write_sanitized(&writer, prefix, 0) < 0 ||
        wreath_writer_byte(&writer, '.') < 0)
        goto error;
    if (fixed_name != NULL) {
        if (wreath_writer_write(
                &writer, fixed_name, (Py_ssize_t)strlen(fixed_name)) < 0) goto error;
    }
    else if (statsd_write_sanitized(&writer, name_prefix, 0) < 0 ||
             wreath_writer_byte(&writer, '.') < 0 ||
             statsd_write_sanitized(&writer, name_suffix, 0) < 0) goto error;
    if (!dogstatsd) {
        PyObject *values = PyDict_Values(labels);
        if (values == NULL) goto error;
        Py_ssize_t count = PyList_GET_SIZE(values);
        for (Py_ssize_t index = 0; index < count; index++) {
            if (wreath_writer_byte(&writer, '.') < 0 ||
                statsd_write_sanitized(
                    &writer, PyList_GET_ITEM(values, index), 0) < 0) {
                Py_DECREF(values);
                goto error;
            }
        }
        Py_DECREF(values);
    }
    number = statsd_number(value);
    if (number == NULL || wreath_writer_byte(&writer, ':') < 0 ||
        prom_write_unicode(&writer, number) < 0 ||
        wreath_writer_byte(&writer, '|') < 0 ||
        wreath_writer_byte(&writer, kind) < 0) goto error;
    if (dogstatsd) {
        merged = PyDict_Copy(static_tags);
        if (merged == NULL || PyDict_Update(merged, labels) < 0) goto error;
        if (PyDict_GET_SIZE(merged) != 0) {
            if (wreath_writer_write(&writer, "|#", 2) < 0) goto error;
            items = PyDict_Items(merged);
            if (items == NULL) goto error;
            Py_ssize_t count = PyList_GET_SIZE(items);
            for (Py_ssize_t index = 0; index < count; index++) {
                PyObject *item = PyList_GET_ITEM(items, index);
                if ((index != 0 && wreath_writer_byte(&writer, ',') < 0) ||
                    statsd_write_sanitized(
                        &writer, PyTuple_GET_ITEM(item, 0), 1) < 0 ||
                    wreath_writer_byte(&writer, ':') < 0) goto error;
                PyObject *tag_value = PyObject_Str(PyTuple_GET_ITEM(item, 1));
                int written = tag_value != NULL
                    ? statsd_write_sanitized(&writer, tag_value, 1) : -1;
                Py_XDECREF(tag_value);
                if (written < 0) goto error;
            }
        }
    }
    if (statsd_sink_line(sink, &writer) < 0) goto error;
    Py_XDECREF(items);
    Py_XDECREF(merged);
    Py_DECREF(number);
    return 0;

error:
    Py_XDECREF(items);
    Py_XDECREF(merged);
    Py_XDECREF(number);
    Py_XDECREF(writer.bytes);
    return -1;
}

static int
statsd_emit(StatsdSink *sink, PyObject *prefix, int dogstatsd,
            PyObject *static_tags, const char *name, PyObject *value,
            char kind, PyObject *labels)
{
    return statsd_emit_parts(
        sink, prefix, dogstatsd, static_tags, name, NULL, NULL,
        value, kind, labels);
}

static int
statsd_emit_counter(StatsdSink *sink, PyObject *prefix, int dogstatsd,
                    PyObject *static_tags, PyObject *subsystem, PyObject *name,
                    PyObject *value, PyObject *labels)
{
    return statsd_emit_parts(
        sink, prefix, dogstatsd, static_tags, NULL, subsystem, name,
        value, 'g', labels);
}

static PyObject *
statsd_optional_attr(PyObject *object, ObservabilityAttr attribute,
                     PyObject *fallback)
{
    PyObject *value = observability_getattr(object, attribute);
    if (value != NULL) return value;
    if (!PyErr_ExceptionMatches(PyExc_AttributeError)) return NULL;
    PyErr_Clear();
    return Py_NewRef(fallback);
}

static PyObject *
statsd_int_attr(PyObject *object, ObservabilityAttr attribute)
{
    PyObject *value = statsd_optional_attr(object, attribute, Py_False);
    if (value == NULL) return NULL;
    PyObject *integer = PyNumber_Long(value);
    Py_DECREF(value);
    return integer;
}

static PyObject *
statsd_int_attr_string(PyObject *object, const char *name)
{
    PyObject *value;
    if (PyObject_GetOptionalAttrString(object, name, &value) < 0) return NULL;
    if (value == NULL) value = Py_NewRef(Py_False);
    PyObject *integer = PyNumber_Long(value);
    Py_DECREF(value);
    return integer;
}

static PyObject *
statsd_ms_attr(PyObject *object, ObservabilityAttr attribute)
{
    PyObject *value = observability_getattr(object, attribute);
    if (value == NULL) return NULL;
    double number = PyFloat_AsDouble(value);
    Py_DECREF(value);
    return PyErr_Occurred() ? NULL : PyFloat_FromDouble(number / 1000.0);
}

static PyObject *
statsd_reason_name(PyObject *reason)
{
    PyObject *name = observability_getattr(reason, OBS_NAME);
    if (name == NULL) {
        if (!PyErr_ExceptionMatches(PyExc_AttributeError)) return NULL;
        PyErr_Clear();
        name = PyObject_Str(reason);
    }
    if (name == NULL) return NULL;
    PyObject *lower = PyObject_CallMethod(name, "lower", NULL);
    Py_DECREF(name);
    return lower;
}

static int
statsd_render(StatsdSink *sink, PyObject *snapshot, PyObject *recorder_loss,
              PyObject *prefix, int dogstatsd, PyObject *static_tags,
              PyObject *route_resolver, MetricDeltaState *state)
{
    PyObject *empty_labels = PyDict_New();
    PyObject *empty_routes = PyTuple_New(0);
    PyObject *routes_value = empty_routes != NULL
        ? statsd_optional_attr(snapshot, OBS_ROUTES, empty_routes) : NULL;
    PyObject *routes = NULL;
    if (routes_value != NULL) {
        int truth = PyObject_IsTrue(routes_value);
        routes = truth < 0 ? NULL : truth
            ? PySequence_Tuple(routes_value) : PyTuple_New(0);
    }
    Py_XDECREF(routes_value);
    Py_XDECREF(empty_routes);
    if (empty_labels == NULL || routes == NULL) goto error;

    Py_ssize_t route_count = PyTuple_GET_SIZE(routes);
    for (Py_ssize_t index = 0; index < route_count; index++) {
        PyObject *route = PyTuple_GET_ITEM(routes, index);
        PyObject *route_id = observability_getattr(route, OBS_ROUTE_ID);
        PyObject *labels = route_id != NULL
            ? statsd_route_labels(route_resolver, route_id) : NULL;
        PyObject *value = route_id != NULL ? statsd_int_attr(route, OBS_COUNT) : NULL;
        value = value != NULL ? statsd_delta(state, 1, route_id, value) : NULL;
        if (labels == NULL || value == NULL || statsd_emit(
                sink, prefix, dogstatsd, static_tags, "http.requests",
                value, 'c', labels) < 0) {
            Py_XDECREF(value); Py_XDECREF(labels); Py_XDECREF(route_id); goto error;
        }
        Py_DECREF(value);
        value = statsd_int_attr(route, OBS_ERRORS);
        value = value != NULL ? statsd_delta(state, 2, route_id, value) : NULL;
        if (value == NULL || statsd_emit(
                sink, prefix, dogstatsd, static_tags, "http.errors",
                value, 'c', labels) < 0) {
            Py_XDECREF(value); Py_DECREF(labels); Py_DECREF(route_id); goto error;
        }
        Py_DECREF(value);
        value = statsd_ms_attr(route, OBS_DURATION_US_SUM);
        value = value != NULL ? statsd_delta(state, 3, route_id, value) : NULL;
        if (value == NULL || statsd_emit(
                sink, prefix, dogstatsd, static_tags, "http.duration.sum_ms",
                value, 'c', labels) < 0) {
            Py_XDECREF(value); Py_DECREF(labels); Py_DECREF(route_id); goto error;
        }
        Py_DECREF(value);
        value = statsd_ms_attr(route, OBS_DURATION_US_MAX);
        if (value == NULL || statsd_emit(
                sink, prefix, dogstatsd, static_tags, "http.duration.max_ms",
                value, 'g', labels) < 0) {
            Py_XDECREF(value); Py_DECREF(labels); Py_DECREF(route_id); goto error;
        }
        Py_DECREF(value);
        Py_DECREF(labels);
        Py_DECREF(route_id);
        if ((index & 63) == 63 && PyErr_CheckSignals() < 0) goto error;
    }
    Py_DECREF(routes); routes = NULL;

    PyObject *value = statsd_int_attr(snapshot, OBS_ASSEMBLED);
    value = value != NULL ? statsd_delta(state, 4, NULL, value) : NULL;
    if (value == NULL || statsd_emit(
            sink, prefix, dogstatsd, static_tags, "flight.assembled",
            value, 'c', empty_labels) < 0) {
        Py_XDECREF(value); goto error;
    }
    Py_DECREF(value);
    value = statsd_int_attr(snapshot, OBS_PENDING);
    if (value == NULL || statsd_emit(
            sink, prefix, dogstatsd, static_tags, "flight.pending",
            value, 'g', empty_labels) < 0) {
        Py_XDECREF(value); goto error;
    }
    Py_DECREF(value);

    static const char *loss_fields[] = {
        "orphan_phase", "orphan_correlation", "pending_evicted",
        "decode_error", "export_error", "recent_evicted",
    };
    PyObject *loss = statsd_optional_attr(snapshot, OBS_LOSS, Py_None);
    if (loss == NULL) goto error;
    for (int index = 0; index < 6; index++) {
        PyObject *labels = Py_BuildValue("{s:s}", "reason", loss_fields[index]);
        value = statsd_int_attr_string(loss, loss_fields[index]);
        value = value != NULL
            ? statsd_delta(state, (unsigned char)(10 + index), NULL, value) : NULL;
        if (labels == NULL || value == NULL || statsd_emit(
                sink, prefix, dogstatsd, static_tags, "flight.projector_loss",
                value, 'c', labels) < 0) {
            Py_XDECREF(value); Py_XDECREF(labels); Py_DECREF(loss); goto error;
        }
        Py_DECREF(value);
        Py_DECREF(labels);
    }
    Py_DECREF(loss);

    if (recorder_loss != Py_None) {
        int truth = PyObject_IsTrue(recorder_loss);
        PyObject *items = truth > 0 ? PyMapping_Items(recorder_loss) : NULL;
        if (truth < 0 || (truth > 0 && items == NULL)) goto error;
        if (items != NULL) {
            Py_ssize_t count = PyList_GET_SIZE(items);
            for (Py_ssize_t index = 0; index < count; index++) {
                PyObject *item = PyList_GET_ITEM(items, index);
                PyObject *name = statsd_reason_name(PyTuple_GET_ITEM(item, 0));
                PyObject *labels = name != NULL
                    ? Py_BuildValue("{s:O}", "reason", name) : NULL;
                value = labels != NULL
                    ? PyNumber_Long(PyTuple_GET_ITEM(item, 1)) : NULL;
                value = value != NULL
                    ? statsd_delta(state, 20, name, value) : NULL;
                if (value == NULL || statsd_emit(
                        sink, prefix, dogstatsd, static_tags,
                        "flight.recorder_loss", value, 'c', labels) < 0) {
                    Py_XDECREF(value); Py_XDECREF(labels); Py_XDECREF(name);
                    Py_DECREF(items); goto error;
                }
                Py_DECREF(value);
                Py_DECREF(labels);
                Py_DECREF(name);
            }
            Py_DECREF(items);
        }
    }
    Py_DECREF(empty_labels);
    return 0;

error:
    Py_XDECREF(routes);
    Py_XDECREF(empty_labels);
    return -1;
}

static int
statsd_render_counter_item(StatsdSink *sink, PyObject *prefix, int dogstatsd,
                           PyObject *static_tags, PyObject *subsystem,
                           PyObject *labels, PyObject *raw_name,
                           PyObject *raw_value)
{
    PyObject *name = PyObject_Str(raw_name);
    PyObject *value = name != NULL ? PyNumber_Long(raw_value) : NULL;
    int status = name != NULL && value != NULL
        ? statsd_emit_counter(
            sink, prefix, dogstatsd, static_tags, subsystem,
            name, value, labels)
        : -1;
    Py_XDECREF(value);
    Py_XDECREF(name);
    return status;
}

static int
statsd_render_counters(StatsdSink *sink, PyObject *readings, PyObject *prefix,
                       int dogstatsd, PyObject *static_tags)
{
    Py_ssize_t reading_count = PyTuple_GET_SIZE(readings);
    for (Py_ssize_t reading_index = 0;
         reading_index < reading_count; reading_index++) {
        PyObject *reading = PyTuple_GET_ITEM(readings, reading_index);
        PyObject *subsystem_raw = observability_getattr(reading, OBS_SUBSYSTEM);
        PyObject *instance_raw = observability_getattr(reading, OBS_INSTANCE);
        PyObject *values = observability_getattr(reading, OBS_VALUES);
        PyObject *subsystem = subsystem_raw != NULL
            ? PyObject_Str(subsystem_raw) : NULL;
        PyObject *instance = instance_raw != NULL
            ? PyObject_Str(instance_raw) : NULL;
        PyObject *labels = instance != NULL
            ? Py_BuildValue("{s:O}", "instance", instance) : NULL;
        PyObject *items = NULL;
        Py_XDECREF(subsystem_raw);
        Py_XDECREF(instance_raw);
        Py_XDECREF(instance);
        if (subsystem == NULL || labels == NULL || values == NULL) {
            Py_XDECREF(values);
            Py_XDECREF(labels);
            Py_XDECREF(subsystem);
            return -1;
        }
        if (PyDict_CheckExact(values)) {
            Py_ssize_t position = 0;
            PyObject *raw_name;
            PyObject *raw_value;
            while (PyDict_Next(values, &position, &raw_name, &raw_value)) {
                if (statsd_render_counter_item(
                        sink, prefix, dogstatsd, static_tags, subsystem,
                        labels, raw_name, raw_value) < 0) goto reading_error;
            }
        }
        else {
            items = PyMapping_Items(values);
            if (items == NULL) goto reading_error;
            Py_ssize_t value_count = PyList_GET_SIZE(items);
            for (Py_ssize_t value_index = 0;
                 value_index < value_count; value_index++) {
                PyObject *item = PyList_GET_ITEM(items, value_index);
                if (statsd_render_counter_item(
                        sink, prefix, dogstatsd, static_tags, subsystem, labels,
                        PyTuple_GET_ITEM(item, 0),
                        PyTuple_GET_ITEM(item, 1)) < 0) goto reading_error;
            }
        }
        Py_XDECREF(items);
        Py_DECREF(values);
        Py_DECREF(labels);
        Py_DECREF(subsystem);
        if ((reading_index & 63) == 63 && PyErr_CheckSignals() < 0) return -1;
        continue;

reading_error:
        Py_XDECREF(items);
        Py_DECREF(values);
        Py_DECREF(labels);
        Py_DECREF(subsystem);
        return -1;
    }
    return 0;
}

PyObject *
wreath_statsd_lines(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *snapshot, *recorder_loss, *prefix, *static_tags;
    PyObject *route_resolver, *state_capsule;
    int dogstatsd;
    if (!PyArg_ParseTuple(args, "OOUiO!OO:statsd_lines", &snapshot,
                          &recorder_loss, &prefix, &dogstatsd,
                          &PyDict_Type, &static_tags, &route_resolver,
                          &state_capsule)) return NULL;
    MetricDeltaState *state = metric_delta_state(state_capsule);
    StatsdSink sink;
    if (state == NULL || statsd_sink_init(&sink, 0, 0) < 0) return NULL;
    if (statsd_render(
            &sink, snapshot, recorder_loss, prefix, dogstatsd,
            static_tags, route_resolver, state) < 0) {
        statsd_sink_clear(&sink);
        return NULL;
    }
    PyObject *result = sink.out;
    sink.out = NULL;
    statsd_sink_clear(&sink);
    return result;
}

PyObject *
wreath_statsd_packets(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *snapshot, *recorder_loss, *prefix, *static_tags;
    PyObject *route_resolver, *state_capsule, *readings;
    Py_ssize_t max_packet_bytes;
    int dogstatsd;
    if (!PyArg_ParseTuple(args, "OOUiO!OOO!n:statsd_packets", &snapshot,
                          &recorder_loss, &prefix, &dogstatsd,
                          &PyDict_Type, &static_tags, &route_resolver,
                          &state_capsule, &PyTuple_Type, &readings,
                          &max_packet_bytes)) return NULL;
    if (max_packet_bytes <= 0) {
        PyErr_SetString(PyExc_ValueError, "StatsD max packet bytes must be positive");
        return NULL;
    }
    MetricDeltaState *state = metric_delta_state(state_capsule);
    StatsdSink sink;
    if (state == NULL || statsd_sink_init(
            &sink, 1, max_packet_bytes) < 0) return NULL;
    if (statsd_render(
            &sink, snapshot, recorder_loss, prefix, dogstatsd,
            static_tags, route_resolver, state) < 0 ||
        statsd_render_counters(
            &sink, readings, prefix, dogstatsd, static_tags) < 0 ||
        statsd_sink_flush_packet(&sink) < 0) {
        statsd_sink_clear(&sink);
        return NULL;
    }
    PyObject *packets = PyList_AsTuple(sink.out);
    PyObject *line_count = packets != NULL
        ? PyLong_FromSsize_t(sink.line_count) : NULL;
    PyObject *result = wreath_tuple2_from_owned(packets, line_count);
    statsd_sink_clear(&sink);
    return result;
}

typedef struct {
    char *key;
    Py_ssize_t key_length;
    char *value;
    Py_ssize_t value_length;
} MetricLabel;

typedef struct {
    MetricLabel *items;
    Py_ssize_t count;
} MetricLabelTape;

typedef struct {
    const char *name;
    Py_ssize_t name_length;
    const char *unit;
    Py_ssize_t unit_length;
    uint64_t integer;
    double real;
    unsigned char is_real;
    unsigned char owns_name;
    PyObject *py_integer;
} EmfMetric;

static void
metric_labels_clear(MetricLabelTape *tape)
{
    for (Py_ssize_t index = 0; index < tape->count; index++) {
        PyMem_Free(tape->items[index].value);
        PyMem_Free(tape->items[index].key);
    }
    PyMem_Free(tape->items);
    *tape = (MetricLabelTape){0};
}

static int
metric_copy_unicode(PyObject *value, char **copy, Py_ssize_t *length)
{
    const char *data = PyUnicode_AsUTF8AndSize(value, length);
    if (data == NULL) return -1;
    *copy = *length != 0 ? PyMem_Malloc((size_t)*length) : NULL;
    if (*length != 0 && *copy == NULL) return PyErr_NoMemory(), -1;
    if (*length != 0) memcpy(*copy, data, (size_t)*length);
    return 0;
}

static int
metric_labels_from_mapping(PyObject *mapping, MetricLabelTape *tape)
{
    PyObject *items = PyMapping_Items(mapping);
    if (items == NULL) return -1;
    Py_ssize_t count = PyList_GET_SIZE(items);
    if (count != 0) {
        tape->items = PyMem_Calloc((size_t)count, sizeof(*tape->items));
        if (tape->items == NULL) {
            Py_DECREF(items);
            return PyErr_NoMemory(), -1;
        }
    }
    for (Py_ssize_t index = 0; index < count; index++) {
        PyObject *item = PyList_GET_ITEM(items, index);
        PyObject *key = PyTuple_GET_ITEM(item, 0);
        PyObject *value = PyObject_Str(PyTuple_GET_ITEM(item, 1));
        if (!PyUnicode_Check(key)) {
            Py_XDECREF(value);
            PyErr_SetString(PyExc_TypeError, "metric label names must be str");
            goto error;
        }
        MetricLabel *label = &tape->items[index];
        if (value == NULL || metric_copy_unicode(
                key, &label->key, &label->key_length) < 0 ||
            metric_copy_unicode(
                value, &label->value, &label->value_length) < 0) {
            Py_XDECREF(value);
            goto error;
        }
        Py_DECREF(value);
        tape->count++;
    }
    Py_DECREF(items);
    return 0;

error:
    Py_DECREF(items);
    metric_labels_clear(tape);
    return -1;
}

static int
metric_default_route_label(uint64_t route_id, MetricLabelTape *tape)
{
    tape->items = PyMem_Calloc(1, sizeof(*tape->items));
    if (tape->items == NULL) return PyErr_NoMemory(), -1;
    tape->items[0].key = PyMem_Malloc(8);
    if (tape->items[0].key == NULL) {
        metric_labels_clear(tape);
        return PyErr_NoMemory(), -1;
    }
    memcpy(tape->items[0].key, "route_id", 8);
    tape->items[0].key_length = 8;
    tape->count = 1;
    char reversed[20];
    Py_ssize_t length = 0;
    do {
        reversed[length++] = (char)('0' + route_id % 10U);
        route_id /= 10U;
    } while (route_id != 0);
    tape->items[0].value = PyMem_Malloc((size_t)length);
    if (tape->items[0].value == NULL) {
        metric_labels_clear(tape);
        return PyErr_NoMemory(), -1;
    }
    for (Py_ssize_t index = 0; index < length; index++)
        tape->items[0].value[index] = reversed[length - index - 1];
    tape->items[0].value_length = length;
    return 0;
}

static int
metric_route_label_tape(PyObject *resolver, PyObject *route_id,
                        uint64_t route_number, MetricLabelTape *tape)
{
    if (resolver == Py_None) return metric_default_route_label(route_number, tape);
    int mapping = PyObject_HasAttrString(resolver, "get");
    if (mapping < 0) return -1;
    PyObject *resolved = mapping
        ? PyObject_CallMethod(resolver, "get", "O", route_id)
        : PyObject_CallOneArg(resolver, route_id);
    if (resolved == NULL) return -1;
    int truth = PyObject_IsTrue(resolved);
    if (truth <= 0) {
        Py_DECREF(resolved);
        return truth < 0 ? -1 : metric_default_route_label(route_number, tape);
    }
    int result = metric_labels_from_mapping(resolved, tape);
    Py_DECREF(resolved);
    return result;
}

static int
metric_label_equal(const MetricLabel *left, const MetricLabel *right)
{
    return left->key_length == right->key_length &&
           (left->key_length == 0 || memcmp(
               left->key, right->key, (size_t)left->key_length) == 0);
}

static const MetricLabel *
metric_label_find(const MetricLabelTape *tape, const MetricLabel *wanted)
{
    for (Py_ssize_t index = 0; index < tape->count; index++)
        if (metric_label_equal(&tape->items[index], wanted)) return &tape->items[index];
    return NULL;
}

static int
emf_write_json_string(WreathBytesWriter *writer, const char *data, Py_ssize_t length)
{
    static const char hex[] = "0123456789abcdef";
    if (wreath_writer_byte(writer, '"') < 0) return -1;
    Py_ssize_t start = 0;
    for (Py_ssize_t index = 0; index < length; index++) {
        const char *escape = NULL;
        char unicode[6];
        unsigned char byte = (unsigned char)data[index];
        if (byte == '"') escape = "\\\"";
        else if (byte == '\\') escape = "\\\\";
        else if (byte == '\b') escape = "\\b";
        else if (byte == '\f') escape = "\\f";
        else if (byte == '\n') escape = "\\n";
        else if (byte == '\r') escape = "\\r";
        else if (byte == '\t') escape = "\\t";
        else if (byte < 0x20) {
            unicode[0] = '\\'; unicode[1] = 'u'; unicode[2] = '0'; unicode[3] = '0';
            unicode[4] = hex[byte >> 4]; unicode[5] = hex[byte & 15];
            escape = unicode;
        }
        else continue;
        if (wreath_writer_write(writer, data + start, index - start) < 0 ||
            wreath_writer_write(writer, escape, byte < 0x20 && escape == unicode ? 6 : 2) < 0)
            return -1;
        start = index + 1;
    }
    return wreath_writer_write(writer, data + start, length - start) < 0
        ? -1 : wreath_writer_byte(writer, '"');
}

static int
emf_write_double(WreathBytesWriter *writer, double value)
{
    if (!isfinite(value)) {
        PyErr_SetString(PyExc_ValueError, "EMF metric values must be finite");
        return -1;
    }
    char *text = PyOS_double_to_string(value, 'r', 0, Py_DTSF_ADD_DOT_0, NULL);
    if (text == NULL) return -1;
    int result = wreath_writer_write(writer, text, (Py_ssize_t)strlen(text));
    PyMem_Free(text);
    return result;
}

static int
emf_write_metric_value(WreathBytesWriter *writer, const EmfMetric *metric)
{
    if (metric->py_integer != NULL) {
        PyObject *text = PyObject_Str(metric->py_integer);
        if (text == NULL) return -1;
        Py_ssize_t length;
        const char *data = PyUnicode_AsUTF8AndSize(text, &length);
        int result = data == NULL ? -1
            : wreath_writer_write(writer, data, length);
        Py_DECREF(text);
        return result;
    }
    return metric->is_real
        ? emf_write_double(writer, metric->real)
        : prom_write_uint64(writer, metric->integer);
}

static int
emf_write_label_pair(WreathBytesWriter *writer, const MetricLabel *label,
                     int *first)
{
    if ((!*first && wreath_writer_byte(writer, ',') < 0) ||
        emf_write_json_string(writer, label->key, label->key_length) < 0 ||
        wreath_writer_byte(writer, ':') < 0 ||
        emf_write_json_string(writer, label->value, label->value_length) < 0)
        return -1;
    *first = 0;
    return 0;
}

static int
emf_write_dimensions(WreathBytesWriter *writer, const MetricLabelTape *base,
                     const MetricLabelTape *route, int names_only)
{
    int first = 1;
    for (Py_ssize_t index = 0; index < base->count; index++) {
        const MetricLabel *label = &base->items[index];
        const MetricLabel *override = route != NULL
            ? metric_label_find(route, label) : NULL;
        const MetricLabel *chosen = override != NULL ? override : label;
        if ((!first && wreath_writer_byte(writer, ',') < 0) ||
            emf_write_json_string(writer, chosen->key, chosen->key_length) < 0 ||
            (!names_only && (wreath_writer_byte(writer, ':') < 0 ||
                emf_write_json_string(
                    writer, chosen->value, chosen->value_length) < 0))) return -1;
        first = 0;
    }
    if (route != NULL) {
        for (Py_ssize_t index = 0; index < route->count; index++) {
            const MetricLabel *label = &route->items[index];
            if (metric_label_find(base, label) != NULL) continue;
            if ((!first && wreath_writer_byte(writer, ',') < 0) ||
                emf_write_json_string(writer, label->key, label->key_length) < 0 ||
                (!names_only && (wreath_writer_byte(writer, ':') < 0 ||
                    emf_write_json_string(
                        writer, label->value, label->value_length) < 0))) return -1;
            first = 0;
        }
    }
    return 0;
}

static int
emf_write_document(WreathBytesWriter *writer, const MetricLabelTape *base,
                   const MetricLabelTape *route, const EmfMetric *metrics,
                   Py_ssize_t metric_count, const char *namespace,
                   Py_ssize_t namespace_length, uint64_t timestamp)
{
    if (writer->len != 0 && wreath_writer_byte(writer, '\n') < 0) return -1;
    if (wreath_writer_byte(writer, '{') < 0) return -1;
    int first = 1;
    for (Py_ssize_t index = 0; index < base->count; index++) {
        const MetricLabel *label = &base->items[index];
        const MetricLabel *override = route != NULL
            ? metric_label_find(route, label) : NULL;
        if (emf_write_label_pair(
                writer, override != NULL ? override : label, &first) < 0) return -1;
    }
    if (route != NULL) {
        for (Py_ssize_t index = 0; index < route->count; index++) {
            const MetricLabel *label = &route->items[index];
            if (metric_label_find(base, label) == NULL &&
                emf_write_label_pair(writer, label, &first) < 0) return -1;
        }
    }
    for (Py_ssize_t index = 0; index < metric_count; index++) {
        if ((!first && wreath_writer_byte(writer, ',') < 0) ||
            emf_write_json_string(
                writer, metrics[index].name, metrics[index].name_length) < 0 ||
            wreath_writer_byte(writer, ':') < 0 ||
            emf_write_metric_value(writer, &metrics[index]) < 0) return -1;
        first = 0;
    }
    if ((!first && wreath_writer_byte(writer, ',') < 0) ||
        wreath_writer_write(
            writer, "\"_aws\":{\"Timestamp\":",
            (Py_ssize_t)(sizeof("\"_aws\":{\"Timestamp\":") - 1)) < 0 ||
        prom_write_uint64(writer, timestamp) < 0 ||
        wreath_writer_write(
            writer, ",\"CloudWatchMetrics\":[{\"Namespace\":",
            (Py_ssize_t)(sizeof(",\"CloudWatchMetrics\":[{\"Namespace\":") - 1)) < 0 ||
        emf_write_json_string(writer, namespace, namespace_length) < 0 ||
        wreath_writer_write(
            writer, ",\"Dimensions\":[[",
            (Py_ssize_t)(sizeof(",\"Dimensions\":[[") - 1)) < 0 ||
        emf_write_dimensions(writer, base, route, 1) < 0 ||
        wreath_writer_write(
            writer, "]],\"Metrics\":[",
            (Py_ssize_t)(sizeof("]],\"Metrics\":[") - 1)) < 0) return -1;
    for (Py_ssize_t index = 0; index < metric_count; index++) {
        if ((index != 0 && wreath_writer_byte(writer, ',') < 0) ||
            wreath_writer_write(
                writer, "{\"Name\":",
                (Py_ssize_t)(sizeof("{\"Name\":") - 1)) < 0 ||
            emf_write_json_string(
                writer, metrics[index].name, metrics[index].name_length) < 0 ||
            wreath_writer_write(
                writer, ",\"Unit\":",
                (Py_ssize_t)(sizeof(",\"Unit\":") - 1)) < 0 ||
            emf_write_json_string(
                writer, metrics[index].unit, metrics[index].unit_length) < 0 ||
            wreath_writer_byte(writer, '}') < 0) return -1;
    }
    return wreath_writer_write(
        writer, "]}]}}", (Py_ssize_t)(sizeof("]}]}}") - 1));
}

static int
metric_u64_attr(PyObject *object, ObservabilityAttr attribute, int optional,
                uint64_t *result)
{
    PyObject *value = optional
        ? statsd_optional_attr(object, attribute, Py_False)
        : observability_getattr(object, attribute);
    if (value == NULL) return -1;
    PyObject *integer = PyNumber_Long(value);
    Py_DECREF(value);
    if (integer == NULL) return -1;
    *result = PyLong_AsUnsignedLongLong(integer);
    Py_DECREF(integer);
    return PyErr_Occurred() ? -1 : 0;
}

static int
metric_u64_attr_string(PyObject *object, const char *name, uint64_t *result)
{
    PyObject *integer = statsd_int_attr_string(object, name);
    if (integer == NULL) return -1;
    *result = PyLong_AsUnsignedLongLong(integer);
    Py_DECREF(integer);
    return PyErr_Occurred() ? -1 : 0;
}

static int
metric_double_attr(PyObject *object, ObservabilityAttr attribute, double *result)
{
    PyObject *value = observability_getattr(object, attribute);
    if (value == NULL) return -1;
    *result = PyFloat_AsDouble(value);
    Py_DECREF(value);
    return PyErr_Occurred() ? -1 : 0;
}

static void
emf_metrics_clear(EmfMetric *metrics, Py_ssize_t count)
{
    for (Py_ssize_t index = 0; index < count; index++) {
        Py_XDECREF(metrics[index].py_integer);
        if (metrics[index].owns_name) PyMem_Free((void *)metrics[index].name);
    }
}

static int
emf_dynamic_metric(EmfMetric *metric, const char *prefix,
                   PyObject *suffix, uint64_t value)
{
    Py_ssize_t suffix_length;
    const char *suffix_data = PyUnicode_AsUTF8AndSize(suffix, &suffix_length);
    Py_ssize_t prefix_length = (Py_ssize_t)strlen(prefix);
    if (suffix_data == NULL || suffix_length > PY_SSIZE_T_MAX - prefix_length)
        return suffix_data == NULL ? -1 : (PyErr_NoMemory(), -1);
    char *name = PyMem_Malloc((size_t)(prefix_length + suffix_length));
    if (name == NULL) return PyErr_NoMemory(), -1;
    memcpy(name, prefix, (size_t)prefix_length);
    memcpy(name + prefix_length, suffix_data, (size_t)suffix_length);
    *metric = (EmfMetric){
        name, prefix_length + suffix_length, "Count", 5, value, 0.0, 0, 1,
    };
    return 0;
}

static int
emf_counter_metric(EmfMetric *metric, PyObject *subsystem,
                   PyObject *name, PyObject *value)
{
    Py_ssize_t subsystem_length, name_length;
    const char *subsystem_data = PyUnicode_AsUTF8AndSize(
        subsystem, &subsystem_length);
    const char *name_data = PyUnicode_AsUTF8AndSize(name, &name_length);
    if (subsystem_data == NULL || name_data == NULL ||
        subsystem_length > PY_SSIZE_T_MAX - 1 ||
        name_length > PY_SSIZE_T_MAX - 1 - subsystem_length)
        return PyErr_Occurred() ? -1 : (PyErr_NoMemory(), -1);
    Py_ssize_t length = subsystem_length + 1 + name_length;
    char *wire_name = PyMem_Malloc((size_t)length);
    if (wire_name == NULL) return PyErr_NoMemory(), -1;
    memcpy(wire_name, subsystem_data, (size_t)subsystem_length);
    wire_name[subsystem_length] = '_';
    memcpy(
        wire_name + subsystem_length + 1, name_data, (size_t)name_length);
    *metric = (EmfMetric){
        .name = wire_name,
        .name_length = length,
        .unit = "None",
        .unit_length = 4,
        .owns_name = 1,
        .py_integer = Py_NewRef(value),
    };
    return 0;
}

static int
emf_counter_delta(MetricDeltaState *state, PyObject *subsystem,
                  PyObject *instance, PyObject *name, PyObject *current,
                  PyObject **result)
{
    Py_ssize_t subsystem_length, instance_length, name_length;
    const char *subsystem_data = PyUnicode_AsUTF8AndSize(
        subsystem, &subsystem_length);
    const char *instance_data = PyUnicode_AsUTF8AndSize(
        instance, &instance_length);
    const char *name_data = PyUnicode_AsUTF8AndSize(name, &name_length);
    if (subsystem_data == NULL || instance_data == NULL || name_data == NULL ||
        subsystem_length > PY_SSIZE_T_MAX - 2 ||
        instance_length > PY_SSIZE_T_MAX - 2 - subsystem_length ||
        name_length > PY_SSIZE_T_MAX - 2 - subsystem_length - instance_length)
        return PyErr_Occurred() ? -1 : (PyErr_NoMemory(), -1);
    Py_ssize_t length = subsystem_length + instance_length + name_length + 2;
    char *key = PyMem_Malloc((size_t)length);
    if (key == NULL) return PyErr_NoMemory(), -1;
    char *cursor = key;
    memcpy(cursor, subsystem_data, (size_t)subsystem_length);
    cursor += subsystem_length;
    *cursor++ = '\0';
    memcpy(cursor, instance_data, (size_t)instance_length);
    cursor += instance_length;
    *cursor++ = '\0';
    memcpy(cursor, name_data, (size_t)name_length);
    int status = metric_delta_pyint(
        state, 30, key, length, current, result);
    PyMem_Free(key);
    return status;
}

static int
emf_instance_labels(PyObject *instance, MetricLabelTape *labels)
{
    labels->items = PyMem_Calloc(1, sizeof(*labels->items));
    if (labels->items == NULL) return PyErr_NoMemory(), -1;
    labels->count = 1;
    labels->items[0].key = PyMem_Malloc(8);
    if (labels->items[0].key == NULL) {
        metric_labels_clear(labels);
        return PyErr_NoMemory(), -1;
    }
    memcpy(labels->items[0].key, "Instance", 8);
    labels->items[0].key_length = 8;
    if (metric_copy_unicode(
            instance, &labels->items[0].value,
            &labels->items[0].value_length) < 0) {
        metric_labels_clear(labels);
        return -1;
    }
    return 0;
}

static int
emf_render_counters(WreathBytesWriter *writer, const MetricLabelTape *base,
                    PyObject *readings, const char *namespace,
                    Py_ssize_t namespace_length, uint64_t timestamp,
                    int cumulative, MetricDeltaState *state,
                    Py_ssize_t max_metrics)
{
    Py_ssize_t reading_count = PyTuple_GET_SIZE(readings);
    for (Py_ssize_t reading_index = 0;
         reading_index < reading_count; reading_index++) {
        PyObject *reading = PyTuple_GET_ITEM(readings, reading_index);
        PyObject *subsystem_raw = observability_getattr(reading, OBS_SUBSYSTEM);
        PyObject *instance_raw = observability_getattr(reading, OBS_INSTANCE);
        PyObject *values = observability_getattr(reading, OBS_VALUES);
        PyObject *gauges = observability_getattr(reading, OBS_GAUGES);
        PyObject *subsystem = subsystem_raw != NULL
            ? PyObject_Str(subsystem_raw) : NULL;
        PyObject *instance = instance_raw != NULL
            ? PyObject_Str(instance_raw) : NULL;
        PyObject *items = NULL;
        MetricLabelTape labels = {0};
        Py_XDECREF(subsystem_raw);
        Py_XDECREF(instance_raw);
        if (subsystem == NULL || instance == NULL || values == NULL ||
            gauges == NULL || emf_instance_labels(instance, &labels) < 0) {
            Py_XDECREF(items);
            Py_XDECREF(values);
            Py_XDECREF(gauges);
            Py_XDECREF(instance);
            Py_XDECREF(subsystem);
            metric_labels_clear(&labels);
            return -1;
        }

        Py_ssize_t value_count;
        Py_ssize_t dict_position = 0;
        if (PyDict_CheckExact(values)) {
            value_count = PyDict_GET_SIZE(values);
        }
        else {
            items = PyMapping_Items(values);
            if (items == NULL) {
                Py_DECREF(values); Py_DECREF(gauges);
                Py_DECREF(instance); Py_DECREF(subsystem);
                metric_labels_clear(&labels);
                return -1;
            }
            value_count = PyList_GET_SIZE(items);
        }
        for (Py_ssize_t offset = 0; offset < value_count;
             offset += max_metrics) {
            Py_ssize_t batch_count = value_count - offset;
            if (batch_count > max_metrics) batch_count = max_metrics;
            EmfMetric *metrics = PyMem_Calloc(
                (size_t)batch_count, sizeof(*metrics));
            if (metrics == NULL) {
                PyErr_NoMemory();
                Py_XDECREF(items); Py_DECREF(values); Py_DECREF(gauges);
                Py_DECREF(instance); Py_DECREF(subsystem);
                metric_labels_clear(&labels);
                return -1;
            }
            Py_ssize_t built = 0;
            for (; built < batch_count; built++) {
                PyObject *name_raw;
                PyObject *value_raw;
                if (items != NULL) {
                    PyObject *item = PyList_GET_ITEM(items, offset + built);
                    name_raw = PyTuple_GET_ITEM(item, 0);
                    value_raw = PyTuple_GET_ITEM(item, 1);
                }
                else if (!PyDict_Next(
                             values, &dict_position, &name_raw, &value_raw)) {
                    PyErr_SetString(PyExc_RuntimeError,
                                    "counter mapping changed during rendering");
                    emf_metrics_clear(metrics, built);
                    PyMem_Free(metrics);
                    Py_DECREF(values); Py_DECREF(gauges);
                    Py_DECREF(instance); Py_DECREF(subsystem);
                    metric_labels_clear(&labels);
                    return -1;
                }
                PyObject *name = PyObject_Str(name_raw);
                PyObject *integer = name != NULL
                    ? PyNumber_Long(value_raw) : NULL;
                int gauge = integer != NULL
                    ? PySet_Contains(gauges, name_raw) : -1;
                PyObject *emitted = integer != NULL ? Py_NewRef(integer) : NULL;
                if (emitted != NULL && !cumulative && !gauge) {
                    Py_CLEAR(emitted);
                    if (emf_counter_delta(
                            state, subsystem, instance, name,
                            integer, &emitted) < 0) emitted = NULL;
                }
                if (name == NULL || integer == NULL || gauge < 0 ||
                    emitted == NULL || emf_counter_metric(
                        &metrics[built], subsystem, name, emitted) < 0) {
                    Py_XDECREF(emitted);
                    Py_XDECREF(integer);
                    Py_XDECREF(name);
                    emf_metrics_clear(metrics, built);
                    PyMem_Free(metrics);
                    Py_XDECREF(items); Py_DECREF(values); Py_DECREF(gauges);
                    Py_DECREF(instance); Py_DECREF(subsystem);
                    metric_labels_clear(&labels);
                    return -1;
                }
                Py_DECREF(emitted);
                Py_DECREF(integer);
                Py_DECREF(name);
            }
            if (emf_write_document(
                    writer, base, &labels, metrics, batch_count,
                    namespace, namespace_length, timestamp) < 0) {
                emf_metrics_clear(metrics, batch_count);
                PyMem_Free(metrics);
                Py_XDECREF(items); Py_DECREF(values); Py_DECREF(gauges);
                Py_DECREF(instance); Py_DECREF(subsystem);
                metric_labels_clear(&labels);
                return -1;
            }
            emf_metrics_clear(metrics, batch_count);
            PyMem_Free(metrics);
        }
        Py_XDECREF(items);
        Py_DECREF(values);
        Py_DECREF(gauges);
        Py_DECREF(instance);
        Py_DECREF(subsystem);
        metric_labels_clear(&labels);
        if ((reading_index & 63) == 63 && PyErr_CheckSignals() < 0) return -1;
    }
    return 0;
}

PyObject *
wreath_emf_render(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *snapshot, *recorder_loss, *namespace_object, *dimensions_object;
    PyObject *resolver, *state_capsule, *readings = NULL;
    unsigned long long timestamp;
    int cumulative;
    Py_ssize_t max_metrics;
    if (!PyArg_ParseTuple(
            args, "OKOUO!OiOn|O!:emf_render", &snapshot, &timestamp,
            &recorder_loss, &namespace_object, &PyDict_Type, &dimensions_object,
            &resolver, &cumulative, &state_capsule, &max_metrics,
            &PyTuple_Type, &readings)) return NULL;
    MetricDeltaState *state = metric_delta_state(state_capsule);
    Py_ssize_t namespace_length;
    const char *namespace = PyUnicode_AsUTF8AndSize(
        namespace_object, &namespace_length);
    MetricLabelTape base = {0};
    WreathBytesWriter writer = {0};
    if (state == NULL || namespace == NULL || max_metrics < 8 ||
        metric_labels_from_mapping(dimensions_object, &base) < 0 ||
        wreath_writer_init(&writer, 4096) < 0) goto error;

    PyObject *empty = PyTuple_New(0);
    PyObject *routes_object = empty != NULL
        ? statsd_optional_attr(snapshot, OBS_ROUTES, empty) : NULL;
    PyObject *iterator = NULL;
    if (routes_object != NULL) {
        int truth = PyObject_IsTrue(routes_object);
        iterator = truth < 0 ? NULL : PyObject_GetIter(truth ? routes_object : empty);
    }
    Py_XDECREF(routes_object);
    Py_XDECREF(empty);
    if (iterator == NULL) goto error;
    PyObject *route;
    Py_ssize_t route_index = 0;
    while ((route = PyIter_Next(iterator)) != NULL) {
        PyObject *route_id_object = observability_getattr(route, OBS_ROUTE_ID);
        uint64_t route_id, count, errors;
        double duration_sum, duration_max;
        MetricLabelTape labels = {0};
        if (route_id_object == NULL ||
            (route_id = PyLong_AsUnsignedLongLong(route_id_object), PyErr_Occurred()) ||
            metric_route_label_tape(
                resolver, route_id_object, route_id, &labels) < 0 ||
            metric_u64_attr(route, OBS_COUNT, 0, &count) < 0 ||
            metric_u64_attr(route, OBS_ERRORS, 0, &errors) < 0 ||
            metric_double_attr(route, OBS_DURATION_US_SUM, &duration_sum) < 0 ||
            metric_double_attr(route, OBS_DURATION_US_MAX, &duration_max) < 0) {
            metric_labels_clear(&labels);
            Py_XDECREF(route_id_object);
            Py_DECREF(route);
            Py_DECREF(iterator);
            goto error;
        }
        Py_DECREF(route_id_object);
        Py_DECREF(route);
        uint64_t count_value = count, error_value = errors;
        double sum_value = duration_sum / 1000.0;
        if (!cumulative && (
                metric_delta_u64(state, 1, route_id, NULL, 0, count, &count_value) < 0 ||
                metric_delta_u64(state, 2, route_id, NULL, 0, errors, &error_value) < 0 ||
                metric_delta_double(state, 3, route_id, sum_value, &sum_value) < 0)) {
            metric_labels_clear(&labels);
            Py_DECREF(iterator);
            goto error;
        }
        EmfMetric metrics[4] = {
            {"Requests", 8, "Count", 5, count_value, 0.0, 0, 0},
            {"Errors", 6, "Count", 5, error_value, 0.0, 0, 0},
            {"DurationSum", 11, "Milliseconds", 12, 0, sum_value, 1, 0},
            {"DurationMax", 11, "Milliseconds", 12, 0, duration_max / 1000.0, 1, 0},
        };
        if (emf_write_document(
                &writer, &base, &labels, metrics, 4,
                namespace, namespace_length, timestamp) < 0) {
            metric_labels_clear(&labels);
            Py_DECREF(iterator);
            goto error;
        }
        metric_labels_clear(&labels);
        if ((route_index++ & 63) == 63 && PyErr_CheckSignals() < 0) {
            Py_DECREF(iterator);
            goto error;
        }
    }
    Py_DECREF(iterator);
    if (PyErr_Occurred()) goto error;

    EmfMetric *global = PyMem_Calloc((size_t)max_metrics, sizeof(*global));
    if (global == NULL) { PyErr_NoMemory(); goto error; }
    Py_ssize_t global_count = 0;
    uint64_t assembled, pending;
    if (metric_u64_attr(snapshot, OBS_ASSEMBLED, 1, &assembled) < 0 ||
        metric_u64_attr(snapshot, OBS_PENDING, 1, &pending) < 0) goto global_error;
    uint64_t assembled_value = assembled;
    if (!cumulative && metric_delta_u64(
            state, 4, 0, NULL, 0, assembled, &assembled_value) < 0) goto global_error;
    global[global_count++] = (EmfMetric){
        "TracesAssembled", 15, "Count", 5, assembled_value, 0.0, 0, 0,
    };
    global[global_count++] = (EmfMetric){
        "Pending", 7, "Count", 5, pending, 0.0, 0, 0,
    };
    static const char *loss_fields[] = {
        "orphan_phase", "orphan_correlation", "pending_evicted",
        "decode_error", "export_error", "recent_evicted",
    };
    PyObject *loss = statsd_optional_attr(snapshot, OBS_LOSS, Py_None);
    if (loss == NULL) goto global_error;
    for (int index = 0; index < 6; index++) {
        uint64_t value, delta;
        PyObject *suffix = PyUnicode_FromString(loss_fields[index]);
        if (metric_u64_attr_string(loss, loss_fields[index], &value) < 0 ||
            (!cumulative && metric_delta_u64(
                state, (unsigned char)(10 + index), 0, NULL, 0, value, &delta) < 0) ||
            suffix == NULL || emf_dynamic_metric(
                &global[global_count], "ProjectorLoss_", suffix,
                cumulative ? value : delta) < 0) {
            Py_XDECREF(suffix);
            Py_DECREF(loss);
            goto global_error;
        }
        Py_DECREF(suffix);
        global_count++;
    }
    Py_DECREF(loss);
    if (recorder_loss != Py_None && PyDict_Check(recorder_loss)) {
        Py_ssize_t position = 0;
        PyObject *reason, *count_object;
        while (global_count < max_metrics && PyDict_Next(
                recorder_loss, &position, &reason, &count_object)) {
            PyObject *name = statsd_reason_name(reason);
            PyObject *integer = name != NULL ? PyNumber_Long(count_object) : NULL;
            uint64_t value = integer != NULL
                ? PyLong_AsUnsignedLongLong(integer) : 0;
            Py_XDECREF(integer);
            Py_ssize_t name_length = 0;
            const char *name_data = name != NULL
                ? PyUnicode_AsUTF8AndSize(name, &name_length) : NULL;
            uint64_t delta;
            if (name_data == NULL || PyErr_Occurred() ||
                (!cumulative && metric_delta_u64(
                    state, 20, 0, name_data, name_length, value, &delta) < 0) ||
                emf_dynamic_metric(
                    &global[global_count], "RecorderLoss_", name,
                    cumulative ? value : delta) < 0) {
                Py_XDECREF(name);
                goto global_error;
            }
            Py_DECREF(name);
            global_count++;
        }
    }
    if (emf_write_document(
            &writer, &base, NULL, global, global_count,
            namespace, namespace_length, timestamp) < 0) goto global_error;
    emf_metrics_clear(global, global_count);
    PyMem_Free(global);
    if (readings != NULL && emf_render_counters(
            &writer, &base, readings, namespace, namespace_length,
            timestamp, cumulative, state, max_metrics) < 0) goto error;
    metric_labels_clear(&base);
    PyObject *bytes = wreath_writer_finish(&writer);
    if (bytes == NULL) return NULL;
    PyObject *result = PyUnicode_DecodeUTF8(
        PyBytes_AS_STRING(bytes), PyBytes_GET_SIZE(bytes), "strict");
    Py_DECREF(bytes);
    return result;

global_error:
    emf_metrics_clear(global, global_count);
    PyMem_Free(global);
error:
    metric_labels_clear(&base);
    Py_XDECREF(writer.bytes);
    return NULL;
}
