"""OTLP projection for the Native Flight Recorder (Stage 4, slice 4b).

The recorder's neutral cells stay the source model; OpenTelemetry names, span
shapes, and OTLP wire structures live only here, in the projector's adapter
layer. This module maps the reassembled :class:`~wreath._projector.ProjectedTrace`
objects (and a :class:`~wreath._projector.ProjectorSnapshot`) to OTLP/JSON
request dicts -- ``ExportTraceServiceRequest`` and ``ExportMetricsServiceRequest``
in the protobuf JSON encoding -- with **no dependency on any OpenTelemetry SDK**.
Building these plain dicts needs only the standard library, so the mapping is
fully unit-testable; a concrete transport (the optional adapter/dependency group
and the server-lifespan wiring) rides slice 4c on top of the :class:`SpanExporter`
protocol and :class:`BoundedExportQueue` defined here.

**Timestamps.** A span's wall clock is anchored on
:attr:`ProjectedTrace.observed_unix_nano`: ``end = observed`` and
``start = observed - duration``. Each completion cell carries its monotonic end
instant (``end_offset_ms`` from the worker's clock epoch), and the projector maps
it to Unix time through the recorder's calibration pair -- so ``observed`` is the
request's true completion instant, drift-free (no wall-clock jumps, no
drain-latency skew), to millisecond precision.

**Cardinality.** Span names and attributes come only from route *metadata* -- the
method and the route *template* (``/users/{id}``), never the concrete path, query
values, header values, user/tenant IDs, or SQL. That keeps names and any future
metric labels low-cardinality by construction.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from typing import Any, Final
from typing import Protocol as _Protocol

from ._flight_schema import (
    MetadataImage,
    PhaseKind,
    Protocol,
)
from ._projector import ProjectedTrace, ProjectorSnapshot, RouteMetric

__all__ = [
    "SpanExporter",
    "MetricExporter",
    "BoundedExportQueue",
    "build_trace_request",
    "build_metrics_request",
    "resource",
]

# OTLP span kinds and status codes (proto enum values).
_KIND_INTERNAL: Final = 1
_KIND_SERVER: Final = 2
_KIND_CLIENT: Final = 3
_STATUS_UNSET: Final = 0
_STATUS_ERROR: Final = 2

_SCOPE: Final = {"name": "wreath.flight"}

#: Phase kinds that represent an outbound/dependency call get CLIENT spans; the
#: rest are INTERNAL segments of the server span.
_CLIENT_PHASES: Final = frozenset(
    {
        PhaseKind.DB_POOL_WAIT,
        PhaseKind.DB_QUERY,
        PhaseKind.ORM_HYDRATE,
        PhaseKind.HTTP_CLIENT,
    }
)

_PROTOCOL_VERSION: Final = {
    Protocol.HTTP1: "1.1",
    Protocol.HTTP2: "2",
    Protocol.HTTP3: "3",
}


# --- OTLP/JSON value helpers -----------------------------------------------
#
# OTLP's JSON encoding carries 64-bit integers and timestamps as decimal
# *strings* to survive JSON's double-precision number range.


def _str(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def _int(key: str, value: int) -> dict[str, Any]:
    return {"key": key, "value": {"intValue": str(value)}}


def _hex_trace(trace_id: int) -> str:
    return format(trace_id & ((1 << 128) - 1), "032x")


def _hex_span(span_id: int) -> str:
    return format(span_id & ((1 << 64) - 1), "016x")


def _mix64(value: int) -> int:
    """A splitmix64 finalizer: a stateless, well-dispersed map used to synthesize
    stable IDs from a request id (mirrors the recorder's own arming mixer)."""
    value &= (1 << 64) - 1
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & ((1 << 64) - 1)
    return value ^ (value >> 31)


def _trace_ids(trace: ProjectedTrace) -> tuple[int, int]:
    """The (trace_id, span_id) to export. A propagated request carries real IDs;
    an unpropagated one has none, so synthesize deterministic, non-zero IDs from
    its request/worker id (OTLP forbids all-zero IDs)."""
    if trace.has_correlation:
        return trace.trace_id, trace.span_id
    seed = (trace.request_id << 8) ^ trace.worker_id
    lo = _mix64(seed)
    hi = _mix64(seed ^ 0xD1B54A32D192ED03)
    span = _mix64(seed ^ 0x9E3779B97F4A7C15) or 1
    return ((hi << 64) | lo) or 1, span


def _phase_span_id(parent_span_id: int, sequence: int) -> int:
    """A deterministic child span id derived from the parent and the phase's
    sequence -- phases carry no span id of their own."""
    return _mix64(parent_span_id ^ (0x9E3779B97F4A7C15 * (sequence + 1))) or 1


# --- span mapping -----------------------------------------------------------


class _Routes:
    """Small route/dependency lookups over a metadata image (or an empty view)."""

    __slots__ = ("_routes", "_names")

    def __init__(self, image: MetadataImage | None) -> None:
        self._routes: dict[int, Any] = {}
        self._names: dict[int, str] = {}
        if image is not None:
            self._routes = {r.route_id: r for r in image.routes}
            for table in (image.databases, image.clients, image.dependencies):
                for row in table:
                    self._names.setdefault(row.entry_id, row.name)

    def route(self, route_id: int) -> Any | None:
        return self._routes.get(route_id)

    def dependency(self, dependency_id: int) -> str | None:
        return self._names.get(dependency_id)


def _server_span(trace: ProjectedTrace, routes: _Routes) -> dict[str, Any]:
    trace_id, span_id = _trace_ids(trace)
    end = trace.observed_unix_nano
    start = end - trace.duration_us * 1000
    route = routes.route(trace.route_id)

    if route is not None:
        name = f"{route.method} {route.path}"
    elif trace.protocol is Protocol.WEBSOCKET:
        name = "WEBSOCKET"
    else:
        name = "HTTP"

    attributes: list[dict[str, Any]] = [
        _int("wreath.route_id", trace.route_id),
        _int("wreath.plan_id", trace.plan_id),
        _str("wreath.terminal", trace.terminal.name.lower()),
        _int("http.request.body.size", trace.bytes_in),
        _int("http.response.body.size", trace.bytes_out),
    ]
    if trace.protocol is Protocol.WEBSOCKET:
        attributes.append(_str("network.protocol.name", "websocket"))
    else:
        attributes.append(_str("network.protocol.name", "http"))
        version = _PROTOCOL_VERSION.get(trace.protocol)
        if version is not None:
            attributes.append(_str("network.protocol.version", version))
    if trace.status:
        attributes.append(_int("http.response.status_code", trace.status))
    if route is not None:
        attributes.append(_str("http.request.method", route.method))
        attributes.append(_str("http.route", route.path))
    if trace.error_class:
        attributes.append(_str("error.type", f"class:{trace.error_class}"))

    span: dict[str, Any] = {
        "traceId": _hex_trace(trace_id),
        "spanId": _hex_span(span_id),
        "name": name,
        "kind": _KIND_SERVER,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": attributes,
    }
    if trace.parent_span_id:
        span["parentSpanId"] = _hex_span(trace.parent_span_id)
    if trace.is_failure:
        span["status"] = {"code": _STATUS_ERROR, "message": trace.terminal.name.lower()}
    else:
        span["status"] = {"code": _STATUS_UNSET}
    return span


def _phase_spans(
    trace: ProjectedTrace, parent_trace_id: int, parent_span_id: int, routes: _Routes
) -> list[dict[str, Any]]:
    parent_start = trace.observed_unix_nano - trace.duration_us * 1000
    spans: list[dict[str, Any]] = []
    for phase in trace.phases:
        start = parent_start + phase.start_offset_us * 1000
        end = start + phase.duration_us * 1000
        kind = _KIND_CLIENT if phase.phase_id in _CLIENT_PHASES else _KIND_INTERNAL
        attributes: list[dict[str, Any]] = [
            _str("wreath.phase", phase.phase_id.name.lower()),
            _str("wreath.coverage", phase.coverage.name.lower()),
        ]
        if phase.dependency_id:
            attributes.append(_int("wreath.dependency_id", phase.dependency_id))
            name = routes.dependency(phase.dependency_id)
            if name is not None:
                attributes.append(_str("wreath.dependency", name))
        spans.append(
            {
                "traceId": _hex_trace(parent_trace_id),
                "spanId": _hex_span(_phase_span_id(parent_span_id, phase.sequence)),
                "parentSpanId": _hex_span(parent_span_id),
                "name": phase.phase_id.name.lower(),
                "kind": kind,
                "startTimeUnixNano": str(start),
                "endTimeUnixNano": str(end),
                "attributes": attributes,
            }
        )
    return spans


def build_trace_request(
    traces: Iterable[ProjectedTrace],
    *,
    image: MetadataImage | None = None,
    resource_attributes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Map reassembled traces to an OTLP ``ExportTraceServiceRequest`` dict.

    Each completion becomes one SERVER span; its detail phases become child spans
    (CLIENT for dependency calls, INTERNAL otherwise). Returns an empty request
    (no ``resourceSpans``) when there is nothing to export.
    """
    routes = _Routes(image)
    spans: list[dict[str, Any]] = []
    for trace in traces:
        trace_id, span_id = _trace_ids(trace)
        spans.append(_server_span(trace, routes))
        spans.extend(_phase_spans(trace, trace_id, span_id, routes))
    if not spans:
        return {"resourceSpans": []}
    return {
        "resourceSpans": [
            {
                "resource": resource(resource_attributes),
                "scopeSpans": [{"scope": _SCOPE, "spans": spans}],
            }
        ]
    }


# --- metric mapping ---------------------------------------------------------


def _exponential_histogram(metric: RouteMetric) -> dict[str, Any]:
    """The recorder's base-2 log buckets are exactly an OTLP exponential
    histogram at scale 0 (bucket i covers roughly ``(2^i, 2^(i+1)]``
    microseconds), so map them directly rather than reconstructing boundaries."""
    counts = metric.buckets
    first = next((i for i, c in enumerate(counts) if c), None)
    if first is None:
        offset, bucket_counts = 0, []
    else:
        last = max(i for i, c in enumerate(counts) if c)
        offset = first
        bucket_counts = [str(counts[i]) for i in range(first, last + 1)]
    return {
        "scale": 0,
        "zeroCount": "0",
        "count": str(metric.count),
        "sum": float(metric.duration_us_sum),
        "max": float(metric.duration_us_max),
        "positive": {"offset": offset, "bucketCounts": bucket_counts},
    }


def build_metrics_request(
    snapshot: ProjectorSnapshot,
    *,
    image: MetadataImage | None = None,
    start_unix_nano: int,
    now_unix_nano: int,
    resource_attributes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Map per-route aggregates to an OTLP ``ExportMetricsServiceRequest`` dict:
    a cumulative request-count Sum (with an error count) and a request-duration
    ExponentialHistogram, both keyed by the low-cardinality ``http.route``."""
    routes = _Routes(image)
    count_points: list[dict[str, Any]] = []
    duration_points: list[dict[str, Any]] = []
    for metric in snapshot.routes:
        route = routes.route(metric.route_id)
        attributes = [_int("wreath.route_id", metric.route_id)]
        if route is not None:
            attributes.append(_str("http.route", route.path))
        window = {
            "startTimeUnixNano": str(start_unix_nano),
            "timeUnixNano": str(now_unix_nano),
            "attributes": attributes,
        }
        count_points.append({**window, "asInt": str(metric.count)})
        error_attrs = [*attributes, _str("wreath.outcome", "error")]
        count_points.append(
            {
                "startTimeUnixNano": str(start_unix_nano),
                "timeUnixNano": str(now_unix_nano),
                "attributes": error_attrs,
                "asInt": str(metric.errors),
            }
        )
        duration_points.append({**window, **_exponential_histogram(metric)})

    if not count_points:
        return {"resourceMetrics": []}
    metrics = [
        {
            "name": "http.server.request.count",
            "unit": "{request}",
            "sum": {
                "aggregationTemporality": 2,  # CUMULATIVE
                "isMonotonic": True,
                "dataPoints": count_points,
            },
        },
        {
            "name": "http.server.request.duration",
            "unit": "us",
            "exponentialHistogram": {
                "aggregationTemporality": 2,  # CUMULATIVE
                "dataPoints": duration_points,
            },
        },
    ]
    return {
        "resourceMetrics": [
            {
                "resource": resource(resource_attributes),
                "scopeMetrics": [{"scope": _SCOPE, "metrics": metrics}],
            }
        ]
    }


def resource(attributes: dict[str, str] | None) -> dict[str, Any]:
    """An OTLP Resource dict. Defaults ``service.name`` to ``wreath`` unless the
    caller overrides it."""
    attrs = {"service.name": "wreath"}
    if attributes:
        attrs.update(attributes)
    return {"attributes": [_str(key, value) for key, value in attrs.items()]}


# --- export contract --------------------------------------------------------


class SpanExporter(_Protocol):
    """The minimal contract a trace exporter satisfies. The concrete OTLP/HTTP
    adapter (slice 4c, optional dependency group) implements this; tests use a
    collecting stub. ``export`` must not raise on the projector's behalf -- the
    queue isolates it -- but it may signal a permanent failure by raising, which
    the queue counts as a drop."""

    def export(self, request: dict[str, Any]) -> None: ...


class MetricExporter(_Protocol):
    def export(self, request: dict[str, Any]) -> None: ...


class BoundedExportQueue:
    """A fixed-capacity, thread-safe hand-off between the projector thread (which
    offers finished traces) and an exporter drainer. Offering to a full queue
    drops the item and counts it (the plan's bounded export queue with visible
    loss), so a slow or stalled exporter can never grow memory without bound or
    stall the drain.
    """

    __slots__ = ("_items", "_lock", "_dropped", "_offered")

    def __init__(self, capacity: int = 4096) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._items: deque[ProjectedTrace] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._dropped = 0
        self._offered = 0

    def offer(self, trace: ProjectedTrace) -> bool:
        """Enqueue a trace, returning False (and counting a drop) if full. Safe
        to hand directly as the projector's ``on_trace`` hook."""
        with self._lock:
            self._offered += 1
            if len(self._items) == self._items.maxlen:
                self._dropped += 1
                return False
            self._items.append(trace)
            return True

    def drain(self, max_items: int | None = None) -> list[ProjectedTrace]:
        """Remove and return up to ``max_items`` queued traces (all if None)."""
        with self._lock:
            if max_items is None or max_items >= len(self._items):
                batch = list(self._items)
                self._items.clear()
                return batch
            return [self._items.popleft() for _ in range(max(0, max_items))]

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def offered(self) -> int:
        with self._lock:
            return self._offered

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
