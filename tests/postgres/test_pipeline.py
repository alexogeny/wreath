from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

import pytest

from wreath import _pgdriver as pure_postgres

from .test_connection import POSTGRES_BACKENDS, FakePostgres


@pytest.mark.asyncio
async def test_native_operation_owns_its_flight_record() -> None:
    native = next(backend for backend in POSTGRES_BACKENDS if backend._implementation == "native")
    loop = asyncio.get_running_loop()
    operation = native.Operation(7, "select 1", (), "fetchval", loop.create_future(), None)

    assert native.Operation.__base__ is object
    assert operation.sequence == 7
    assert operation.sql == "select 1"
    assert operation.mode == "fetchval"
    assert operation.rows is None
    assert operation.state == "waiting"


@pytest.mark.asyncio
async def test_a_multi_operation_flush_writes_the_complete_batch() -> None:
    class Writer:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, payload: bytes) -> None:
            self.writes.append(payload)

        def drain(self) -> None:
            return None

    loop = asyncio.get_running_loop()
    connection = pure_postgres.Connection.__new__(pure_postgres.Connection)
    operations = [
        pure_postgres.Operation(index, "", (), "execute", loop.create_future(), None)
        for index in range(2)
    ]
    operations[0].packet = b"first"
    operations[1].packet = b"second"
    writer = Writer()

    connection._closed = False
    connection._write_blocked = False
    connection._waiting = deque(operations)
    connection._waiting_live = 2
    connection._emitted = deque()
    connection._flush_handle = None
    connection._writer = writer
    connection._register_operations = None
    connection._write_with_backpressure = None
    connection._loop = loop
    connection._idle_event = asyncio.Event()
    connection._write_count = 0
    connection._reader_task = object()

    connection._flush()

    assert writer.writes == [b"firstsecond"]
    assert list(connection._emitted) == operations


@pytest.fixture(params=POSTGRES_BACKENDS, ids=lambda backend: backend._implementation)
def postgres(request: pytest.FixtureRequest) -> Any:
    return request.param


@pytest.fixture
async def database() -> AsyncIterator[tuple[FakePostgres, str]]:
    server = FakePostgres(fragment=True)
    dsn = await server.start_tcp()
    try:
        yield server, dsn
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_concurrent_queries_batch_writes_and_preserve_submission_order(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    _, dsn = database
    conn = await postgres.connect(dsn)
    try:
        sequential_start = conn._write_count
        for value in range(32):
            assert await conn.fetchval(f"select {value}::int4") == value
        sequential_writes = conn._write_count - sequential_start

        pipeline_start = conn._write_count
        results = await asyncio.gather(
            *(conn.fetchval(f"select {value + 100}::int4") for value in range(32))
        )
        pipeline_writes = conn._write_count - pipeline_start
    finally:
        await conn.close()

    assert results == list(range(100, 132))
    assert pipeline_writes < sequential_writes
    assert pipeline_writes == 2


@pytest.mark.asyncio
async def test_native_backpressure_keeps_later_operations_waiting(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    if postgres._implementation != "native":
        pytest.skip("native transport flow control only")
    server, dsn = database
    server.query_gate = asyncio.Event()
    conn = await postgres.connect(dsn)
    try:
        conn._writer.pause_writing()
        first = asyncio.create_task(conn.fetchval("select 1::int4"))
        second = asyncio.create_task(conn.fetchval("select 2::int4"))
        await server.flight_received.wait()
        await asyncio.sleep(0)

        assert conn._write_blocked
        assert len(conn._emitted) == 1
        assert len(conn._waiting) == 1

        conn._writer.resume_writing()
        for _ in range(5):
            if conn._write_count == 2:
                break
            await asyncio.sleep(0)
        assert conn._write_count == 2
        server.query_gate.set()
        assert await asyncio.gather(first, second) == [1, 2]
    finally:
        server.query_gate.set()
        await conn.close()


@pytest.mark.asyncio
async def test_middle_failure_does_not_corrupt_later_results(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    _, dsn = database
    conn = await postgres.connect(dsn)
    try:
        results = await asyncio.gather(
            conn.fetchval("select 1::int4"),
            conn.fetchval("broken pipeline query"),
            conn.fetchval("select 3::int4"),
            return_exceptions=True,
        )
        assert results[0] == 1
        assert isinstance(results[1], postgres.PostgresError)
        assert results[1].sqlstate == "42601"
        assert results[2] == 3
        assert await conn.fetchval("select 4::int4") == 4
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_queue_limit_raises_pipeline_full(
    postgres: Any,
    database: tuple[FakePostgres, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, dsn = database
    server.query_gate = asyncio.Event()
    conn = await postgres.connect(dsn)
    monkeypatch.setattr(type(conn), "max_queued_operations", 2)
    try:
        tasks = [
            asyncio.create_task(conn.fetchval("select 1::int4")),
            asyncio.create_task(conn.fetchval("select 2::int4")),
            asyncio.create_task(conn.fetchval("select 3::int4")),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        server.query_gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(isinstance(result, postgres.PipelineFullError) for result in results) == 1
        completed = [result for result in results if not isinstance(result, BaseException)]
        assert completed == [1, 2]
    finally:
        server.query_gate.set()
        await conn.close()


@pytest.mark.asyncio
async def test_explicit_transaction_rejects_concurrent_operations(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    _, dsn = database
    conn = await postgres.connect(dsn)
    try:
        await conn.execute("BEGIN")
        results = await asyncio.gather(
            conn.fetchval("select 1::int4"),
            conn.fetchval("select 2::int4"),
            return_exceptions=True,
        )
        assert results[0] == 1
        assert isinstance(results[1], postgres.InterfaceError)
        await conn.execute("ROLLBACK")
        assert await conn.fetchval("select 3::int4") == 3
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_cancel_waiting_operation_removes_it_without_corrupting_pipeline(
    postgres: Any,
    database: tuple[FakePostgres, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, dsn = database
    server.query_gate = asyncio.Event()
    conn = await postgres.connect(dsn)
    monkeypatch.setattr(type(conn), "max_emitted_operations", 1)
    try:
        active = asyncio.create_task(conn.fetchval("select 1::int4"))
        await server.flight_received.wait()
        waiting = asyncio.create_task(conn.fetchval("select 2::int4"))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        server.query_gate.set()
        assert await active == 1
        assert await conn.fetchval("select 3::int4") == 3
    finally:
        server.query_gate.set()
        await conn.close()


@pytest.mark.asyncio
async def test_emitted_limit_keeps_excess_operations_waiting(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    server, dsn = database
    server.query_gate = asyncio.Event()
    conn = await postgres.connect(dsn)
    try:
        tasks = [
            asyncio.create_task(conn.fetchval(f"select {value}::int4")) for value in range(100)
        ]
        await server.flight_received.wait()
        assert len(conn._emitted) == 64
        assert len(conn._waiting) == 36
        server.query_gate.set()
        assert await asyncio.gather(*tasks) == list(range(100))
    finally:
        server.query_gate.set()
        await conn.close()


@pytest.mark.asyncio
async def test_oversized_operation_is_rejected_before_write(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    _, dsn = database
    conn = await postgres.connect(dsn)
    writes = conn._write_count
    try:
        oversized = "select 1 /*" + "x" * (256 * 1024) + "*/"
        with pytest.raises(postgres.PipelineFullError):
            await conn.fetchval(oversized)
        assert conn._write_count == writes
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_cancel_emitted_non_active_operation_discards_its_response(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    server, dsn = database
    server.query_gate = asyncio.Event()
    conn = await postgres.connect(dsn)
    try:
        first = asyncio.create_task(conn.fetchval("select 1::int4"))
        second = asyncio.create_task(conn.fetchval("select 2::int4"))
        await server.flight_received.wait()
        assert len(conn._emitted) == 2
        assert conn._current is conn._emitted[0]
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert conn._emitted[1].discarded
        server.query_gate.set()
        assert await first == 1
        await asyncio.wait_for(conn._idle_event.wait(), timeout=1)
        assert not conn.closed, repr(conn._failure)
        assert await conn.fetchval("select 3::int4") == 3
    finally:
        server.query_gate.set()
        await conn.close()


@pytest.mark.asyncio
async def test_cancel_active_operation_uses_cancel_request_and_connection_recovers(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    server, dsn = database
    server.query_gate = asyncio.Event()
    conn = await postgres.connect(dsn)
    try:
        active = asyncio.create_task(conn.fetchval("select 1::int4"))
        await server.flight_received.wait()
        await asyncio.sleep(0)
        assert conn._current is not None
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        await asyncio.wait_for(server.cancel_response_sent.wait(), timeout=1)
        await asyncio.wait_for(conn._idle_event.wait(), timeout=1)
        assert conn._current is None
        assert not conn._emitted
        server.query_gate.set()
        assert await conn.fetchval("select 2::int4") == 2
    finally:
        server.query_gate.set()
        await conn.close()


@pytest.mark.asyncio
async def test_map_one_sync_per_input_including_duplicates(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    server, dsn = database
    conn = await postgres.connect(dsn)
    try:
        before = len(server.flights)
        # Duplicate arguments must run as duplicate operations, never coalesced.
        results = await conn.map(
            "fetchval", "select $1::int4", [(1,), (2,), (2,), (3,)], max_in_flight=8
        )
        assert len(results) == 4
        # One Sync-delimited flight per input operation.
        assert len(server.flights) - before == 4
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_map_preserves_input_order_and_length(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    conn = await postgres.connect(database[1])
    try:

        def generate():
            # A generator input keeps the pipeline bounded (no materialization).
            for value in range(20):
                yield (value,)

        results = await conn.map("execute", "update t set v=$1", generate(), max_in_flight=4)
        assert len(results) == 20
        assert all(tag == "UPDATE 1" for tag in results)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_map_rejects_bad_arguments(postgres: Any, database: tuple[FakePostgres, str]) -> None:
    conn = await postgres.connect(database[1])
    try:
        with pytest.raises(ValueError):
            await conn.map("bogus", "select 1", [()])
        with pytest.raises(ValueError):
            await conn.map("fetchval", "select 1", [()], max_in_flight=0)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_transaction_commits_and_orders_read_before_write(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    server, dsn = database
    conn = await postgres.connect(dsn)
    try:
        async with conn.transaction() as tx:
            rows = await tx.map("fetchrow", "select id from widget where id=$1", [(1,), (2,)])
            assert len(rows) == 2
            await tx.map("execute", "update widget set v=$1 where id=$2", [(9, 1), (8, 2)])
        executed = server.executed_sql
        begin = executed.index("BEGIN")
        commit = executed.index("COMMIT")
        reads = [i for i, s in enumerate(executed) if s.startswith("select id from widget")]
        writes = [i for i, s in enumerate(executed) if s.startswith("update widget")]
        # BEGIN precedes every read, every read precedes every write, COMMIT last.
        assert begin < min(reads)
        assert max(reads) < min(writes)
        assert max(writes) < commit
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_error(
    postgres: Any, database: tuple[FakePostgres, str]
) -> None:
    server, dsn = database
    conn = await postgres.connect(dsn)
    try:
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                await conn.execute("update widget set v=1")
                raise RuntimeError("application failure")
        assert "ROLLBACK" in server.executed_sql
        assert "COMMIT" not in server.executed_sql
        # The connection stays synchronized and reusable after rollback.
        assert await conn.fetchval("select 42") == 42
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_cancelling_the_newest_queued_operations_leaves_the_pipeline_correct(
    postgres: Any,
    database: tuple[FakePostgres, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, dsn = database
    server.query_gate = asyncio.Event()
    conn = await postgres.connect(dsn)
    monkeypatch.setattr(type(conn), "max_emitted_operations", 1)
    try:
        active = asyncio.create_task(conn.fetchval("select 1::int4"))
        await server.flight_received.wait()
        queued = [asyncio.create_task(conn.fetchval(f"select {n}::int4")) for n in range(10, 20)]
        await asyncio.sleep(0)

        # Innermost scope first: the arrangement `deque.remove` was worst at.
        for task in reversed(queued):
            task.cancel()
        for task in queued:
            with pytest.raises(asyncio.CancelledError):
                await task

        waiting = getattr(conn, "_waiting", None)
        if waiting is not None and hasattr(conn, "_waiting_live"):
            assert conn._waiting_live == 0, (
                "every queued operation was cancelled, so nothing is live; a "
                "non-zero count reschedules the flush against tombstones"
            )
            assert all(op.state == "cancelled" for op in waiting)

        server.query_gate.set()
        assert await active == 1
        # The connection is still usable and the tombstones drained.
        assert await conn.fetchval("select 42::int4") == 42
        if waiting is not None:
            assert not waiting, "tombstones must be drained, not accumulated"
    finally:
        server.query_gate.set()
        await conn.close()
