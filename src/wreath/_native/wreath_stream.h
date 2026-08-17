/* The stream-fusion C API: zero-Python ingress/egress between the native
 * transport and any native stream protocol.
 *
 * A protocol module exports a capsule of this shape; the metal transport
 * probes registered capsules at protocol-bind time and, when `check` matches,
 * delivers ingress through `feed_external` (completion-driven provided
 * buffers) or `acquire_read_buffer`/`commit_read` (synchronous poll reads)
 * with no per-read Python calling convention, memoryview, or boxed sizes.
 * HTTP/1 was the first implementer (see docs/plans/native-buffered-protocol-
 * ingress.md); the shape is protocol-agnostic and versioned once here.
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

#endif
