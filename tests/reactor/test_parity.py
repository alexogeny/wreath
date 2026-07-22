"""Differential parity: the native loop must reproduce asyncio's *observable*
behaviour on a battery of scenarios. This is the executable definition of "it is
really an asyncio loop" — each scenario is run on a throwaway asyncio loop
(the oracle) and on the native loop, and the results must be identical.
"""
from __future__ import annotations

import asyncio

import pytest

from .support import asyncio_reference


async def _timer_ordering(loop):
    order = []
    loop.call_later(0.03, order.append, "c")
    loop.call_later(0.01, order.append, "a")
    loop.call_later(0.02, order.append, "b")
    await asyncio.sleep(0.05)
    return order


async def _gather_ordering(loop):
    async def leaf(n, d):
        await asyncio.sleep(d)
        return n

    return await asyncio.gather(leaf(1, 0.02), leaf(2, 0.0), leaf(3, 0.01))


async def _future_callback_ordering(loop):
    log = []
    fut = loop.create_future()
    fut.add_done_callback(lambda f: log.append("cb"))
    fut.set_result(1)
    log.append("inline")
    await asyncio.sleep(0)
    return log


async def _queue_roundtrip(loop):
    q: asyncio.Queue = asyncio.Queue()

    async def producer():
        for i in range(5):
            await q.put(i)

    loop.create_task(producer())
    return [await q.get() for _ in range(5)]


async def _wait_first_completed(loop):
    async def quick():
        await asyncio.sleep(0.0)
        return "quick"

    async def slow():
        await asyncio.sleep(0.1)
        return "slow"

    done, pending = await asyncio.wait(
        {loop.create_task(quick()), loop.create_task(slow())},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    return sorted(t.result() for t in done)


SCENARIOS = {
    "timer_ordering": _timer_ordering,
    "gather_ordering": _gather_ordering,
    "future_callback_ordering": _future_callback_ordering,
    "queue_roundtrip": _queue_roundtrip,
    "wait_first_completed": _wait_first_completed,
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_native_matches_asyncio(loop, name):
    scenario = SCENARIOS[name]
    expected = asyncio_reference(scenario)
    actual = loop.run_until_complete(scenario(loop))
    assert actual == expected
