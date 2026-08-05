/* wreath._native._edge: the reverse proxy's request path.
 *
 * `wreath.edge` is native-only by design and has no pure twin -- see AGENTS.md.
 * For a reverse proxy a Python fallback is a footgun rather than a safety net:
 * it degrades silently, by roughly five times, in the one component whose whole
 * job is to be faster than what it replaces, and nothing at runtime announces
 * which path a request took.
 *
 * This module is deliberately separate from `_core` and `_server`. A build may
 * have one extension and not another, and a proxy that imports is a proxy that
 * is fast; there is no configuration in which importing half of this is useful.
 */
#include "edge.h"

static PyMethodDef edge_methods[] = {
    {"request_headers", (PyCFunction)(void (*)(void))wreath_edge_request_headers,
     METH_FASTCALL | METH_KEYWORDS,
     "request_headers(inbound, *, client, scheme, via) -> list[tuple[bytes, bytes]]\n\n"
     "The outbound request headers for one forwarded request, in a single pass."},
    {NULL, NULL, 0, NULL},
};

static PyModuleDef edge_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "wreath._native._edge",
    .m_doc = "Native request path for wreath.edge.",
    .m_size = 0,
    .m_methods = edge_methods,
};

PyMODINIT_FUNC
PyInit__edge(void)
{
    PyObject *module = PyModule_Create(&edge_module);
    if (module == NULL) {
        return NULL;
    }
    if (wreath_edge_serve_ready(module) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
