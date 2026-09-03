#include "harness.h"

#include <stdlib.h>

void
wreath_fuzz_abort_python(void)
{
    PyErr_Print();
    abort();
}

void *
wreath_fuzz_allocate_state(size_t size)
{
    void *state = PyMem_Calloc(1, size);
    if (state == NULL) wreath_fuzz_abort_python();
    return state;
}

void
wreath_fuzz_store_state(const char *key, void *state,
                        PyCapsule_Destructor destructor)
{
    PyObject *interpreter_state =
        PyInterpreterState_GetDict(PyInterpreterState_Get());
    PyObject *capsule;
    if (interpreter_state == NULL) wreath_fuzz_abort_python();
    capsule = PyCapsule_New(state, key, destructor);
    if (capsule == NULL) wreath_fuzz_abort_python();
    if (PyDict_SetItemString(interpreter_state, key, capsule) < 0) {
        Py_DECREF(capsule);
        wreath_fuzz_abort_python();
    }
    Py_DECREF(capsule);
}

void *
wreath_fuzz_get_state(const char *key)
{
    PyObject *interpreter_state =
        PyInterpreterState_GetDict(PyInterpreterState_Get());
    PyObject *capsule;
    void *state;
    if (interpreter_state == NULL) wreath_fuzz_abort_python();
    capsule = PyDict_GetItemString(interpreter_state, key);
    if (capsule == NULL) {
        PyErr_Format(PyExc_RuntimeError, "fuzz harness state %s is unavailable", key);
        wreath_fuzz_abort_python();
    }
    state = PyCapsule_GetPointer(capsule, key);
    if (state == NULL) wreath_fuzz_abort_python();
    return state;
}

static void
wreath_fuzz_call_state_destroy(PyObject *capsule)
{
    const char *key = PyCapsule_GetName(capsule);
    WreathFuzzCallState *state;
    if (key == NULL) {
        PyErr_Clear();
        return;
    }
    state = PyCapsule_GetPointer(capsule, key);
    if (state == NULL) {
        PyErr_Clear();
        return;
    }
    Py_XDECREF(state->function);
    Py_XDECREF(state->refusal);
    PyMem_Free(state);
}

int
wreath_fuzz_initialize(const char *key, const char *module_name,
                       const char *function_name, const char *refusal_name)
{
    PyObject *module;
    WreathFuzzCallState *state;

    Py_Initialize();
    state = wreath_fuzz_allocate_state(sizeof(*state));
    module = PyImport_ImportModule(module_name);
    if (module == NULL) wreath_fuzz_abort_python();
    state->function = PyObject_GetAttrString(module, function_name);
    state->refusal =
        refusal_name != NULL ? PyObject_GetAttrString(module, refusal_name) : NULL;
    Py_DECREF(module);
    if (state->function == NULL ||
        (refusal_name != NULL && state->refusal == NULL)) {
        wreath_fuzz_abort_python();
    }
    wreath_fuzz_store_state(key, state, wreath_fuzz_call_state_destroy);
    return 0;
}

PyObject *
wreath_fuzz_call(const WreathFuzzCallState *state,
                 const uint8_t *data, size_t size, int *refused)
{
    PyObject *input = PyBytes_FromStringAndSize((const char *)data, (Py_ssize_t)size);
    PyObject *result;
    if (input == NULL) wreath_fuzz_abort_python();
    result = PyObject_CallOneArg(state->function, input);
    Py_DECREF(input);
    if (result != NULL) return result;
    if (state->refusal != NULL && PyErr_ExceptionMatches(state->refusal)) {
        PyErr_Clear();
        *refused = 1;
        return NULL;
    }
    wreath_fuzz_abort_python();
    return NULL;
}
