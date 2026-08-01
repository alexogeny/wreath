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

from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: the exporter bridges are imported lazily inside each activate_*
    # function so importing telemetry stays cheap. Borrowing just the alias keeps
    # that property while giving the wrappers below the real parameter contract.
    from ._logscratch import LogSamplingPolicy
    from ._prometheus import RouteLabels

from ._flight_schema import (
    CELL_SIZE,
    HISTOGRAM_BUCKETS,
    PHASE_CELL_BUDGET,
    PHASE_RECORDS_PER_BATCH,
    Mode,
    Severity,
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
    "LoggingConfig",
    "OTLPConfig",
    "TelemetryConfig",
    "MemoryBudget",
    "TelemetryConfigError",
    "SpanContextView",
    "server_span",
    "current_span",
    "bind_propagation",
    "outbound_context",
    "propagates",
    "trace_id_of",
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
#: Ceilings on logging's tables, in the same spirit: generous provisional bounds
#: so a misconfiguration cannot ask for unbounded memory (ADR 0021).
_MAX_LOG_SITES = 1 << 20
_MAX_LOG_SCRATCH = 1 << 16
_MAX_LOG_QUEUE = 1 << 22
#: Per-active-slot bookkeeping (context + generation/seqlock), provisional.
_ACTIVE_SLOT_BYTES = 128
#: A single log2 histogram is HISTOGRAM_BUCKETS 64-bit counters.
_HISTOGRAM_BYTES = HISTOGRAM_BUCKETS * 8

# --- logging's fixed tables (measured Python-object footprints) -------------
#
# These four were guesses at plausible shapes until `benchmarks/bench_logging.py
# --suite memory` measured them: it builds each table at its default capacity
# and reads `tracemalloc`'s *current* allocation across the build, so a
# builder's temporaries do not count and what remains is what the table costs to
# hold. Three of the four guesses were low -- a limiter slot by 1.6x, a queued
# record by 1.6x, a buffered record by 1.3x -- and low is the dangerous
# direction for a budget, because it under-reports what a configuration will
# actually reserve. Each is now the measured figure rounded up.
#
# They remain estimates rather than exact reservations, and the emitter moving
# to C did not change that: these describe Python *tables*, not the packing.
# Re-measure with the suite above when the objects change shape.

#: One interned call site: the site object, its template and event-name strings,
#: its declared fields, and the flattened spec blob the native emitter walks.
#: Measured 425.9B at capacity 4096 on CPython 3.14 (2026-07-28).
_LOG_SITE_BYTES = 448
#: One limiter slot: tick, count, dropped, plus its dict entry.
#: Measured 153.3B.
_LOG_LIMITER_SLOT_BYTES = 160
#: One queued record awaiting the writer: a `ProjectedLog` around one `LogCell`
#: around its packed `LogArg`s. Measured 405.5B.
_LOG_QUEUED_RECORD_BYTES = 416
#: One buffered TRACE/DEBUG record held for a possible promotion.
#: Measured 335.1B.
_LOG_SCRATCH_RECORD_BYTES = 352


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
class LoggingConfig:
    """Fixed sizes for `wreath.logging`'s tables.

    Every one is a ceiling with counted overflow, never a growth trigger: a full
    site table degrades to uninterned records, a full scratch buffer drops the
    record, a full writer queue drops and counts. See
    `docs/reference/logging.md`.
    """

    enabled: bool = True
    #: Severity at and above which a record is published immediately.
    level: Severity = Severity.INFO
    #: Severity floor. Below it a call does nothing at all; between it and
    #: `level` a record is buffered for promotion when a request is in flight,
    #: and dropped when one is not. The two thresholds are separate because
    #: failure-triggered logging needs verbose records to be *created* while
    #: staying unpublished -- a single level cannot express that.
    capture_level: Severity = Severity.DEBUG
    #: Per-call-site rate limiting. None uses the default policy; disable it
    #: with `LogSamplingPolicy(enabled=False)`.
    sampling: LogSamplingPolicy | None = None
    #: Interned call sites. Overflow -> LossReason.LOG_SITE_TABLE_FULL.
    site_capacity: int = 4096
    #: Sites the per-call-site limiter tracks. Beyond it, records pass.
    limiter_capacity: int = 4096
    #: TRACE/DEBUG records held per request awaiting a promotion.
    scratch_budget: int = 64
    #: Records queued for the writer thread. Overflow -> a counted drop.
    writer_queue: int = 8192

    def __post_init__(self) -> None:
        _require(self.site_capacity >= 1, "site_capacity must be >= 1")
        _require(self.site_capacity <= _MAX_LOG_SITES, "site_capacity is too large")
        _require(self.limiter_capacity >= 0, "limiter_capacity must be >= 0")
        _require(self.limiter_capacity <= _MAX_LOG_SITES, "limiter_capacity is too large")
        _require(self.scratch_budget >= 0, "scratch_budget must be >= 0")
        _require(self.scratch_budget <= _MAX_LOG_SCRATCH, "scratch_budget is too large")
        _require(self.writer_queue >= 0, "writer_queue must be >= 0")
        _require(self.writer_queue <= _MAX_LOG_QUEUE, "writer_queue is too large")
        _require(
            self.capture_level <= self.level,
            "capture_level must not exceed level; a floor above the publish "
            "threshold would buffer records that can never be published",
        )

    def memory_bytes(self, active_requests: int) -> int:
        """Approximate fixed footprint. Zero when logging is disabled.

        `active_requests` bounds how many per-request scratch buffers can be
        live at once, so the worst case is every in-flight request holding a
        full buffer.
        """
        if not self.enabled:
            return 0
        return (
            self.site_capacity * _LOG_SITE_BYTES
            + self.limiter_capacity * _LOG_LIMITER_SLOT_BYTES
            + self.writer_queue * _LOG_QUEUED_RECORD_BYTES
            + active_requests * self.scratch_budget * _LOG_SCRATCH_RECORD_BYTES
        )


@dataclass(frozen=True, slots=True)
class MemoryBudget:
    """The fixed bytes a configuration reserves, by component.

    Every component but `logging` is exact -- native code reserves precisely
    that many bytes. `logging` is an estimate over Python objects; its field
    documents why.
    """

    active_slots: int
    ring: int
    histograms: int
    phase_scratch: int
    capture: int
    export_queue: int
    #: Logging's fixed tables: the interned call sites, the per-call-site
    #: limiter, and the writer hand-off queue.
    #:
    #: **This one is an estimate, and the others are not.** The recorder's
    #: reservations above are exact because native code allocates exactly that
    #: many bytes; logging's tables are Python objects, so this is their
    #: approximate footprint from the per-entry constants above. It is here
    #: rather than absent because a budget that silently omits a component is
    #: worse than one that says which part it is estimating.
    #:
    #: It used to say it would become exact once the emitter moved to C. The
    #: emitter has, and it did not: what these constants describe is the site
    #: table, the limiter, the writer queue and the per-request scratch, none of
    #: which were ever the packing. They are measured now instead --
    #: `benchmarks/bench_logging.py --suite memory` -- which is the honest
    #: version of the same intent.
    logging: int = 0

    @property
    def total(self) -> int:
        return (
            self.active_slots
            + self.ring
            + self.histograms
            + self.phase_scratch
            + self.capture
            + self.export_queue
            + self.logging
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
    #: Fixed sizes for `wreath.logging`'s tables.
    logging: LoggingConfig = field(default_factory=LoggingConfig)
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
    #: Map the ring from this file instead of the heap, so the records survive a
    #: process that dies badly. Deliberately **not** tied to Forensic mode: a
    #: crash is worth reconstructing whether or not anyone armed request
    #: capture, and requiring `Mode.FORENSIC` would mean paying for slab pools
    #: and redaction machinery to answer "what was it doing when it died".
    #:
    #: This is not durability. The mapping survives the *process*; it does not
    #: survive a machine losing power before the pages are written back. Read it
    #: with `wreath.recording.read_ring_file`, or `wreath flight read`.
    ring_path: str | None = None

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

        _require(
            self.ring_path is None or self.ring_records > 0,
            "ring_path maps the ring from a file, so it needs a non-empty ring",
        )

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
            logging=self.logging.memory_bytes(self.active_requests),
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
    correlation source when it is present.

    What it *means* depends on which constructor handed it over. From
    `current_span` it is the **incoming (remote) parent** parsed from
    `traceparent`. From `server_span` it is this request's **own server span**,
    read from the recorder's flight context on the native path and falling back
    to the incoming parent where that id is not exposed. Anything parenting work
    under "the span that caused this" wants `server_span`, and that is what
    outbound propagation and the durable queue both carry.
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


_HEX = frozenset("0123456789abcdef")


def trace_id_of(traceparent: str | None) -> str | None:
    """The 32-hex trace id out of a stored `traceparent`, or `None`.

    The inverse of `SpanContextView.traceparent`, and the reader every durable
    row needs: what is stored is the whole interchange string, because that is
    what pastes into a tracing UI, but what an operator *searches* by is the
    trace id alone. `wreath jobs`, `wreath passes status` and `wreath doctor
    trace` all go through here so they agree on what a trace id is.

    Anything that is not a well-formed `traceparent` reads as `None` rather than
    raising. These are forensic surfaces: they run against whatever is actually
    in the table, including a row written by a build that is not this one, and a
    lookup that crashes on a malformed value is worse than one that reports it
    has nothing. An all-zero id is invalid per the W3C spec and is refused here
    too -- it is the shape a broken instrumentation emits, and treating it as a
    real id would join every such row to every other.
    """
    if not traceparent:
        return None
    parts = traceparent.split("-")
    if len(parts) != 4 or len(parts[1]) != 32:
        return None
    ident = parts[1]
    if not _HEX.issuperset(ident) or ident == "0" * 32:
        return None
    return ident


#: The serialized `(traceparent, tracestate)` an outbound call should carry, or
#: `None` outside a propagating request. Serialized rather than structured
#: because it is written once per request and read once per outbound call, and
#: the wire format is what both ends want.
outbound_context: ContextVar[tuple[str, str] | None] = ContextVar(
    "wreath_outbound_context", default=None
)

#: Whether anything in this process could *carry this request's context past
#: its own boundary*. Latched by `HTTPClient.__init__`, by `JobRunner` and by
#: `MessageBus`, and read before the request path binds anything -- the same shape as
#: `_nplusone.WATCHING`, and for the same reason: an application with no
#: outbound seam must not pay a `ContextVar.set` per request to discover that it
#: has nothing to propagate to.
#:
#: It started as "could make an outbound HTTP call" and widened when the queue
#: became the second seam, then the durable bus the third. The distinction that
#: matters is *causal*, not transport: enqueuing a durable job or publishing a
#: durable message hands work to a later process exactly as a client call hands
#: it to another service, and a trace that stops at the queue loses the same
#: link for the same reason.
PROPAGATING = False


def propagates() -> None:
    """Arm context propagation. Called when a seam that can carry it is built.

    Idempotent and never cleared: a process that has ever constructed such a
    seam keeps the latch, because the cost it guards is one `ContextVar.set` and
    the alternative -- reference-counting live seams -- would be a far larger
    mechanism than the thing it saves.

    **Three sites arm it**: `HTTPClient.__init__`, `JobRunner.__init__` and
    `MessageBus.__init__`. Because the latch is a process global that is never
    cleared, any *measurement* over it is order-dependent -- a test that happens
    to build a client arms propagation for an unrelated scenario, which is how
    the request-boundary gate once passed alone and failed under `pytest -n`.
    `_devtools/request_trace.py` sets the latch from the app in front of it and
    restores it afterwards, and a fourth arming site has to keep that working.
    """
    global PROPAGATING
    PROPAGATING = True


def bind_propagation(request: object) -> object | None:
    """Bind this request's outgoing trace context, if anything could send it.

    Returns the `ContextVar` token to reset, or `None` when nothing was bound.

    The context is the request's **owned server span** (`server_span`), not the
    incoming remote parent: work this request causes is a child of *this*
    server's span, and on the native path that id is real rather than inferred.
    Where the recorder cannot supply one -- the pure and bare-ASGI paths --
    `server_span` already falls back to the incoming parent, which keeps the
    trace joined even though the parentage is one level coarser.
    """
    if not PROPAGATING:
        return None
    # One guard, not two: `traceparent()` returns None exactly when the view is
    # invalid, so an `is_valid` check before it is a second spelling of the same
    # question. (A bounded mutant sweep found the pair mutually redundant --
    # neither could be made to matter.)
    parent = server_span(request).traceparent()
    if parent is None:
        # Bind *None* rather than returning early. A context can outlive one
        # request -- keep-alive hands the next request the same one -- and a
        # request that carries no trace of its own must not inherit the last
        # one's. Returning without setting leaks request A's parent onto
        # request B's outbound calls, which is a misattribution, and a trace
        # that points at the wrong cause is worse than no trace at all.
        # Overwriting unconditionally costs one `set` on an untraced request
        # and makes the staleness unrepresentable rather than merely unlikely.
        return outbound_context.set(None)
    state = ""
    getter = getattr(request, "header", None)
    if callable(getter):
        state = getter("tracestate") or ""
    return outbound_context.set((parent, state))


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
