#ifndef WREATH_SERVER_REQUEST_CAPI_H
#define WREATH_SERVER_REQUEST_CAPI_H

#include <Python.h>
#include "flight.h"
#include "server_policy.h"

#define WREATH_REQUEST_CAPI_NAME "wreath._native._server._REQUEST_C_API"
#define WREATH_REQUEST_CAPI_VERSION 5

typedef struct {
    uint32_t version;
    PyObject *(*new_context)(
        PyObject *, PyObject *, PyObject *, PyObject *, PyObject *, PyObject *,
        PyObject *, PyObject *, PyObject *, PyObject *, PyObject *, PyObject *
    );
    int (*check)(PyObject *);
    void (*set_flight)(PyObject *, wreath_nfr_context *, wreath_nfr_worker *);
    int (*set_armed)(PyObject *);
    void (*sever)(PyObject *);
    int (*seed_flight)(PyObject *, const wreath_nfr_context *);
    void (*set_policy)(PyObject *, const WreathPolicyState *);
    void (*update_policy)(PyObject *, const WreathPolicyState *);
    PyObject *(*header_block_new_objects)(Py_ssize_t);
    int (*header_block_append_objects)(PyObject *, PyObject *, PyObject *);
    PyObject *(*decode_request_path)(const char *, Py_ssize_t, int *);
} WreathRequestCAPI;

#endif
