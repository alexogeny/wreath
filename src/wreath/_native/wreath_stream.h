/* The stream-fusion C API: zero-Python ingress/egress between the native
 * transport and any native stream protocol.
 *
 * A protocol module exports a capsule of this shape; the metal transport
 * probes registered capsules at protocol-bind time and, when `check` matches,
 * delivers ingress through `feed_external` (completion-driven provided
 * buffers) or `acquire_read_buffer`/`commit_read` (synchronous poll reads)
 * with no per-read Python calling convention, memoryview, or boxed sizes.
 */
#ifndef WREATH_STREAM_H
#define WREATH_STREAM_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

#define WREATH_STREAM_CAPI_VERSION 2

typedef struct {
    uint32_t version;
    int (*check)(PyObject *);
    int (*acquire_read_buffer)(PyObject *, char **, Py_ssize_t *);
    int (*commit_read)(PyObject *, Py_ssize_t);
    int (*feed_external)(PyObject *, const char *, Py_ssize_t);
} WreathStreamCAPI;

/* Reverse direction: native protocols emit directly through the metal
 * transport without a PyObject_Call boundary. */
#define WREATH_TRANSPORT_CAPI_NAME "wreath._native._reactor._TRANSPORT_C_API"
#define WREATH_TRANSPORT_CAPI_VERSION 2

typedef struct {
    uint32_t version;
    int (*check)(PyObject *);
    int (*write)(PyObject *, PyObject *);
    int (*writelines)(PyObject *, PyObject *);
    int (*is_closing)(PyObject *);
} WreathTransportCAPI;

/* Resolve the immutable reactor ABI without a process-global mutable cache.
 * Kept inline because client HTTP and PostgreSQL are separate extensions: each
 * retains the pointer on its connection, while the resolution and error-
 * clearing contract has one source definition. */
static inline const WreathTransportCAPI *
wreath_transport_capi_resolve(void)
{
    PyObject *modules = PyImport_GetModuleDict();  /* borrowed */
    PyObject *module = modules == NULL
        ? NULL : PyDict_GetItemString(modules, "wreath._native._reactor");
    if (module == NULL) return NULL;
    PyObject *capsule = PyObject_GetAttrString(module, "_TRANSPORT_C_API");
    if (capsule == NULL) {
        PyErr_Clear();
        return NULL;
    }
    const WreathTransportCAPI *capi = PyCapsule_GetPointer(
        capsule, WREATH_TRANSPORT_CAPI_NAME);
    Py_DECREF(capsule);
    if (capi == NULL) {
        PyErr_Clear();
        return NULL;
    }
    return capi->version == WREATH_TRANSPORT_CAPI_VERSION ? capi : NULL;
}

#endif
