/* wreath._native._server module definition and initialization. */
#include "server.h"
#include "server_request_capi.h"

static WreathRequestCAPI request_capi = {
    WREATH_REQUEST_CAPI_VERSION,
    wreath_request_context_new,
    wreath_request_context_check,
    wreath_request_context_set_flight,
    wreath_request_context_set_armed,
    wreath_request_context_sever,
    wreath_request_scope_seed_flight,
    wreath_request_context_set_policy,
    wreath_request_context_update_policy,
};

static WreathHttp1CAPI http1_capi = {
    WREATH_HTTP1_CAPI_VERSION,
    wreath_http1_protocol_check,
    wreath_http1_acquire_read_buffer,
    wreath_http1_commit_read,
    wreath_http1_feed_external,
};

static WreathHttp2CAPI http2_capi = {
    WREATH_HTTP2_CAPI_VERSION,
    wreath_http2_protocol_check,
    wreath_http2_acquire_read_buffer,
    wreath_http2_commit_read,
    wreath_http2_feed_external,
};

static PyModuleDef server_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "wreath._native._server",
    .m_doc = "Native HTTP/1.1 server protocol for Wreath.",
    .m_size = -1,
    .m_free = server_module_free,
};


/* Http1Protocol must genuinely inherit asyncio.BufferedProtocol: asyncio
 * selects the zero-copy buffered receive path with an isinstance() check,
 * not by method presence. */
static PyObject *
make_http1_protocol_type(void)
{
    PyObject *protocols_module;
    PyObject *buffered_base;
    PyObject *bases;
    PyObject *protocol_type;

    protocols_module = PyImport_ImportModule("asyncio.protocols");
    if (protocols_module == NULL) {
        return NULL;
    }
    buffered_base = PyObject_GetAttrString(protocols_module, "BufferedProtocol");
    Py_DECREF(protocols_module);
    if (buffered_base == NULL) {
        return NULL;
    }
    bases = PyTuple_Pack(1, buffered_base);
    Py_DECREF(buffered_base);
    if (bases == NULL) {
        return NULL;
    }
    protocol_type = PyType_FromSpecWithBases(&http_protocol_spec, bases);
    Py_DECREF(bases);
    return protocol_type;
}


PyMODINIT_FUNC
PyInit__server(void)
{
    PyObject *module;
    PyObject *protocol_type;
    PyObject *request_capsule;
    PyObject *http1_capsule;
    PyObject *http2_capsule;

    module = PyModule_Create(&server_module);
    if (module == NULL) {
        return NULL;
    }
    /* A private heap exception type adds permanent interpreter allocations on
     * non-ASan CPython builds. ConnectionError has the exact internal role:
     * distinguish peer disconnect cancellation from application failures. */
    disconnect_error = PyExc_ConnectionError; /* borrowed */
    if (PyType_Ready(&ImmediateAwaitableType) < 0 ||
        PyType_Ready(&StartedCoroutineType) < 0 ||
        PyType_Ready(&ValueAwaitableType) < 0) {
        disconnect_error = NULL;
        Py_DECREF(module);
        return NULL;
    }
    immediate_none = PyObject_CallNoArgs((PyObject *)&ImmediateAwaitableType);
    if (immediate_none == NULL || init_cached_constants() < 0) {
        server_module_free(NULL);
        Py_DECREF(module);
        return NULL;
    }
    if (wreath_request_context_ready(module) < 0) {
        server_module_free(NULL);
        Py_DECREF(module);
        return NULL;
    }
    wreath_header_block_freelist_enable();
    request_capsule = PyCapsule_New(
        &request_capi, WREATH_REQUEST_CAPI_NAME, NULL
    );
    if (request_capsule == NULL ||
        PyModule_AddObject(module, "_REQUEST_C_API", request_capsule) < 0) {
        Py_XDECREF(request_capsule);
        server_module_free(NULL);
        Py_DECREF(module);
        return NULL;
    }
    protocol_type = make_http1_protocol_type();
    if (protocol_type == NULL) {
        disconnect_error = NULL;
        Py_DECREF(module);
        return NULL;
    }
    wreath_http1_protocol_set_type(protocol_type);
    http1_capsule = PyCapsule_New(&http1_capi, WREATH_HTTP1_CAPI_NAME, NULL);
    if (http1_capsule == NULL ||
        PyModule_AddObject(module, "_HTTP1_C_API", http1_capsule) < 0) {
        Py_XDECREF(http1_capsule);
        Py_DECREF(protocol_type);
        disconnect_error = NULL;
        Py_DECREF(module);
        return NULL;
    }
    /* Http1Protocol is the canonical name; HttpProtocol is retained as an
     * alias for backward compatibility. Http2Protocol and
     * NegotiatingHttpProtocol arrive in later checkpoints. */
    /* Its await/send/throw/close surface makes asyncio accept it as a
     * coroutine without a Python adapter. */
    if (PyModule_AddObjectRef(module, "StartedCoroutine",
                              (PyObject *)&StartedCoroutineType) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    if (PyModule_AddObjectRef(module, "Http1Protocol", protocol_type) < 0 ||
        PyModule_AddObjectRef(module, "HttpProtocol", protocol_type) < 0) {
        Py_DECREF(protocol_type);
        disconnect_error = NULL;
        Py_DECREF(module);
        return NULL;
    }
    Py_DECREF(protocol_type);
    if (PyModule_AddObjectRef(module, "_Disconnect", disconnect_error) < 0) {
        disconnect_error = NULL;
        Py_DECREF(module);
        return NULL;
    }
    if (PyModule_AddStringConstant(module, "IMPLEMENTATION", "c-native") < 0 ||
        PyModule_AddIntConstant(module, "HOT_PATH_NATIVE", 1) < 0) {
        disconnect_error = NULL;
        Py_DECREF(module);
        return NULL;
    }
    /* Register the HTTP/2 protocol type (server_http2.c) and build the shared
     * HPACK Huffman decode tree. */
    if (wreath_http2_ready(module) < 0) {
        disconnect_error = NULL;
        Py_DECREF(module);
        return NULL;
    }
    http2_capsule = PyCapsule_New(&http2_capi, WREATH_HTTP2_CAPI_NAME, NULL);
    if (http2_capsule == NULL ||
        PyModule_AddObject(module, "_HTTP2_C_API", http2_capsule) < 0) {
        Py_XDECREF(http2_capsule);
        disconnect_error = NULL;
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
