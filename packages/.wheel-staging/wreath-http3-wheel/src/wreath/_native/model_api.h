/* Immutable C API for model cells shared by the core and PostgreSQL modules. */
#ifndef WREATH_MODEL_API_H
#define WREATH_MODEL_API_H

#include <Python.h>

#define WREATH_MODEL_API_NAME "wreath._native._postgres._MODEL_API"
#define WREATH_MODEL_API_VERSION 2u

typedef struct {
    unsigned version;
    PyObject *(*alloc)(PyTypeObject *type);
    PyObject *(*get)(PyObject *instance, Py_ssize_t index);
    int (*is_loaded)(PyObject *instance, Py_ssize_t index);
    int (*is_null)(PyObject *instance, Py_ssize_t index);
    int (*is_dirty)(PyObject *instance, Py_ssize_t index);
    PyObject *(*get_relation)(PyObject *instance, Py_ssize_t index);
    int (*set_loaded)(PyObject *instance, Py_ssize_t index, PyObject *value);
    int (*set_relation)(PyObject *instance, Py_ssize_t index, PyObject *value);
    int (*make_persistent)(PyObject *instance, PyObject *owner);
} WreathModelAPI;

static inline const WreathModelAPI *
wreath_model_api(PyObject *capsule)
{
    const WreathModelAPI *api = PyCapsule_GetPointer(capsule, WREATH_MODEL_API_NAME);
    if (api != NULL && api->version != WREATH_MODEL_API_VERSION) {
        PyErr_Format(PyExc_RuntimeError,
                     "model API version %u is not supported; expected %u",
                     api->version, WREATH_MODEL_API_VERSION);
        return NULL;
    }
    return api;
}

#endif /* WREATH_MODEL_API_H */
