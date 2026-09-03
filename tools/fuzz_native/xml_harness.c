#include "harness.h"

#include <stdlib.h>

static const char state_key[] = "wreath.fuzz.xml";

int
LLVMFuzzerInitialize(int *argc, char ***argv)
{
    (void)argc;
    (void)argv;
    return wreath_fuzz_initialize(
        state_key, "wreath.xml", "_parse_native", "XMLRefusal");
}

int
LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    PyObject *document;
    PyObject *canonical;
    PyObject *reparsed;
    PyObject *second;
    WreathFuzzCallState *state;
    int refused = 0;
    int equal;
    if (size > 65536) return 0;
    state = wreath_fuzz_get_state(state_key);
    document = wreath_fuzz_call(state, data, size, &refused);
    if (refused) return 0;
    canonical = PyObject_CallMethod(document, "canonicalize", NULL);
    Py_DECREF(document);
    if (canonical == NULL) wreath_fuzz_abort_python();
    reparsed = PyObject_CallOneArg(state->function, canonical);
    if (reparsed == NULL) wreath_fuzz_abort_python();
    second = PyObject_CallMethod(reparsed, "canonicalize", NULL);
    Py_DECREF(reparsed);
    if (second == NULL) wreath_fuzz_abort_python();
    equal = PyObject_RichCompareBool(canonical, second, Py_EQ);
    Py_DECREF(canonical);
    Py_DECREF(second);
    if (equal < 0) wreath_fuzz_abort_python();
    if (!equal) abort();
    return 0;
}
