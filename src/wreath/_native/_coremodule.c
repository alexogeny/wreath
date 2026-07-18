/* wreath._native._core: hand-written C accelerators for Wreath's hot paths.
 *
 * Every function here has a pure-Python twin in wreath._pure with identical
 * observable behavior; the facades in wreath.* select whichever is available.
 */
#include "wreathcore.h"

static PyMethodDef core_methods[] = {
    {"parse_dotenv", wreath_parse_dotenv, METH_O,
     "parse_dotenv(data) -> dict[str, str]"},
    {"read_osenv", wreath_read_osenv, METH_NOARGS,
     "read_osenv() -> dict[str, str]"},
    {"run_validation", wreath_run_validation, METH_VARARGS,
     "run_validation(plan, value, loc) -> (result, errors)"},
    {"orm_shape", wreath_orm_shape, METH_VARARGS,
     "orm_shape(registry, select) -> bytes\nORM query cache key."},
    {"orm_shape_configure", wreath_orm_shape_configure, METH_VARARGS,
     "orm_shape_configure(*expr_types, ORMError) -> None"},
    {"orm_collect_values", wreath_orm_collect_values, METH_VARARGS,
     "orm_collect_values(select) -> list[ValueExpr]\nBind nodes in order."},
    {"csrf_sign", wreath_csrf_sign, METH_VARARGS,
     "csrf_sign(secret, issued, nonce) -> str"},
    {"csrf_new_token", wreath_csrf_new_token, METH_VARARGS,
     "csrf_new_token(secret, issued) -> str"},
    {"csrf_validate", wreath_csrf_validate, METH_VARARGS,
     "csrf_validate(secret, token, now, max_age) -> (bool, int)"},
    {"host_allowed", wreath_host_allowed, METH_VARARGS,
     "host_allowed(host, patterns) -> bool"},
    {"request_id_valid", wreath_request_id_valid, METH_VARARGS,
     "request_id_valid(value, max_len) -> bool"},
    {"format_server_timing", wreath_format_server_timing, METH_VARARGS,
     "format_server_timing(name, seconds) -> bytes"},
    {"build_capability_mask", wreath_build_capability_mask, METH_VARARGS,
     "build_capability_mask(capabilities, roles, permissions) -> int"},
    {"normalize_authorization_decision", wreath_normalize_authorization_decision,
     METH_VARARGS,
     "normalize_authorization_decision(result, decision_type) -> decision"},
    {"find_header", wreath_find_header, METH_VARARGS,
     "find_header(headers, name) -> bytes | None\n"
     "Return the first value for a lowercase header name."},
    {"build_header_map", wreath_build_header_map, METH_VARARGS,
     "build_header_map(headers) -> dict[bytes, bytes]\n"
     "Build a first-value-wins mapping from an ASGI header list."},
    {"percent_decode", (PyCFunction)(void (*)(void))wreath_percent_decode,
     METH_VARARGS | METH_KEYWORDS,
     "percent_decode(data, plus_as_space=False) -> bytes"},
    {"parse_qs", wreath_parse_qs, METH_VARARGS,
     "parse_qs(query) -> list[tuple[str, str]]"},
    {"parse_cookies", wreath_parse_cookies, METH_VARARGS,
     "parse_cookies(header) -> dict[str, str]"},
    {"ws_mask", wreath_ws_mask, METH_VARARGS,
     "ws_mask(data, key) -> bytes\nXOR-(un)mask a WebSocket payload."},
    {"ws_parse_frame", wreath_ws_parse_frame, METH_VARARGS,
     "ws_parse_frame(buffer) -> (fin, opcode, payload, consumed) | None"},
    {"ws_build_frame", (PyCFunction)(void (*)(void))wreath_ws_build_frame,
     METH_VARARGS | METH_KEYWORDS,
     "ws_build_frame(opcode, payload, fin=True, mask_key=None) -> bytes"},
    {"multipart_parse", (PyCFunction)(void (*)(void))wreath_multipart_parse,
     METH_VARARGS | METH_KEYWORDS,
     "multipart_parse(body, boundary, max_parts=-1, max_part_header_bytes=-1, "
     "max_part_bytes=-1) -> list[(headers, data)]\nA negative limit means no limit."},
    {"json_dumps", wreath_json_dumps, METH_O,
     "json_dumps(obj) -> bytes\nSerialize to compact UTF-8 JSON."},
    {"json_loads", wreath_json_loads, METH_O,
     "json_loads(data) -> object\n"
     "Parse JSON from str/bytes/bytearray with stdlib json.loads semantics."},
    {"template_render", wreath_template_render, METH_VARARGS,
     "template_render(tape, context, max_output) -> bytes\n"
     "Execute a compiled template tape to escaped UTF-8."},
    {"template_configure", wreath_template_configure, METH_VARARGS,
     "template_configure(markup_type, render_error_type) -> None"},
    {"http_parse_request", wreath_http_parse_request, METH_O,
     "http_parse_request(data) -> (method, target, minor, headers, consumed) | None"},
    {"http_parse_response", wreath_http_parse_response, METH_O,
     "http_parse_response(data) -> (minor, status, reason, headers, consumed) | None"},
    {"http_serialize_request", wreath_http_serialize_request, METH_VARARGS,
     "http_serialize_request(method, target, host, headers, body) -> bytes"},
    {"select_content_encoding", wreath_select_content_encoding, METH_O,
     "select_content_encoding(accept_encoding) -> str | None"},
    {"is_compressible_content_type", wreath_is_compressible_content_type, METH_O,
     "is_compressible_content_type(content_type) -> bool"},
    {"cache_control_flags", wreath_cache_control_flags, METH_O,
     "cache_control_flags(value) -> int"},
    {"origin_matches", wreath_origin_matches, METH_VARARGS,
     "origin_matches(origin, allowed) -> bool"},
    {"append_missing_headers", wreath_append_missing_headers, METH_VARARGS,
     "append_missing_headers(headers, additions) -> None"},
    {"append_vary", wreath_append_vary, METH_VARARGS,
     "append_vary(headers, token) -> None"},
    {"replace_content_length", wreath_replace_content_length, METH_VARARGS,
     "replace_content_length(headers, length) -> None"},
    {"find_response_header", wreath_find_response_header, METH_VARARGS,
     "find_response_header(headers, name) -> bytes | None"},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef core_module = {
    PyModuleDef_HEAD_INIT,
    "wreath._native._core",
    "C accelerators for Wreath (optional; pure-Python twins live in wreath._pure).",
    -1,
    core_methods,
    NULL,
    NULL,
    NULL,
    NULL,
};

static WreathCoreCAPI core_capi = {
    wreath_ws_parse_header_raw,
    wreath_ws_unmask_raw,
};


PyMODINIT_FUNC
PyInit__core(void)
{
    PyObject *module = PyModule_Create(&core_module);
    PyObject *capsule;
    if (module == NULL) {
        return NULL;
    }
    if (wreath_security_ready() < 0 ||
        wreath_register_router(module) < 0 || wreath_register_dtrouter(module) < 0 ||
        wreath_register_dtbitset(module) < 0 ||
        wreath_register_webpolicy(module) < 0 || wreath_register_proxy(module) < 0 ||
        wreath_register_ratelimit(module) < 0) {
        Py_DECREF(module);
        return NULL;
    }
    /* C-level API for sibling extensions (the native server) so hot paths
     * can share this module's parsers without per-call Python overhead. */
    capsule = PyCapsule_New(&core_capi, WREATH_CORE_CAPI_NAME, NULL);
    if (capsule == NULL || PyModule_AddObject(module, "_C_API", capsule) < 0) {
        Py_XDECREF(capsule);
        Py_DECREF(module);
        return NULL;
    }
    return module;
}
