"""Asynchronous projection for the Native Flight Recorder (Stage 4).

The recorder's request path only ever *publishes* fixed cells into a single
writer / single reader ring. Turning those cells back into whole requests --
joining a completion to its correlation carrier and detail phases, aggregating
route metrics, and handing finished traces to an exporter -- is work that must
never touch a request stack. This module is that work: a :class:`Projector`
owns one background thread that drains the ring in bounded batches, reassembles
traces, keeps a bounded window of recent completions (and failures) for the
Inspector, and offers each finished trace to an optional export hook whose
failures are isolated to a counter.

Everything here is pure Python and reads only through the recorder's public
``drain`` / ``loss`` / ``histogram`` accessors, so it works identically over the
native ``Recorder`` and the pure oracle. The OTLP mapping (Stage 4b) and the
server lifespan wiring (Stage 4c) build on the export hook and snapshot API
exposed here; nothing in this file imports an exporter SDK.

**Assembly ordering.** ``context_end`` publishes a request's cells in the fixed
order completion, then correlation, then phase batches, so a completion is seen
*before* its trailing carriers. Rather than guess when the tail has arrived, the
projector settles a completion after a *quiet cycle* -- one full drain cycle in
which no further cell for that request arrived. In the common case all of a
request's cells land in one drain, so it settles on the next cycle; if a batch
boundary spreads the tail across several cycles, each arriving cell pushes the
settle out, so nothing is finalized until the whole tail is in. Cells whose
completion never arrives (a dropped ring head) are counted as orphans, never
emitted.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final
from typing import Protocol as _Protocol

from ._flight_schema import (
    CELL_SIZE,
    FLAG_ERROR_PROMOTED,
    FLAG_SLOW_PROMOTED,
    CompletionCell,
    CorrelationCell,
    EventKind,
    LossReason,
    PhaseBatchCell,
    PhaseRecord,
    Protocol,
    SchemaError,
    TerminalStatus,
    histogram_bucket,
)

__all__ = [
    "ProjectedTrace",
    "RouteMetric",
    "ProjectorSnapshot",
    "ProjectorLoss",
    "Projector",
]

class _RecorderLike(_Protocol):
    """The recorder surface the projector reads: the native ``Recorder`` and the
    pure oracle both satisfy it structurally."""

    def drain(self, max_cells: int = ..., /) -> bytes: ...
    def loss(self, reason: int, /) -> int: ...


#: Default projector tuning. All are overridable per :class:`Projector`.
_DEFAULT_INTERVAL: Final = 0.05  # seconds between drain cycles
_DEFAULT_MAX_CELLS: Final = 4096  # cells drained per cycle
_DEFAULT_RECENT: Final = 1024  # finished traces retained for the Inspector
_DEFAULT_FAILURES: Final = 256  # failed traces retained separately
_DEFAULT_PENDING: Final = 8192  # completions awaiting their trailing cells
_DEFAULT_ROUTES: Final = 4096  # distinct route buckets in the metric table


@dataclass(slots=True)
class ProjectorLoss:
    """A snapshot of the projector's own categorized loss counters."""

    orphan_phase: int = 0
    orphan_correlation: int = 0
    pending_evicted: int = 0
    decode_error: int = 0
    export_error: int = 0
    recent_evicted: int = 0


@dataclass(frozen=True, slots=True)
class ProjectedTrace:
    """One fully reassembled request: its completion joined to any correlation
    carrier and detail phases."""

    request_id: int
    connection_id: int
    route_id: int
    plan_id: int
    worker_id: int
    duration_us: int
    status: int
    terminal: TerminalStatus
    protocol: Protocol
    error_class: int
    flags: int
    bytes_in: int
    bytes_out: int
    trace_id: int = 0
    span_id: int = 0
    parent_span_id: int = 0
    phases: tuple[PhaseRecord, ...] = ()
    #: Wall-clock time (Unix nanoseconds) the projector finalized this trace.
    #: The completion cell carries only a duration, so this observation time is
    #: the anchor an exporter uses to place the span on a wall clock:
    #: ``start ~= observed_unix_nano - duration_us*1000``. It is later than the
    #: true completion instant by at most one drain interval.
    observed_unix_nano: int = 0

    @property
    def has_correlation(self) -> bool:
        return self.trace_id != 0 or self.span_id != 0

    @property
    def is_failure(self) -> bool:
        """Whether this completion should be retained as a failure. Covers a
        non-OK terminal disposition, a 5xx status, and the recorder's own
        slow/error promotion flags."""
        return (
            self.terminal is not TerminalStatus.OK
            or self.status >= 500
            or bool(self.flags & (FLAG_ERROR_PROMOTED | FLAG_SLOW_PROMOTED))
        )


@dataclass(slots=True)
class RouteMetric:
    """Cumulative counters for one route since the projector started."""

    route_id: int
    count: int = 0
    errors: int = 0
    duration_us_sum: int = 0
    duration_us_max: int = 0
    #: base-2 log-bucket histogram of per-request duration, like the recorder's.
    buckets: list[int] = field(default_factory=lambda: [0] * 64)


@dataclass(frozen=True, slots=True)
class ProjectorSnapshot:
    """A consistent read of the projector's aggregated state for the Inspector
    and exporters. Cheap to build and safe to hand across threads (all values
    are plain immutables or fresh copies)."""

    assembled: int
    recent: tuple[ProjectedTrace, ...]
    failures: tuple[ProjectedTrace, ...]
    routes: tuple[RouteMetric, ...]
    loss: ProjectorLoss
    pending: int


@dataclass(slots=True)
class _Assembly:
    """A completion accumulating its trailing correlation/phase cells until it
    settles. ``cycle`` is the last drain cycle in which any cell for this request
    arrived; a completion settles once a later cycle passes without one."""

    completion: CompletionCell | None = None
    correlation: CorrelationCell | None = None
    phases: list[PhaseRecord] = field(default_factory=list)
    cycle: int = -1


class Projector:
    """Drains a recorder's ring on a background thread and reassembles traces.

    The projector is inert until :meth:`start`; construction allocates only its
    bounded buffers. ``on_trace``, when given, is called with each finished
    :class:`ProjectedTrace` on the projector thread; any exception it raises is
    swallowed and counted (``export_error``) so a failing exporter can never
    stall the drain or perturb the request path.
    """

    __slots__ = (
        "_recorder",
        "_interval",
        "_max_cells",
        "_on_trace",
        "_max_routes",
        "_lock",
        "_pending",
        "_max_pending",
        "_recent",
        "_failures",
        "_routes",
        "_loss",
        "_assembled",
        "_cycle",
        "_thread",
        "_stop",
    )

    def __init__(
        self,
        recorder: _RecorderLike,
        *,
        interval: float = _DEFAULT_INTERVAL,
        max_cells: int = _DEFAULT_MAX_CELLS,
        recent: int = _DEFAULT_RECENT,
        failures: int = _DEFAULT_FAILURES,
        pending: int = _DEFAULT_PENDING,
        max_routes: int = _DEFAULT_ROUTES,
        on_trace: Callable[[ProjectedTrace], None] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        if max_cells <= 0:
            raise ValueError("max_cells must be positive")
        self._recorder = recorder
        self._interval = interval
        self._max_cells = max_cells
        self._on_trace = on_trace
        self._max_routes = max_routes
        # Guards every mutable buffer below: the drain thread writes under it,
        # snapshot()/metrics() read under it. Held only for O(batch) work.
        self._lock = threading.Lock()
        self._pending: dict[int, _Assembly] = {}
        self._max_pending = pending
        self._recent: deque[ProjectedTrace] = deque(maxlen=recent)
        self._failures: deque[ProjectedTrace] = deque(maxlen=failures)
        self._routes: dict[int, RouteMetric] = {}
        self._loss = ProjectorLoss()
        self._assembled = 0
        self._cycle = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the drain thread. Idempotent; a stopped projector can restart."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        thread = threading.Thread(
            target=self._run, name="wreath-flight-projector", daemon=True
        )
        self._thread = thread
        thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the drain thread to finish, drain once more, and join.

        A final :meth:`poll` on the way out flushes whatever the ring still
        holds, so shutdown does not silently strand recorded cells.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            self._thread = None
        # A last drain after the writer paths are quiesced by the caller. Settle
        # twice so completions seen on the final cycle still get their tail.
        self.poll()
        self.poll()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self._interval)

    # --- draining and assembly --------------------------------------------

    def poll(self) -> int:
        """Run one drain cycle synchronously: drain, ingest, settle. Returns the
        number of traces finalized this cycle. Exposed for deterministic tests
        and for the shutdown flush; the thread calls it on the interval."""
        raw = self._recorder.drain(self._max_cells)
        with self._lock:
            self._cycle += 1
            self._ingest(raw)
            return self._settle()

    def _ingest(self, raw: bytes) -> None:
        """Decode a drained buffer and fold each cell into the pending table.
        Nothing finalizes here -- a completion settles only after a quiet cycle
        (see :meth:`_settle`), so its whole tail has a chance to arrive first."""
        for offset in range(0, len(raw) - CELL_SIZE + 1, CELL_SIZE):
            cell = raw[offset : offset + CELL_SIZE]
            kind = cell[1]
            try:
                if kind == EventKind.COMPLETION:
                    self._ingest_completion(CompletionCell.decode(cell))
                elif kind == EventKind.CORRELATION:
                    self._ingest_correlation(CorrelationCell.decode(cell))
                elif kind == EventKind.PHASE:
                    self._ingest_phase(PhaseBatchCell.decode(cell))
                else:
                    # CONTROL/INVALID carry no trace payload for the projector.
                    continue
            except SchemaError:
                self._loss.decode_error += 1

    def _slot(self, request_id: int) -> _Assembly:
        entry = self._pending.get(request_id)
        if entry is None:
            if len(self._pending) >= self._max_pending:
                self._evict_oldest_pending()
            entry = _Assembly()
            self._pending[request_id] = entry
        # ``cycle`` tracks the last cycle a cell for this request arrived, so a
        # completion settles only after a full quiet cycle -- however many cycles
        # its correlation/phase tail is spread across.
        entry.cycle = self._cycle
        return entry

    def _ingest_completion(self, cell: CompletionCell) -> None:
        # Record the completion and let the quiet-cycle rule settle it. We never
        # finalize on the spot even when a correlation/phase already arrived
        # (reordered ahead of it): the rest of the tail may still be split across
        # this drain and the next, and settling early would orphan it. In
        # production the ring publishes completion first anyway, so the tail
        # always follows.
        self._slot(cell.request_id).completion = cell

    def _ingest_correlation(self, cell: CorrelationCell) -> None:
        self._slot(cell.request_id).correlation = cell

    def _ingest_phase(self, cell: PhaseBatchCell) -> None:
        self._slot(cell.request_id).phases.extend(cell.records)

    def _settle(self) -> int:
        """Finalize completions first seen in an earlier cycle (their tail has
        had a full cycle to arrive) and retire stale partials as orphans."""
        emitted = 0
        for request_id in list(self._pending):
            entry = self._pending.get(request_id)
            if entry is None or entry.cycle >= self._cycle:
                continue  # first seen this cycle: give the tail one more cycle
            if entry.completion is not None:
                self._finalize(request_id, entry)
                emitted += 1
            else:
                # A correlation/phase cell whose completion never came: the ring
                # dropped its head. Count it and stop holding the slot.
                if entry.correlation is not None:
                    self._loss.orphan_correlation += 1
                if entry.phases:
                    self._loss.orphan_phase += 1
                del self._pending[request_id]
        return emitted

    def _evict_oldest_pending(self) -> None:
        """Drop the pending entry seen longest ago to bound the table. A
        completion evicted this way is a lost trace; a headless partial is an
        orphan. Either way it is categorized, never silent."""
        oldest_id = min(self._pending, key=lambda rid: self._pending[rid].cycle)
        entry = self._pending.pop(oldest_id)
        if entry.completion is not None:
            self._loss.pending_evicted += 1
        else:
            if entry.correlation is not None:
                self._loss.orphan_correlation += 1
            if entry.phases:
                self._loss.orphan_phase += 1

    def _finalize(self, request_id: int, entry: _Assembly) -> None:
        completion = entry.completion
        assert completion is not None
        del self._pending[request_id]
        corr = entry.correlation
        phases = tuple(sorted(entry.phases, key=lambda p: p.sequence))
        trace = ProjectedTrace(
            request_id=completion.request_id,
            connection_id=completion.connection_id,
            route_id=completion.route_id,
            plan_id=completion.plan_id,
            worker_id=completion.worker_id,
            duration_us=completion.duration_us,
            status=completion.status,
            terminal=completion.terminal,
            protocol=completion.protocol,
            error_class=completion.error_class,
            flags=completion.flags,
            bytes_in=completion.bytes_in,
            bytes_out=completion.bytes_out,
            trace_id=corr.trace_id if corr is not None else 0,
            span_id=corr.span_id if corr is not None else 0,
            parent_span_id=corr.parent_span_id if corr is not None else 0,
            phases=phases,
            observed_unix_nano=time.time_ns(),
        )
        self._assembled += 1
        self._record_metric(trace)
        if len(self._recent) == self._recent.maxlen:
            self._loss.recent_evicted += 1
        self._recent.append(trace)
        if trace.is_failure:
            self._failures.append(trace)
        self._export(trace)

    def _record_metric(self, trace: ProjectedTrace) -> None:
        metric = self._routes.get(trace.route_id)
        if metric is None:
            if len(self._routes) >= self._max_routes:
                return  # cardinality ceiling: extra routes go uncounted, bounded
            metric = RouteMetric(route_id=trace.route_id)
            self._routes[trace.route_id] = metric
        metric.count += 1
        if trace.is_failure:
            metric.errors += 1
        metric.duration_us_sum += trace.duration_us
        if trace.duration_us > metric.duration_us_max:
            metric.duration_us_max = trace.duration_us
        metric.buckets[histogram_bucket(trace.duration_us)] += 1

    def _export(self, trace: ProjectedTrace) -> None:
        hook = self._on_trace
        if hook is None:
            return
        try:
            hook(trace)
        except Exception:  # noqa: BLE001 -- a failing exporter must not stall drain
            self._loss.export_error += 1

    # --- snapshots ---------------------------------------------------------

    def snapshot(
        self, *, recent: int | None = None, failures: int | None = None
    ) -> ProjectorSnapshot:
        """A consistent, copied read of the projector's aggregated state."""
        with self._lock:
            recent_items = tuple(self._recent)
            failure_items = tuple(self._failures)
            if recent is not None:
                recent_items = recent_items[-recent:]
            if failures is not None:
                failure_items = failure_items[-failures:]
            routes = tuple(
                RouteMetric(
                    route_id=m.route_id,
                    count=m.count,
                    errors=m.errors,
                    duration_us_sum=m.duration_us_sum,
                    duration_us_max=m.duration_us_max,
                    buckets=list(m.buckets),
                )
                for m in self._routes.values()
            )
            loss = ProjectorLoss(
                orphan_phase=self._loss.orphan_phase,
                orphan_correlation=self._loss.orphan_correlation,
                pending_evicted=self._loss.pending_evicted,
                decode_error=self._loss.decode_error,
                export_error=self._loss.export_error,
                recent_evicted=self._loss.recent_evicted,
            )
            return ProjectorSnapshot(
                assembled=self._assembled,
                recent=recent_items,
                failures=failure_items,
                routes=routes,
                loss=loss,
                pending=len(self._pending),
            )

    def recorder_loss(self) -> dict[LossReason, int]:
        """The recorder's own ring loss counters, read through its accessor.
        These count items dropped *before* the projector ever saw them."""
        return {reason: self._recorder.loss(int(reason)) for reason in LossReason}
