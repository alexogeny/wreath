#include "harness.h"

#include <stdlib.h>

typedef struct {
    PyObject *multipart_parse;
    PyObject *part_type;
    PyObject *boundary;
    PyObject *max_parts;
    PyObject *max_header_bytes;
    PyObject *max_part_bytes;
} MultipartState;

static const char state_key[] = "wreath.fuzz.multipart";

static void
destroy_state(PyObject *capsule)
{
    MultipartState *state = PyCapsule_GetPointer(capsule, state_key);
    if (state == NULL) {
        PyErr_Clear();
        return;
    }
    Py_XDECREF(state->multipart_parse);
    Py_XDECREF(state->part_type);
    Py_XDECREF(state->boundary);
    Py_XDECREF(state->max_parts);
    Py_XDECREF(state->max_header_bytes);
    Py_XDECREF(state->max_part_bytes);
    PyMem_Free(state);
}

static PyObject *
import_attribute(const char *module_name, const char *attribute)
{
    PyObject *module = PyImport_ImportModule(module_name);
    PyObject *value;
    if (module == NULL) wreath_fuzz_abort_python();
    value = PyObject_GetAttrString(module, attribute);
    Py_DECREF(module);
    if (value == NULL) wreath_fuzz_abort_python();
    return value;
}

int
LLVMFuzzerInitialize(int *argc, char ***argv)
{
    PyObject *module;
    MultipartState *state;
    (void)argc;
    (void)argv;
    Py_Initialize();
    state = wreath_fuzz_allocate_state(sizeof(*state));
    module = PyImport_ImportModule("wreath._native._core");
    if (module == NULL) wreath_fuzz_abort_python();
    state->multipart_parse = PyObject_GetAttrString(module, "multipart_parse");
    Py_DECREF(module);
    if (state->multipart_parse == NULL) wreath_fuzz_abort_python();
    state->part_type = import_attribute("wreath._multipart", "Part");
    state->boundary = PyBytes_FromString("wreath-fuzz");
    state->max_parts = PyLong_FromLong(64);
    state->max_header_bytes = PyLong_FromLong(8192);
    state->max_part_bytes = PyLong_FromLong(65536);
    if (state->boundary == NULL || state->max_parts == NULL ||
        state->max_header_bytes == NULL || state->max_part_bytes == NULL) {
        wreath_fuzz_abort_python();
    }
    wreath_fuzz_store_state(state_key, state, destroy_state);
    return 0;
}

static void
check_lowercase_headers(PyObject *parts)
{
    Py_ssize_t part_count = PyList_Size(parts);
    if (part_count < 0) wreath_fuzz_abort_python();
    for (Py_ssize_t part_index = 0; part_index < part_count; part_index++) {
        PyObject *part = PyList_GetItem(parts, part_index);
        PyObject *headers = PyTuple_GetItem(part, 2);
        Py_ssize_t header_count;
        if (headers == NULL) wreath_fuzz_abort_python();
        header_count = PyList_Size(headers);
        if (header_count < 0) wreath_fuzz_abort_python();
        for (Py_ssize_t header_index = 0; header_index < header_count;
             header_index++) {
            PyObject *header = PyList_GetItem(headers, header_index);
            PyObject *name = PyTuple_GetItem(header, 0);
            PyObject *lower;
            int equal;
            if (name == NULL) wreath_fuzz_abort_python();
            lower = PyObject_CallMethod(name, "lower", NULL);
            if (lower == NULL) wreath_fuzz_abort_python();
            equal = PyObject_RichCompareBool(name, lower, Py_EQ);
            Py_DECREF(lower);
            if (equal < 0) wreath_fuzz_abort_python();
            if (!equal) abort();
        }
    }
}

int
LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    PyObject *input;
    PyObject *parts;
    MultipartState *state;
    if (size > 65536) return 0;
    state = wreath_fuzz_get_state(state_key);
    input = PyBytes_FromStringAndSize((const char *)data, (Py_ssize_t)size);
    if (input == NULL) wreath_fuzz_abort_python();
    parts = PyObject_CallFunctionObjArgs(
        state->multipart_parse, input, state->boundary, state->max_parts,
        state->max_header_bytes, state->max_part_bytes, Py_None,
        state->part_type, NULL);
    Py_DECREF(input);
    if (parts == NULL) {
        if (PyErr_ExceptionMatches(PyExc_ValueError)) {
            PyErr_Clear();
            return 0;
        }
        wreath_fuzz_abort_python();
    }
    check_lowercase_headers(parts);
    Py_DECREF(parts);
    return 0;
}
