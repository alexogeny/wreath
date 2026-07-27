"""Wreath telemetry — native metrics, tracing configuration, and OpenTelemetry integration.

This module carries the Native Flight Recorder's public configuration and value
types (`TelemetryConfig` and friends): constructing one validates it and
lets you compute its exact fixed memory budget. Passing it to `wreath.server`
creates a native recorder and starts the off-path projector that drains its ring.

It also hosts the lazy OpenTelemetry bridge (`current_span`,
`activate_otel`): the request path never constructs a Python OTel object,
so these let user code opt in only at the call site, degrading to an immutable
`SpanContextView` when no OTel packages are installed. The runtime spine
(worker, ring, projector, exporter) lives behind `wreath._native._flight`,
`wreath._projector`, `wreath._otlp`, and `wreath._export`.

See `docs/plans/native-flight-recorder-stage-1.md` (modes, sizing) and
`docs/decisions/0021-native-flight-recorder-provisional-parameters.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: the exporter bridges are imported lazily inside each activate_*
    # function so importing telemetry stays cheap. Borrowing just the alias keeps
    # that property while giving the wrappers below the real parameter contract.
    from ._prometheus import RouteLabels

from ._flight_schema import (
    CELL_SIZE,
    HISTOGRAM_BUCKETS,
    PHASE_CELL_BUDGET,
    PHASE_RECORDS_PER_BATCH,
    Mode,
)

#: One phase-scratch block holds a request's whole phase budget laid out as
#: ring-ready 64-byte batch cells: BUDGET / RECORDS_PER_BATCH cells.
PHASE_BLOCK_BYTES = (PHASE_CELL_BUDGET // PHASE_RECORDS_PER_BATCH) * CELL_SIZE

__all__ = [
    "Mode",
    "PerRoutePolicy",
    "HistogramConfig",
    "SamplingPolicy",
    "PropagationConfig",
    "OTLPConfig",
    "TelemetryConfig",
    "MemoryBudget",
    "TelemetryConfigError",
    "SpanContextView",
    "server_span",
    "current_span",
    "activate_otel",
    "activate_prometheus",
    "activate_openmetrics",
    "activate_statsd",
    "activate_cloudwatch_emf",
]

#: Hard ceilings so a misconfiguration cannot ask for unbounded memory. These are
#: generous provisional bounds (ADR 0021), not tuning targets.
_MAX_RING_RECORDS = 1 << 24  # 16 Mi cells (~1 GiB at 64 B) upper sanity bound
_MAX_ACTIVE_REQUESTS = 1 << 20
_MAX_PHASE_SLOTS = 1 << 20
_MAX_ROUTE_HISTOGRAMS = 1 << 16
_MAX_CAPTURE_SLABS = 1 << 16
_MAX_CAPTURE_BYTES = 1 << 30  # 1 GiB per recorder/worker
_MAX_EXPORT_QUEUE = 1 << 20
#: Per-active-slot bookkeeping (context + generation/seqlock), provisional.
_ACTIVE_SLOT_BYTES = 128
#: A single log2 histogram is HISTOGRAM_BUCKETS 64-bit counters.
_HISTOGRAM_BYTES = HISTOGRAM_BUCKETS * 8


class TelemetryConfigError(ValueError):
    """A telemetry configuration is invalid (overflow, cardinality, unbounded)."""


class PerRoutePolicy(StrEnum):
    """How route-level histograms are allocated."""

    GLOBAL = "global"  # one shared histogram, no per-route cardinality
    SELECTED = "selected"  # only explicitly selected routes get one
    CAPPED = "capped"  # every route, but bounded by max_route_histograms


@dataclass(frozen=True, slots=True)
class HistogramConfig:
    per_route: PerRoutePolicy = PerRoutePolicy.GLOBAL
    #: Cap for CAPPED / SELECTED policies. Ignored for GLOBAL.
    max_route_histograms: int = 256

    def __post_init__(self) -> None:
        if not isinstance(self.per_route, PerRoutePolicy):
            object.__setattr__(self, "per_route", PerRoutePolicy(self.per_route))
        _require(self.max_route_histograms >= 0, "max_route_histograms must be >= 0")
        _require(
            self.max_route_histograms <= _MAX_ROUTE_HISTOGRAMS,
            f"max_route_histograms exceeds {_MAX_ROUTE_HISTOGRAMS}",
        )

    def histogram_count(self, route_count: int) -> int:
        """The number of histograms this policy allocates for `route_count` routes."""
        if self.per_route is PerRoutePolicy.GLOBAL:
            return 1
        if self.per_route is PerRoutePolicy.SELECTED:
            return 1 + min(route_count, self.max_route_histograms)
        # CAPPED: reject rather than silently truncate cardinality.
        if route_count > self.max_route_histograms:
            raise TelemetryConfigError(
                f"capped per-route histograms need {route_count} slots but the cap "
                f"is {self.max_route_histograms}; raise the cap or use 'selected'"
            )
        return 1 + route_count


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    """Deterministic sampling for Detailed/Forensic arming."""

    rate: float = 0.0

    def __post_init__(self) -> None:
        _require(0.0 <= self.rate <= 1.0, "sampling rate must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class PropagationConfig:
    """W3C trace-context handling. Stage 0 stores intent only."""

    accept_incoming: bool = True
    emit_outgoing: bool = True
    #: Copy tracestate/baggage through. Off by default (never a metric label).
    propagate_tracestate: bool = False


@dataclass(frozen=True, slots=True)
class OTLPConfig:
    """Off-path OTLP export settings. No exporter runs in Stage 0."""

    enabled: bool = False
    endpoint: str | None = None
    export_queue: int = 4096
    batch_size: int = 512
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        _require(self.export_queue >= 0, "export_queue must be >= 0")
        _require(self.export_queue <= _MAX_EXPORT_QUEUE, "export_queue is too large")
        _require(self.batch_size >= 0, "batch_size must be >= 0")
        _require(
            self.export_queue == 0 or self.batch_size <= self.export_queue,
            "batch_size cannot exceed export_queue",
        )
        _require(self.timeout_seconds >= 0, "timeout_seconds must be >= 0")


@dataclass(frozen=True, slots=True)
class MemoryBudget:
    """The exact fixed bytes a configuration reserves, by component."""

    active_slots: int
    ring: int
    histograms: int
    phase_scratch: int
    capture: int
    export_queue: int

    @property
    def total(self) -> int:
        return (
            self.active_slots
            + self.ring
            + self.histograms
            + self.phase_scratch
            + self.capture
            + self.export_queue
        )


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    """An immutable, validated telemetry configuration.

    Constructing it never starts anything; it is a value passed to `Wreath(...)`
    or `configure_telemetry(...)` in a later stage. Validation here is the whole
    point of Stage 0: reject overflow, unbounded cardinality, and invalid modes
    before any native memory is ever reserved.
    """

    mode: Mode = Mode.OFF
    completion_summaries: bool = True
    ring_records: int = 16_384
    active_requests: int = 2_048
    #: Concurrent armed (Detailed/Forensic) requests that can hold phase scratch.
    #: Sized to the sampled subset, not to active_requests; exhaustion drops phases.
    phase_slots: int = 256
    histograms: HistogramConfig = field(default_factory=HistogramConfig)
    detailed: SamplingPolicy = field(default_factory=SamplingPolicy)
    forensic: SamplingPolicy = field(default_factory=SamplingPolicy)
    #: A Detailed completion at or beyond this many microseconds is flagged
    #: SLOW_PROMOTED; 0 disables the latency trigger. Errors/timeouts are always
    #: flagged ERROR_PROMOTED in Detailed mode. Promotion flags the completion
    #: cell only -- it cannot recover phases that were not armed.
    detailed_slow_us: int = 0
    propagation: PropagationConfig = field(default_factory=PropagationConfig)
    otlp: OTLPConfig = field(default_factory=OTLPConfig)
    #: Preallocated forensic capture, only meaningful in Forensic mode.
    capture_slabs: int = 0
    slab_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.mode, Mode):
            try:
                object.__setattr__(self, "mode", Mode(self.mode))
            except ValueError as exc:
                raise TelemetryConfigError(f"invalid mode {self.mode!r}") from exc
        _require(self.ring_records >= 0, "ring_records must be >= 0")
        _require(self.ring_records <= _MAX_RING_RECORDS, "ring_records is too large")
        # A power-of-two ring lets the writer mask instead of divide.
        _require(
            self.ring_records == 0 or (self.ring_records & (self.ring_records - 1)) == 0,
            "ring_records must be a power of two",
        )
        _require(self.active_requests >= 0, "active_requests must be >= 0")
        _require(self.active_requests <= _MAX_ACTIVE_REQUESTS, "active_requests is too large")
        _require(self.phase_slots >= 0, "phase_slots must be >= 0")
        _require(self.phase_slots <= _MAX_PHASE_SLOTS, "phase_slots is too large")
        _require(self.detailed_slow_us >= 0, "detailed_slow_us must be >= 0")
        _require(self.capture_slabs >= 0, "capture_slabs must be >= 0")
        _require(self.capture_slabs <= _MAX_CAPTURE_SLABS, "capture_slabs is too large")
        _require(self.slab_bytes >= 0, "slab_bytes must be >= 0")

        if self.mode is Mode.OFF:
            return
        _require(
            self.ring_records > 0 or not self.completion_summaries,
            "Pulse with completion summaries needs a non-empty ring",
        )
        _require(self.active_requests > 0, "a non-Off mode needs active_requests > 0")
        if self.mode is Mode.FORENSIC:
            _require(self.capture_slabs > 0, "Forensic mode needs capture_slabs > 0")
            total = self.capture_slabs * self.slab_bytes
            _require(total <= _MAX_CAPTURE_BYTES, "capture budget exceeds the ceiling")

    def memory_budget(self, route_count: int = 0) -> MemoryBudget:
        """Compute exact fixed memory. Raises on unbounded cardinality; this is the
        config-validation acceptance gate."""
        if route_count < 0:
            raise TelemetryConfigError("route_count must be >= 0")
        histogram_count = self.histograms.histogram_count(route_count)
        budget = MemoryBudget(
            active_slots=self.active_requests * _ACTIVE_SLOT_BYTES,
            ring=self.ring_records * CELL_SIZE,
            histograms=histogram_count * _HISTOGRAM_BYTES,
            phase_scratch=(
                self.phase_slots * PHASE_BLOCK_BYTES if self.mode >= Mode.DETAILED else 0
            ),
            capture=(
                self.capture_slabs * self.slab_bytes
                if self.mode is Mode.FORENSIC
                else 0
            ),
            export_queue=(self.otlp.export_queue * CELL_SIZE if self.otlp.enabled else 0),
        )
        # Guard against a computed budget that is itself implausible.
        if budget.total > (1 << 40):
            raise TelemetryConfigError(
                f"computed memory budget {budget.total} bytes exceeds 1 TiB"
            )
        return budget


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TelemetryConfigError(message)


# --- lazy OpenTelemetry bridge (Stage 4c) -----------------------------------
#
# The recorder generates and carries trace/span IDs in native code; the request
# path never constructs a Python OpenTelemetry object. These helpers let user
# code or third-party instrumentation *opt in* to the OTel API when it wants to,
# creating an SDK object only at the call site -- never merely by entering a
# handler. With no OTel packages installed they degrade to an immutable native
# view. The native recorder never reads a Python context variable.


@dataclass(frozen=True, slots=True)
class SpanContextView:
    """An immutable, dependency-free view of a request's W3C trace context.

    This is what the bridge returns when the OpenTelemetry API is absent, and the
    correlation source when it is present. It reflects the *incoming* (remote)
    context parsed from `traceparent`; exposing the server's own generated span
    id to Python needs a native read seam and is deferred, so instrumentation
    treats this as the remote parent to child-span under.
    """

    trace_id: int = 0  # 128-bit; 0 when the request is unpropagated
    span_id: int = 0  # incoming parent span; 0 when unpropagated
    sampled: bool = False

    @property
    def is_valid(self) -> bool:
        return self.trace_id != 0 and self.span_id != 0

    @property
    def trace_id_hex(self) -> str:
        return format(self.trace_id, "032x")

    @property
    def span_id_hex(self) -> str:
        return format(self.span_id, "016x")

    def traceparent(self) -> str | None:
        """The W3C `traceparent` string for this context, or None if invalid."""
        if not self.is_valid:
            return None
        return f"00-{self.trace_id_hex}-{self.span_id_hex}-{'01' if self.sampled else '00'}"


def current_span(request: object) -> SpanContextView:
    """The current request's W3C trace context as an immutable view.

    Reads only the incoming `traceparent` header and constructs no
    OpenTelemetry object; returns an empty (invalid) view for an unpropagated or
    malformed request. Safe to call whether or not telemetry is enabled.
    """
    header = None
    getter = getattr(request, "header", None)
    if callable(getter):
        header = getter("traceparent")
    if not header:
        return SpanContextView()
    from ._pure.flight import parse_traceparent

    parsed = parse_traceparent(header.encode("ascii", "ignore"))
    if parsed is None:
        return SpanContextView()
    hi, lo, span, sampled = parsed
    return SpanContextView(trace_id=(hi << 64) | lo, span_id=span, sampled=sampled)


def server_span(request: object) -> SpanContextView:
    """The request's *owned server span* — the span the recorder generated for
    this request (a child of the incoming parent), within the incoming trace.

    On the native server this reads the generated span id straight from the
    request's flight context, so an app that parents its own spans here gets the
    same server span the recorder exports over OTLP. When no recorder is attached
    (or on the pure/bare-ASGI path where the id is not exposed) it falls back to
    the incoming remote context.
    """
    context = getattr(request, "_context", None)
    reader = getattr(context, "_flight_server_span", None)
    if callable(reader):
        hi, lo, span = reader()
        trace_id = (hi << 64) | lo
        if trace_id != 0 and span != 0:
            incoming = current_span(request)
            sampled = incoming.sampled if incoming.is_valid else True
            return SpanContextView(trace_id=trace_id, span_id=span, sampled=sampled)
    return current_span(request)


def activate_otel(request: object) -> object:
    """Lazily hand the request's trace context to the OpenTelemetry API.

    When the OTel API is importable and the request carries a valid context, this
    returns an OTel `Context` holding the request's *owned server span* (see
    `server_span`), so app instrumentation parents its spans under the same
    server span the recorder exports -- not the incoming remote parent. When OTel
    is absent, or the request is unpropagated, it returns the native
    `SpanContextView`. It creates an SDK object only here, at the call site
    -- never on the request path -- so an app that never calls it pays nothing.
    """
    view = server_span(request)
    if not view.is_valid:
        return view
    import importlib

    try:
        otel_trace = importlib.import_module("opentelemetry.trace")
    except ImportError:
        return view
    # The owned server span is a *local* span (in-process), so instrumentation
    # creates children under it; a bare incoming context (the fallback) stays
    # remote. `_context` is present exactly when we resolved the owned span.
    is_owned = getattr(getattr(request, "_context", None), "_flight_server_span", None) is not None
    context = otel_trace.SpanContext(
        trace_id=view.trace_id,
        span_id=view.span_id,
        is_remote=not is_owned,
        trace_flags=otel_trace.TraceFlags(0x01 if view.sampled else 0x00),
    )
    return otel_trace.set_span_in_context(otel_trace.NonRecordingSpan(context))


# --- Prometheus exposition bridge -------------------------------------------
#
# Where the OTLP path (`wreath._otlp`/`wreath._export`) pushes the projector's
# aggregated metrics to a collector, this bridge renders the *same* projector
# snapshot as Prometheus text exposition to be *scraped*. It is opt-in and off the
# request path: a scrape calls `bridge.render()`, which reads one consistent
# `Projector.snapshot()` (plus `recorder_loss()`); an app that never mounts it
# pays nothing. The renderer and format live in `wreath._prometheus`.


def activate_prometheus(
    source: object,
    *,
    namespace: str = "wreath",
    route_labels: RouteLabels = None,
) -> object:
    """Wrap a metrics snapshot source in a Prometheus exposition bridge.

    `source` is a `wreath._projector.Projector` (or anything exposing
    `snapshot()` and optionally `recorder_loss()`). The returned
    `wreath._prometheus.PrometheusBridge` renders Prometheus text
    exposition format 0.0.4 from the projector's per-route counters/errors,
    duration histogram, pending gauge, and loss counters — the same aggregates
    the OTLP exporter reads. Mount `bridge.handler()` (or
    `wreath._prometheus.metrics_router(source)`) at `/metrics` yourself, so
    exposure and any auth gating stay your decision.

    `route_labels` maps a numeric `route_id` to scrape labels (e.g. from the
    metadata image's route table); without it rows are labelled by `route_id`.
    """
    from ._prometheus import PrometheusBridge

    return PrometheusBridge(source, namespace=namespace, route_labels=route_labels)


def activate_openmetrics(
    source: object,
    *,
    namespace: str = "wreath",
    route_labels: RouteLabels = None,
) -> object:
    """Like `activate_prometheus`, but the bridge renders OpenMetrics 1.0.0.

    Same `Projector.snapshot()` aggregates; the exposition drops the `_total`
    suffix from counter `# TYPE` families, terminates with `# EOF`, and
    advertises `application/openmetrics-text; version=1.0.0`.
    """
    from ._prometheus import PrometheusBridge

    return PrometheusBridge(
        source, namespace=namespace, route_labels=route_labels, openmetrics=True,
    )


def activate_statsd(
    source: object,
    *,
    host: str = "127.0.0.1",
    port: int = 8125,
    prefix: str = "wreath",
    dogstatsd: bool = False,
    tags: dict | None = None,
    route_labels: RouteLabels = None,
) -> object:
    """Wrap a snapshot source in a StatsD/DogStatsD UDP push bridge.

    Where Prometheus/OpenMetrics expose the projector aggregates for scrape, this
    *pushes* the same `Projector.snapshot()` state as StatsD lines: counters as
    deltas since the last `flush`, gauges
    absolute. `dogstatsd=True` emits `|#k:v` tags (route/method/path labels);
    plain StatsD folds labels into the metric name. Drive `bridge.flush()` (or
    `bridge.run_periodic(interval)` from a supervised task) yourself.
    """
    from ._statsd import StatsDBridge

    return StatsDBridge(
        source, host=host, port=port, prefix=prefix,
        dogstatsd=dogstatsd, tags=tags, route_labels=route_labels,
    )


def activate_cloudwatch_emf(
    source: object,
    *,
    namespace: str = "Wreath",
    dimensions: dict | None = None,
    route_labels: RouteLabels = None,
    cumulative: bool = False,
) -> object:
    """Wrap a snapshot source in a CloudWatch EMF bridge.

    Renders the same `Projector.snapshot()` aggregates as EMF structured-JSON
    log lines (one blob per route + a global blob); written to stdout, CloudWatch
    Logs turns them into metrics with no agent. Counters are per-period deltas
    (CloudWatch SUMs) unless `cumulative=True`. Call `bridge.emit()` on a
    cadence (or `bridge.render()` for the text).
    """
    from ._cloudwatch_emf import EmfBridge

    return EmfBridge(
        source, namespace=namespace, dimensions=dimensions,
        route_labels=route_labels, cumulative=cumulative,
    )
