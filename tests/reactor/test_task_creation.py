"""What `create_task` must keep doing while it stops going the long way round.

Every request that waits on anything gets one task, and on the metal tier that
task is CPython's C `Task` -- `metal_event_loop` chooses `tasks="auto"`
deliberately, because the Python inline-drive `WreathTask` measured as a loss.
Reaching that C constructor through `BaseEventLoop.create_task` costs 5,005
instructions a task on top of the constructor itself: two Python frames, a
`_check_closed`, a `**kwargs` dict built and unpacked, and a debug-only
traceback trim.

Constructing it directly is only safe while everything that wrapper does is
either preserved or provably absent, so each of those is pinned here rather
than assumed. `asyncio` is the reference: where a behaviour is observable, the
stock loop is asserted alongside.
"""

from __future__ import annotations

import asyncio

import pytest

from .support import run


async def _answer() -> str:
    await asyncio.sleep(0)
    return "ok"


def test_a_task_still_runs_and_returns_its_value(loop) -> None:
    async def main() -> str:
        return await loop.create_task(_answer())

    assert run(loop, main()) == "ok"


def test_a_task_factory_is_still_consulted(loop) -> None:
    """`set_task_factory` is a documented hook and the fast path must yield to it.

    A factory exists to replace the task type -- for tracing, for a context
    policy, for a subclass carrying request identity. Constructing the stock
    Task directly would leave the hook installed and silently unused, which
    looks like the factory being wrong rather than being skipped.
    """
    built: list[object] = []

    class Tracked(asyncio.Task):
        pass

    def factory(target_loop, coro, **kwargs):
        task = Tracked(coro, loop=target_loop, **kwargs)
        built.append(task)
        return task

    loop.set_task_factory(factory)
    try:
        made: list[object] = []

        async def main() -> str:
            task = loop.create_task(_answer())
            made.append(task)
            return await task

        assert run(loop, main()) == "ok"
        # The count is not asserted: `run_until_complete` wraps `main` itself,
        # so the factory legitimately builds more than the one task under test.
        assert built, "the task factory was installed and never called"
        assert isinstance(made[0], Tracked), (
            f"create_task returned {type(made[0]).__name__}, bypassing the factory"
        )
    finally:
        loop.set_task_factory(None)


def test_a_name_and_a_context_still_reach_the_task(loop) -> None:
    """Both are keyword arguments of the wrapper being skipped."""
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker")
    context = contextvars.copy_context()
    context.run(marker.set, "carried")

    async def read() -> str:
        return marker.get("missing")

    async def main() -> tuple[str, str]:
        task = loop.create_task(read(), name="named-task", context=context)
        return task.get_name(), await task

    assert run(loop, main()) == ("named-task", "carried")


def test_the_task_is_visible_to_all_tasks(loop) -> None:
    """`asyncio.all_tasks` is how a supervisor and a shutdown find work in flight."""
    seen: list[int] = []

    async def observe() -> None:
        await asyncio.sleep(0)
        seen.append(len(asyncio.all_tasks(loop)))

    async def main() -> None:
        await loop.create_task(observe())

    run(loop, main())
    assert seen and seen[0] >= 1, "a created task was invisible to all_tasks"


def test_cancelling_the_task_reaches_the_coroutine(loop) -> None:
    caught: list[str] = []

    async def slow() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            caught.append("cancelled")
            raise

    async def main() -> None:
        task = loop.create_task(slow())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(loop, main())
    assert caught == ["cancelled"]


def test_a_closed_loop_refuses_to_create_a_task(loop) -> None:
    """Refused, and as `RuntimeError`, exactly as the base class refuses it.

    This does not pin *where* the refusal comes from. Dropping the explicit
    check still refuses -- the Task constructor's own `call_soon` raises the
    same error -- but only after building a Task that then reports "Task was
    destroyed but it is pending!" when it is collected. That difference is real
    and is why the check is kept; it is not asserted here because the
    `pytest.raises` traceback holds the half-built task alive past collection,
    so a test claiming to observe it would pass either way.
    """
    coro = _answer()
    loop.close()
    try:
        with pytest.raises(RuntimeError, match="closed"):
            loop.create_task(coro)
    finally:
        coro.close()


def test_an_exception_still_propagates_out_of_the_task(loop) -> None:
    async def boom() -> None:
        await asyncio.sleep(0)
        raise ValueError("from the task")

    async def main() -> None:
        with pytest.raises(ValueError, match="from the task"):
            await loop.create_task(boom())

    run(loop, main())
