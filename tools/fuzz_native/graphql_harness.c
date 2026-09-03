#include "harness.h"

typedef struct {
    PyObject *graphql_parse;
    PyObject *graphql_refusal;
    PyObject *limits;
    PyObject *config;
} GraphQLState;

static const char state_key[] = "wreath.fuzz.graphql";

static void
destroy_state(PyObject *capsule)
{
    GraphQLState *state = PyCapsule_GetPointer(capsule, state_key);
    if (state == NULL) {
        PyErr_Clear();
        return;
    }
    Py_XDECREF(state->graphql_parse);
    Py_XDECREF(state->graphql_refusal);
    Py_XDECREF(state->limits);
    Py_XDECREF(state->config);
    PyMem_Free(state);
}

int
LLVMFuzzerInitialize(int *argc, char ***argv)
{
    PyObject *module;
    PyObject *limits_type;
    PyObject *arguments;
    PyObject *keywords;
    GraphQLState *state;
    (void)argc;
    (void)argv;
    Py_Initialize();
    state = wreath_fuzz_allocate_state(sizeof(*state));
    module = PyImport_ImportModule("wreath._native._core");
    if (module == NULL) wreath_fuzz_abort_python();
    state->graphql_parse = PyObject_GetAttrString(module, "graphql_parse");
    Py_DECREF(module);
    if (state->graphql_parse == NULL) wreath_fuzz_abort_python();
    module = PyImport_ImportModule("wreath._graphql.parser");
    if (module == NULL) wreath_fuzz_abort_python();
    limits_type = PyObject_GetAttrString(module, "Limits");
    state->config = PyObject_GetAttrString(module, "_CONFIG");
    state->graphql_refusal = PyObject_GetAttrString(module, "GraphQLSyntaxError");
    Py_DECREF(module);
    if (limits_type == NULL || state->config == NULL ||
        state->graphql_refusal == NULL) {
        wreath_fuzz_abort_python();
    }
    arguments = PyTuple_New(0);
    keywords = Py_BuildValue(
        "{s:i,s:i,s:i,s:i,s:i}",
        "max_depth", 16,
        "max_complexity", 2048,
        "max_aliases", 64,
        "max_steps", 50000,
        "max_document_bytes", 16384);
    if (arguments == NULL || keywords == NULL) wreath_fuzz_abort_python();
    state->limits = PyObject_Call(limits_type, arguments, keywords);
    Py_DECREF(keywords);
    Py_DECREF(arguments);
    Py_DECREF(limits_type);
    if (state->limits == NULL) wreath_fuzz_abort_python();
    wreath_fuzz_store_state(state_key, state, destroy_state);
    return 0;
}

int
LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    PyObject *source;
    PyObject *document;
    GraphQLState *state;
    if (size > 16384) return 0;
    state = wreath_fuzz_get_state(state_key);
    source = PyUnicode_DecodeUTF8((const char *)data, (Py_ssize_t)size, "strict");
    if (source == NULL) {
        if (PyErr_ExceptionMatches(PyExc_UnicodeDecodeError)) {
            PyErr_Clear();
            return 0;
        }
        wreath_fuzz_abort_python();
    }
    document = PyObject_CallFunctionObjArgs(
        state->graphql_parse, source, state->limits, state->config, NULL);
    Py_DECREF(source);
    if (document == NULL) {
        if (PyErr_ExceptionMatches(state->graphql_refusal)) {
            PyErr_Clear();
            return 0;
        }
        wreath_fuzz_abort_python();
    }
    Py_DECREF(document);
    return 0;
}
