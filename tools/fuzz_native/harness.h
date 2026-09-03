#ifndef WREATH_FUZZ_HARNESS_H
#define WREATH_FUZZ_HARNESS_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    PyObject *function;
    PyObject *refusal;
} WreathFuzzCallState;

void *wreath_fuzz_allocate_state(size_t size);
void wreath_fuzz_store_state(const char *key, void *state,
                             PyCapsule_Destructor destructor);
void *wreath_fuzz_get_state(const char *key);
int wreath_fuzz_initialize(const char *key, const char *module_name,
                           const char *function_name, const char *refusal_name);
PyObject *wreath_fuzz_call(const WreathFuzzCallState *state,
                           const uint8_t *data, size_t size, int *refused);
void wreath_fuzz_abort_python(void);

#endif
