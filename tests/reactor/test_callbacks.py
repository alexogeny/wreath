"""call_soon / call_soon_threadsafe ordering, handles, cancellation.

RED until the native reactor exists. Oracle: stock asyncio.
"""
from __future__ import annotations

import asyncio
import threading

from .support import run


def test_call_soon_runs_in_fifo_order(loop):
    order = []

    async def main():
        for i in range(6):
            loop.call_soon(order.append, i)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return list(order)

    assert run(loop, main()) == [0, 1, 2, 3, 4, 5]


def test_call_soon_passes_arguments(loop):
    seen = []

    async def main():
        loop.call_soon(seen.append, ("a", 1))
        await asyncio.sleep(0)
        return list(seen)

    assert run(loop, main()) == [("a", 1)]


def test_call_soon_handle_cancel_prevents_call(loop):
    ran = []

    async def main():
        h = loop.call_soon(ran.append, 1)
        h.cancel()
        await asyncio.sleep(0)
        return list(ran)

    assert run(loop, main()) == []


def test_callback_may_schedule_another_callback(loop):
    order = []

    async def main():
        def first():
            order.append("first")
            loop.call_soon(order.append, "second")

        loop.call_soon(first)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return list(order)

    assert run(loop, main()) == ["first", "second"]


def test_call_soon_threadsafe_wakes_a_blocked_loop(loop):
    got = []

    async def main():
        def from_thread():
            loop.call_soon_threadsafe(got.append, "x")

        t = threading.Thread(target=from_thread)
        t.start()
        for _ in range(1000):
            if got:
                break
            await asyncio.sleep(0.001)
        t.join()
        return list(got)

    assert run(loop, main()) == ["x"]
