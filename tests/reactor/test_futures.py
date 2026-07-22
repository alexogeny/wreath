"""loop.create_future: result/exception/cancel and done-callback scheduling.

The done-callback-scheduling test pins the semantic the fused fast path must
still honour for *foreign* futures: callbacks run via the loop's ready queue,
never inline at set_result time.
"""
from __future__ import annotations

import asyncio

import pytest

from .support import run


def test_create_future_result_is_awaitable(loop):
    async def main():
        fut = loop.create_future()
        loop.call_soon(fut.set_result, 99)
        return await fut

    assert run(loop, main()) == 99


def test_create_future_is_bound_to_loop(loop):
    async def main():
        return loop.create_future().get_loop() is loop

    assert run(loop, main()) is True


def test_future_exception_propagates(loop):
    class Boom(Exception):
        pass

    async def main():
        fut = loop.create_future()
        loop.call_soon(fut.set_exception, Boom())
        with pytest.raises(Boom):
            await fut
        return "ok"

    assert run(loop, main()) == "ok"


def test_future_cancel(loop):
    async def main():
        fut = loop.create_future()
        assert fut.cancel() is True
        with pytest.raises(asyncio.CancelledError):
            await fut
        return fut.cancelled()

    assert run(loop, main()) is True


def test_done_callback_runs_via_loop_not_inline(loop):
    log = []

    async def main():
        fut = loop.create_future()
        fut.add_done_callback(lambda f: log.append("cb"))
        fut.set_result(1)
        log.append("after-set")  # inline; the callback must be scheduled after it
        await asyncio.sleep(0)
        return list(log)

    assert run(loop, main()) == ["after-set", "cb"]
