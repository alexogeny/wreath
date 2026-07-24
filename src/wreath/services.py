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
import contextlib
from collections.abc import Coroutine
from typing import Any, Protocol


class Service(Protocol):
    """A process-lifetime background service managed by the supervisor."""

    async def start(self, supervisor: Supervisor) -> None: ...

    async def drain(self, deadline: float) -> None: ...


class Supervisor:
    """Owns and lifecycles the background tasks of registered services."""

    __slots__ = ("_services", "_tasks", "_stopping", "_started", "drain_timeout")

    def __init__(self, *, drain_timeout: float = 10.0) -> None:
        self._services: list[Service] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stopping = asyncio.Event()
        self._started = False
        self.drain_timeout = drain_timeout

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
        with contextlib.suppress(Exception):  # set_name is best-effort/version-dependent
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
        except BaseException:
            # Roll back a partial start the same way we shut down: signal, drain
            # what came up, then cancel everything.
            self._stopping.set()
            deadline = asyncio.get_running_loop().time() + self.drain_timeout
            for service in reversed(started):
                with contextlib.suppress(Exception):
                    await service.drain(deadline)
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
            with contextlib.suppress(Exception):
                await service.drain(deadline)
        await self._cancel_all()
        self._started = False

    async def _cancel_all(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        for task in tuple(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
