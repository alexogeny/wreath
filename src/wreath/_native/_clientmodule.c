/* wreath._native._client: outbound HTTP codec boundary.
 *
 * Connection policy and asyncio ownership remain in Python for now. This
 * independently importable module compiles the shared HTTP byte tooling so the
 * client does not depend on the framework accelerator or native server module.
 */
#include "wreathcore.h"

static PyMethodDef client_methods[] = {
    {"response_framing", wreath_http_response_framing, METH_VARARGS,
     "response_framing(method, status, headers) -> (mode, length)"},
    {"response_keeps_alive", wreath_http_response_keeps_alive, METH_VARARGS,
     "response_keeps_alive(minor, headers, framed) -> bool"},
    {"parse_chunk_size", wreath_http_parse_chunk_size, METH_O,
     "parse_chunk_size(line) -> int"},
    {"serialize_request", wreath_http_serialize_request, METH_VARARGS,
     "serialize_request(method, target, host, headers, body) -> bytes"},
    {"_configure_fast_path", wreath_http_client_configure_fast_path,
     METH_VARARGS, NULL},
    {"_request_once", wreath_http_client_request_once, METH_VARARGS, NULL},
    {"_request_default", wreath_http_client_request_default, METH_VARARGS, NULL},
    {"_counters_new", wreath_http_client_counters_new, METH_NOARGS, NULL},
    {"_counters_snapshot", wreath_http_client_counters_snapshot, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};

static PyModuleDef client_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "wreath._native._client",
    .m_doc = "Native outbound HTTP codecs for Wreath.",
    .m_size = 0,
    .m_methods = client_methods,
};

PyMODINIT_FUNC
PyInit__client(void)
{
    PyObject *module = PyModule_Create(&client_module);
    if (module == NULL) return NULL;
    if (wreath_http_register_client_response_parser(module) < 0 ||
        wreath_register_http_client_protocol(module) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
