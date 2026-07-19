/* wreath._native._http3: optional HTTP/3 (QUIC) datagram endpoint.
 *
 * Built only when WREATH_BUILD_HTTP3=1 with ngtcp2/nghttp3 available (ADR 0011).
 * This module exposes a datagram endpoint type used solely by wreath.server. The
 * endpoint implementation lands in a later checkpoint; for now the module
 * initializes and registers its types via wreath_http3_ready().
 */
#include "http3.h"

const WreathRequestCAPI *wreath_h3_request_capi = NULL;

static struct PyModuleDef http3_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "wreath._native._http3",
    .m_doc = "Optional native HTTP/3 (QUIC) datagram endpoint for Wreath.",
    .m_size = -1,
};

/* Resolve the server extension's request C API. `_http3` depends on the request
 * context capsule that `wreath._native._server` publishes, so we import that
 * module first: PyCapsule_Import will not import it on our behalf, and a process
 * that imports `_http3` before `_server` (e.g. an isolated HTTP/3 test run) would
 * otherwise fail to find the capsule. Kept out of PyInit itself so module init
 * does no importing directly. Returns 0 on success, -1 with an exception set. */
static int
request_capi_ready(void)
{
    PyObject *server_module = PyImport_ImportModule("wreath._native._server"); /* native-lint: allow NC004 -- one module-init import makes capsule ordering explicit */
    if (server_module == NULL) {
        return -1;
    }
    Py_DECREF(server_module);
    wreath_h3_request_capi = (const WreathRequestCAPI *)PyCapsule_Import(
        WREATH_REQUEST_CAPI_NAME, 0
    );
    if (wreath_h3_request_capi == NULL ||
        wreath_h3_request_capi->version != WREATH_REQUEST_CAPI_VERSION) {
        return -1;
    }
    return 0;
}

PyMODINIT_FUNC
PyInit__http3(void)
{
    PyObject *module = PyModule_Create(&http3_module);
    if (module == NULL) {
        return NULL;
    }
    if (request_capi_ready() < 0) {
        Py_DECREF(module);
        return NULL;
    }
    if (PyModule_AddStringConstant(module, "IMPLEMENTATION", "ngtcp2-nghttp3") < 0) {
        Py_DECREF(module);
        return NULL;
    }
    if (wreath_h3_ready(module) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
