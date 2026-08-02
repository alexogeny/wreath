"""Fused native ingress for the PostgreSQL wire protocol.

The metal transport delivers ingress to native HTTP/1 through the stream
C API capsule with no Python calling convention per read. These tests pin the
PostgreSQL driver as the second implementer of that seam: on a metal loop the
driver's connection must fuse (no ``get_buffer``/``buffer_updated`` object
churn per socket read) while returning byte-identical query results, including
rows spanning provided buffers and slab boundaries.
"""
from __future__ import annotations

import asyncio
import errno
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))  # tests/ for sibling imports

from postgres.test_connection import FakePostgres


def _metal_loop_or_skip():
    import pytest

    reactor = importlib.import_module("wreath.reactor")
    try:
        return reactor.metal_event_loop(diagnostics=True)
    except OSError as exc:
        assert exc.errno in {errno.ENOSYS, errno.EPERM, errno.EACCES, errno.ENOMEM}
        pytest.skip("io_uring unavailable")


def _run_fused(loop, coro, transports=None):
    """Run `coro` while capturing every native transport the loop creates."""
    if transports is None:
        transports = []
    original = loop._make_socket_transport

    def capture(*args, **kwargs):
        transport = original(*args, **kwargs)
        transports.append(transport)
        return transport

    loop._make_socket_transport = capture
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    return result, transports


def test_pg_ingress_fuses_on_metal_loop() -> None:
    native_pg = importlib.import_module("wreath._native._postgres")
    loop = _metal_loop_or_skip()

    async def exercise():
        # The receive-buffer unit corpus separately fragments frames at every
        # byte. Here 20,000 complete DataRow frames are the useful load: they
        # still cross many provided buffers and parser slabs without making
        # the fake server schedule one drain per byte.
        server = FakePostgres(fragment=False)
        dsn = await server.start_tcp()
        try:
            conn = await native_pg.connect(dsn)
            try:
                value = await conn.fetchval("select $1::int4", 42)
                # ~20k rows: crosses many 16 KiB provided buffers and several
                # 64 KiB parser slabs, so messages span both boundaries.
                rows = await conn.fetch("select * from generate_series(1, 20000)")
            finally:
                await conn.close()
            return value, rows
        finally:
            await server.close()

    (value, rows), transports = _run_fused(loop, exercise())

    assert value == 42
    assert len(rows) == 20000
    assert [row[0] for row in rows[:3]] == [1, 2, 3]
    assert rows[-1][0] == 20000

    fused = [
        t for t in transports
        if getattr(t, "_fused_stream", None) == "wreath._native._postgres"
    ]
    assert fused, [getattr(t, "_fused_stream", None) for t in transports]
    assert all(t._fused_http1 is False for t in fused)
    # egress also fused: query dispatch entered through the transport C API
    assert all(t._direct_protocol_writes >= 1 for t in fused)


def test_pg_fusion_survives_abrupt_connection_loss() -> None:
    """connection_lost with fused ingress leaves the driver in a clean error
    state instead of crashing or hanging."""
    native_pg = importlib.import_module("wreath._native._postgres")
    loop = _metal_loop_or_skip()
    transports: list = []

    async def exercise():
        server = FakePostgres()
        dsn = await server.start_tcp()
        try:
            conn = await native_pg.connect(dsn)
            assert await conn.fetchval("select 7") == 7
            # Tear the wire down under the driver: only client-side transports
            # pass through _make_socket_transport, so abort them all.
            for transport in list(transports):
                transport.abort()
            await asyncio.sleep(0)
            with pytest.raises(native_pg.OperationalError, match="connection lost"):
                await asyncio.wait_for(conn.fetchval("select 8"), 0.8)
            await conn.close()
            # Let the fake server's accepted connection observe the abort and
            # finish its owned writer before `_run_fused` closes the loop.
            await asyncio.sleep(0)
        finally:
            await server.close()

    _, captured = _run_fused(loop, exercise(), transports)
    assert any(
        getattr(t, "_fused_stream", None) == "wreath._native._postgres"
        for t in captured
    )


def test_pg_fallback_path_unchanged_on_stock_loop() -> None:
    """On a plain asyncio loop the driver still works through the Python
    BufferedProtocol path (no metal transport, no fusion)."""
    native_pg = importlib.import_module("wreath._native._postgres")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def exercise():
        server = FakePostgres()
        dsn = await server.start_tcp()
        try:
            conn = await native_pg.connect(dsn)
            try:
                return await conn.fetchval("select $1::int4", 42)
            finally:
                await conn.close()
        finally:
            await server.close()

    try:
        assert loop.run_until_complete(exercise()) == 42
    finally:
        asyncio.set_event_loop(None)
        loop.close()
