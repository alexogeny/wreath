"""Loop lifecycle: run_until_complete / run_forever / stop / close / is_running.

RED until wreath.reactor.new_event_loop() exists. Oracle: stock asyncio.
"""
from __future__ import annotations

import asyncio

import pytest

from .support import run


def test_run_until_complete_returns_result(loop):
    async def main():
        return 21 * 2

    assert run(loop, main()) == 42


def test_run_until_complete_propagates_exception(loop):
    class Boom(Exception):
        pass

    async def main():
        raise Boom("x")

    with pytest.raises(Boom):
        run(loop, main())


def test_is_running_true_inside_false_outside(loop):
    async def main():
        return loop.is_running()

    assert run(loop, main()) is True
    assert loop.is_running() is False


def test_run_forever_returns_after_stop(loop):
    loop.call_soon(loop.stop)
    loop.run_forever()  # must return promptly, not hang
    assert loop.is_running() is False


def test_run_until_complete_is_not_reentrant(loop):
    async def main():
        with pytest.raises(RuntimeError):
            loop.run_until_complete(asyncio.sleep(0))
        return "ok"

    assert run(loop, main()) == "ok"


def test_close_marks_loop_closed(loop):
    assert loop.is_closed() is False
    loop.close()
    assert loop.is_closed() is True  # double close (fixture) must be tolerated


def test_running_loop_is_discoverable(loop):
    async def main():
        return asyncio.get_running_loop() is loop

    assert run(loop, main()) is True


def test_stop_then_run_resumes_pending_work(loop):
    log = []

    async def main():
        loop.call_soon(log.append, "a")
        loop.call_soon(loop.stop)
        loop.call_soon(log.append, "b")  # scheduled after stop; runs on next run
        return None

    run(loop, main())
    # 'b' was queued before the loop was re-entered; it must not be lost.
    loop.run_until_complete(asyncio.sleep(0))
    assert "a" in log and "b" in log


def test_native_loop_requires_wheel_timers():
    # The C _run_once never compacts cancelled heap TimerHandles (asyncio's
    # compaction lives in the Python _run_once it replaces), so the pairing
    # is refused at construction rather than leaking under wait_for churn.
    import selectors

    from wreath.reactor import EventLoop

    pytest.importorskip("wreath._native._reactor")
    with pytest.raises(ValueError, match="timers='wheel'"):
        EventLoop(selectors.EpollSelector(), native_loop=True, timers="heap")
