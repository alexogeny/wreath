"""Report and expose the progress of a long-running task.

A slow job — an import, a rollup, a batch call — wants to tell the client "42%,
processing invoices". Wreath gives you the transport (SSE, WebSockets, JSON) and
durable jobs give you the *state*; this is the small piece in between: a task
writes progress to a bounded in-process registry, and a status endpoint or a
stream reads it.

    progress = ProgressRegistry()

    @app.post("/imports")
    async def start_import(request):
        task_id = new_id()
        asyncio.create_task(run_import(progress.reporter(task_id), ...))  # your runner
        return {"task_id": task_id}

    @app.get("/imports/{task_id}/status")
    async def status(request):
        return status_response(progress, request.path_params["task_id"])

    @app.get("/imports/{task_id}/stream")
    async def stream(request):
        return progress_stream(progress, request.path_params["task_id"])

Inside the task, ``reporter.update(42, "processing invoices")`` /
``reporter.done()`` / ``reporter.fail(exc)``.

**Across workers.** The registry is in-process, which is exactly wrong for the
case that matters most: the durable job runs on worker 3 and the browser is
connected to worker 1. Give the registry the message bus and every report
reaches every worker, so whichever one holds the stream can answer it::

    progress = ProgressRegistry(app.messaging("bus", database="app"))

No Redis — the bus is the database you already have. Delivery is at-most-once,
as ephemeral fan-out is: a worker that missed an update gets the next one, and
percentages are a running commentary rather than a ledger.

The natural pairing is :meth:`wreath.jobs.JobRunner.launch`, which uses the job
id as the task id and lets the runner set the terminal states itself.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ._busbridge import BusBridge
from ._json import dumps as _json_dumps
from .cache import BoundedCache
from .response import JSONResponse, Response, ServerSentEvent, SSEResponse

__all__ = [
    "PROGRESS_CHANNEL",
    "Progress",
    "ProgressRegistry",
    "ProgressReporter",
    "progress_stream",
    "push_progress",
    "status_response",
]

#: Default bus channel carrying every task's progress. A valid SQL identifier,
#: because `wreath.messaging` validates channel names as one.
PROGRESS_CHANNEL = "wreath_progress"

_TERMINAL = ("done", "failed")


def _clamp(percent: float) -> float:
    return 0.0 if percent < 0 else 100.0 if percent > 100 else float(percent)


def _as_text(data: Any) -> str:
    encoded = _json_dumps(data)
    return encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded


@dataclass(frozen=True, slots=True)
class Progress:
    """A snapshot of a task's progress."""

    percent: float
    message: str = ""
    state: str = "running"     # "running" | "done" | "failed"
    error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    def as_dict(self) -> dict[str, Any]:
        return {"percent": self.percent, "message": self.message,
                "state": self.state, "error": self.error}


class ProgressRegistry:
    """A bounded map of task id -> latest :class:`Progress`.

    Pass the message bus to make it fleet-wide: every report is applied here and
    published once, and every other worker applies what it receives without
    relaying it onward. Omit it for single-process work, which is the right
    default for tests and one-worker deployments.
    """

    __slots__ = ("_bridge", "_store")

    def __init__(
        self,
        bus: Any = None,
        *,
        max_tasks: int = 4096,
        ttl: float | None = 3600,
        channel: str = PROGRESS_CHANNEL,
    ) -> None:
        self._store: BoundedCache = BoundedCache(max_entries=max_tasks, ttl=ttl)
        self._bridge = BusBridge(bus, channel=channel, apply=self._apply)

    def report(
        self, task_id: str, percent: float, message: str = "",
        *, state: str = "running", error: str | None = None,
    ) -> None:
        progress = Progress(_clamp(percent), message, state, error)
        self._store.set(task_id, progress)
        # Guarded rather than left to the bridge's own no-bus check, so a
        # registry with no bus -- the default, and every test -- does not build
        # a wire payload per report only to throw it away.
        if self._bridge.attached:
            # Deferred: a bus that is down must not fail the work being reported
            # on. The percentage is commentary; losing one costs the client a
            # stale bar until the next update, and the terminal state is what
            # actually matters.
            self._bridge.publish_soon({"task_id": task_id, **progress.as_dict()})

    # -- across workers --------------------------------------------------------

    async def _apply(self, payload: dict[str, Any]) -> None:
        """Apply another worker's report. Never republished -- one hop only."""
        task_id = payload.get("task_id")
        percent = payload.get("percent")
        if not isinstance(task_id, str) or not isinstance(percent, (int, float)):
            return
        state = payload.get("state")
        error = payload.get("error")
        self._store.set(
            task_id,
            Progress(
                _clamp(percent),
                str(payload.get("message") or ""),
                state if isinstance(state, str) else "running",
                error if isinstance(error, str) else None,
            ),
        )

    def reporter(self, task_id: str) -> ProgressReporter:
        """A handle bound to ``task_id`` to hand to the running task."""
        return ProgressReporter(self, task_id)

    def get(self, task_id: str) -> Progress | None:
        return self._store.get(task_id)

    async def stream(self, task_id: str, *, interval: float = 1.0):
        """Yield each new :class:`Progress` for ``task_id`` until it is terminal.

        Polls every ``interval`` seconds (thread-safe, no cross-task signalling);
        stops after a ``done``/``failed`` state or once the entry is gone.
        """
        last: Progress | None = None
        while True:
            current = self.get(task_id)
            if current is not None and current != last:
                yield current
                last = current
                if current.terminal:
                    return
            elif current is None and last is not None:
                return           # expired or evicted mid-stream
            await asyncio.sleep(interval)


class ProgressReporter:
    """A task's write handle for one ``task_id`` (from ``registry.reporter``)."""

    __slots__ = ("_registry", "_task_id")

    def __init__(self, registry: ProgressRegistry, task_id: str) -> None:
        self._registry = registry
        self._task_id = task_id

    def update(self, percent: float, message: str = "") -> None:
        self._registry.report(self._task_id, percent, message)

    def done(self, message: str = "done") -> None:
        self._registry.report(self._task_id, 100, message, state="done")

    def fail(self, error: object, message: str = "") -> None:
        self._registry.report(
            self._task_id, self._current_percent(), message, state="failed", error=str(error))

    def _current_percent(self) -> float:
        current = self._registry.get(self._task_id)
        return current.percent if current is not None else 0.0


def status_response(registry: ProgressRegistry, task_id: str) -> Response:
    """A JSON status for ``task_id`` (``404`` if unknown or expired)."""
    progress = registry.get(task_id)
    if progress is None:
        return JSONResponse({"error": "unknown or expired task", "task_id": task_id}, status=404)
    return JSONResponse({"task_id": task_id, **progress.as_dict()})


async def _progress_events(registry: ProgressRegistry, task_id: str, interval: float):
    async for progress in registry.stream(task_id, interval=interval):
        yield ServerSentEvent(data=_as_text(progress.as_dict()), event="progress")


def progress_stream(
    registry: ProgressRegistry, task_id: str, *, interval: float = 1.0
) -> SSEResponse:
    """An SSE response streaming ``progress`` events until the task is terminal."""
    return SSEResponse(_progress_events(registry, task_id, interval))


async def push_progress(
    websocket: Any, registry: ProgressRegistry, task_id: str, *, interval: float = 1.0
) -> None:
    """Push progress as JSON text frames over an accepted WebSocket until terminal."""
    async for progress in registry.stream(task_id, interval=interval):
        await websocket.send_text(_as_text(progress.as_dict()))
