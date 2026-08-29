from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from wreath._flight_markers import phase_marker
from wreath._native import extension
from wreath.postgres import Database, Pool, PoolConfig, Statement, _implementation

from .test_connection import FakePostgres

native_postgres = extension("_postgres")


class SlowConnection:
    """A connection that answers, and records how many calls overlap."""

    def __init__(self, registry: dict[str, Any]) -> None:
        self.closed = False
        self._registry = registry
        self.inflight = 0

    async def fetch(self, sql: str, *args: object) -> list[Any]:
        self.inflight += 1
        self._registry["peak"] = max(self._registry["peak"], self.inflight)
        try:
            await asyncio.sleep(0)  # a suspension, so callers can overlap
            return [{"sql": sql}]
        finally:
            self.inflight -= 1

    async def execute(self, sql: str, *args: object) -> str:
        return "OK"

    async def fetchval(self, sql: str, *args: object) -> object:
        return 1

    async def close(self) -> None:
        self.closed = True

    @property
    def prepared_plan_count(self) -> int:
        return 0


async def _database(depth: int, registry: dict[str, Any]) -> Database:
    async def connect(dsn: str) -> Any:
        return SlowConnection(registry)

    database = Database(
        "t",
        "postgresql://u:p@localhost/db",
        pools={"read": PoolConfig(min_size=1, max_size=1, max_queue=256, pipeline_depth=depth)},
        connector=connect,
    )
    await database.start()
    return database


@pytest.mark.asyncio
async def test_serial_is_still_available_and_still_serial() -> None:
    registry = {"peak": 0}
    database = await _database(1, registry)
    statement = database.statement("q", "SELECT 1")
    try:
        await asyncio.gather(*(statement.fetch() for _ in range(8)))
        assert registry["peak"] == 1, (
            f"pipeline_depth=1 let {registry['peak']} operations onto one "
            f"connection at once; it must serialise"
        )
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_batching_lets_concurrent_statements_share_a_connection() -> None:
    registry = {"peak": 0}
    database = await _database(4, registry)
    statement = database.statement("q", "SELECT 1")
    try:
        results = await asyncio.gather(*(statement.fetch() for _ in range(8)))
        assert len(results) == 8
        assert registry["peak"] > 1, (
            "pipeline_depth=4 still ran one operation at a time; nothing batched"
        )
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_the_depth_is_a_bound_and_is_respected() -> None:
    registry = {"peak": 0}
    database = await _database(3, registry)
    statement = database.statement("q", "SELECT 1")
    try:
        await asyncio.gather(*(statement.fetch() for _ in range(24)))
        assert registry["peak"] <= 3, (
            f"{registry['peak']} operations shared one connection with pipeline_depth=3"
        )
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_explicit_acquisition_is_never_shared() -> None:
    registry = {"peak": 0}
    database = await _database(8, registry)
    try:
        first = await database.acquire("read")
        second = asyncio.ensure_future(database.acquire("read"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not second.done(), (
            "a second explicit acquire was served while the first was held; "
            "explicit leases must stay exclusive"
        )
        await database.release("read", first)
        await database.release("read", await second)
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_every_caller_gets_its_own_result() -> None:
    registry = {"peak": 0}
    database = await _database(8, registry)
    statements = [database.statement(f"q{i}", f"SELECT {i}") for i in range(12)]
    try:
        results = await asyncio.gather(*(s.fetch() for s in statements))
        assert [r[0]["sql"] for r in results] == [f"SELECT {i}" for i in range(12)]
    finally:
        await database.stop()


# The uncontended lease takes no await points.
# Borrowing and returning a connection with no query at all measured 18,732
# instructions a lease -- roughly a tenth of a Fortunes request -- for a deque
# pop, a dict store and their inverses. Seven coroutines were created and
# stepped to do it, and `wreath-decomp --suite calibrate` prices a
# non-suspending await at 95.7ns against 49.8ns for a guarded synchronous call.
# So the uncontended case is done in the calling frame. These tests pin what
# that fast path must not change.


@pytest.mark.asyncio
async def test_a_statement_is_refused_once_the_pool_is_stopping() -> None:
    from wreath.postgres import InterfaceError

    registry = {"peak": 0}
    database = await _database(4, registry)
    statement = database.statement("q", "SELECT 1")
    await statement.fetch()
    pool = database._resolve_pool("read")
    # The state a drain is in: still started, connections still idle, but no
    # longer accepting. Reached directly because a drain that stays in it long
    # enough to race is exactly what the fixture cannot arrange.
    pool._stopping = True
    try:
        assert pool._available, "the fixture left no idle connection to be tempted by"
        with pytest.raises(InterfaceError, match="not accepting acquisitions"):
            await pool.acquire(shared=True)
    finally:
        pool._stopping = False
        await database.stop()


@pytest.mark.asyncio
async def test_releasing_a_connection_twice_is_still_refused() -> None:
    from wreath.postgres import InterfaceError

    registry = {"peak": 0}
    database = await _database(4, registry)
    try:
        connection = await database.acquire_shared("read")
        await database.release_shared("read", connection)
        with pytest.raises(InterfaceError, match="not borrowed from this pool"):
            await database.release_shared("read", connection)
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_an_armed_request_still_records_its_pool_wait() -> None:
    from wreath._flight_markers import PH_DB_POOL_WAIT
    from wreath.postgres import _phase_marker

    registry = {"peak": 0}
    database = await _database(4, registry)
    statement = database.statement("q", "SELECT 1")
    assert statement._pool is database._resolve_pool("read"), (
        "the fixture did not exercise the startup-compiled Statement path"
    )
    phases: list[int] = []

    def marker(phase: int, dep: object, coverage: object, elapsed: int) -> None:
        phases.append(phase)

    token = _phase_marker.set(marker)
    try:
        await statement.fetch()
    finally:
        _phase_marker.reset(token)
        await database.stop()

    assert PH_DB_POOL_WAIT in phases, (
        "an armed request recorded no pool-wait phase; the fast path skipped "
        "the seam the recorder attributes from"
    )


@pytest.mark.asyncio
async def test_startup_compiles_a_statement_onto_its_workload_pool() -> None:
    registry = {"peak": 0}

    async def connect(dsn: str) -> Any:
        return SlowConnection(registry)

    database = Database(
        "t",
        "postgresql://u:p@localhost/db",
        pools={"read": PoolConfig(min_size=1, max_size=1, pipeline_depth=4)},
        connector=connect,
    )
    statement = database.statement("q", "SELECT 1")
    assert statement._pool is None
    await database.start()
    try:
        pool = database._resolve_pool("read")
        assert statement._pool is pool
        assert await statement.fetch() == [{"sql": "SELECT 1"}]
    finally:
        await database.stop()


def test_statement_query_methods_return_the_backend_work_awaitable_directly() -> None:
    database = Database("t", "postgresql://u:p@localhost/db")
    statement = database.statement("q", "SELECT 1")
    for name in ("execute", "fetch", "fetch_batch", "fetchrow", "fetchval"):
        awaitable = getattr(statement, name)()
        try:
            if _implementation == "native":
                assert not inspect.iscoroutine(awaitable)
                assert type(awaitable).__name__ == "_StatementAwait"
            else:
                assert inspect.iscoroutine(awaitable)
                assert awaitable.cr_code is Statement._call.__code__
        finally:
            awaitable.close()


@pytest.mark.asyncio
@pytest.mark.skipif(_implementation != "native", reason="native Statement continuation path")
async def test_native_statement_is_its_own_operation_completion_cell() -> None:
    server = FakePostgres(fragment=False)
    dsn = await server.start_tcp()
    database = Database(
        "t",
        dsn,
        pools={"read": PoolConfig(min_size=1, max_size=1, pipeline_depth=4)},
        connector=native_postgres.connect,
    )
    await database.start()
    statement = database.statement("q", "select 1::int4")
    awaitable = statement.fetch()
    iterator = awaitable.__await__()
    completed = asyncio.Event()

    def wake(_completion: object) -> None:
        completed.set()

    try:
        yielded = iterator.send(None)
        assert yielded is awaitable
        assert yielded._asyncio_future_blocking is True
        yielded._asyncio_future_blocking = False
        yielded.add_done_callback(wake)
        await asyncio.wait_for(completed.wait(), timeout=1.0)
        with pytest.raises(StopIteration) as stopped:
            iterator.send(None)
        assert stopped.value.value[0]["value"] == 1
    finally:
        iterator.close()
        await database.stop()
        await server.close()


@pytest.mark.asyncio
@pytest.mark.skipif(_implementation != "native", reason="native Statement continuation path")
async def test_native_statement_completion_propagates_task_cancellation() -> None:
    server = FakePostgres(fragment=False)
    dsn = await server.start_tcp()
    database = Database(
        "t",
        dsn,
        pools={"read": PoolConfig(min_size=1, max_size=1, pipeline_depth=4)},
        connector=native_postgres.connect,
    )
    await database.start()
    statement = database.statement("q", "select 1::int4")
    server.query_gate = asyncio.Event()
    server.flight_received.clear()
    task = asyncio.create_task(statement.fetch())
    try:
        await asyncio.wait_for(server.flight_received.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
        server.query_gate.set()
        assert (await asyncio.wait_for(statement.fetch(), timeout=1.0))[0]["value"] == 1
    finally:
        server.query_gate.set()
        await database.stop()
        await server.close()


@pytest.mark.asyncio
async def test_a_shared_lease_still_idles_the_connection_for_an_exclusive_caller() -> None:
    registry = {"peak": 0}
    database = await _database(4, registry)
    statement = database.statement("q", "SELECT 1")
    try:
        await statement.fetch()
        connection = await asyncio.wait_for(database.acquire("read"), timeout=1.0)
        await database.release("read", connection)
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_the_fast_lease_declines_rather_than_deciding_what_it_cannot() -> None:
    registry = {"peak": 0}
    shared = await _database(4, registry)
    serial = await _database(1, registry)
    try:
        shared_pool = shared._resolve_pool("read")
        serial_pool = serial._resolve_pool("read")

        # Depth 1 leases exclusively: there is no shared lease to take or give.
        assert serial_pool.try_acquire_shared() is None
        connection = await serial.acquire("read")
        assert serial_pool.try_release_shared(connection) is False
        await serial.release("read", connection)

        # Depth above 1, a connection idle: both halves are taken in-frame.
        leased = shared_pool.try_acquire_shared()
        assert leased is not None
        assert shared_pool.try_release_shared(leased) is True

        # Nothing idle, but a shared connection with room: still in-frame, and
        # this is the branch a pool sized to collide spends its life in.
        held = [shared_pool.try_acquire_shared() for _ in range(4)]
        assert all(item is not None for item in held), (
            "the fast path declined a connection that had room to share"
        )
        assert len({id(item) for item in held}) == 1, "the fixture has one connection"

        # Full at the configured depth: only the coroutine can queue for one.
        assert shared_pool.try_acquire_shared() is None
        for item in held:
            assert shared_pool.try_release_shared(item) is True
    finally:
        await shared.stop()
        await serial.stop()


@pytest.mark.asyncio
async def test_native_pool_core_mutates_the_python_owned_shared_state() -> None:
    native_postgres._statement_configure(Statement, Pool, PoolConfig, phase_marker)
    registry = {"peak": 0}
    database = await _database(4, registry)
    try:
        pool = database._resolve_pool("read")

        first = native_postgres._pool_try_acquire(pool)
        second = native_postgres._pool_try_acquire(pool)

        assert first is second
        assert pool._shared[id(first)] == (first, 2)
        assert native_postgres._pool_try_release(pool, second) is True
        assert pool._shared[id(first)] == (first, 1)
        # The test connection is deliberately not the exact native Connection,
        # so the C core leaves the last-borrower policy to Pool.
        assert native_postgres._pool_try_release(pool, first) is False
        assert pool.try_release_shared(first) is True
        assert pool._available == [first]
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_a_closed_connection_is_not_idled_by_the_fast_release() -> None:
    registry = {"peak": 0}
    database = await _database(4, registry)
    try:
        pool = database._resolve_pool("read")
        connection = await database.acquire_shared("read")
        connection.closed = True
        assert pool.try_release_shared(connection) is False, (
            "the fast path idled a closed connection"
        )
        await database.release_shared("read", connection)
        assert connection not in pool._available
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_a_shutting_down_pool_does_not_idle_a_returned_shared_lease() -> None:
    registry = {"peak": 0}
    database = await _database(4, registry)
    try:
        pool = database._resolve_pool("read")
        connection = await database.acquire_shared("read")
        pool._stopping = True
        assert pool.try_release_shared(connection) is False, (
            "the fast path idled a lease returned during shutdown"
        )
    finally:
        pool._stopping = False
        await database.stop()
