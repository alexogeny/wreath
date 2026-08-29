from __future__ import annotations

import asyncio

import pytest

from .support import run


def test_sleep(loop):
    async def main():
        await asyncio.sleep(0.01)
        return "done"

    assert run(loop, main()) == "done"


def test_wait_for_times_out(loop):
    async def main():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.sleep(1), timeout=0.01)
        return "ok"

    assert run(loop, main()) == "ok"


def test_gather(loop):
    async def leaf(n):
        await asyncio.sleep(0.005)
        return n

    async def main():
        return await asyncio.gather(*(leaf(i) for i in range(4)))

    assert run(loop, main()) == [0, 1, 2, 3]


def test_lock_serializes_critical_section(loop):
    order = []

    async def main():
        lock = asyncio.Lock()

        async def worker(n):
            async with lock:
                order.append(("in", n))
                await asyncio.sleep(0.005)
                order.append(("out", n))

        await asyncio.gather(worker(1), worker(2))
        return order

    res = run(loop, main())
    # The two critical sections must not interleave.
    assert res in (
        [("in", 1), ("out", 1), ("in", 2), ("out", 2)],
        [("in", 2), ("out", 2), ("in", 1), ("out", 1)],
    )


def test_event_wait_and_set(loop):
    async def main():
        ev = asyncio.Event()

        async def setter():
            await asyncio.sleep(0.01)
            ev.set()

        loop.create_task(setter())
        await ev.wait()
        return ev.is_set()

    assert run(loop, main()) is True


def test_queue_producer_consumer(loop):
    async def main():
        q: asyncio.Queue = asyncio.Queue()

        async def producer():
            for i in range(3):
                await q.put(i)

        loop.create_task(producer())
        return [await q.get() for _ in range(3)]

    assert run(loop, main()) == [0, 1, 2]


def test_semaphore_bounds_concurrency(loop):
    peak = {"now": 0, "max": 0}

    async def main():
        sem = asyncio.Semaphore(2)

        async def worker():
            async with sem:
                peak["now"] += 1
                peak["max"] = max(peak["max"], peak["now"])
                await asyncio.sleep(0.005)
                peak["now"] -= 1

        await asyncio.gather(*(worker() for _ in range(6)))
        return peak["max"]

    assert run(loop, main()) == 2


def test_run_in_executor(loop):
    async def main():
        return await loop.run_in_executor(None, lambda: 6 * 7)

    assert run(loop, main()) == 42


def test_to_thread(loop):
    async def main():
        return await asyncio.to_thread(lambda: 1 + 1)

    assert run(loop, main()) == 2
