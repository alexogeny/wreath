from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from .test_connection import FakePostgres

native: Any = None
try:
    native = importlib.import_module("wreath._native._postgres")
except ImportError:
    pass

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(native is None, reason="native PostgreSQL extension not built"),
]


@pytest.fixture
async def database() -> AsyncIterator[tuple[FakePostgres, str]]:
    server = FakePostgres(fragment=True)
    dsn = await server.start_tcp()
    try:
        yield server, dsn
    finally:
        await server.close()


@pytest.mark.parametrize(
    ("method", "sql", "expected"),
    [
        ("execute", "update things set value = 2", "UPDATE 1"),
        ("fetch", "select 41", [41]),
        ("fetch_batch", "select 44", [44]),
        ("fetchrow", "select 42", 42),
    ],
)
async def test_cached_result_modes_complete_from_native_receive_slab(
    database: tuple[FakePostgres, str], method: str, sql: str, expected: object
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)
    try:
        query = getattr(connection, method)
        await query(sql)
        before = connection._reader._receive_stats()
        result = await query(sql)
        after = connection._reader._receive_stats()
    finally:
        await connection.close()

    if method in {"fetch", "fetch_batch"}:
        assert [row[0] for row in result] == expected
    elif method == "fetchrow":
        assert result[0] == expected
    else:
        assert result == expected
    assert after["direct_completions"] - before["direct_completions"] == 1
    assert after["queued_messages"] - before["queued_messages"] == 0


async def test_cached_transaction_control_completes_directly(
    database: tuple[FakePostgres, str],
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)
    try:
        async with connection.transaction():
            assert connection._transaction_status == b"T"
        before = connection._reader._receive_stats()

        async with connection.transaction():
            assert connection._transaction_status == b"T"

        after = connection._reader._receive_stats()
        assert connection._transaction_status == b"I"
    finally:
        await connection.close()

    assert after["direct_completions"] - before["direct_completions"] == 2
    assert after["queued_messages"] - before["queued_messages"] == 0


async def test_direct_completions_preserve_pipeline_order(
    database: tuple[FakePostgres, str],
) -> None:
    server, dsn = database
    connection = await native.connect(dsn)
    try:
        await connection.fetchrow("select 41")
        await connection.execute("update things set value = 2")
        await connection.fetch("select 43")
        before = connection._reader._receive_stats()

        row, command, rows = await asyncio.gather(
            connection.fetchrow("select 41"),
            connection.execute("update things set value = 2"),
            connection.fetch("select 43"),
        )
        after = connection._reader._receive_stats()
    finally:
        await connection.close()

    assert row[0] == 41
    assert command == "UPDATE 1"
    assert [item[0] for item in rows] == [43]
    assert server.executed_sql[-3:] == [
        "select 41",
        "update things set value = 2",
        "select 43",
    ]
    assert after["direct_completions"] - before["direct_completions"] == 3
    assert after["queued_messages"] - before["queued_messages"] == 0


async def test_overridden_finish_operation_keeps_cached_reader_seam(
    database: tuple[FakePostgres, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dsn = database
    connection = await native.connect(dsn)
    try:
        assert await connection.fetchrow("select 42") is not None
        before = connection._reader._receive_stats()

        def explode(*_args: object) -> None:
            raise ValueError("simulated finish failure")

        monkeypatch.setattr(native.Connection, "_finish_operation", explode)
        with pytest.raises(native.InterfaceError, match="simulated finish failure"):
            await connection.fetchrow("select 42")
        after = connection._reader._receive_stats()
    finally:
        monkeypatch.undo()
        await connection.close()

    assert after["direct_completions"] - before["direct_completions"] == 0
    assert after["queued_messages"] - before["queued_messages"] == 1
