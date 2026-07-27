"""A minimal supervisor for process-lifetime background services.

The supervisor owns the long-lived asyncio tasks that the durable-jobs and
messaging coordinators run (workers, durable-subscription consumers, and the
lease sweeper). It is started during the application lifespan *after* databases
come up and *before* user startup handlers, and stopped in reverse: stop
fetching new work, drain bounded in-flight handlers, then cancel anything that
outlives the grace period.

Design 01 §3 (lifespan) is the contract implemented here. A "service" is any
object exposing ``async start(supervisor)`` and ``async drain(deadline)``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, Protocol


class Service(Protocol):
    """A process-lifetime background service managed by the supervisor."""

    async def start(self, supervisor: Supervisor) -> None: ...

    async def drain(self, deadline: float) -> None: ...


class Supervisor:
    """Owns and lifecycles the background tasks of registered services."""

    __slots__ = (
        "_services", "_tasks", "_stopping", "_started", "drain_timeout", "drain_errors",
    )

    def __init__(self, *, drain_timeout: float = 10.0) -> None:
        self._services: list[Service] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stopping = asyncio.Event()
        self._started = False
        self.drain_timeout = drain_timeout
        #: Services whose `drain` raised. A drain failure cannot be allowed to
        #: abort the shutdown of its siblings, but a shutdown that reports
        #: success while a service failed to quiesce is the silent degradation
        #: this codebase keeps finding. Counting it is what makes the
        #: difference legible without changing the shutdown contract.
        self.drain_errors = 0

    def add(self, service: Service) -> None:
        if self._started:
            raise RuntimeError("cannot register a service after the supervisor started")
        self._services.append(service)

    @property
    def stopping(self) -> asyncio.Event:
        """Set once shutdown begins; loops should stop fetching when it is set."""
        return self._stopping

    def is_stopping(self) -> bool:
        return self._stopping.is_set()

    @property
    def empty(self) -> bool:
        return not self._services

    def spawn(self, name: str, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Own a long-lived task; it is cancelled on stop if it outlives drain."""
        task = asyncio.ensure_future(coro)
        # `ensure_future` over a coroutine always yields a Task, and `Task.set_name`
        # has existed since 3.8 -- on a 3.14-only codebase there is nothing here
        # that can raise, so the guard this used to carry was suppressing nothing.
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self) -> None:
        if self._started:
            return
        self._stopping.clear()
        started: list[Service] = []
        try:
            for service in self._services:
                await service.start(self)
                started.append(service)
        except BaseException:  # noqa: BLE001 -- re-raised below; see the comment
            # Broad *and* re-raised: a partial start must be rolled back however
            # it failed, including when the failure is a `CancelledError` that
            # `except Exception` would miss -- leaving half the services running
            # and the supervisor believing it never started. Nothing is
            # swallowed; the original propagates once cleanup is done.
            # Roll back a partial start the same way we shut down: signal, drain
            # what came up, then cancel everything.
            self._stopping.set()
            deadline = asyncio.get_running_loop().time() + self.drain_timeout
            for service in reversed(started):
                await self._drain_quietly(service, deadline)
            await self._cancel_all()
            raise
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        self._stopping.set()
        deadline = asyncio.get_running_loop().time() + self.drain_timeout
        # Drain in reverse registration order so producers quiesce before the
        # services they feed.
        for service in reversed(self._services):
            await self._drain_quietly(service, deadline)
        await self._cancel_all()
        self._started = False

    async def _drain_quietly(self, service: Service, deadline: float) -> None:
        """Drain one service, counting rather than propagating its failure.

        Both callers are unwinding: `stop` is quiescing every sibling, and
        `start`'s rollback is already carrying a failure it must re-raise. In
        neither can one service's `drain` be allowed to abort the others, and in
        the rollback case propagating would *mask* the original error with a
        cleanup error -- strictly less useful.

        So the catch is broad on purpose. What makes it the exceptional minority
        rather than the rule is that it is counted: `drain_errors` is the
        difference between "everything quiesced" and "shutdown finished anyway",
        which the caller could not otherwise tell apart. `CancelledError` is a
        `BaseException` and so passes straight through -- a cancelled shutdown
        must stay cancelled.
        """
        try:
            await service.drain(deadline)
        except Exception:  # noqa: BLE001 -- counted above; see the docstring
            self.drain_errors += 1

    async def _cancel_all(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        for task in tuple(self._tasks):
            try:
                await task
            except asyncio.CancelledError:
                # Expected: we cancelled it on the line above. Reaping a task we
                # cancelled ourselves is not an outage, so this one is not counted.
                pass
            except Exception:  # noqa: BLE001 -- counted; a task's dying breath
                # A service task that died of something other than our own
                # cancellation failed on its way out, and nobody else will ever
                # look at it -- this `await` is the only place that exception is
                # ever observed. Swallowing it silently is how a worker dies
                # unnoticed, so it lands in the same counter a failed drain does.
                self.drain_errors += 1
        self._tasks.clear()
