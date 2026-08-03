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

Inside the task, `reporter.update(42, "processing invoices")` /
`reporter.done()` / `reporter.fail(exc)`.

**Across workers.** The registry is in-process, which is exactly wrong for the
case that matters most: the durable job runs on worker 3 and the browser is
connected to worker 1. Give the registry the message bus and every report
reaches every worker, so whichever one holds the stream can answer it:

```python
progress = ProgressRegistry(app.messaging("bus", database="app"))
```
No Redis — the bus is the database you already have. Delivery is at-most-once,
as ephemeral fan-out is: a worker that missed an update gets the next one, and
percentages are a running commentary rather than a ledger.

The natural pairing is `wreath.jobs.JobRunner.launch`, which uses the job
id as the task id and lets the runner set the terminal states itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ._busbridge import BusBridge
from ._json import dumps as _json_dumps
from .cache import BoundedCache
from .response import JSONResponse, Response, ServerSentEvent, SSEResponse
from .temporal import Duration

__all__ = [
    "MAX_MESSAGE_CHARS",
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

#: States only `ProgressRegistry.stream` synthesises, to say *why* a stream
#: ended. A registry never stores one: they describe the stream, not the task.
#: Without them every non-terminal close -- entry expired, id never seen, budget
#: spent -- looked identical to each other and to a dropped connection, so a long
#: import ended by appearing to still be running.
_STREAM_ENDED = ("expired", "unknown", "detached")

#: Longest message a report may carry. Applies to what arrives over the bus,
#: where the length is another worker's decision, and to local reports, where it
#: keeps one runaway f-string from filling the registry.
MAX_MESSAGE_CHARS = 4096

#: How many consecutive polls a stream waits for a task that has never been
#: reported before giving up. Covers the client-connects-first race without
#: letting an id nobody ever launches hold a connection open.
MISSING_TASK_POLLS = 5


def _clamp(percent: float) -> float:
    return 0.0 if percent < 0 else 100.0 if percent > 100 else float(percent)


def _as_text(data: Any) -> str:
    encoded = _json_dumps(data)
    return encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded


def _ended(last: Progress | None, state: str, message: str) -> Progress:
    """The closing event of a stream that ended without the task finishing.

    Carries the last percent seen rather than zero, so a client can render
    "stalled at 40%" instead of a bar that jumps back to the start on the way
    out. `error` stays `None`: nothing failed, the stream just stopped.
    """
    return Progress(last.percent if last is not None else 0.0, message, state, None)


@dataclass(frozen=True, slots=True)
class Progress:
    """A snapshot of a task's progress."""

    percent: float
    message: str = ""
    state: str = "running"     # "running" | "done" | "failed"
    error: str | None = None

    @property
    def terminal(self) -> bool:
        """The *task* finished: `done` or `failed`."""
        return self.state in _TERMINAL

    @property
    def ends_stream(self) -> bool:
        """Nothing further will arrive on the stream that yielded this.

        Broader than `terminal`, which is about the task. A stream also
        ends when the registry forgets an entry, when an id never appears, or
        when a watch budget runs out -- and a client needs to tell those apart
        from the connection simply dropping.
        """
        return self.state in _TERMINAL or self.state in _STREAM_ENDED

    def as_dict(self) -> dict[str, Any]:
        return {"percent": self.percent, "message": self.message,
                "state": self.state, "error": self.error}


class ProgressRegistry:
    """A bounded map of task id -> latest `Progress`.

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
        ttl: Any = 24 * 3600,
        channel: str = PROGRESS_CHANNEL,
    ) -> None:
        # `None` means never expire by time and must survive the coercion.
        window = None if ttl is None else Duration.of(ttl).total_seconds()
        self._store: BoundedCache = BoundedCache(max_entries=max_tasks, ttl=window)
        self._bridge = BusBridge(bus, channel=channel, apply=self._apply)

    def report(
        self, task_id: str, percent: float, message: str = "",
        *, state: str = "running", error: str | None = None,
    ) -> None:
        progress = Progress(_clamp(percent), message[:MAX_MESSAGE_CHARS], state, error)
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
                # Bounded: this arrives from the bus, so its size is not this
                # worker's to trust, and it is held in a bounded-by-count store
                # and streamed to clients.
                str(payload.get("message") or "")[:MAX_MESSAGE_CHARS],
                state if isinstance(state, str) else "running",
                error if isinstance(error, str) else None,
            ),
        )

    def reporter(self, task_id: str) -> ProgressReporter:
        """A handle bound to `task_id` to hand to the running task."""
        return ProgressReporter(self, task_id)

    def get(self, task_id: str) -> Progress | None:
        return self._store.get(task_id)

    async def stream(
        self, task_id: str, *, interval: float = 1.0, max_duration: float | None = None
    ):
        """Yield each new `Progress` for `task_id` until it is terminal.

        Polls every `interval` seconds (thread-safe, no cross-task signalling);
        stops after a `done`/`failed` state, once the entry is gone, once a
        task that never appeared has been waited for long enough, or once
        `max_duration` seconds have passed.

        That last case is the one worth naming: a stream for an id that does not
        exist used to poll forever, so any caller -- including an unauthenticated
        one, since these helpers carry no auth of their own -- could hold a
        connection open indefinitely by asking about a task that was never
        launched. A short grace period still covers the real race, where a client
        starts watching a moment before the task is registered.

        **Every stream ends with an event saying why**, so a close is never
        ambiguous with a dropped connection: `done`/`failed` when the task
        finished, `expired` when the registry forgot the entry mid-stream,
        `unknown` when the id never appeared, `detached` when
        `max_duration` ran out while the task was still going. The last three
        are `Progress.ends_stream` but not `Progress.terminal` in the
        `expired`/`unknown` sense the task never reached -- the registry
        stopped being able to answer, which is not the same as the work stopping.
        """
        last: Progress | None = None
        missing = 0
        deadline = (
            None if max_duration is None
            else asyncio.get_running_loop().time() + max_duration
        )
        while True:
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                # A task that never finishes must not mean a connection that
                # never closes. The client reconnects and picks up where the
                # registry is, which is what an SSE client does anyway -- and the
                # `detached` event is what tells it to.
                yield _ended(last, "detached", "watch budget spent; reconnect to resume")
                return
            current = self.get(task_id)
            if current is not None and current != last:
                yield current
                last = current
                missing = 0
                if current.terminal:
                    return
            elif current is None:
                if last is not None:
                    yield _ended(last, "expired", "the registry no longer holds this task")
                    return
                missing += 1
                if missing > MISSING_TASK_POLLS:
                    yield _ended(last, "unknown", "no such task")
                    return
            await asyncio.sleep(interval)


class ProgressReporter:
    """A task's write handle for one `task_id` (from `registry.reporter`)."""

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


def status_response(
    registry: ProgressRegistry,
    task_id: str,
    *,
    authorize: Callable[[str], bool] | None = None,
) -> Response:
    """A JSON status for `task_id` (`404` if unknown, expired, or refused).

    `authorize(task_id) -> bool` decides whether *this* caller may watch that
    task. It matters more than it looks: `wreath.jobs.JobRunner.launch`
    makes the task id the job id, which is a sequence, so without a guard the
    ids are countable and every task's state, message, and error text is
    readable by whoever counts. The predicate is synchronous on purpose -- this
    function is not a coroutine, and a handler that needs to await something can
    do it before calling.

    A refusal answers `404`, identical to an unknown id: a distinct `403`
    would confirm which ids exist, which is most of what enumeration wants.
    """
    if authorize is not None and not authorize(task_id):
        return _unknown_task(task_id)
    progress = registry.get(task_id)
    if progress is None:
        return _unknown_task(task_id)
    return JSONResponse({"task_id": task_id, **progress.as_dict()})


def _unknown_task(task_id: str) -> Response:
    return JSONResponse({"error": "unknown or expired task", "task_id": task_id}, status=404)


async def _progress_events(
    registry: ProgressRegistry,
    task_id: str,
    interval: float,
    max_duration: float | None = None,
):
    async for progress in registry.stream(
        task_id, interval=interval, max_duration=max_duration
    ):
        yield ServerSentEvent(data=_as_text(progress.as_dict()), event="progress")


def progress_stream(
    registry: ProgressRegistry,
    task_id: str,
    *,
    interval: float = 1.0,
    max_duration: float | None = None,
    authorize: Callable[[str], bool] | None = None,
) -> SSEResponse | Response:
    """An SSE response streaming `progress` events until the task is terminal.

    `authorize` and `max_duration` are `status_response`'s and
    `ProgressRegistry.stream`'s respectively; a refused caller gets the
    same `404` a missing task does, before any stream is opened.
    """
    if authorize is not None and not authorize(task_id):
        return _unknown_task(task_id)
    return SSEResponse(_progress_events(registry, task_id, interval, max_duration))


async def push_progress(
    websocket: Any,
    registry: ProgressRegistry,
    task_id: str,
    *,
    interval: float = 1.0,
    max_duration: float | None = None,
) -> None:
    """Push progress as JSON text frames over an accepted WebSocket until terminal."""
    async for progress in registry.stream(
        task_id, interval=interval, max_duration=max_duration
    ):
        await websocket.send_text(_as_text(progress.as_dict()))
