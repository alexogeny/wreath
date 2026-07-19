from __future__ import annotations

import re
from pathlib import Path

_NATIVE = Path(__file__).parents[1] / "src" / "wreath" / "_native"


def _function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"\n{name}(")
    end = source.index(f"\n{next_name}(", start)
    return source[start:end]


def test_http1_complete_head_is_not_scanned_twice() -> None:
    """A complete request head should have one delimiter scan across the call chain."""
    driver = (_NATIVE / "server_http1.c").read_text()
    parser = (_NATIVE / "http.c").read_text()

    drive_head = _function(driver, "drive_head", "drive_fixed_body")
    parse_head = _function(parser, "wreath_http_parse_request_parts", "wreath_http_parse_request")
    scans = drive_head.count('find_sub_from(p, n, "\\r\\n\\r\\n"')
    scans += parse_head.count('wreath_memmem(\n        data, len, (const uint8_t *)"\\r\\n\\r\\n"')

    assert scans == 1, "the HTTP/1 driver and parser both scan the complete request head"


def test_decision_router_compile_has_no_pairwise_distinctness_scan() -> None:
    """Route compilation must not compare every candidate with its predecessors."""
    source = (_NATIVE / "dtrouter.c").read_text()
    build = _function(source, "dnode_build", "install_group")

    pairwise = re.search(
        r"for \(Py_ssize_t i = 0; i < ncand; i\+\+\).*?"
        r"for \(Py_ssize_t j = 0; j < i; j\+\+\).*?seg_equal",
        build,
        re.DOTALL,
    )
    assert pairwise is None, "dnode_build performs an O(candidate_count**2) distinctness scan"


def test_eager_request_completion_does_not_cross_back_to_probe_task_state() -> None:
    """The synchronous fast path should not need a Python Task.done() round trip."""
    source = (_NATIVE / "server_http1.c").read_text()
    spawn = _function(source, "spawn_app_task", "is_upgrade_request")

    assert "task_done_fn" not in spawn, (
        "every eager request calls back into Python to probe Task.done()"
    )


def test_h3_scope_build_notes_host_inline_without_a_second_pass() -> None:
    """The HTTP/3 scope builder must not rescan the copied headers to find host."""
    source = (_NATIVE / "http3_asgi.c").read_text()
    start = _function(source, "start_request", "end_headers_cb")

    assert "PyList_GET_SIZE(scope_headers)" not in start, (
        "start_request walks the scope headers a second time just to detect host"
    )


def test_scope_builders_do_not_allocate_the_host_name_per_request() -> None:
    """Synthesizing host from :authority must reuse a cached name, not build one."""
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
    """Matching methods/header names must use compile-time lengths, not strlen."""
    source = (_NATIVE / "http.c").read_text()
    method = _function(source, "method_object", "header_name_object")
    header = _function(source, "header_name_object", "wreath_http_parse_request_parts")

    assert "strlen(" not in method, "method_object recomputes strlen on every request"
    assert "strlen(" not in header, "header_name_object recomputes strlen on every header"


def test_default_bitset_router_matches_literals_without_python_segment_objects() -> None:
    """The default matcher must compare literal UTF-8 slices without Unicode keys."""
    source = (_NATIVE / "dtbitset.c").read_text()
    match = _function(source, "brt_match_impl", "brt_dispatch")

    assert "PyUnicode_FromStringAndSize(" not in match
    assert "PyUnicode_DecodeUTF8(" not in match


def test_decision_router_has_no_process_global_python_objects() -> None:
    """Cached Python objects must belong to module state for subinterpreters and nogil."""
    source = (_NATIVE / "dtrouter.c").read_text()

    assert "static PyObject *public_caller_mask" not in source
    assert "static PyObject *get_method" not in source


def test_bitset_hot_methods_use_fastcall() -> None:
    """The default router must not allocate argument tuples for each hot call."""
    source = (_NATIVE / "dtbitset.c").read_text()
    methods = source[source.index("static PyMethodDef brt_methods[]"):]

    for name in ("match", "classify", "resolve", "probe"):
        row = next(line for line in methods.splitlines() if f'{{"{name}"' in line)
        assert "METH_FASTCALL" in row, f"bitset {name} still uses METH_VARARGS"


def test_bitset_groups_are_compiled_before_the_first_match() -> None:
    """A request must never compile a route group lazily."""
    source = (_NATIVE / "dtbitset.c").read_text()
    match = _function(source, "brt_match_impl", "brt_dispatch")

    assert "brt_group_build(" not in match, "first match pays route-group compilation"


def test_bitset_common_large_groups_do_not_spill_survivors_to_heap() -> None:
    """Groups through 4096 routes should use bounded stack scratch."""
    source = (_NATIVE / "dtbitset.c").read_text()
    size = re.search(r"uint64_t stack_words\[(\d+)\]", source)

    assert size is not None and int(size.group(1)) >= 64, (
        "bitset groups above 1024 routes allocate survivor scratch per match"
    )


def test_bitset_path_parameters_are_lazy_native_slices() -> None:
    """Matching should not eagerly allocate a dict and Unicode object per capture."""
    source = (_NATIVE / "dtbitset.c").read_text()
    build = _function(source, "build_match", "brt_match_impl")

    assert "PyDict_New(" not in build
    assert "PyUnicode_DecodeUTF8(" not in build


def test_bitset_has_no_process_global_python_objects() -> None:
    """The default router's cached objects must be held by module state."""
    source = (_NATIVE / "dtbitset.c").read_text()

    assert "static PyObject *brt_zero" not in source
    assert "static PyObject *get" not in source


def test_http1_idle_keepalive_releases_spike_capacity() -> None:
    """A large read must not pin its input allocation for the connection lifetime."""
    source = (_NATIVE / "server_http1.c").read_text()

    assert "shrink_idle_input_buffer" in source


def test_http2_idle_connection_releases_spike_capacity() -> None:
    """HTTP/2 must decay an oversized connection input allocation after draining."""
    source = (_NATIVE / "server_http2.c").read_text()

    assert "shrink_idle_input_buffer" in source


def test_multipart_parser_does_not_copy_every_part_payload() -> None:
    """Parsing a complete multipart body should not duplicate all payload bytes."""
    source = (_NATIVE / "multipart.c").read_text()

    assert "PyBytes_FromStringAndSize((const char *)body_start" not in source


def test_http1_receive_queue_stores_native_descriptors() -> None:
    """Buffered body chunks should not allocate a Python list entry and ASGI dict."""
    source = (_NATIVE / "server_http1.c").read_text()

    assert "PyList_Append(self->receive_queue" not in source


def test_http2_receive_queue_stores_native_descriptors() -> None:
    """Each HTTP/2 DATA frame should not become a separately queued Python object."""
    source = (_NATIVE / "server_http2.c").read_text()

    assert "PyList_Append(st->body_chunks" not in source


def test_http3_response_queue_is_a_native_ack_ring() -> None:
    """Acknowledgement bookkeeping should not retain a Python list geometry."""
    source = (_NATIVE / "http3_asgi.c").read_text()

    assert "PyList_Append(s->resp_chunks" not in source


def test_http_parser_builds_asgi_headers_without_generic_python_calls() -> None:
    """Portable ASGI needs Python pairs, but parsing must not call Python code."""
    source = (_NATIVE / "http.c").read_text()
    parse = _function(source, "wreath_http_parse_request_parts", "wreath_http_parse_request")

    assert "PyObject_Call" not in parse
    assert "PyObject_CallMethod" not in parse
    assert "Py_BuildValue" not in parse


def test_hpack_hard_limit_reclaims_entries_and_capacity_immediately() -> None:
    """A SETTINGS table reduction must not retain the old table watermark."""
    source = (_NATIVE / "server_hpack.c").read_text()
    setter = _function(source, "wreath_hpack_table_set_hard_max", "table_evict_to")

    assert "table_evict_to(" in setter
    assert "t->cap" in setter


def test_eager_http1_completion_only_allocates_a_task_after_suspension() -> None:
    """The synchronous path completes inline; loop task ownership starts on yield."""
    source = (_NATIVE / "server_http1.c").read_text()
    spawn = _function(source, "spawn_app_task", "is_upgrade_request")

    assert "PyIter_Send(" in spawn
    assert "loop_create_task" in spawn
    assert "task_class" not in spawn
