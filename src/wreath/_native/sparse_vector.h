#ifndef WREATH_SPARSE_VECTOR_H
#define WREATH_SPARSE_VECTOR_H

#include <Python.h>
#include <stdint.h>

#define WREATH_SPARSE_VECTOR_CAPSULE "wreath.sparse_vector"

typedef struct {
    int32_t dimension;
    Py_ssize_t count;
    /* One compact allocation: indices first, then an aligned value span.
     * `values` is an interior pointer and must not be freed separately. */
    int32_t *indices;
    double *values;
} WreathSparseVector;

static inline WreathSparseVector *
wreath_sparse_vector_get(PyObject *capsule)
{
    return PyCapsule_GetPointer(capsule, WREATH_SPARSE_VECTOR_CAPSULE);
}

static inline void
wreath_sparse_vector_destroy(PyObject *capsule)
{
    WreathSparseVector *data = wreath_sparse_vector_get(capsule);
    if (data == NULL) {
        PyErr_Clear();
        return;
    }
    PyMem_Free(data->indices);
    PyMem_Free(data);
}

#endif
