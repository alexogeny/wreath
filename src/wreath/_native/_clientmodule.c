/* wreath._native._client: outbound HTTP codec boundary.
 *
 * Connection policy and asyncio ownership remain in Python for now. This
 * independently importable module compiles the shared HTTP byte tooling so the
 * client does not depend on the framework accelerator or native server module.
 */
#include "wreathcore.h"

static PyMethodDef client_methods[] = {
    {"parse_response_head", wreath_http_parse_response, METH_O,
     "parse_response_head(data) -> (minor, status, reason, headers, consumed) | None"},
    {"serialize_request", wreath_http_serialize_request, METH_VARARGS,
     "serialize_request(method, target, host, headers, body) -> bytes"},
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
    return PyModule_Create(&client_module);
}
