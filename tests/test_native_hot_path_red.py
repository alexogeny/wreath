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
