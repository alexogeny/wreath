"""A minimal supervisor for process-lifetime background services.

The supervisor owns the long-lived asyncio tasks that the durable-jobs and
messaging coordinators run (workers, durable-subscription consumers, and the
lease sweeper). It is started during the application lifespan *after* databases
come up and *before* user startup handlers, and stopped in reverse: stop
fetching new work, drain bounded in-flight handlers, then cancel anything that
outlives the grace period.

Design 01 §3 (lifespan) is the contract implemented here. A "service" is any
object exposing `async start(supervisor)` and `async drain(deadline)`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, Protocol


class Service(Protocol):
    """A process-lifetime background service managed by the supervisor.

    Structural, not nominal: any object with these two coroutines is a service.
    Implementations are expected to spawn their long-lived tasks through
    `Supervisor.spawn` rather than owning them, so the supervisor can
    cancel what outlives the grace period.
    """

    async def start(self, supervisor: Supervisor) -> None:
        """Begin running. Raising here aborts startup and rolls back siblings.

        Args:
            supervisor: The owner, for `spawn` and for reading `stopping`.
        """
        ...

    async def drain(self, deadline: float) -> None:
        """Stop taking new work and finish what is in flight, by `deadline`.

        Returning does not have to mean every task exited: whatever is still
        running when this returns is cancelled. Raising is survivable and
        counted in `Supervisor.drain_errors`; it never aborts a sibling's drain.

        Args:
            deadline: An event-loop clock time (`loop.time()` units), not a duration.
        """
        ...


class Supervisor:
    """Owns and lifecycles the background tasks of registered services.

    Register every service with `add` before `start`; services come
    up in registration order and drain in reverse, so a producer quiesces before
    whatever it feeds. `stop` is the only path that resolves the shutdown,
    and it always completes -- a service whose `drain` fails increments
    `drain_errors` instead of aborting its siblings.

    Args:
        drain_timeout: Seconds allowed for the whole drain, shared by every service.
    """

    __slots__ = (
        "_services",
        "_tasks",
        "_stopping",
        "_started",
        "drain_timeout",
        "drain_errors",
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
        """Register a service to be started and drained with this supervisor.

        Raises:
            RuntimeError: The supervisor has already started; registration is closed.
        """
        if self._started:
            raise RuntimeError("cannot register a service after the supervisor started")
        self._services.append(service)

    @property
    def stopping(self) -> asyncio.Event:
        """Set once shutdown begins; loops should stop fetching when it is set."""
        return self._stopping

    def is_stopping(self) -> bool:
        """Whether shutdown has begun. The non-awaiting read of `stopping`."""
        return self._stopping.is_set()

    @property
    def empty(self) -> bool:
        """Whether no service is registered. An empty supervisor still starts."""
        return not self._services

    def spawn(self, name: str, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Own a long-lived task; it is cancelled on stop if it outlives drain.

        The supervisor holds a strong reference until the task completes, which
        is what keeps a fire-and-forget worker from being garbage collected
        mid-flight. A task that raises is not observed until `stop` reaps it,
        and its exception is counted in `drain_errors` rather than re-raised.

        Args:
            name: Task name, as it appears in `asyncio` diagnostics.

        Returns:
            The task, for a caller that wants to await or cancel it itself.
        """
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self) -> None:
        """Start every registered service, in registration order.

        Idempotent: a second call on a started supervisor returns immediately.
        Startup is all-or-nothing. If one service's `start` raises -- including
        by cancellation -- the services that already came up are drained in
        reverse, every spawned task is cancelled, and the original exception
        propagates unchanged. The supervisor is left un-started, so it can be
        started again once the cause is fixed.

        Raises:
            Exception: Whatever a service's `start` raised, after the rollback.
        """
        if self._started:
            return
        self._stopping.clear()
        started: list[Service] = []
        try:
            for service in self._services:
                await service.start(self)
                started.append(service)
        except BaseException:  # re-raised below; see the comment
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
        """Signal shutdown, drain in reverse registration order, cancel the rest.

        Idempotent: a call on a supervisor that never started returns
        immediately. This never raises on a service's behalf -- a `drain` that
        fails, and a spawned task that died of something other than the
        supervisor's own cancellation, each increment `drain_errors`. Read that
        counter to tell "everything quiesced" from "shutdown finished anyway";
        the shutdown itself completes either way. `CancelledError` still
        propagates, so a cancelled shutdown stays cancelled.
        """
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
