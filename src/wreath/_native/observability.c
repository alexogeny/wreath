/* Request correlation and timing primitives.
 *
 * Both functions exist to keep untrusted bytes out of places that concatenate
 * without escaping: a request id is echoed into a response header and (later)
 * into access logs and trace attributes, so it is validated against a strict
 * charset rather than sanitized. Rejecting is cheaper and safer than rewriting;
 * the caller mints a fresh id instead.
 */
#include "wreathcore.h"

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
