"""Sharing a connection between concurrent statements, and what bounds it.

A pool that leases exclusively gives a PostgreSQL backend one query per wakeup.
The driver has always been able to hold several operations in flight on one
connection -- that is what `_waiting` and `_emitted` are for -- but the pool
never let two callers reach the same connection at once, so the capability was
unreachable from `Statement`.

`PoolConfig.pipeline_depth` is what reaches it: up to that many concurrent
operations share a connection, and the driver batches whatever is queued when
it flushes. `pipeline_depth=1` is the old exclusive behaviour exactly.

The bound matters in both directions. Too shallow and nothing batches; too deep
and one connection accumulates a queue that a `max_emitted_operations` flush
cannot drain in one flight, which is latency with no throughput to show for it.

**Explicit acquisition is never shared.** `Database.acquire()` hands out a
connection the caller may run a transaction on, and the driver refuses
concurrent operations once a transaction is open -- so a shared connection
would turn one caller's `BEGIN` into another caller's `InterfaceError`. Only
`Statement`'s single autocommit statements share.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from wreath.postgres import Database, PoolConfig, Statement


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
        "t", "postgresql://u:p@localhost/db",
        pools={"read": PoolConfig(
            min_size=1, max_size=1, max_queue=256, pipeline_depth=depth
        )},
        connector=connect,
    )
    await database.start()
    return database


@pytest.mark.asyncio
async def test_serial_is_still_available_and_still_serial() -> None:
    """`pipeline_depth=1` is the old behaviour, unchanged.

    It is the option, not the default, and it has to keep working exactly:
    one caller on a connection at a time, whatever the concurrency.
    """
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
    """Sharing is capped, so a connection cannot accumulate unbounded work."""
    registry = {"peak": 0}
    database = await _database(3, registry)
    statement = database.statement("q", "SELECT 1")
    try:
        await asyncio.gather(*(statement.fetch() for _ in range(24)))
        assert registry["peak"] <= 3, (
            f"{registry['peak']} operations shared one connection with "
            f"pipeline_depth=3"
        )
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_explicit_acquisition_is_never_shared() -> None:
    """`Database.acquire()` stays exclusive whatever the depth is set to.

    A caller holding one may open a transaction, and the driver refuses
    concurrent operations once it has -- so sharing here would turn one
    caller's `BEGIN` into another caller's failure.
    """
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
    """Sharing must not cross results between callers."""
    registry = {"peak": 0}
    database = await _database(8, registry)
    statements = [database.statement(f"q{i}", f"SELECT {i}") for i in range(12)]
    try:
        results = await asyncio.gather(*(s.fetch() for s in statements))
        assert [r[0]["sql"] for r in results] == [f"SELECT {i}" for i in range(12)]
    finally:
        await database.stop()


# --------------------------------------------------------------------------
# The uncontended lease takes no await points.
#
# Borrowing and returning a connection with no query at all measured 18,732
# instructions a lease -- roughly a tenth of a Fortunes request -- for a deque
# pop, a dict store and their inverses. Seven coroutines were created and
# stepped to do it, and `wreath-decomp --suite calibrate` prices a
# non-suspending await at 95.7ns against 49.8ns for a guarded synchronous call.
#
# So the uncontended case is done in the calling frame. These tests pin what
# that fast path must not change.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_statement_is_refused_once_the_pool_is_stopping() -> None:
    """The fast path must check `_stopping`, not just look for an idle connection.

    A stopped pool still has its connections in `_available` for the length of
    the drain, so a lease that only tests availability would hand one out after
    shutdown began and the drain would then wait for a lease nobody knew about.
    """
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
    """A double release would put one connection in the idle set twice."""
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
    """Telemetry attribution survives the fast path.

    `Database.acquire_shared` is the one seam the Flight Recorder attributes
    pool waiting from. A fast path that skipped it would leave an armed
    request's `db.pool_wait` phase empty, which reads as a pool that never
    waited rather than as a measurement that was not taken.
    """
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
        "t", "postgresql://u:p@localhost/db",
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


def test_statement_query_methods_return_the_work_coroutine_directly() -> None:
    """The public method must not wrap `_call` in an empty async frame."""
    database = Database("t", "postgresql://u:p@localhost/db")
    statement = database.statement("q", "SELECT 1")
    for name in ("execute", "fetch", "fetchrow", "fetchval"):
        awaitable = getattr(statement, name)()
        try:
            assert inspect.iscoroutine(awaitable)
            assert awaitable.cr_code is Statement._call.__code__
        finally:
            awaitable.close()


@pytest.mark.asyncio
async def test_a_shared_lease_still_idles_the_connection_for_an_exclusive_caller() -> None:
    """The last borrower out must return the connection to the idle set.

    A fast release that decremented the share count without idling the
    connection would leave a pool that looks fully borrowed forever, and the
    next explicit `acquire()` would queue against nothing.
    """
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
    """Each case the synchronous path must hand back, stated as its return value.

    A fast path that quietly did the slow path's job would still pass every
    behavioural test in this file -- it would just never be fast. These pin
    which branch runs, which is the only way a mutation of the guard is
    distinguishable from the guard being there.
    """
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
async def test_a_closed_connection_is_not_idled_by_the_fast_release() -> None:
    """A connection that died in use must leave the pool, not go back into it.

    The synchronous release ends in `_available.append`, so a missing check here
    returns a dead connection to the idle set and the next caller gets it. The
    slow path drops it and frees the capacity instead.
    """
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
    """Mid-drain, a returned connection is dropped rather than made available."""
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
