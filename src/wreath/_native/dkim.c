/* RFC 6376 relaxed body canonicalisation. */
#include "wreathcore.h"


PyObject *
wreath_dkim_canonicalize_body(PyObject *Py_UNUSED(self), PyObject *body)
{
    const uint8_t *source;
    uint8_t *target;
    Py_ssize_t length;
    Py_ssize_t out = 0;
    Py_ssize_t last_nonempty = 0;
    int pending_space = 0;
    int line_nonempty = 0;
    PyObject *result;

    if (!PyBytes_Check(body)) {
        PyErr_Format(PyExc_TypeError,
                     "canonicalize_body_relaxed() requires bytes, not %.200s",
                     Py_TYPE(body)->tp_name);
        return NULL;
    }
    length = PyBytes_GET_SIZE(body);
    if (length > (PY_SSIZE_T_MAX - 2) / 2) {
        return PyErr_NoMemory();
    }
    result = PyBytes_FromStringAndSize(NULL, length * 2 + 2);
    if (result == NULL) return NULL;
    source = (const uint8_t *)PyBytes_AS_STRING(body);
    target = (uint8_t *)PyBytes_AS_STRING(result);

    for (Py_ssize_t index = 0; index < length; index++) {
        uint8_t ch = source[index];
        if (ch == ' ' || ch == '\t') {
            pending_space = 1;
            continue;
        }
        if (ch == '\r' || ch == '\n') {
            if (ch == '\r' && index + 1 < length && source[index + 1] == '\n') {
                index++;
            }
            pending_space = 0;
            target[out++] = '\r';
            target[out++] = '\n';
            if (line_nonempty) last_nonempty = out;
            line_nonempty = 0;
            continue;
        }
        if (pending_space) {
            target[out++] = ' ';
            pending_space = 0;
        }
        target[out++] = ch;
        line_nonempty = 1;
    }

    if (line_nonempty) {
        target[out++] = '\r';
        target[out++] = '\n';
        last_nonempty = out;
    }
    if (_PyBytes_Resize(&result, last_nonempty) < 0) return NULL;
    return result;
}
