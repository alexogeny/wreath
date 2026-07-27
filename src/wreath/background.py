"""Response-bound background tasks.

A background task is application work that runs *after* the complete response
has been emitted but still inside the ASGI application invocation. Ownership is
explicit: a task belongs to a response, is awaited by `Wreath._finish_http` once
the final response message is sent, and is interrupted by process shutdown like
any other in-process work. This is not a durable job queue -- for work that must
survive process loss, use an external queue and hand this task only a durable
identifier or payload.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

Background = Callable[[], Awaitable[None]]


def _is_async_callable(function: Callable[..., Any]) -> bool:
    """Classify a callable as async once, at task-construction time.

    `functools.partial` is unwrapped to its underlying callable, and callable
    objects are classified by their `__call__`. Everything else is treated as
    synchronous and offloaded to a thread so it cannot block the event loop.
    """
    unwrapped = function
    while isinstance(unwrapped, functools.partial):
        unwrapped = unwrapped.func
    if inspect.iscoroutinefunction(unwrapped):
        return True
    call = getattr(unwrapped, "__call__", None)  # noqa: B004 - intentional dunder lookup
    return call is not None and inspect.iscoroutinefunction(call)


class BackgroundTask:
    """A single callable bound with its arguments, run after the response."""

    __slots__ = ("_is_async", "args", "func", "kwargs")

    def __init__(
        self,
        function: Callable[..., Awaitable[None] | None],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.func = function
        self.args = args
        self.kwargs = kwargs
        self._is_async = _is_async_callable(function)

    async def __call__(self) -> None:
        if self._is_async:
            await cast("Awaitable[None]", self.func(*self.args, **self.kwargs))
            return
        # Synchronous user code runs off the event-loop thread. A callable
        # classified as synchronous may still return an awaitable (decorated
        # functions, unusual callable objects); await it without having run the
        # potentially blocking body on the loop.
        result = await asyncio.to_thread(self.func, *self.args, **self.kwargs)
        if inspect.isawaitable(result):
            await result


class BackgroundTasks:
    """An ordered group of tasks run sequentially after the response.

    Tasks execute in insertion order. If one raises, the exception propagates
    and later tasks do not run. Configure the group before returning the
    response; it is not a concurrent queue and must not be mutated while it is
    executing.
    """

    __slots__ = ("tasks",)

    def __init__(self, tasks: list[BackgroundTask] | None = None) -> None:
        self.tasks: list[BackgroundTask] = list(tasks) if tasks is not None else []

    def add_task(
        self,
        function: Callable[..., Awaitable[None] | None],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.tasks.append(BackgroundTask(function, *args, **kwargs))

    async def __call__(self) -> None:
        for task in self.tasks:
            await task()
