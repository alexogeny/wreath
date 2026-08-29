from __future__ import annotations

from pathlib import Path

from wreath.request import Request

_ROOT = Path(__file__).parents[1]
_SRC = _ROOT / "src" / "wreath"
_NATIVE = _SRC / "_native"


def _function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"\n{name}(")
    end = source.index(f"\n{next_name}(", start)
    return source[start:end]


def test_h2_data_delivery_batches_window_updates() -> None:
    source = (_NATIVE / "server_http2.c").read_text()
    delivery = _function(source, "deliver_body", "process_settings")
    assert delivery.count("h2_write_frame(") == 0


def test_h2_request_queue_coalesces_adjacent_body_chunks() -> None:
    source = (_NATIVE / "server_http2.c").read_text()
    assert "body_queue_coalesce" in source


def test_h3_request_queue_does_not_allocate_one_object_per_callback() -> None:
    source = (_NATIVE / "http3_asgi.c").read_text()
    receive = _function(source, "recv_data_cb", "end_stream_cb")
    assert "PyList_Append(s->body_chunks" not in receive
    assert "PyBytes_FromStringAndSize" not in receive


def test_request_json_is_decoded_once() -> None:
    assert "_json" in Request.__slots__


def test_request_form_is_parsed_once() -> None:
    assert "_form" in Request.__slots__


def test_request_body_does_not_join_a_python_chunk_list() -> None:
    source = (_SRC / "request.py").read_text()
    body = source[source.index("    async def body(") : source.index("    async def json(")]
    assert "chunks.append(" not in body
    assert 'b"".join(chunks)' not in body


def test_snapshot_iteration_does_not_copy_the_generation() -> None:
    source = (_SRC / "_snapshot.py").read_text()
    start = source.index("    def __iter__(")
    iteration = source[start : source.index("    @property", start)]
    assert "tuple(" not in iteration


def test_eager_http1_handler_does_not_allocate_asyncio_task() -> None:
    source = (_NATIVE / "server_http1.c").read_text()
    spawn = _function(source, "spawn_app_task", "send_policy_reply")
    assert "task_class" not in spawn


def test_json_key_cache_is_parser_local() -> None:
    source = (_NATIVE / "json.c").read_text()
    assert "static PyObject *wreath_key_cache" not in source


def test_h2_flush_transfers_output_without_bytearray_copy() -> None:
    source = (_NATIVE / "server_http2.c").read_text()
    flush = _function(source, "h2_flush", "h2_connection_error")
    assert "PyBytes_FromStringAndSize(PyByteArray_AS_STRING(self->out)" not in flush


def test_multipart_disposition_is_decoded_in_the_native_pass() -> None:
    source = (_SRC / "_multipart.py").read_text()
    parse = source[source.index("def parse(") : source.index("\n\n__all__")]
    assert "_disposition_param(" not in parse


def test_h2_settings_applies_initial_window_once_per_frame() -> None:
    source = (_NATIVE / "server_http2.c").read_text()
    settings = _function(source, "process_settings", "finish_header_block")
    assert "PyDict_Next" not in settings


def test_h2_initial_window_update_does_not_scan_all_active_streams() -> None:
    source = (_NATIVE / "server_http2.c").read_text()
    update = _function(source, "apply_peer_initial_window", "process_settings")
    assert "PyDict_Next" not in update


def test_hpack_enforces_decoded_limits_before_materializing_fields() -> None:
    source = (_NATIVE / "server_hpack.c").read_text()
    decode = _function(source, "wreath_hpack_decode", "huffman_encoded_len")
    append_at = decode.index("wreath_header_block_append_objects")
    assert "max_header_count" in decode[:append_at]
    assert "max_header_list" in decode[:append_at]


def test_h2_and_h3_wreath_responses_bypass_generic_asgi_messages() -> None:
    h2 = (_NATIVE / "server_http2.c").read_text()
    h2_fast = _function(h2, "stream_wreath_response", "h2_maybe_close_stream")
    assert "Py_BuildValue" not in h2_fast
    assert "stream_send(" not in h2_fast

    h3 = (_NATIVE / "http3_asgi.c").read_text()
    h3_fast = _function(h3, "h3_stream_wreath_response", "begin_headers_cb")
    assert "Py_BuildValue" not in h3_fast
    assert "h3_stream_send(" not in h3_fast


def test_h2_and_h3_cache_invariant_native_scope_values() -> None:
    h2 = (_NATIVE / "server_http2.c").read_text()
    h2_scope = _function(h2, "build_h2_scope", "parse_priority_field")
    assert 'PyUnicode_FromString("http")' not in h2_scope
    assert 'PyUnicode_FromString("2")' not in h2_scope
    assert 'PyUnicode_FromString("")' not in h2_scope

    h3 = (_NATIVE / "http3_asgi.c").read_text()
    h3_start = _function(h3, "start_request", "end_headers_cb")
    assert 'PyUnicode_FromString("http")' not in h3_start
    assert 'PyUnicode_FromString("3")' not in h3_start
    assert 'PyUnicode_FromString("")' not in h3_start


def test_h3_immediate_results_do_not_allocate_asyncio_futures() -> None:
    source = (_NATIVE / "http3_asgi.c").read_text()
    helper = _function(source, "resolved_future", "h3_response_over_high_water")
    assert "create_future" not in helper
    assert "set_result" not in helper
