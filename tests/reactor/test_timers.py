from __future__ import annotations

import asyncio

from .support import run


def test_call_later_fires_in_delay_order(loop):
    order = []

    async def main():
        loop.call_later(0.03, order.append, "c")
        loop.call_later(0.01, order.append, "a")
        loop.call_later(0.02, order.append, "b")
        await asyncio.sleep(0.06)
        return list(order)

    assert run(loop, main()) == ["a", "b", "c"]


def test_call_later_cancel(loop):
    fired = []

    async def main():
        h = loop.call_later(0.01, fired.append, 1)
        h.cancel()
        await asyncio.sleep(0.03)
        return list(fired)

    assert run(loop, main()) == []


def test_call_at_uses_loop_clock(loop):
    fired = []

    async def main():
        loop.call_at(loop.time() + 0.01, fired.append, "now")
        await asyncio.sleep(0.03)
        return list(fired)

    assert run(loop, main()) == ["now"]


def test_time_is_monotonic_nondecreasing(loop):
    async def main():
        t0 = loop.time()
        await asyncio.sleep(0.01)
        t1 = loop.time()
        return t1 >= t0 and (t1 - t0) >= 0.005

    assert run(loop, main()) is True


def test_zero_and_negative_delay_run_promptly(loop):
    fired = []

    async def main():
        loop.call_later(-1.0, fired.append, "neg")
        loop.call_later(0.0, fired.append, "zero")
        await asyncio.sleep(0.01)
        return sorted(fired)

    assert run(loop, main()) == ["neg", "zero"]


def test_many_timers_fire_in_order(loop):
    n = 400
    fired = []

    async def main():
        for i in range(n):
            loop.call_later(i * 0.0005, fired.append, i)
        await asyncio.sleep(n * 0.0005 + 0.1)
        return fired

    assert run(loop, main()) == list(range(n))


def test_cancel_one_of_many_timers(loop):
    fired = []

    async def main():
        handles = [loop.call_later(0.01 + i * 0.001, fired.append, i) for i in range(10)]
        handles[5].cancel()
        await asyncio.sleep(0.05)
        return fired

    assert run(loop, main()) == [0, 1, 2, 3, 4, 6, 7, 8, 9]
