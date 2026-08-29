from __future__ import annotations

import asyncio

import pytest

from wreath._native import _server


@pytest.fixture
def continuation():
    """The factory under test: `(coroutine, first_yield) -> continuation`."""
    return _server.StartedCoroutine


async def _drive(continuation, coroutine):
    """Start `coroutine`, then hand the rest of it to the loop as the server does."""
    first = coroutine.send(None)
    return asyncio.ensure_future(continuation(coroutine, first))


async def _finished(task):
    """Await a driven request, bounded.

    Every await here is bounded because the failure this file exists to catch
    is a continuation that never advances -- and an unbounded await turns that
    into a hang, which every tool reports as undecided rather than as red.
    """
    return await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_the_rest_of_the_coroutine_runs_and_its_value_comes_back(continuation) -> None:
    async def work() -> str:
        await asyncio.sleep(0)
        return "finished"

    assert await _finished(await _drive(continuation, work())) == "finished"


@pytest.mark.asyncio
async def test_several_suspensions_all_resume(continuation) -> None:
    seen: list[int] = []

    async def work() -> int:
        for step in range(4):
            await asyncio.sleep(0)
            seen.append(step)
        return len(seen)

    # Bounded: a continuation that re-yields its first value forever instead of
    # advancing hangs rather than fails, and a hang is reported as undecided by
    # every tool that would otherwise catch it.
    assert await _finished(await _drive(continuation, work())) == 4
    assert seen == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_cancelling_the_task_raises_inside_the_coroutine(continuation) -> None:
    caught: list[str] = []

    async def work() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            caught.append("cancelled")
            raise

    task = await _drive(continuation, work())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await _finished(task)
    assert caught == ["cancelled"], "cancellation never reached the handler"


@pytest.mark.asyncio
async def test_a_handler_may_catch_the_cancellation_and_still_answer(continuation) -> None:

    async def work() -> str:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            return "cleaned up"
        return "never"

    task = await _drive(continuation, work())
    await asyncio.sleep(0)
    task.cancel()
    assert await _finished(task) == "cleaned up"


@pytest.mark.asyncio
async def test_an_exception_from_the_awaited_future_lands_in_the_coroutine(continuation) -> None:
    async def work(future: asyncio.Future) -> str:
        try:
            await future
        except ValueError as error:
            return f"caught {error}"
        return "no error"

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    task = await _drive(continuation, work(future))
    future.set_exception(ValueError("boom"))
    assert await _finished(task) == "caught boom"


@pytest.mark.asyncio
async def test_an_exception_the_handler_raises_propagates_out_of_the_task(continuation) -> None:
    async def work() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("handler failed")

    task = await _drive(continuation, work())
    with pytest.raises(RuntimeError, match="handler failed"):
        await _finished(task)


@pytest.mark.asyncio
async def test_a_future_result_is_delivered_to_the_await_that_asked_for_it(continuation) -> None:

    async def work(first: asyncio.Future, second: asyncio.Future) -> tuple:
        return (await first, await second)

    loop = asyncio.get_running_loop()
    first: asyncio.Future = loop.create_future()
    second: asyncio.Future = loop.create_future()
    task = await _drive(continuation, work(first, second))
    first.set_result("one")
    await asyncio.sleep(0)
    second.set_result("two")
    assert await _finished(task) == ("one", "two")


@pytest.mark.asyncio
async def test_closing_the_continuation_closes_the_coroutine_underneath(continuation) -> None:
    closed: list[str] = []

    async def work() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            closed.append("cleanup")

    coroutine = work()
    first = coroutine.send(None)
    adopted = continuation(coroutine, first)
    adopted.close()
    assert closed == ["cleanup"]


@pytest.mark.asyncio
async def test_it_is_a_coroutine_as_far_as_the_loop_is_concerned(continuation) -> None:

    async def work() -> str:
        await asyncio.sleep(0)
        return "ok"

    coroutine = work()
    first = coroutine.send(None)
    adopted = continuation(coroutine, first)
    assert asyncio.iscoroutine(adopted)
    assert await _finished(asyncio.get_running_loop().create_task(adopted)) == "ok"
