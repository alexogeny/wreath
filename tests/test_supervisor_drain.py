"""A drain that fails is counted, not silently absorbed.

The supervisor cannot let one service's `drain` abort its siblings' shutdown --
so the catch around it is broad on purpose. What makes that the exceptional
minority rather than the rule is `drain_errors`: without it, `stop()` returned
having quiesced nothing and reported exactly the same as a clean shutdown.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from wreath.services import Supervisor


class _Service:
    """A service whose `drain` fails, or whose task dies of its own accord."""

    def __init__(self, *, drain_raises: BaseException | None = None) -> None:
        self.drain_raises = drain_raises
        self.drained = False

    async def start(self, supervisor: Any) -> None:
        return None

    async def drain(self, deadline: float) -> None:
        self.drained = True
        if self.drain_raises is not None:
            raise self.drain_raises


@pytest.mark.asyncio
async def test_a_failed_drain_is_counted_and_does_not_stop_its_siblings() -> None:
    broken = _Service(drain_raises=RuntimeError("the queue would not quiesce"))
    healthy = _Service()
    supervisor = Supervisor(drain_timeout=0.1)
    supervisor.add(broken)
    supervisor.add(healthy)

    await supervisor.start()
    await supervisor.stop()

    # The sibling still drained -- one failure must not abort the shutdown.
    assert healthy.drained
    # And the failure is visible. Before `drain_errors`, this read zero and a
    # shutdown that quiesced nothing was indistinguishable from a clean one.
    assert supervisor.drain_errors == 1


@pytest.mark.asyncio
async def test_a_clean_shutdown_counts_nothing() -> None:
    supervisor = Supervisor(drain_timeout=0.1)
    supervisor.add(_Service())

    await supervisor.start()
    await supervisor.stop()

    assert supervisor.drain_errors == 0


@pytest.mark.asyncio
async def test_a_service_task_that_dies_on_its_way_out_is_counted() -> None:
    """`_cancel_all`'s `await task` is the only place that exception is ever seen."""
    supervisor = Supervisor(drain_timeout=0.1)
    supervisor.add(_Service())
    await supervisor.start()

    async def dies() -> None:
        raise RuntimeError("worker died holding something")

    supervisor.spawn("doomed", dies())
    await asyncio.sleep(0)  # let it run and fail

    await supervisor.stop()
    assert supervisor.drain_errors == 1


@pytest.mark.asyncio
async def test_a_task_we_cancelled_ourselves_is_not_an_error() -> None:
    """Reaping our own cancellation is expected, and must not inflate the count."""
    supervisor = Supervisor(drain_timeout=0.1)
    supervisor.add(_Service())
    await supervisor.start()

    async def forever() -> None:
        await asyncio.sleep(3600)

    supervisor.spawn("long-lived", forever())
    await asyncio.sleep(0)

    await supervisor.stop()
    assert supervisor.drain_errors == 0
