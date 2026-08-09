#ifndef WREATH_RECORD_API_H
#define WREATH_RECORD_API_H

#include <Python.h>
#include <stdint.h>

/* Optional, versioned bridge from the template VM to the PostgreSQL Record.
 * `_core` remains independently buildable and usable: wreath.postgres hands
 * this capsule to it only when both native twins are active. */
#define WREATH_RECORD_CAPI_VERSION 2U
#define WREATH_RECORD_CAPI_NAME "wreath._native._postgres._RECORD_C_API"

typedef struct {
    uint32_t version;
    /* Return 1 with a borrowed value, 2 for a missing key, 0 when `record` is
     * not the exact native type, or -1 with an exception.  The caller-owned
     * names cache makes a
     * compiled lookup monomorphic for a result shape without storing a pointer
     * whose lifetime ends with an individual row. */
    int (*get_borrowed)(PyObject *record, PyObject *key,
                        PyObject **cached_names, Py_ssize_t *cached_position,
                        PyObject **value);
    int (*batch_check)(PyObject *batch);
    Py_ssize_t (*batch_size)(PyObject *batch);
    PyObject *(*batch_get_borrowed)(PyObject *batch, Py_ssize_t position);
    int (*batch_get_value)(PyObject *batch, Py_ssize_t row, PyObject *key,
                           PyObject **cached_names,
                           Py_ssize_t *cached_position, PyObject **value);
} WreathRecordCAPI;

#endif
