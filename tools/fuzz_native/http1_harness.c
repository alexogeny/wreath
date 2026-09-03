#include "harness.h"

static const char state_key[] = "wreath.fuzz.http1";

int
LLVMFuzzerInitialize(int *argc, char ***argv)
{
    WreathFuzzCallState *state;
    (void)argc;
    (void)argv;
    wreath_fuzz_initialize(
        state_key, "wreath._native._core", "http_parse_request", NULL);
    state = wreath_fuzz_get_state(state_key);
    state->refusal = Py_NewRef(PyExc_ValueError);
    return 0;
}

int
LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    PyObject *result;
    WreathFuzzCallState *state;
    int refused = 0;
    if (size > 65536) return 0;
    state = wreath_fuzz_get_state(state_key);
    result = wreath_fuzz_call(state, data, size, &refused);
    if (!refused) Py_XDECREF(result);
    return 0;
}
