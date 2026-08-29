from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from wreath._snapshot import SnapshotCache
from wreath.orm.registry import Registry
from wreath.postgres import PoolConfig
from wreath.server import ServerConfig
from wreath.telemetry import _MAX_CAPTURE_BYTES

_ROOT = Path(__file__).parents[1]
_SRC = _ROOT / "src" / "wreath"
_NATIVE = _SRC / "_native"


def _function(source: str, name: str, next_name: str) -> str:
    start = source.index(f"\n{name}(")
    end = source.index(f"\n{next_name}(", start)
    return source[start:end]


def test_snapshot_cache_is_bounded_by_default() -> None:
    cache: SnapshotCache[str, object] = SnapshotCache()
    assert cache._max_entries is not None


def test_snapshot_cache_accepts_a_byte_budget() -> None:
    assert "max_bytes" in inspect.signature(SnapshotCache).parameters


def test_default_multiplexed_body_budget_is_connection_bounded() -> None:
    config = ServerConfig(protocols=("h2",))
    aggregate = config.max_concurrent_streams * config.max_body_bytes
    assert aggregate <= 64 * 1024 * 1024


def test_h2_flow_credit_waits_for_application_consumption() -> None:
    source = (_NATIVE / "server_http2.c").read_text()
    delivery = _function(source, "deliver_body", "process_settings")
    assert "FRAME_WINDOW_UPDATE" not in delivery


def test_h3_request_queue_has_connection_wide_backpressure() -> None:
    source = (_NATIVE / "http3_asgi.c").read_text()
    receive = _function(source, "recv_data_cb", "end_stream_cb")
    assert "read_high_water" in receive
    assert "queued_body_bytes" in receive


def test_request_body_uses_one_bounded_contiguous_owner() -> None:
    source = (_SRC / "request.py").read_text()
    body = source[source.index("    async def body(") : source.index("    async def json(")]
    assert "chunks: list[bytes]" not in body
    assert 'b"".join(chunks)' not in body


def test_public_multipart_parse_consumes_parts_incrementally() -> None:
    source = (_SRC / "_multipart.py").read_text()
    parse = source[source.index("def parse(") : source.index("\n\n__all__")]
    assert "parts = []" not in parse
    assert "bytes(data)" not in parse


@pytest.mark.parametrize("filename", ["server_http1.c", "server_http2.c"])
def test_idle_connection_receive_floor_is_at_most_32k(filename: str) -> None:
    source = (_NATIVE / filename).read_text()
    match = re.search(r"const Py_ssize_t retained = (\d+);", source)
    assert match is not None
    assert int(match.group(1)) <= 32 * 1024


def test_compiled_bitset_releases_build_only_route_storage() -> None:
    source = (_NATIVE / "policy_router.c").read_text()
    compile_groups = _function(source, "brt_compile_groups", "brt_compile")
    assert "PyMem_Free(self->routes)" in compile_groups
    assert "self->routes = NULL" in compile_groups


def test_postgres_idle_connections_trim_spare_slabs() -> None:
    source = (_NATIVE / "postgres" / "protocol.c").read_text()
    assert "trim_idle_spares" in source


@pytest.mark.parametrize(
    ("owner", "parameter"),
    [
        (Registry, "query_cache_bytes"),
        (PoolConfig, "statement_cache_bytes"),
    ],
)
def test_plan_caches_accept_byte_budgets(owner: type[object], parameter: str) -> None:
    assert parameter in inspect.signature(owner).parameters


def test_forensic_capture_ceiling_is_at_most_one_gibibyte() -> None:
    assert _MAX_CAPTURE_BYTES <= 1 << 30
