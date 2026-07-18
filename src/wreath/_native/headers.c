/* Header-list scanning.
 *
 * ASGI guarantees header names arrive lowercased, so lookups compare raw
 * bytes without case folding. Callers pass lowercase names.
 */
#include "wreathcore.h"

static int
unpack_pair(PyObject *pair, PyObject **key, PyObject **value)
{
    if (PyTuple_Check(pair) && PyTuple_GET_SIZE(pair) == 2) {
        *key = PyTuple_GET_ITEM(pair, 0);
        *value = PyTuple_GET_ITEM(pair, 1);
    }
    else if (PyList_Check(pair) && PyList_GET_SIZE(pair) == 2) {
        *key = PyList_GET_ITEM(pair, 0);
        *value = PyList_GET_ITEM(pair, 1);
    }
    else {
        PyErr_SetString(PyExc_TypeError, "header entries must be two-item tuples");
        return -1;
    }
    if (!PyBytes_Check(*key) || !PyBytes_Check(*value)) {
        PyErr_SetString(PyExc_TypeError, "header names and values must be bytes");
        return -1;
    }
    return 0;
}

PyObject *
wreath_find_header(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *headers;
    Py_buffer name;
    if (!PyArg_ParseTuple(args, "Oy*:find_header", &headers, &name)) {
        return NULL;
    }

    PyObject *seq = PySequence_Fast(headers, "headers must be a sequence");
    if (seq == NULL) {
        PyBuffer_Release(&name);
        return NULL;
    }

    PyObject *result = NULL;
    Py_ssize_t count = PySequence_Fast_GET_SIZE(seq);
    PyObject **items = PySequence_Fast_ITEMS(seq);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *key, *value;
        if (unpack_pair(items[i], &key, &value) < 0) {
            goto done;
        }
        if (PyBytes_GET_SIZE(key) == name.len &&
            memcmp(PyBytes_AS_STRING(key), name.buf, (size_t)name.len) == 0) {
            result = Py_NewRef(value);
            goto done;
        }
    }
    result = Py_NewRef(Py_None);

done:
    Py_DECREF(seq);
    PyBuffer_Release(&name);
    return result;
}

PyObject *
wreath_build_header_map(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *headers;
    if (!PyArg_ParseTuple(args, "O:build_header_map", &headers)) {
        return NULL;
    }

    PyObject *seq = PySequence_Fast(headers, "headers must be a sequence");
    if (seq == NULL) {
        return NULL;
    }

    PyObject *map = PyDict_New();
    if (map == NULL) {
        Py_DECREF(seq);
        return NULL;
    }

    Py_ssize_t count = PySequence_Fast_GET_SIZE(seq);
    PyObject **items = PySequence_Fast_ITEMS(seq);
    for (Py_ssize_t i = 0; i < count; i++) {
        PyObject *key, *value;
        if (unpack_pair(items[i], &key, &value) < 0 ||
            PyDict_SetDefault(map, key, value) == NULL) {
            Py_DECREF(map);
            Py_DECREF(seq);
            return NULL;
        }
    }
    Py_DECREF(seq);
    return map;
}
