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
    PyObject *route_id;
    const char *route_data;
    Py_ssize_t route_length;
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
        Py_XDECREF(routes[index].buckets);
        Py_XDECREF(routes[index].duration_max);
        Py_XDECREF(routes[index].duration_sum);
        Py_XDECREF(routes[index].errors);
        Py_XDECREF(routes[index].count);
        Py_XDECREF(routes[index].route_id);
    }
    PyMem_Free(routes);
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
        wreath_writer_write(writer, "{route_id=\"", 11) < 0 ||
        prom_write_label_value(
            writer, route->route_data, route->route_length) < 0) return -1;
    if (bound != NULL) {
        if (wreath_writer_write(writer, "\",le=\"", 6) < 0 ||
            prom_write_unicode(writer, bound) < 0) return -1;
    }
    return wreath_writer_write(writer, "\"} ", 3);
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

PyObject *
wreath_prometheus_route_blocks(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *routes_object, *names;
    PyObject *routes = NULL, *groups = NULL;
    PromRoute *plan = NULL;
    WreathBytesWriter writers[4] = {{0}};
    const char *name_data[4];
    Py_ssize_t name_lengths[4];
    Py_ssize_t count = 0;
    if (!PyArg_ParseTuple(args, "OO!:prometheus_route_blocks", &routes_object,
                          &PyTuple_Type, &names)) return NULL;
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
        PyObject *route_id = PyObject_GetAttrString(route, "route_id");
        PyObject *route_id_text = NULL;
        if (route_id == NULL) goto error;
        route_id_text = PyObject_Str(route_id);
        Py_DECREF(route_id);
        if (route_id_text == NULL) goto error;
        plan[index].route_id = route_id_text;
        plan[index].route_data = PyUnicode_AsUTF8AndSize(
            route_id_text, &plan[index].route_length);
        if (plan[index].route_data == NULL) goto error;
        plan[index].count = PyObject_GetAttrString(route, "count");
        plan[index].errors = PyObject_GetAttrString(route, "errors");
        plan[index].duration_sum = PyObject_GetAttrString(route, "duration_us_sum");
        plan[index].duration_max = PyObject_GetAttrString(route, "duration_us_max");
        plan[index].buckets = PyObject_GetAttrString(route, "buckets");
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
