"""The WreathTask fast path — the reactor's reason to exist.

Native-only: these assert behaviour asyncio cannot provide (a task that finishes
on create_task without a loop turn), observed through `loop.reactor_stats()`.
They run on the native `loop` fixture and are RED until the reactor exists.

Contract:
  * A request coroutine that completes without suspending is driven to
    completion *inline* at create_task time — no Task object scheduled, no
    ready-queue round trip. (`inline_completions` ++, `call_soon_scheduled` flat)
  * A coroutine that actually suspends is promoted to a full cancellable driver
    exactly once, and never counts as an inline completion.
  * Promotion preserves cancellation and contextvar isolation.
"""
from __future__ import annotations

import asyncio
import contextvars

import pytest

from .support import run


def test_stats_expose_the_required_counters(loop):
    async def main():
        stats = loop.reactor_stats()
        for key in (
            "inline_completions",
            "fused_resumes",
            "call_soon_scheduled",
            "coro_steps",
            "poll_calls",
            "timers_fired",
            "tasks_promoted",
        ):
            assert key in stats, key
        return True

    assert run(loop, main()) is True


def test_sync_handler_completes_inline_without_scheduling(loop):
    async def sync_coro():
        return 7

    async def main():
        before = loop.reactor_stats()
        task = loop.create_task(sync_coro())
        # Native fast path: a non-suspending coro is already done, with no loop
        # iteration in between and no ready-queue entry spent on it.
        assert task.done() is True
        assert task.result() == 7
        after = loop.reactor_stats()
        assert after["inline_completions"] == before["inline_completions"] + 1
        assert after["call_soon_scheduled"] == before["call_soon_scheduled"]
        return True

    assert run(loop, main()) is True


def test_suspending_handler_is_promoted_not_inlined(loop):
    async def suspends():
        await asyncio.sleep(0.01)
        return 1

    async def main():
        before = loop.reactor_stats()
        task = loop.create_task(suspends())
        assert task.done() is False  # cannot complete inline
        result = await task
        after = loop.reactor_stats()
        assert result == 1
        assert after["tasks_promoted"] == before["tasks_promoted"] + 1
        assert after["inline_completions"] == before["inline_completions"]
        return True

    assert run(loop, main()) is True


def test_promoted_task_is_cancellable(loop):
    async def forever():
        await asyncio.sleep(100)

    async def main():
        task = loop.create_task(forever())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task.cancelled()

    assert run(loop, main()) is True


def test_inline_completion_still_reports_exceptions(loop):
    class Boom(Exception):
        pass

    async def sync_boom():
        raise Boom()

    async def main():
        task = loop.create_task(sync_boom())
        assert task.done() is True
        return isinstance(task.exception(), Boom)

    assert run(loop, main()) is True


def test_contextvar_isolation_across_tasks(loop):
    var: contextvars.ContextVar[str] = contextvars.ContextVar("v", default="base")

    async def worker(val, out):
        var.set(val)
        await asyncio.sleep(0.005)
        out.append(var.get())

    async def main():
        out: list[str] = []
        await asyncio.gather(worker("a", out), worker("b", out))
        assert var.get() == "base"  # child mutations never leak to the parent
        return sorted(out)

    assert run(loop, main()) == ["a", "b"]
