#ifndef WREATH_RECORD_API_H
#define WREATH_RECORD_API_H

#include <Python.h>
#include <stdint.h>

/* Optional, versioned bridge from the template VM to the PostgreSQL Record.
 * `_core` remains independently buildable and usable: wreath.postgres hands
 * this capsule to it only when both native twins are active. */
#define WREATH_RECORD_CAPI_VERSION 4U
#define WREATH_RECORD_CAPI_NAME "wreath._native._postgres._RECORD_C_API"

enum {
    WREATH_RECORD_VALUE_OBJECT = 1,
    WREATH_RECORD_VALUE_INT64 = 2,
    WREATH_RECORD_VALUE_UTF8 = 3,
};

typedef struct {
    int kind;
    PyObject *object;       /* borrowed when kind == OBJECT */
    const char *data;       /* borrowed while the batch lives, for UTF8 */
    Py_ssize_t length;
    int64_t integer;
} WreathRecordValue;

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
    /* Resolve a batch cell without forcing its Python representation.  Exact
     * Python-facing access still goes through batch_get_value and materializes
     * lazily; native consumers can keep validated wire scalars native. */
    int (*batch_get_typed)(PyObject *batch, Py_ssize_t row, PyObject *key,
                           PyObject **cached_names,
                           Py_ssize_t *cached_position,
                           WreathRecordValue *value);
    /* Resolve a name once outside a typed template loop, then read each row by
     * numeric column.  The latter returns 0 for a non-native appended object,
     * allowing the renderer to restart through its fully generic VM. */
    int (*batch_resolve_column)(PyObject *batch, PyObject *key,
                                Py_ssize_t *position);
    int (*batch_get_typed_at)(PyObject *batch, Py_ssize_t row,
                              Py_ssize_t position,
                              WreathRecordValue *value);
} WreathRecordCAPI;

#endif
