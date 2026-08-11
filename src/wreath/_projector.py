"""Asynchronous projection for the Native Flight Recorder (Stage 4).

The recorder's request path only ever *publishes* fixed cells into a single
writer / single reader ring. Turning those cells back into whole requests --
joining a completion to its correlation carrier and detail phases, aggregating
route metrics, and handing finished traces to an exporter -- is work that must
never touch a request stack. This module is that work: a `Projector`
owns one background thread that drains the ring in bounded batches, reassembles
traces, keeps a bounded window of recent completions (and failures) for the
Inspector, and offers each finished trace to an optional export hook whose
failures are isolated to a counter.

Cell decoding and dispatch run as one bounded C operation over each drained
buffer. Assembly state remains owned by this projector instance. The OTLP
mapping and server lifespan wiring build on the export hook and snapshot API;
nothing in this file imports an exporter SDK.

**Assembly ordering.** `context_end` publishes a request's cells in the fixed
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
from dataclasses import dataclass, field, replace
from typing import Any, Final
from typing import Protocol as _Protocol

from ._drainthread import DrainThread
from ._flight_schema import (
    FLAG_ERROR_PROMOTED,
    FLAG_SLOW_PROMOTED,
    CompletionCell,
    CorrelationCell,
    LogArg,
    LogArgType,
    LogCell,
    LossReason,
    PhaseBatchCell,
    PhaseCoverage,
    PhaseKind,
    PhaseRecord,
    Protocol,
    SchemaError,
    Severity,
    TerminalStatus,
    histogram_bucket,
)
from ._native import _core

__all__ = [
    "ProjectedLog",
    "ProjectedTrace",
    "RouteMetric",
    "ProjectorSnapshot",
    "ProjectorLoss",
    "Projector",
]

class _RecorderLike(_Protocol):
    """The recorder surface the projector reads, also implemented by test doubles."""

    def drain(self, max_cells: int = ..., /) -> bytes: ...
    def loss(self, reason: int, /) -> int: ...


#: Default projector tuning. All are overridable per `Projector`.
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
    orphan_log: int = 0
    pending_evicted: int = 0
    decode_error: int = 0
    export_error: int = 0
    recent_evicted: int = 0


def _mix64(value: int) -> int:
    """A splitmix64 finalizer: a stateless, well-dispersed map used to synthesize
    stable ids from a request id (mirrors the recorder's own arming mixer)."""
    value &= (1 << 64) - 1
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & ((1 << 64) - 1)
    return value ^ (value >> 31)


@dataclass(frozen=True, slots=True)
class ProjectedLog:
    """One log record with whatever correlation the projector could join to it.

    Lives here rather than in `_logsink` for the same reason `ProjectedTrace`
    does: the projector produces it and the sinks consume it, so the dependency
    points one way.

    A record that is not request-scoped (startup, shutdown, a background job)
    carries zeros and is delivered as soon as it is drained -- it must never be
    held waiting for a completion that will not arrive.
    """

    cell: LogCell
    trace_id: int = 0
    span_id: int = 0
    route_id: int = 0
    #: Wall-clock time (Unix nanoseconds) the projector observed this record.
    observed_unix_nano: int = 0

    @property
    def has_correlation(self) -> bool:
        return self.trace_id != 0 or self.span_id != 0


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
    #: Log records emitted during this request, joined by request id. Ordered as
    #: the ring published them.
    logs: tuple[LogCell, ...] = ()
    #: Wall-clock time (Unix nanoseconds) the projector finalized this trace.
    #: The completion cell carries only a duration, so this observation time is
    #: the anchor an exporter uses to place the span on a wall clock:
    #: `start ~= observed_unix_nano - duration_us*1000`. It is later than the
    #: true completion instant by at most one drain interval.
    observed_unix_nano: int = 0

    @property
    def has_correlation(self) -> bool:
        return self.trace_id != 0 or self.span_id != 0

    @property
    def effective_ids(self) -> tuple[int, int]:
        """The (trace_id, span_id) every consumer should use for this request.

        A propagated request carries real ids. An unpropagated one has none, so
        deterministic non-zero ids are synthesized from its request and worker
        id: OTLP forbids all-zero ids, and more importantly a log record and the
        span for the same request must agree, which they only do if both ask
        here rather than each inventing an answer.
        """
        if self.has_correlation:
            return self.trace_id, self.span_id
        seed = (self.request_id << 8) ^ self.worker_id
        lo = _mix64(seed)
        hi = _mix64(seed ^ 0xD1B54A32D192ED03)
        span = _mix64(seed ^ 0x9E3779B97F4A7C15) or 1
        return ((hi << 64) | lo) or 1, span

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


class Projector:
    """Drains a recorder's ring on a background thread and reassembles traces.

    The projector is inert until `start`; construction allocates only its
    bounded buffers. `on_trace`, when given, is called with each finished
    `ProjectedTrace` on the projector thread; any exception it raises is
    swallowed and counted (`export_error`) so a failing exporter can never
    stall the drain or perturb the request path.
    """

    __slots__ = (
        "_recorder",
        "_epoch_unix_ns",
        "_max_cells",
        "_on_trace",
        "_on_log",
        "_on_cells",
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
        "_cell_config",
        "_drain",
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
        on_log: Callable[[ProjectedLog], None] | None = None,
        on_cells: Callable[[bytes], None] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        if max_cells <= 0:
            raise ValueError("max_cells must be positive")
        self._recorder = recorder
        # Clock calibration: (epoch_mono_ns, epoch_unix_ns). A completion's
        # end_offset_ms maps to Unix time as epoch_unix + end_offset_ms*1e6, which
        # is drift-free (no wall-clock jumps, no drain-latency skew). Recorders
        # without the accessor fall back to stamping the wall clock at finalize.
        calibration = getattr(recorder, "clock_calibration", None)
        self._epoch_unix_ns = calibration[1] if calibration else 0
        self._max_cells = max_cells
        self._on_trace = on_trace
        self._on_log = on_log
        #: The archival stream: every drained cell, before assembly. The ring
        #: refuses once full, so anything wanting more history than one ring
        #: holds has to be fed here.
        self._on_cells = on_cells
        self._max_routes = max_routes
        # Guards every mutable buffer below: the drain thread writes under it,
        # snapshot()/metrics() read under it. Held only for O(batch) work.
        self._lock = threading.Lock()
        self._pending: dict[int, Any] = {}
        self._max_pending = pending
        self._recent: deque[ProjectedTrace] = deque(maxlen=recent)
        self._failures: deque[ProjectedTrace] = deque(maxlen=failures)
        self._routes: dict[int, RouteMetric] = {}
        self._loss = ProjectorLoss()
        self._assembled = 0
        self._cycle = 0
        self._cell_config = (
            CompletionCell,
            CorrelationCell,
            PhaseBatchCell,
            LogCell,
            SchemaError,
            _core.FlightAssembly,
            Protocol,
            TerminalStatus,
            PhaseRecord,
            PhaseKind,
            PhaseCoverage,
            LogArg,
            LogArgType,
            Severity,
        )
        self._drain = DrainThread(
            "wreath-flight-projector", interval, self.poll, self._flush
        )

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the drain thread. Idempotent; a stopped projector can restart."""
        self._drain.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the drain thread to finish, join it, and drain once more.

        A final drain on the way out flushes whatever the ring still holds, so
        shutdown does not silently strand recorded cells.
        """
        self._drain.stop(timeout)

    def _flush(self) -> None:
        """The last drain, after the writer paths are quiesced by the caller.

        Settle *twice*: a completion seen on the final cycle still needs a
        second pass to pick up its tail, so one poll would strand exactly the
        requests that finished last.
        """
        self.poll()
        self.poll()

    # --- draining and assembly --------------------------------------------

    def poll(self) -> int:
        """Run one drain cycle synchronously: drain, ingest, settle. Returns the
        number of traces finalized this cycle. Exposed for deterministic tests
        and for the shutdown flush; the thread calls it on the interval."""
        raw = self._recorder.drain(self._max_cells)
        self._archive(raw)
        with self._lock:
            self._cycle += 1
            self._ingest(raw)
            return self._settle()

    def _ingest(self, raw: bytes) -> None:
        """Decode a drained buffer and fold each cell into the pending table.
        Nothing finalizes here -- a completion settles only after a quiet cycle
        (see `_settle`), so its whole tail has a chance to arrive first."""
        self._loss.decode_error += _core.flight_project_cells(
            self, raw, self._cell_config
        )

    def _emit_standalone_log(self, cell: LogCell) -> None:
        """Deliver a non-request log immediately after native decoding."""
        if cell.request_id != 0:
            raise ValueError(
                "standalone flight log must have request_id 0; "
                f"received {cell.request_id}"
            )
        self._emit_log(ProjectedLog(cell=cell, observed_unix_nano=time.time_ns()))

    def _settle(self) -> int:
        """Finalize completions first seen in an earlier cycle (their tail has
        had a full cycle to arrive) and retire stale partials as orphans."""
        return _core.flight_settle(self, self._pending, self._loss, self._cycle)

    def _evict_oldest_pending(self) -> None:
        """Drop the pending entry seen longest ago to bound the table. A
        completion evicted this way is a lost trace; a headless partial is an
        orphan. Either way it is categorized, never silent."""
        _core.flight_evict_pending(self._pending, self._loss)

    def _finalize(self, request_id: int, entry: Any) -> None:
        completion = entry.completion
        if completion is None:
            raise ValueError(
                f"flight assembly {request_id} has no completion cell to finalize"
            )
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
            logs=tuple(entry.logs),
            # Precise, drift-free wall time from the recorder's calibration and
            # the cell's monotonic end offset; fall back to stamping now.
            observed_unix_nano=(
                self._epoch_unix_ns + completion.end_offset_ms * 1_000_000
                if self._epoch_unix_ns
                else time.time_ns()
            ),
        )
        self._assembled += 1
        self._record_metric(trace)
        if len(self._recent) == self._recent.maxlen:
            self._loss.recent_evicted += 1
        self._recent.append(trace)
        if trace.is_failure:
            self._failures.append(trace)
        self._export(trace)
        self._export_logs(trace)

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

    def set_cell_archive(self, hook: Callable[[bytes], None] | None) -> None:
        """Point the archival stream at a sink, after construction.

        The recording sink is built after the projector -- it needs the app's
        metadata image, which is not available until later in startup -- so the
        wiring runs in this direction rather than through the constructor. The
        same shape as `ExportPipeline.set_log_registry`, and for the same
        reason: a hook that exists later than the object that calls it.
        """
        self._on_cells = hook

    def _archive(self, raw: bytes) -> None:
        """Hand the drained bytes to the archival stream, before assembly.

        The ring holds `ring_records` cells and then refuses; a recording that
        wants more history than that has to be fed as the ring is drained, which
        is here. It runs *before* ingestion deliberately: assembly discards what
        it cannot join, and an archive is supposed to be what happened rather
        than what could be reassembled.

        Outside the lock, because the sink only appends to its own file and
        holding the projector's lock across a write would put a disk between
        every consumer and the drain.
        """
        hook = self._on_cells
        if hook is None or not raw:
            return
        try:
            hook(raw)
        except Exception:  # noqa: BLE001 - archival sink; counted in export_error
            # Same posture as `_export`: the cells have already happened, and a
            # sink that cannot write them must not stall the drain that feeds
            # the trace assembly, the writer and the exporters. Counted rather
            # than logged, so a broken archive is a rising number.
            with self._lock:
                self._loss.export_error += 1

    def _export(self, trace: ProjectedTrace) -> None:
        hook = self._on_trace
        if hook is None:
            return
        try:
            hook(trace)
        except Exception:  # noqa: BLE001 - user exporter; counted in loss.export_error
            # `hook` is an application-supplied exporter and may raise anything.
            # This is a *publish* site: the trace it describes has already
            # happened, so a failing exporter must not stall the drain that
            # feeds every other consumer. Counted rather than logged, so a
            # permanently broken exporter is a rising number rather than
            # silence -- the `MessageBus` shape.
            self._loss.export_error += 1

    def _export_logs(self, trace: ProjectedTrace) -> None:
        """Offer each of a settled request's records with its correlation.

        Called after `_export` so a consumer that reads both sees the trace
        before the records that belong to it.
        """
        if self._on_log is None or not trace.logs:
            return
        # `effective_ids`, not the raw fields: an unpropagated request has no
        # correlation cell, and a record that reported zeros while the span
        # export reported synthesized ids would make the two signals disagree
        # about the same request -- worse than either being absent.
        trace_id, span_id = trace.effective_ids
        for cell in trace.logs:
            self._emit_log(
                ProjectedLog(
                    cell=cell,
                    trace_id=trace_id,
                    span_id=span_id,
                    route_id=trace.route_id,
                    observed_unix_nano=trace.observed_unix_nano,
                )
            )

    def _emit_log(self, record: ProjectedLog) -> None:
        hook = self._on_log
        if hook is None:
            return
        try:
            hook(record)
        except Exception:  # noqa: BLE001 - user sink; counted in loss.export_error
            # Same shape and same reasoning as `_export`: the record already
            # happened, and a sink that raises must not stop the drain that
            # feeds the trace pipeline alongside it.
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
            # `replace` with no changes copies *every* field, including ones
            # added later. The field-by-field copy this used to be silently
            # dropped `orphan_log` when it was introduced -- the counter rose
            # and the snapshot reported zero, which is the exact failure mode a
            # loss counter exists to prevent.
            loss = replace(self._loss)
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
