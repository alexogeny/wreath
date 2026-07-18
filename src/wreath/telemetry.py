"""Wreath telemetry — native metrics, tracing configuration, and OpenTelemetry integration.

Stage 0 of the Native Flight Recorder (NFR) exposes *configuration and value
types only*. There is no recorder, ring, exporter, or request-path behavior yet:
constructing a :class:`TelemetryConfig` validates it and lets you compute its
exact fixed memory budget, nothing more. The runtime spine lands in later stages
behind ``wreath._native._flight``.

See ``docs/plans/native-flight-recorder-stage-1.md`` (modes, sizing) and
``docs/decisions/0021-native-flight-recorder-provisional-parameters.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

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
]

#: Hard ceilings so a misconfiguration cannot ask for unbounded memory. These are
#: generous provisional bounds (ADR 0021), not tuning targets.
_MAX_RING_RECORDS = 1 << 24  # 16 Mi cells (~1 GiB at 64 B) upper sanity bound
_MAX_ACTIVE_REQUESTS = 1 << 20
_MAX_PHASE_SLOTS = 1 << 20
_MAX_ROUTE_HISTOGRAMS = 1 << 16
_MAX_CAPTURE_SLABS = 1 << 16
_MAX_CAPTURE_BYTES = 1 << 34  # 16 GiB
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
        """The number of histograms this policy allocates for ``route_count`` routes."""
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

    Constructing it never starts anything; it is a value passed to ``Wreath(...)``
    or ``configure_telemetry(...)`` in a later stage. Validation here is the whole
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
