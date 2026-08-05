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
from typing import Any

import pytest

from wreath.postgres import Database, PoolConfig


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
