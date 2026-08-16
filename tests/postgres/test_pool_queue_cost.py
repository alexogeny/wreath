"""What `Pool.acquire` is allowed to do to the wait queue on the way out.

The queue is `concurrency - max_size` deep under saturation -- roughly 456
entries at the Fortunes board's 512 concurrent connections against a pool of
56 -- and it is walked once per acquisition if the exit path scans it. That is
not a constant factor: it grows with exactly the load the pool exists to
survive, so it is worst precisely when it matters.

`_hand_off`, `_wake_one` and `stop` all `popleft` a waiter *before* resolving
it, so on every successful acquisition the waiter is already gone and
`deque.remove` scans to the end, finds nothing, and raises `ValueError` for the
caller to swallow. Only `_expire` and task cancellation leave a waiter in
place, so only those two need to unqueue.

These tests pin both halves: no scan when the acquisition succeeds, and no
leak when it does not. The second half is what makes the first safe to change.
"""

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
        "t", "postgresql://u:p@localhost/db",
        pools={"read": PoolConfig(
            min_size=1, max_size=1, max_queue=64, acquire_timeout=5.0
        )},
        connector=lambda dsn: _instant(SlowConnection()),
    )
    await database.start()
    return database


async def _instant(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_a_successful_acquire_does_not_walk_the_wait_queue() -> None:
    """The hot path is O(1); the queue is only scanned when the wait failed.

    Queued callers are handed their connection by `_hand_off`, which has
    already removed them. Scanning for them afterwards is pure loss, and it is
    the acquisition path of every request on a saturated pool.
    """
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
    """The path that *does* need unqueueing still unqueues.

    `_expire` resolves the waiter without removing it, so if the exception path
    stopped removing, a timed-out caller would sit in the deque forever and
    `max_queue` would fill with corpses until the pool refused everybody.
    """
    database = Database(
        "t", "postgresql://u:p@localhost/db",
        pools={"read": PoolConfig(
            min_size=1, max_size=1, max_queue=64, acquire_timeout=0.05
        )},
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
    """Cancellation is the other path that leaves its waiter behind.

    Nothing in the pool observes a cancelled `await`, so the waiter is still in
    the deque when `CancelledError` propagates and only the exception path can
    take it out.
    """
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
        assert len(pool._waiters) == 0, (
            "a cancelled waiter was left in the queue"
        )
        await pool.release(held)
    finally:
        await database.stop()


@pytest.mark.asyncio
async def test_reverse_cancellation_never_scans_the_waiter_fifo() -> None:
    """A cancellation storm has one O(1) deletion per caller.

    Newest-first is the hostile order for ``deque.remove``: every deletion
    would walk all older entries and make the aggregate quadratic.
    """
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
