from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

import pytest

from wreath.postgres import Database, PoolConfig


class CountingDeque(deque):
    """A `_waiters` that reports how often the exit path walked it.

    `remove` is the O(n) call; counting it is the whole measurement, and it is
    exact rather than timed, so this test says nothing about how fast the
    machine is.
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.removes = 0

    def remove(self, value: Any) -> None:
        self.removes += 1
        super().remove(value)


class SlowConnection:
    """Enough of a connection for the pool; every query is instant."""

    def __init__(self) -> None:
        self.closed = False

    async def execute(self, sql: str, *args: object) -> str:
        return "OK"

    async def fetchval(self, sql: str, *args: object) -> object:
        return 1

    async def close(self) -> None:
        self.closed = True

    @property
    def prepared_plan_count(self) -> int:
        return 0


async def _pool_of_one() -> Any:
    """A started pool with exactly one connection, so callers must queue."""
    database = Database(
        "t",
        "postgresql://u:p@localhost/db",
        pools={"read": PoolConfig(min_size=1, max_size=1, max_queue=64, acquire_timeout=5.0)},
        connector=lambda dsn: _instant(SlowConnection()),
    )
    await database.start()
    return database


async def _instant(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_a_successful_acquire_does_not_walk_the_wait_queue() -> None:
    database = await _pool_of_one()
    pool = database.pool("read")
    counting = CountingDeque(pool._waiters)
    pool._waiters = counting
    try:
        held = await pool.acquire()

        # Three callers queue behind the single connection.
        queued = [asyncio.ensure_future(pool.acquire()) for _ in range(3)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(counting) == 3, "the three callers should be queued"

        # Release once per queued caller; each is handed off directly.
        for _ in range(3):
            await pool.release(held)
            held = await queued.pop(0)

        assert counting.removes == 0, (
            f"acquire scanned the wait queue {counting.removes} time(s) on the "
            f"success path; every one of those is O(queue depth)"
        )
        await pool.release(held)
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_a_timed_out_acquire_leaves_nothing_in_the_queue() -> None:
    database = Database(
        "t",
        "postgresql://u:p@localhost/db",
        pools={"read": PoolConfig(min_size=1, max_size=1, max_queue=64, acquire_timeout=0.05)},
        connector=lambda dsn: _instant(SlowConnection()),
    )
    await database.start()
    pool = database.pool("read")
    try:
        held = await pool.acquire()
        with pytest.raises(TimeoutError):
            await pool.acquire()
        assert len(pool._waiters) == 0, (
            "a timed-out waiter was left in the queue; max_queue would fill "
            "with callers that are never coming back"
        )
        await pool.release(held)
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_a_cancelled_acquire_leaves_nothing_in_the_queue() -> None:
    database = await _pool_of_one()
    pool = database.pool("read")
    try:
        held = await pool.acquire()
        queued = asyncio.ensure_future(pool.acquire())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(pool._waiters) == 1

        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert len(pool._waiters) == 0, "a cancelled waiter was left in the queue"
        await pool.release(held)
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_reverse_cancellation_never_scans_the_waiter_fifo() -> None:
    database = await _pool_of_one()
    pool = database.pool("read")
    counting = CountingDeque(pool._waiters)
    pool._waiters = counting
    try:
        held = await pool.acquire()
        queued = [asyncio.ensure_future(pool.acquire()) for _ in range(32)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(pool._active_waiters) == len(queued)

        for task in reversed(queued):
            task.cancel()
        await asyncio.gather(*queued, return_exceptions=True)

        assert counting.removes == 0
        assert not pool._active_waiters
        assert not counting
        await pool.release(held)
    finally:
        await database.stop()
