"""The cold half of logging: rendering records and writing them out.

Everything here runs off the request path. The request path published a 64-byte
cell and returned; this module takes the projector's reassembled records,
renders them against the interned call-site registry, and writes the bytes.

The isolation is the same shape `_export.py` uses for OTLP, and for the same
reason: a slow disk, a full pipe, or a raising writer must not be able to reach
back into anything that serves a request.

- `on_log` only *offers* to a bounded queue, dropping and counting when it is
  full, so the projector thread never blocks on a writer.
- A dedicated writer thread drains that queue on an interval. Logs outnumber
  completions by one to two orders of magnitude, so they get their own thread
  rather than sharing the projector's with trace assembly and OTLP mapping.
- Every write is wrapped: a raising sink increments a counter and the pipeline
  keeps draining. Backpressure shows up as visible drops, never as growth.

Two renderers over one formatter: text for a terminal, JSON lines for a
collector. Choosing between them is a flag, not a format break.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any, Final
from typing import Protocol as _Protocol

from ._flight_schema import (
    LOG_FLAG_EVENT_FIELDS,
    LOG_FLAG_OFF_LOOP,
    LOG_FLAG_PROMOTED,
    LOG_FLAG_REDACTED,
    LOG_FLAG_TRUNCATED,
    TerminalStatus,
    severity_text,
)
from ._logsite import SiteRegistry
from ._logsite import attributes as _attributes
from ._logsite import render as _render
from ._projector import ProjectedLog, ProjectedTrace
from .queue import Queue

#: Records held between writer ticks. Sized like the export queue: generous
#: enough to absorb a burst, small enough that a stalled writer is bounded.
DEFAULT_LOG_QUEUE_CAPACITY: Final = 8192

#: Seconds between writer drains. Logs are latency-tolerant on the way out; the
#: latency that matters was already paid off at the ring.
DEFAULT_WRITER_INTERVAL: Final = 0.2

#: Flag bit -> the word that appears in a rendered line.
_FLAG_WORDS: Final = (
    (LOG_FLAG_PROMOTED, "promoted"),
    (LOG_FLAG_TRUNCATED, "truncated"),
    (LOG_FLAG_REDACTED, "redacted"),
    (LOG_FLAG_OFF_LOOP, "off-loop"),
)


class Renderer(_Protocol):
    """Turns one record into one line, with no trailing newline."""

    def __call__(self, registry: SiteRegistry, record: ProjectedLog) -> str: ...


class TextRenderer:
    """Human-first: severity, message, then correlation and attributes.

    This is what an operator sees on a terminal at 3am, and it is the reason
    formatting is deferred to a thread rather than to an offline decoder.
    """

    __slots__ = ("show_attributes",)

    def __init__(self, *, show_attributes: bool = True) -> None:
        self.show_attributes = show_attributes

    def __call__(self, registry: SiteRegistry, record: ProjectedLog) -> str:
        cell = record.cell
        parts = [f"{severity_text(cell.severity):<5}", _render(registry, cell)]
        if record.has_correlation:
            parts.append(f"trace={record.trace_id:032x} span={record.span_id:016x}")
        if self.show_attributes:
            values = _attributes(registry, cell)
            if values:
                parts.append(" ".join(f"{key}={value!r}" for key, value in values.items()))
        if cell.dropped_siblings:
            parts.append(f"(+{cell.dropped_siblings} sampled out)")
        words = [word for bit, word in _FLAG_WORDS if cell.flags & bit]
        if words:
            parts.append(f"[{','.join(words)}]")
        return "  ".join(parts)


class JsonRenderer:
    """One JSON object per line, keyed to the OpenTelemetry log fields.

    Correlation is omitted rather than zero-filled when a record is not
    request-scoped: a collector distinguishes absent from zero, and a trace id
    of all zeros is a real value that means "unset" to nobody.
    """

    __slots__ = ()

    def __call__(self, registry: SiteRegistry, record: ProjectedLog) -> str:
        cell = record.cell
        site = registry.get(cell.site_id)
        payload: dict[str, Any] = {
            "severity": severity_text(cell.severity),
            "severity_number": int(cell.severity),
            "event": site.event_name if site is not None else f"site:{cell.site_id}",
            "message": _render(registry, cell),
        }
        attrs = _attributes(registry, cell)
        if attrs:
            payload["attributes"] = attrs
        if record.has_correlation:
            payload["trace_id"] = f"{record.trace_id:032x}"
            payload["span_id"] = f"{record.span_id:016x}"
        if record.route_id:
            payload["route_id"] = record.route_id
        if record.observed_unix_nano:
            payload["observed_unix_nano"] = record.observed_unix_nano
        if cell.dropped_siblings:
            payload["dropped_siblings"] = cell.dropped_siblings
        words = [word for bit, word in _FLAG_WORDS if cell.flags & bit]
        if words:
            payload["flags"] = words
        return json.dumps(payload, separators=(",", ":"), default=str)


def default_renderer(*, is_tty: bool) -> Renderer:
    """Text on a terminal, JSON lines everywhere else.

    Both ship from the first release deliberately: picking one and adding the
    other later would change what an operator's pipeline parses.
    """
    return TextRenderer() if is_tty else JsonRenderer()


#: The hand-off from the projector thread to the writer.
#:
#: Was a class here, and a second copy of it beside `_otlp.BoundedExportQueue`:
#: the same deque, the same lock, the same two counters, the same
#: drop-and-count policy, differing only in the type of the thing queued. Both
#: are now `wreath.queue.Queue`, which is that policy in C -- one call per item
#: instead of a lock acquire, two increments, a length test and a method call.
#: Measured at 0.17us per offer before, against a 0.02us bare `deque.append`.
#:
#: The name stays because it says what this queue is *for*, and because the
#: alias keeps the import in `_export.py` and the log tests working unchanged.
BoundedLogQueue = Queue


class LogPipeline:
    """Renders and writes log records on a dedicated thread.

    Args:
        registry: The interned call sites. Passed explicitly rather than read
            from the `wreath.logging` singleton so a pipeline can be pointed at
            a specific registry, and so the dependency is visible.
        write: Receives one rendered line at a time, without a trailing newline.
        renderer: How a record becomes a line. Defaults to JSON lines.
        capacity: Records held between ticks before offering starts dropping.
        interval: Seconds between drains on the writer thread.
    """

    __slots__ = (
        "_interval",
        "_queue",
        "_registry",
        "_render_error",
        "_renderer",
        "_stop",
        "_thread",
        "_write",
        "_write_error",
        "_written",
    )

    def __init__(
        self,
        registry: SiteRegistry,
        *,
        write: Callable[[str], None],
        renderer: Renderer | None = None,
        capacity: int = DEFAULT_LOG_QUEUE_CAPACITY,
        interval: float = DEFAULT_WRITER_INTERVAL,
    ) -> None:
        self._registry = registry
        self._write = write
        self._renderer: Renderer = renderer if renderer is not None else JsonRenderer()
        self._queue = BoundedLogQueue(capacity)
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._written = 0
        self._write_error = 0
        self._render_error = 0

    def on_log(self, record: ProjectedLog) -> None:
        """Offer a record. Safe to hand straight to the projector as its hook.

        Only enqueues -- never renders, never writes -- so the projector thread
        cannot be slowed by a sink.
        """
        self._queue.offer(record)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wreath-log-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the writer thread, flushing whatever is still queued."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
        self.flush()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.flush()
            self._stop.wait(self._interval)

    def flush(self) -> int:
        """Render and write everything queued. Returns the number written.

        Exposed for deterministic tests and for the shutdown flush, exactly as
        the projector exposes `poll`.
        """
        written = 0
        for record in self._queue.drain():
            try:
                line = self._renderer(self._registry, record)
            except ValueError, TypeError, KeyError, LookupError:
                # A record that cannot be rendered is a defect in a call site,
                # not a reason to stop writing the ones that can be.
                self._render_error += 1
                continue
            try:
                self._write(line)
            except OSError, ValueError:
                # The sink is the outside world: a full disk, a closed pipe, a
                # rotated file. Count it and keep draining -- a writer that
                # stops on the first error loses every record after it.
                self._write_error += 1
                continue
            written += 1
            self._written += 1
        return written

    def stats(self) -> dict[str, int]:
        """Counters an operator reads to tell backpressure from breakage."""
        return {
            "offered": self._queue.offered,
            "dropped": self._queue.dropped,
            "queued": len(self._queue),
            "written": self._written,
            "write_error": self._write_error,
            "render_error": self._render_error,
        }


# One structured record per request, carrying what the recorder already knows
# plus whatever the application attached. The completion cell is already this
# record in binary; these two functions are its rendering.
# Application fields arrive as ordinary log cells flagged LOG_FLAG_EVENT_FIELDS,
# joined to the trace by request id like every other record. Folding them here
# rather than printing them as their own lines is what makes the result *one*
# authoritative record instead of a scatter of partial ones.


def _canonical_parts(registry: SiteRegistry, trace: ProjectedTrace) -> tuple[dict[str, Any], int]:
    """Split a trace's records into attached fields and ordinary records."""
    attributes: dict[str, Any] = {}
    records = 0
    for cell in trace.logs:
        if cell.flags & LOG_FLAG_EVENT_FIELDS:
            attributes.update(_attributes(registry, cell))
        else:
            records += 1
    return attributes, records


def canonical_json(registry: SiteRegistry, trace: ProjectedTrace) -> str:
    """One wide JSON object per request: the authoritative record of what happened."""
    attributes, records = _canonical_parts(registry, trace)
    payload: dict[str, Any] = {
        "request_id": trace.request_id,
        "route_id": trace.route_id,
        "status": trace.status,
        "duration_us": trace.duration_us,
        "protocol": trace.protocol.name,
        "terminal": trace.terminal.name,
        "bytes_in": trace.bytes_in,
        "bytes_out": trace.bytes_out,
    }
    if trace.plan_id:
        payload["plan_id"] = trace.plan_id
    if trace.error_class:
        payload["error_class"] = trace.error_class
    if trace.is_failure:
        payload["failure"] = True
    if trace.has_correlation:
        payload["trace_id"] = f"{trace.trace_id:032x}"
        payload["span_id"] = f"{trace.span_id:016x}"
    if trace.observed_unix_nano:
        payload["observed_unix_nano"] = trace.observed_unix_nano
    if attributes:
        payload["attributes"] = attributes
    if records:
        payload["records"] = records
    if trace.phases:
        payload["phases"] = len(trace.phases)
    return json.dumps(payload, separators=(",", ":"), default=str)


def canonical_text(registry: SiteRegistry, trace: ProjectedTrace) -> str:
    """The same record, for a terminal."""
    attributes, records = _canonical_parts(registry, trace)
    parts = [
        f"route={trace.route_id}",
        f"status={trace.status}",
        f"{trace.duration_us}us",
        f"proto={trace.protocol.name}",
    ]
    if trace.terminal is not TerminalStatus.OK:
        parts.append(f"terminal={trace.terminal.name}")
    if trace.has_correlation:
        parts.append(f"trace={trace.trace_id:032x}")
    parts.extend(f"{key}={value}" for key, value in attributes.items())
    if records:
        parts.append(f"records={records}")
    return "  ".join(parts)
