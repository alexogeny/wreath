#include "harness.h"

#include <stdlib.h>

typedef struct {
    PyObject *decode_exchange;
    PyObject *encode_exchange;
    PyObject *record_type;
    PyObject *error_type;
    PyObject *forbidden_headers;
} HttpReplayState;

static const char state_key[] = "wreath.fuzz.http-replay";

static void
destroy_state(PyObject *capsule)
{
    HttpReplayState *state = PyCapsule_GetPointer(capsule, state_key);
    if (state == NULL) {
        PyErr_Clear();
        return;
    }
    Py_XDECREF(state->decode_exchange);
    Py_XDECREF(state->encode_exchange);
    Py_XDECREF(state->record_type);
    Py_XDECREF(state->error_type);
    Py_XDECREF(state->forbidden_headers);
    PyMem_Free(state);
}

static PyObject *
import_attribute(PyObject *module, const char *attribute)
{
    PyObject *value = PyObject_GetAttrString(module, attribute);
    if (value == NULL) wreath_fuzz_abort_python();
    return value;
}

int
LLVMFuzzerInitialize(int *argc, char ***argv)
{
    PyObject *module;
    HttpReplayState *state;
    (void)argc;
    (void)argv;
    Py_Initialize();
    state = wreath_fuzz_allocate_state(sizeof(*state));
    module = PyImport_ImportModule("wreath._native._core");
    if (module == NULL) wreath_fuzz_abort_python();
    state->decode_exchange = import_attribute(module, "http_exchange_decode");
    state->encode_exchange = import_attribute(module, "http_exchange_encode");
    Py_DECREF(module);
    module = PyImport_ImportModule("wreath._http_replay");
    if (module == NULL) wreath_fuzz_abort_python();
    state->record_type = import_attribute(module, "RecordedHttpExchange");
    state->error_type = import_attribute(module, "HttpReplayError");
    state->forbidden_headers =
        import_attribute(module, "_NEVER_CAPTURE_HEADER_BYTES");
    Py_DECREF(module);
    wreath_fuzz_store_state(state_key, state, destroy_state);
    return 0;
}

static PyObject *
decode(const HttpReplayState *state, PyObject *input)
{
    return PyObject_CallFunctionObjArgs(
        state->decode_exchange, input, state->record_type, state->error_type, NULL);
}

static PyObject *
encode(const HttpReplayState *state, PyObject *exchange)
{
    return PyObject_CallFunctionObjArgs(
        state->encode_exchange, exchange, state->forbidden_headers,
        state->error_type, NULL);
}

int
LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    PyObject *input;
    PyObject *exchange;
    PyObject *canonical;
    PyObject *reparsed;
    PyObject *second;
    HttpReplayState *state;
    int equal;
    if (size > 65536) return 0;
    state = wreath_fuzz_get_state(state_key);
    input = PyBytes_FromStringAndSize((const char *)data, (Py_ssize_t)size);
    if (input == NULL) wreath_fuzz_abort_python();
    exchange = decode(state, input);
    Py_DECREF(input);
    if (exchange == NULL) {
        if (PyErr_ExceptionMatches(state->error_type)) {
            PyErr_Clear();
            return 0;
        }
        wreath_fuzz_abort_python();
    }
    canonical = encode(state, exchange);
    Py_DECREF(exchange);
    if (canonical == NULL) wreath_fuzz_abort_python();
    reparsed = decode(state, canonical);
    if (reparsed == NULL) wreath_fuzz_abort_python();
    second = encode(state, reparsed);
    Py_DECREF(reparsed);
    if (second == NULL) wreath_fuzz_abort_python();
    equal = PyObject_RichCompareBool(canonical, second, Py_EQ);
    Py_DECREF(canonical);
    Py_DECREF(second);
    if (equal < 0) wreath_fuzz_abort_python();
    if (!equal) abort();
    return 0;
}
