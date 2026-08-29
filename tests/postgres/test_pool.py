from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _pgfidelity import check_for

from wreath.postgres import Database, InterfaceError, PoolConfig


class FakeConnection:
    def __init__(self, label: int) -> None:
        self.label = label
        self.closed = False
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.prepared: list[str] = []

    async def execute(self, sql: str, *args: object) -> str:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return "OK"

    async def fetchrow(self, sql: str, *args: object) -> dict[str, object]:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return {"connection": self.label, "args": args}

    async def fetchval(self, sql: str, *args: object) -> object:
        check_for(self, sql, args)
        self.calls.append((sql, args))
        return args[0] if args else 1

    async def prepare(self, sql: str) -> None:
        self.prepared.append(sql)

    async def close(self) -> None:
        self.closed = True


class Connector:
    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.dsns: list[str] = []

    async def __call__(self, dsn: str) -> FakeConnection:
        self.dsns.append(dsn)
        connection = FakeConnection(len(self.connections))
        self.connections.append(connection)
        return connection


@pytest.mark.asyncio
async def test_workloads_use_isolated_pools_with_one_dsn() -> None:
    connector = Connector()
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={
            "security_read": PoolConfig(min_size=1, max_size=1),
            "read": PoolConfig(min_size=1, max_size=1),
            "write": PoolConfig(min_size=1, max_size=1),
        },
        connector=connector,
    )

    await db.start()
    try:
        assert connector.dsns == ["postgresql://primary/app"] * 3
        assert db.pool("security_read") is not db.pool("read")
        assert db.pool("read") is not db.pool("write")
        assert connector.connections[0].calls == [("SET default_transaction_read_only = on", ())]
        assert connector.connections[1].calls == [("SET default_transaction_read_only = on", ())]
        assert connector.connections[2].calls == []
    finally:
        await db.stop()

    assert all(connection.closed for connection in connector.connections)


@pytest.mark.asyncio
async def test_saturated_read_pool_does_not_starve_security_pool() -> None:
    connector = Connector()
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={
            "security_read": PoolConfig(min_size=1, max_size=1),
            "read": PoolConfig(min_size=1, max_size=1),
        },
        connector=connector,
    )
    await db.start()
    try:
        read = await db.acquire("read")
        security = await asyncio.wait_for(db.acquire("security_read"), timeout=0.05)
        assert security is not read
        await db.release("security_read", security)
        await db.release("read", read)
    finally:
        await db.stop()


@pytest.mark.asyncio
async def test_registered_statement_prepares_and_routes_to_its_workload() -> None:
    connector = Connector()
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={"security_read": PoolConfig(min_size=1, max_size=1)},
        connector=connector,
    )
    statement = db.statement(
        "security.resolve_session",
        "select user_id from sessions where token_hash = $1",
        workload="security_read",
    )

    await db.start()
    try:
        connection = connector.connections[0]
        assert connection.prepared == [statement.sql]
        assert await statement.fetchrow("digest") == {
            "connection": 0,
            "args": ("digest",),
        }
        assert connection.calls[-1] == (statement.sql, ("digest",))
        assert db.pool("security_read").borrowed == 0
    finally:
        await db.stop()


def test_unused_security_pool_is_not_configured_implicitly() -> None:
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={"read": PoolConfig()},
        connector=Connector(),
    )

    with pytest.raises(KeyError, match="security_read"):
        db.pool("security_read")


@pytest.mark.asyncio
async def test_invalid_registered_statement_fails_startup_and_closes_connection() -> None:
    connector = Connector()

    async def reject(sql: str) -> None:
        raise RuntimeError(f"invalid SQL: {sql}")

    original_call = connector.__call__

    async def connect(dsn: str) -> FakeConnection:
        connection = await original_call(dsn)
        connection.prepare = reject  # type: ignore[method-assign]
        return connection

    db = Database(
        "main",
        "postgresql://primary/app",
        pools={"read": PoolConfig(min_size=1)},
        connector=connect,
    )
    db.statement("broken", "not valid sql", workload="read")

    with pytest.raises(RuntimeError, match="invalid SQL"):
        await db.start()

    assert connector.connections[-1].closed
    assert not db.started


def test_the_duplicate_statement_guard_holds_across_threads(monkeypatch: Any) -> None:
    import threading

    import wreath.postgres as pg

    real_init = pg.Statement.__init__
    parked = threading.Event()
    release = threading.Event()

    def parking_init(self: Any, database: Any, name: str, sql: str, workload: Any) -> None:
        real_init(self, database, name, sql, workload)
        if not parked.is_set():
            parked.set()
            release.wait(5.0)

    monkeypatch.setattr(pg.Statement, "__init__", parking_init)

    db = Database("main", "postgresql://primary/app")
    won: list[Any] = []
    refused: list[Exception] = []

    def register(sql: str) -> None:
        try:
            won.append(db.statement("dup", sql))
        except ValueError as exc:
            refused.append(exc)

    first = threading.Thread(target=register, args=("SELECT 1",))
    first.start()
    assert parked.wait(5.0), "the first caller never reached the window"

    second = threading.Thread(target=register, args=("SELECT 2",))
    second.start()
    # The second caller blocks on the lock the first holds while parked, so this
    # join is *expected* to time out -- it only gives it time to reach the window.
    # Both threads are joined for real after the release; joining only `first`
    # asserted on `refused` before the second caller had appended to it, which is
    # rare serially and routine under `pytest -n 6`.
    second.join(0.5)
    release.set()
    first.join(5.0)
    second.join(5.0)
    assert not first.is_alive() and not second.is_alive(), "a caller never returned"

    assert len(won) == 1, "the duplicate guard let both callers through"
    assert len(refused) == 1
    assert "duplicate PostgreSQL statement" in str(refused[0])
    # And the survivor is the one the pools will actually prepare.
    assert db._statements["dup"] is won[0]
    assert db._for_workload("read") == (won[0],)


@pytest.mark.asyncio
async def test_snapshot_reports_the_pool_as_it_stands() -> None:
    connector = Connector()
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={"read": PoolConfig(min_size=1, max_size=2)},
        connector=connector,
    )
    await db.start()
    try:
        pool = db.pool("read")
        idle = pool.snapshot()
        assert (idle.borrowed, idle.waiters, idle.max_size) == (0, 0, 2)
        assert idle.available >= 1

        held = await db.acquire("read")
        busy = pool.snapshot()
        assert busy.borrowed == 1
        assert busy.available == idle.available - 1
        await db.release("read", held)
    finally:
        await db.stop()


@pytest.mark.asyncio
async def test_the_queue_high_water_survives_the_queue_draining() -> None:
    connector = Connector()
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={"read": PoolConfig(min_size=1, max_size=1)},
        connector=connector,
    )
    await db.start()
    try:
        pool = db.pool("read")
        held = await db.acquire("read")
        assert pool.snapshot().queue_high_water == 0

        queued = [asyncio.create_task(db.acquire("read")) for _ in range(2)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert pool.snapshot().waiters == 2, "both callers should be queued"

        await db.release("read", held)
        for task in asyncio.as_completed(queued):
            await db.release("read", await task)

        drained = pool.snapshot()
        assert drained.waiters == 0, "the queue drained"
        assert drained.queue_high_water == 2, "but the watermark remembers"
    finally:
        await db.stop()


#: What `Pool.acquire` can raise: its two documented failures, plus the
#: `RuntimeError` this driver exists to catch -- `asyncio.timeout` outside a
#: Task. Named rather than caught blindly, so a fourth kind of failure escapes
#: to the loop's handler and is seen instead of being re-homed onto a future.
_POOL_RAISES = (InterfaceError, TimeoutError, RuntimeError)


def _drive_taskless(loop: asyncio.AbstractEventLoop, coroutine: Any) -> asyncio.Future:
    """Drive a coroutine to completion without ever creating a Task.

    This is what `app.py`'s `_handle_http_plain` does: the elided-call fast path
    steps the handler's coroutine from a protocol callback rather than handing
    it to `loop.create_task`, which is the point of
    `benchmarks/bench_request_call_elision.py`. `asyncio.current_task()` is
    therefore `None` for everything the handler awaits.

    Wrapping the coroutine in `ensure_future` to test it would create the very
    Task whose absence is the bug -- the first version of this test did exactly
    that and passed against the broken pool.
    """
    done: asyncio.Future = loop.create_future()

    def step(value: object = None, error: BaseException | None = None) -> None:
        try:
            awaited = coroutine.throw(error) if error else coroutine.send(value)
        except StopIteration as stop:
            if not done.done():
                done.set_result(stop.value)
            return
        except _POOL_RAISES as raised:
            if not done.done():
                done.set_exception(raised)
            return

        def resume(finished: asyncio.Future) -> None:
            try:
                step(finished.result())
            except _POOL_RAISES as raised:
                step(error=raised)

        awaited.add_done_callback(resume)

    loop.call_soon(step)
    return done


@pytest.mark.asyncio
async def test_contended_acquire_works_without_an_enclosing_task() -> None:
    connector = Connector()
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={"read": PoolConfig(min_size=1, max_size=1, acquire_timeout=5.0)},
        connector=connector,
    )
    await db.start()
    try:
        pool = db.pool("read")
        held = await pool.acquire()

        loop = asyncio.get_running_loop()
        queued = _drive_taskless(loop, pool.acquire())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not queued.done(), "the pool should be holding this caller"

        await pool.release(held)
        handed = await asyncio.wait_for(queued, timeout=2.0)
        assert handed is held, "the released connection goes to the waiting caller"
        await pool.release(handed)
    finally:
        await db.stop()


@pytest.mark.asyncio
async def test_a_queued_acquire_still_times_out() -> None:
    connector = Connector()
    db = Database(
        "main",
        "postgresql://primary/app",
        pools={"read": PoolConfig(min_size=1, max_size=1, acquire_timeout=0.05)},
        connector=connector,
    )
    await db.start()
    try:
        pool = db.pool("read")
        held = await pool.acquire()
        with pytest.raises(TimeoutError, match="timed out acquiring"):
            await pool.acquire()
        await pool.release(held)
    finally:
        await db.stop()
