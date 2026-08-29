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
import contextvars
import functools
import inspect
import os
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from ._awaitable import is_awaitable

Background = Callable[[], Awaitable[None]]

#: Threads reserved for synchronous background callables.
#:
#: They get their own pool because `asyncio.to_thread` uses the loop's *default*
#: executor, and so does everything else that has to leave the loop --
#: `wreath.objects` is on it for every read, write and fsync. Sharing meant a
#: route that queued a blocking sync task (an SMTP send, a vendor SDK) competed
#: for threads with file serving for unrelated users, and `min(32, cpu + 4)` of
#: them stalled every offload in the process.
#:
#: A deadline does not substitute for this. Cancelling the coroutine that awaits
#: `to_thread` does not interrupt the worker: `background_timeout` bounds how
#: long a *connection* is held, never how long a thread is occupied. Only
#: separate pools bound the blast radius, which is what this is for -- the
#: number matters much less than the separation.
BACKGROUND_THREADS = max(4, os.cpu_count() or 1)

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _background_executor() -> ThreadPoolExecutor:
    """The pool sync background callables run in, created on first use.

    Deliberately process-wide rather than per-application: it exists to keep
    background work off *the interpreter's* default pool, and two applications
    in one process share that pool whether they like it or not. Nothing is
    allocated by an application that never queues a synchronous task.
    """
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=BACKGROUND_THREADS,
                thread_name_prefix="wreath-background",
            )
        return _executor


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
        # Synchronous user code runs off the event-loop thread, in wreath's own
        # pool rather than the interpreter's default one -- see
        # `BACKGROUND_THREADS`. A callable classified as synchronous may still
        # return an awaitable (decorated functions, unusual callable objects);
        # await it without having run the potentially blocking body on the loop.
        # The context is copied exactly as `asyncio.to_thread` does it, so a
        # task still sees the ContextVars its request set.
        context = contextvars.copy_context()
        call = functools.partial(context.run, self.func, *self.args, **self.kwargs)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_background_executor(), call)
        if is_awaitable(result):
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
