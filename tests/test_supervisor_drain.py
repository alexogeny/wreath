from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from wreath.services import Supervisor


@pytest.mark.parametrize("drain_timeout", [float("nan"), float("inf")])
def test_supervisor_drain_timeout_must_be_finite(drain_timeout: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        Supervisor(drain_timeout=drain_timeout)


@pytest.mark.parametrize("drain_timeout", [True, False, 0, -1, "1"])
def test_supervisor_drain_timeout_must_be_a_positive_number(drain_timeout: object) -> None:
    with pytest.raises(ValueError, match="positive finite number"):
        Supervisor(drain_timeout=cast(float, drain_timeout))


@pytest.mark.asyncio
async def test_supervisor_refuses_tasks_while_it_is_inactive() -> None:
    supervisor = Supervisor()
    before_start = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="only while it is running"):
        supervisor.spawn("too-early", before_start)
    before_start.close()

    supervisor.add(_Service())
    await supervisor.start()
    await supervisor.stop()

    after_stop = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="only while it is running"):
        supervisor.spawn("too-late", after_stop)
    after_stop.close()


@pytest.mark.asyncio
async def test_service_can_spawn_during_start_but_not_during_drain() -> None:
    class SpawningService:
        def __init__(self) -> None:
            self.supervisor: Supervisor | None = None
            self.drain_refused = False

        async def start(self, supervisor: Supervisor) -> None:
            self.supervisor = supervisor
            supervisor.spawn("started-worker", asyncio.sleep(3600))

        async def drain(self, deadline: float) -> None:
            assert self.supervisor is not None
            during_drain = asyncio.sleep(0)
            try:
                self.supervisor.spawn("too-late", during_drain)
            except RuntimeError:
                self.drain_refused = True
            finally:
                during_drain.close()

    service = SpawningService()
    supervisor = Supervisor()
    supervisor.add(service)
    await supervisor.start()
    await supervisor.stop()

    assert service.drain_refused


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
async def test_a_completed_failed_task_remains_owned_until_shutdown() -> None:
    supervisor = Supervisor(drain_timeout=0.1)
    supervisor.add(_Service())
    await supervisor.start()

    async def dies() -> None:
        raise RuntimeError("worker failed before shutdown")

    supervisor.spawn("already-doomed", dies())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await supervisor.stop()
    assert supervisor.drain_errors == 1


@pytest.mark.asyncio
async def test_a_task_we_cancelled_ourselves_is_not_an_error() -> None:
    supervisor = Supervisor(drain_timeout=0.1)
    supervisor.add(_Service())
    await supervisor.start()

    async def forever() -> None:
        await asyncio.sleep(3600)

    supervisor.spawn("long-lived", forever())
    await asyncio.sleep(0)

    await supervisor.stop()
    assert supervisor.drain_errors == 0
