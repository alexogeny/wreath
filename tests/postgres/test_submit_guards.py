from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from .test_connection import POSTGRES_BACKENDS, FakePostgres

#: Every refusal below is expected to raise *before* anything reaches the wire,
#: so a second is several orders of magnitude more than any of them needs.
#:
#: It is here because of how these tests fail when the guard is missing rather
#: than how they pass. A dropped guard does not return the wrong answer -- it
#: accepts the operation, which then waits on a server that has been told to
#: answer nothing, and the test hangs. That was verified by recompiling
#: `_submit` with the two bound checks deleted: without this timeout the run
#: never finished and printed nothing, which is the one failure shape AGENTS.md
#: singles out as indistinguishable from a slow suite. With it, the same
#: deletion fails in a second and names the guard.
REFUSAL_TIMEOUT = 1.0


@contextlib.asynccontextmanager
async def refuses(exception: type[BaseException], match: str) -> Any:
    """Require `exception` promptly, and turn a missing refusal into a failure.

    `asyncio.timeout` is the load-bearing half: `pytest.raises` alone cannot
    fail a body that never returns.
    """
    try:
        with pytest.raises(exception, match=match):
            async with asyncio.timeout(REFUSAL_TIMEOUT):
                yield
    except TimeoutError:
        pytest.fail(
            f"no {exception.__name__} matching {match!r} within "
            f"{REFUSAL_TIMEOUT}s -- the operation was accepted, not refused"
        )


@pytest.fixture
async def gated() -> Any:
    """A connected backend-agnostic factory whose server answers nothing.

    Yields `(open_connection, server)`. The gate is created closed, so the
    first operation reaches the server and parks there; everything submitted
    behind it queues in the driver, which is where these guards live.
    """
    server = FakePostgres(fragment=False)
    dsn = await server.start_tcp()
    server.query_gate = asyncio.Event()
    connections: list[Any] = []

    async def open_connection(backend: Any) -> Any:
        connection = await backend.connect(dsn)
        connections.append(connection)
        return connection

    try:
        yield open_connection, server
    finally:
        server.query_gate.set()
        for connection in connections:
            await connection.close()
        await server.close()


@pytest.mark.parametrize("backend", POSTGRES_BACKENDS)
@pytest.mark.asyncio
async def test_the_pipeline_refuses_work_past_its_queue_bound(
    gated: Any,
    backend: Any,
) -> None:
    open_connection, _server = gated
    connection = await open_connection(backend)
    depth = connection.max_queued_operations

    parked = [
        asyncio.ensure_future(connection.fetchval("select $1::int4", index))
        for index in range(depth)
    ]
    # Let every one of them reach `_submit` and take its queue slot.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    try:
        async with refuses(backend.PipelineFullError, "pipeline is full"):
            await connection.fetchval("select $1::int4", depth)
    finally:
        for task in parked:
            task.cancel()
        await asyncio.gather(*parked, return_exceptions=True)


@pytest.mark.parametrize("backend", POSTGRES_BACKENDS)
@pytest.mark.asyncio
async def test_an_operation_larger_than_the_outbound_batch_is_refused(
    gated: Any,
    backend: Any,
) -> None:
    open_connection, _server = gated
    connection = await open_connection(backend)
    oversized = "select " + ("1" * (connection.max_outbound_batch + 1))

    async with refuses(backend.PipelineFullError, "exceeds maximum outbound batch"):
        await connection.execute(oversized)


@pytest.mark.parametrize("backend", POSTGRES_BACKENDS)
@pytest.mark.asyncio
async def test_a_closed_connection_refuses_new_work(backend: Any) -> None:
    server = FakePostgres(fragment=False)
    dsn = await server.start_tcp()
    try:
        connection = await backend.connect(dsn)
        await connection.close()
        async with refuses(backend.InterfaceError, "connection is closed"):
            await connection.fetchval("select 1")
    finally:
        await server.close()


@pytest.mark.parametrize("backend", POSTGRES_BACKENDS)
@pytest.mark.parametrize("sql", ["", None, 7])
@pytest.mark.asyncio
async def test_sql_must_be_a_non_empty_string(backend: Any, sql: Any) -> None:
    server = FakePostgres(fragment=False)
    dsn = await server.start_tcp()
    try:
        connection = await backend.connect(dsn)
        try:
            async with refuses(backend.InterfaceError, "non-empty string"):
                await connection.execute(sql)
        finally:
            await connection.close()
    finally:
        await server.close()


@pytest.mark.parametrize("backend", POSTGRES_BACKENDS)
@pytest.mark.asyncio
async def test_an_open_transaction_refuses_a_concurrent_operation(
    backend: Any,
) -> None:
    server = FakePostgres(fragment=False)
    dsn = await server.start_tcp()
    server.query_gate = asyncio.Event()
    try:
        connection = await backend.connect(dsn)
        server.query_gate.set()
        await connection.execute("BEGIN")
        server.query_gate.clear()

        parked = asyncio.ensure_future(connection.fetchval("select $1::int4", 1))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        async with refuses(backend.InterfaceError, "explicit transactions reject concurrent"):
            await connection.fetchval("select $1::int4", 2)

        parked.cancel()
        await asyncio.gather(parked, return_exceptions=True)
        server.query_gate.set()
        await connection.close()
    finally:
        await server.close()


@pytest.mark.parametrize("backend", POSTGRES_BACKENDS)
@pytest.mark.asyncio
async def test_transaction_control_cannot_enter_an_active_pipeline(
    gated: Any,
    backend: Any,
) -> None:
    open_connection, _server = gated
    connection = await open_connection(backend)

    parked = asyncio.ensure_future(connection.fetchval("select $1::int4", 1))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    async with refuses(
        backend.InterfaceError, "transaction control cannot enter an active pipeline"
    ):
        await connection.execute("BEGIN")

    parked.cancel()
    await asyncio.gather(parked, return_exceptions=True)


# `FakePostgres` ignores bound parameters and answers `select <n>` with `n`,
# so distinct literals -- not distinct `$1` bindings -- are what make a result
# traceable back to the operation that asked for it.
@pytest.mark.parametrize("backend", POSTGRES_BACKENDS)
@pytest.mark.asyncio
async def test_pipelined_results_arrive_in_submission_order(backend: Any) -> None:
    server = FakePostgres(fragment=False)
    dsn = await server.start_tcp()
    try:
        connection = await backend.connect(dsn)
        try:
            results = await asyncio.gather(
                *(connection.fetchval(f"select {index}") for index in range(16))
            )
            assert results == list(range(16))
        finally:
            await connection.close()
    finally:
        await server.close()


@pytest.mark.parametrize("backend", POSTGRES_BACKENDS)
@pytest.mark.asyncio
async def test_one_failing_operation_fails_only_its_own_caller(backend: Any) -> None:
    server = FakePostgres(fragment=False)
    dsn = await server.start_tcp()
    try:
        connection = await backend.connect(dsn)
        try:
            good_before = asyncio.ensure_future(connection.fetchval("select 11"))
            bad = asyncio.ensure_future(connection.execute("broken statement"))
            good_after = asyncio.ensure_future(connection.fetchval("select 22"))

            assert await good_before == 11
            with pytest.raises(backend.PostgresError):
                await bad
            assert await good_after == 22
        finally:
            await connection.close()
    finally:
        await server.close()


@pytest.mark.parametrize("backend", POSTGRES_BACKENDS)
@pytest.mark.asyncio
async def test_a_cancelled_queued_operation_does_not_strand_the_ones_behind_it(
    gated: Any,
    backend: Any,
) -> None:
    open_connection, server = gated
    connection = await open_connection(backend)
    depth = connection.max_emitted_operations + 6

    tasks = [
        asyncio.ensure_future(connection.fetchval(f"select {index}")) for index in range(depth)
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    doomed = tasks[depth - 2]
    doomed.cancel()
    server.query_gate.set()

    assert await tasks[0] == 0
    with pytest.raises(asyncio.CancelledError):
        await doomed
    assert await tasks[depth - 1] == depth - 1
