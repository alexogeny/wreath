"""Red proofs for reducible CPU-pressure findings.

The suite intentionally fails. Assertions describe deterministic hot-path
properties and avoid wall-clock thresholds, which are too noisy for correctness
CI. Runtime attribution still belongs in the benchmark/decomposition harnesses.
"""

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
    """A tiny DATA frame must not immediately emit two control frames."""
    source = (_NATIVE / "server_http2.c").read_text()
    delivery = _function(source, "deliver_body", "process_settings")
    assert delivery.count("h2_write_frame(") == 0


def test_h2_request_queue_coalesces_adjacent_body_chunks() -> None:
    """CPU and object count should scale with bytes, not peer frame count."""
    source = (_NATIVE / "server_http2.c").read_text()
    assert "body_queue_coalesce" in source


def test_h3_request_queue_does_not_allocate_one_object_per_callback() -> None:
    """HTTP/3 DATA callbacks should feed a native/coalescing body queue."""
    source = (_NATIVE / "http3_asgi.c").read_text()
    receive = _function(source, "recv_data_cb", "end_stream_cb")
    assert "PyList_Append(s->body_chunks" not in receive
    assert "PyBytes_FromStringAndSize" not in receive


def test_request_json_is_decoded_once() -> None:
    """Repeated Request.json() calls should return one cached decode."""
    assert "_json" in Request.__slots__


def test_request_form_is_parsed_once() -> None:
    """Repeated Request.form() calls should return one cached FormData."""
    assert "_form" in Request.__slots__


def test_request_body_does_not_join_a_python_chunk_list() -> None:
    """Body construction should avoid list management plus a complete join copy."""
    source = (_SRC / "request.py").read_text()
    body = source[source.index("    async def body("):source.index("    async def json(")]
    assert "chunks.append(" not in body
    assert 'b"".join(chunks)' not in body


def test_snapshot_iteration_does_not_copy_the_generation() -> None:
    """An iterator can retain the current dict without first making a tuple."""
    source = (_SRC / "_pure" / "snapshot.py").read_text()
    start = source.index("    def __iter__(")
    iteration = source[start:source.index("    @property", start)]
    assert "tuple(" not in iteration


def test_eager_http1_handler_does_not_allocate_asyncio_task() -> None:
    """A handler that never suspends should not pay Task construction."""
    source = (_NATIVE / "server_http1.c").read_text()
    spawn = _function(source, "spawn_app_task", "is_upgrade_request")
    assert "task_class" not in spawn


def test_json_key_cache_is_parser_local() -> None:
    """High-cardinality clients should not thrash one process-global direct map."""
    source = (_NATIVE / "json.c").read_text()
    assert "static PyObject *wreath_key_cache" not in source


def test_h2_flush_transfers_output_without_bytearray_copy() -> None:
    """Every flush should not copy the complete output buffer into bytes."""
    source = (_NATIVE / "server_http2.c").read_text()
    flush = _function(source, "h2_flush", "h2_connection_error")
    assert "PyBytes_FromStringAndSize(PyByteArray_AS_STRING(self->out)" not in flush


def test_opt_in_decision_router_uses_raw_segment_keys() -> None:
    """The legacy backend should not create Unicode keys while descending."""
    source = (_NATIVE / "dtrouter.c").read_text()
    match = _function(source, "match_group", "drt_match")
    assert "seg_obj(" not in match


def test_multipart_disposition_is_decoded_in_the_native_pass() -> None:
    """Part metadata should not be reparsed by a Python loop after splitting."""
    source = (_SRC / "_multipart.py").read_text()
    parse = source[source.index("def parse("):source.index("\n\n__all__")]
    assert "_disposition_param(" not in parse
