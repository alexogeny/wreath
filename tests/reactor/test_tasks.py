"""create_task, gather, cancellation, current_task/all_tasks — generic Task
semantics the native driver must reproduce for the ordinary (suspending) path.

The native fast path (inline-drive, fusion) is specified separately in
test_wreath_task.py; here everything must behave exactly like asyncio.
"""
from __future__ import annotations

import asyncio

import pytest

from .support import run


def test_create_task_runs_coroutine(loop):
    async def leaf():
        return "ran"

    async def main():
        return await loop.create_task(leaf())

    assert run(loop, main()) == "ran"


def test_gather_preserves_argument_order(loop):
    async def leaf(n, delay):
        await asyncio.sleep(delay)
        return n

    async def main():
        return await asyncio.gather(leaf(1, 0.02), leaf(2, 0.01), leaf(3, 0.0))

    assert run(loop, main()) == [1, 2, 3]


def test_task_cancellation(loop):
    async def forever():
        await asyncio.sleep(100)

    async def main():
        t = loop.create_task(forever())
        await asyncio.sleep(0)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        return t.cancelled()

    assert run(loop, main()) is True


def test_task_exception_is_retrievable(loop):
    class Boom(Exception):
        pass

    async def boom():
        raise Boom()

    async def main():
        t = loop.create_task(boom())
        try:
            await t
        except Boom:
            pass
        return isinstance(t.exception(), Boom)

    assert run(loop, main()) is True


def test_current_task_and_all_tasks(loop):
    async def main():
        here = asyncio.current_task()
        alive = asyncio.all_tasks(loop)
        return here is not None and here in alive

    assert run(loop, main()) is True


def test_taskgroup_cancels_siblings_on_error(loop):
    """TaskGroup (unlike gather) cancels siblings when one child fails — a deep
    exercise of the driver's cancellation path."""
    cancelled = []

    async def boom():
        raise ValueError("x")

    async def slow():
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def main():
        with pytest.raises(BaseExceptionGroup):
            async with asyncio.TaskGroup() as tg:
                tg.create_task(boom())
                tg.create_task(slow())
        return cancelled

    assert run(loop, main()) == [True]
