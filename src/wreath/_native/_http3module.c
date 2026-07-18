/* wreath._native._http3: optional HTTP/3 (QUIC) datagram endpoint.
 *
 * Built only when WREATH_BUILD_HTTP3=1 with ngtcp2/nghttp3 available (ADR 0011).
 * This module exposes a datagram endpoint type used solely by wreath.server. The
 * endpoint implementation lands in a later checkpoint; for now the module
 * initializes and registers its types via wreath_http3_ready().
 */
#include "http3.h"

static struct PyModuleDef http3_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "wreath._native._http3",
    .m_doc = "Optional native HTTP/3 (QUIC) datagram endpoint for Wreath.",
    .m_size = -1,
};

PyMODINIT_FUNC
PyInit__http3(void)
{
    PyObject *module = PyModule_Create(&http3_module);
    if (module == NULL) {
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
