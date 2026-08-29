from __future__ import annotations

import re
from pathlib import Path

_NATIVE = Path(__file__).parents[1] / "src" / "wreath" / "_native"


def _function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"\n{name}(")
    end = source.index(f"\n{next_name}(", start)
    return source[start:end]


def test_http1_complete_head_is_not_scanned_twice() -> None:
    driver = (_NATIVE / "server_http1.c").read_text()
    parser = (_NATIVE / "http.c").read_text()

    drive_head = _function(driver, "drive_head", "drive_fixed_body")
    parse_head = _function(parser, "wreath_http_parse_request_parts", "wreath_http_parse_request")
    scans = drive_head.count('find_sub_from(p, n, "\\r\\n\\r\\n"')
    scans += parse_head.count('wreath_memmem(\n        data, len, (const uint8_t *)"\\r\\n\\r\\n"')

    assert scans == 1, "the HTTP/1 driver and parser both scan the complete request head"


def test_http1_framing_does_not_walk_materialized_headers_again() -> None:
    driver = (_NATIVE / "server_http1.c").read_text()
    parser = (_NATIVE / "http.c").read_text()
    parse = _function(parser, "wreath_http_parse_request_parts", "wreath_http_parse_request")

    assert "\ndecide_framing(" not in driver
    assert 'memcmp(name, "content-length", 14)' in parse
    assert "request_meta->kind" in parse


def test_http1_common_default_headers_are_one_contiguous_copy() -> None:
    source = (_NATIVE / "server_http1.c").read_text()
    begin = _function(source, "begin_response_parts", "begin_response")
    start = begin.index("if (!has_date && !has_server)")
    common = begin[start : begin.index("else {", start)]

    assert "default_response_wire" in common
    assert "PySequence_Fast" not in common


def test_http1_reused_immutable_response_headers_skip_revalidation() -> None:
    source = (_NATIVE / "server_http1.c").read_text()
    begin = _function(source, "begin_response_parts", "begin_response")

    assert "headers == self->response_header_cache_key" in begin
    assert "response_header_cache_wire" in begin
    assert "PyBytes_GET_SIZE(cache_wire) <= 1024" in begin


def test_http1_equivalent_dynamic_response_headers_reuse_validated_wire() -> None:
    source = (_NATIVE / "server_http1.c").read_text()
    begin = _function(source, "begin_response_parts", "begin_response")

    assert "response_headers_match_cache(self, headers)" in begin
    assert "PyList_AsTuple(headers)" in begin


def test_eager_request_completion_does_not_cross_back_to_probe_task_state() -> None:
    source = (_NATIVE / "server_http1.c").read_text()
    spawn = _function(source, "spawn_app_task", "send_policy_reply")

    assert "task_done_fn" not in spawn, (
        "every eager request calls back into Python to probe Task.done()"
    )


def test_h3_scope_build_notes_host_inline_without_a_second_pass() -> None:
    source = (_NATIVE / "http3_asgi.c").read_text()
    start = _function(source, "start_request", "end_headers_cb")

    assert "PyList_GET_SIZE(scope_headers)" not in start, (
        "start_request walks the scope headers a second time just to detect host"
    )


def test_scope_builders_do_not_allocate_the_host_name_per_request() -> None:
    h2 = (_NATIVE / "server_http2.c").read_text()
    h3 = (_NATIVE / "http3_asgi.c").read_text()
    build_h2 = _function(h2, "build_h2_scope", "start_request")
    start_h3 = _function(h3, "start_request", "end_headers_cb")

    assert 'PyBytes_FromString("host")' not in build_h2, (
        "build_h2_scope allocates a fresh host name on every request"
    )
    assert 'PyBytes_FromString("host")' not in start_h3, (
        "the HTTP/3 start_request allocates a fresh host name on every request"
    )


def test_well_known_name_matching_does_not_strlen_constants_per_request() -> None:
    source = (_NATIVE / "http.c").read_text()
    method = _function(source, "method_object", "header_name_object")
    header = _function(source, "header_name_object", "wreath_http_parse_request_parts")

    assert "strlen(" not in method, "method_object recomputes strlen on every request"
    assert "strlen(" not in header, "header_name_object recomputes strlen on every header"


def test_default_bitset_router_matches_literals_without_python_segment_objects() -> None:
    source = (_NATIVE / "policy_router.c").read_text()
    match = _function(source, "brt_match_impl", "brt_dispatch")

    assert "PyUnicode_FromStringAndSize(" not in match
    assert "PyUnicode_DecodeUTF8(" not in match


def test_bitset_hot_methods_use_fastcall() -> None:
    source = (_NATIVE / "policy_router.c").read_text()
    methods = source[source.index("static PyMethodDef brt_methods[]") :]

    for name in ("match", "classify", "resolve", "probe"):
        row = next(line for line in methods.splitlines() if f'{{"{name}"' in line)
        assert "METH_FASTCALL" in row, f"bitset {name} still uses METH_VARARGS"


def test_bitset_groups_are_compiled_before_the_first_match() -> None:
    source = (_NATIVE / "policy_router.c").read_text()
    match = _function(source, "brt_match_impl", "brt_dispatch")

    assert "brt_group_build(" not in match, "first match pays route-group compilation"


def test_bitset_common_large_groups_do_not_spill_survivors_to_heap() -> None:
    source = (_NATIVE / "policy_router.c").read_text()
    size = re.search(r"uint64_t stack_words\[(\d+)\]", source)

    assert size is not None and int(size.group(1)) >= 64, (
        "bitset groups above 1024 routes allocate survivor scratch per match"
    )


def test_bitset_path_parameters_are_lazy_native_slices() -> None:
    source = (_NATIVE / "policy_router.c").read_text()
    build = _function(source, "build_match", "brt_match_impl")

    assert "PyDict_New(" not in build
    assert "PyUnicode_DecodeUTF8(" not in build


def test_bitset_has_no_process_global_python_objects() -> None:
    source = (_NATIVE / "policy_router.c").read_text()

    assert "static PyObject *brt_zero" not in source
    assert "static PyObject *get" not in source
    assert "static MethodGroups brt_no_groups" not in source


def test_http1_idle_keepalive_releases_spike_capacity() -> None:
    source = (_NATIVE / "server_http1.c").read_text()

    assert "shrink_idle_input_buffer" in source


def test_http2_idle_connection_releases_spike_capacity() -> None:
    source = (_NATIVE / "server_http2.c").read_text()

    assert "shrink_idle_input_buffer" in source


def test_multipart_parser_does_not_copy_every_part_payload() -> None:
    source = (_NATIVE / "multipart.c").read_text()

    assert "PyBytes_FromStringAndSize((const char *)body_start" not in source


def test_http1_receive_queue_stores_native_descriptors() -> None:
    source = (_NATIVE / "server_http1.c").read_text()

    assert "PyList_Append(self->receive_queue" not in source


def test_http2_receive_queue_stores_native_descriptors() -> None:
    source = (_NATIVE / "server_http2.c").read_text()

    assert "PyList_Append(st->body_chunks" not in source


def test_http3_response_queue_is_a_native_ack_ring() -> None:
    source = (_NATIVE / "http3_asgi.c").read_text()

    assert "PyList_Append(s->resp_chunks" not in source


def test_http_parser_builds_asgi_headers_without_generic_python_calls() -> None:
    source = (_NATIVE / "http.c").read_text()
    parse = _function(source, "wreath_http_parse_request_parts", "wreath_http_parse_request")

    assert "PyObject_Call" not in parse
    assert "PyObject_CallMethod" not in parse
    assert "Py_BuildValue" not in parse


def test_hpack_hard_limit_reclaims_entries_and_capacity_immediately() -> None:
    source = (_NATIVE / "server_hpack.c").read_text()
    setter = _function(source, "wreath_hpack_table_set_hard_max", "table_evict_to")

    assert "table_evict_to(" in setter
    assert "t->cap" in setter


def test_eager_http1_completion_only_allocates_a_task_after_suspension() -> None:
    source = (_NATIVE / "server_http1.c").read_text()
    spawn = _function(source, "spawn_app_task", "send_policy_reply")

    assert "PyIter_Send(" in spawn
    assert "loop_create_task" in spawn
    assert "task_class" not in spawn
