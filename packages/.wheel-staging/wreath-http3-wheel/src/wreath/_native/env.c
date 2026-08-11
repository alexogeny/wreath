#include "wreathcore.h"

#include <stdlib.h>

#ifdef _WIN32
extern char **_environ;
#define WREATH_ENVIRON _environ
#else
extern char **environ;
#define WREATH_ENVIRON environ
#endif

static int
valid_key(const unsigned char *key, Py_ssize_t size)
{
    Py_ssize_t i;
    if (size == 0 || !((key[0] >= 'A' && key[0] <= 'Z') ||
                       (key[0] >= 'a' && key[0] <= 'z') || key[0] == '_')) {
        return 0;
    }
    for (i = 1; i < size; i++) {
        unsigned char c = key[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              (c >= '0' && c <= '9') || c == '_')) {
            return 0;
        }
    }
    return 1;
}

PyObject *
wreath_parse_dotenv(PyObject *self, PyObject *arg)
{
    Py_buffer view;
    PyObject *result = NULL;
    const unsigned char *data;
    Py_ssize_t start = 0;
    Py_ssize_t line = 1;
    Py_ssize_t i;
    (void)self;

    if (PyObject_GetBuffer(arg, &view, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    data = (const unsigned char *)view.buf;
    result = PyDict_New();
    if (result == NULL) {
        goto done;
    }

    for (i = 0; i <= view.len; i++) {
        Py_ssize_t end;
        Py_ssize_t equals;
        PyObject *key = NULL;
        PyObject *value = NULL;
        if (i < view.len && data[i] != '\n') {
            continue;
        }
        end = i;
        if (end > start && data[end - 1] == '\r') {
            end--;
        }
        if (end == start) {
            start = i + 1;
            line++;
            continue;
        }
        equals = start;
        while (equals < end && data[equals] != '=') {
            equals++;
        }
        if (equals == start || equals == end) {
            PyErr_Format(PyExc_ValueError, "invalid dotenv entry on line %zd", line);
            Py_CLEAR(result);
            goto done;
        }
        if (!valid_key(data + start, equals - start)) {
            PyErr_Format(PyExc_ValueError, "invalid dotenv key on line %zd", line);
            Py_CLEAR(result);
            goto done;
        }
        key = PyUnicode_DecodeASCII((const char *)data + start, equals - start, NULL);
        value = PyUnicode_DecodeUTF8(
            (const char *)data + equals + 1, end - equals - 1, "strict"
        );
        if (key == NULL || value == NULL || PyDict_SetItem(result, key, value) < 0) {
            if (PyErr_ExceptionMatches(PyExc_UnicodeDecodeError)) {
                PyErr_Clear();
                PyErr_Format(
                    PyExc_ValueError, "invalid UTF-8 dotenv value on line %zd", line
                );
            }
            Py_XDECREF(key);
            Py_XDECREF(value);
            Py_CLEAR(result);
            goto done;
        }
        Py_DECREF(key);
        Py_DECREF(value);
        start = i + 1;
        line++;
    }

done:
    PyBuffer_Release(&view);
    return result;
}

PyObject *
wreath_read_osenv(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    PyObject *result = PyDict_New();
    char **entry;
    (void)self;
    if (result == NULL) {
        return NULL;
    }
    for (entry = WREATH_ENVIRON; entry != NULL && *entry != NULL; entry++) {
        const char *equals = strchr(*entry, '=');
        PyObject *key;
        PyObject *value;
        if (equals == NULL || equals == *entry) {
            continue;
        }
        key = PyUnicode_DecodeFSDefaultAndSize(*entry, equals - *entry);
        value = PyUnicode_DecodeFSDefault(equals + 1);
        if (key == NULL || value == NULL || PyDict_SetItem(result, key, value) < 0) {
            Py_XDECREF(key);
            Py_XDECREF(value);
            Py_DECREF(result);
            return NULL;
        }
        Py_DECREF(key);
        Py_DECREF(value);
    }
    return result;
}
