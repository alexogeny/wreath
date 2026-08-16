"""OTLP projection for the Native Flight Recorder (Stage 4, slice 4b).

The recorder's neutral cells stay the source model; OpenTelemetry names, span
shapes, and OTLP wire structures live only here, in the projector's adapter
layer. This module maps the reassembled `ProjectedTrace`
objects (and a `ProjectorSnapshot`) to OTLP/JSON
request dicts -- `ExportTraceServiceRequest` and `ExportMetricsServiceRequest`
in the protobuf JSON encoding -- with **no dependency on any OpenTelemetry SDK**.
Building these plain dicts needs only the standard library, so the mapping is
fully unit-testable; a concrete transport (the optional adapter/dependency group
and the server-lifespan wiring) rides slice 4c on top of the `SpanExporter`
protocol and `BoundedExportQueue` defined here.

**Timestamps.** A span's wall clock is anchored on
`ProjectedTrace.observed_unix_nano`: `end = observed` and
`start = observed - duration`. Each completion cell carries its monotonic end
instant (`end_offset_ms` from the worker's clock epoch), and the projector maps
it to Unix time through the recorder's calibration pair -- so `observed` is the
request's true completion instant, drift-free (no wall-clock jumps, no
drain-latency skew), to millisecond precision.

**Cardinality.** Span names and attributes come only from route *metadata* -- the
method and the route *template* (`/users/{id}`), never the concrete path, query
values, header values, user/tenant IDs, or SQL. That keeps names and any future
metric labels low-cardinality by construction.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final
from typing import Protocol as _Protocol

from ._flight_schema import (
    FLAG_AI_SCRAPING_REFUSED,
    FLAG_POLICY_REFUSED,
    ClientFactFlag,
    MetadataImage,
    PhaseKind,
    Protocol,
    severity_text,
)
from ._logsite import SiteRegistry
from ._logsite import attributes as log_attributes
from ._logsite import render as log_render
from ._projector import ProjectedLog, ProjectedTrace, ProjectorSnapshot, RouteMetric, _mix64
from .queue import Queue

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


def _bool(key: str, value: bool) -> dict[str, Any]:
    return {"key": key, "value": {"boolValue": value}}


def _hex_trace(trace_id: int) -> str:
    return format(trace_id & ((1 << 128) - 1), "032x")


def _hex_span(span_id: int) -> str:
    return format(span_id & ((1 << 64) - 1), "016x")


def _trace_ids(trace: ProjectedTrace) -> tuple[int, int]:
    """The (trace_id, span_id) to export.

    Delegates to `ProjectedTrace.effective_ids` so spans and log records for one
    request cannot disagree about its identity -- they used to be able to,
    because the synthesis for an unpropagated request lived only here.
    """
    return trace.effective_ids


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
    if trace.flags & FLAG_POLICY_REFUSED:
        attributes.append(_bool("wreath.policy.refused", True))
        attributes.append(
            _str(
                "wreath.policy.disposition",
                "ai_scraping"
                if trace.flags & FLAG_AI_SCRAPING_REFUSED
                else "refused",
            )
        )
    client = trace.client_facts
    if client is not None:
        facts = ClientFactFlag(client.flags)
        attributes.append(
            _bool("wreath.client.agent.claimed", bool(facts & ClientFactFlag.BOT_CLAIMED))
        )
        attributes.append(
            _bool(
                "wreath.client.agent.verified",
                bool(facts & ClientFactFlag.AGENT_VERIFIED),
            )
        )
        attributes.append(
            _bool("wreath.user_agent.classified", bool(facts & ClientFactFlag.UA_KNOWN))
        )
        if client.user_agent_rule_id:
            attributes.append(
                _int("wreath.user_agent.rule_id", client.user_agent_rule_id)
            )
        if facts & ClientFactFlag.BOT_CLAIMED:
            attributes.append(_str("user_agent.synthetic.type", "bot"))
        if facts & ClientFactFlag.MOBILE_KNOWN:
            attributes.append(
                _bool("browser.mobile", bool(facts & ClientFactFlag.MOBILE))
            )
        if facts & ClientFactFlag.IP_KNOWN:
            attributes.append(
                _str(
                    "network.type",
                    "ipv6" if facts & ClientFactFlag.IPV6 else "ipv4",
                )
            )
            attributes.append(
                _str(
                    "wreath.client.address_source",
                    "forwarded"
                    if facts & ClientFactFlag.IP_FORWARDED
                    else "socket",
                )
            )
        if client.country is not None:
            attributes.append(_str("geo.country.iso_code", client.country))

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
    """Map reassembled traces to an OTLP `ExportTraceServiceRequest` dict.

    Each completion becomes one SERVER span; its detail phases become child spans
    (CLIENT for dependency calls, INTERNAL otherwise). Returns an empty request
    (no `resourceSpans`) when there is nothing to export.
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
    histogram at scale 0 (bucket i covers roughly `(2^i, 2^(i+1)]`
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
    """Map per-route aggregates to an OTLP `ExportMetricsServiceRequest` dict:
    a cumulative request-count Sum (with an error count) and a request-duration
    ExponentialHistogram, both keyed by the low-cardinality `http.route`."""
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
    """An OTLP Resource dict. Defaults `service.name` to `wreath` unless the
    caller overrides it."""
    attrs = {"service.name": "wreath"}
    if attributes:
        attrs.update(attributes)
    return {"attributes": [_str(key, value) for key, value in attrs.items()]}


# --- export contract --------------------------------------------------------


class SpanExporter(_Protocol):
    """The minimal contract a trace exporter satisfies. The concrete OTLP/HTTP
    adapter (slice 4c, optional dependency group) implements this; tests use a
    collecting stub. `export` must not raise on the projector's behalf -- the
    queue isolates it -- but it may signal a permanent failure by raising, which
    the queue counts as a drop."""

    def export(self, request: dict[str, Any]) -> None: ...


class MetricExporter(_Protocol):
    def export(self, request: dict[str, Any]) -> None: ...


#: The hand-off between the projector thread and the exporter drainer.
#:
#: See the note on `_logsink.BoundedLogQueue`: these two were the same class
#: written twice, and both are now `wreath.queue.Queue`. A slow or stalled
#: exporter still cannot grow memory without bound or stall the drain, and the
#: loss it causes is still counted rather than silent -- that policy moved into
#: the primitive rather than being restated here.
BoundedExportQueue = Queue


# --- log mapping ------------------------------------------------------------
#
# Logs are the third signal on the transport that already carries traces and
# metrics. The mapping is a projection rather than a translation because the log
# cell was laid out against the OTel data model in the first place: severity is
# already a SeverityNumber, the interned site is already an EventName, and
# correlation comes from the projector's join rather than from a context lookup.


def _log_attributes(registry: SiteRegistry, record: ProjectedLog) -> list[dict[str, Any]]:
    """A record's declared arguments as OTLP attributes.

    Redacted arguments arrive already reduced to a fingerprint or a length, so
    there is no disclosure decision left to make here -- the value the exporter
    sees is the only value that ever existed off the request path.
    """
    attributes: list[dict[str, Any]] = []
    for key, value in log_attributes(registry, record.cell).items():
        if isinstance(value, bool):
            attributes.append({"key": key, "value": {"boolValue": value}})
        elif isinstance(value, int):
            attributes.append(_int(key, value))
        elif isinstance(value, float):
            attributes.append({"key": key, "value": {"doubleValue": value}})
        else:
            attributes.append(_str(key, str(value)))
    if record.cell.dropped_siblings:
        attributes.append(
            _int("wreath.dropped_siblings", record.cell.dropped_siblings)
        )
    if record.route_id:
        attributes.append(_int("wreath.route_id", record.route_id))
    return attributes


def build_logs_request(
    records: Iterable[ProjectedLog],
    *,
    registry: SiteRegistry,
    resource_attributes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Map projected log records to an OTLP `ExportLogsServiceRequest` dict.

    Returns an empty request (no `resourceLogs`) when there is nothing to
    export. Records are grouped under one shared scope entry rather than
    repeating scope metadata per record, as the protocol intends.
    """
    log_records: list[dict[str, Any]] = []
    for record in records:
        cell = record.cell
        site = registry.get(cell.site_id)
        stamp = str(record.observed_unix_nano)
        item: dict[str, Any] = {
            "timeUnixNano": stamp,
            "observedTimeUnixNano": stamp,
            "severityNumber": int(cell.severity),
            "severityText": severity_text(cell.severity),
            "body": {"stringValue": log_render(registry, cell)},
        }
        if site is not None:
            item["eventName"] = site.event_name
        # OTLP forbids an all-zero id; a record with no correlation omits both
        # rather than exporting zeros that mean "unset" to nobody.
        if record.has_correlation:
            item["traceId"] = _hex_trace(record.trace_id)
            item["spanId"] = _hex_span(record.span_id)
        attributes = _log_attributes(registry, record)
        if attributes:
            item["attributes"] = attributes
        log_records.append(item)
    if not log_records:
        return {"resourceLogs": []}
    return {
        "resourceLogs": [
            {
                "resource": resource(resource_attributes),
                "scopeLogs": [{"scope": _SCOPE, "logRecords": log_records}],
            }
        ]
    }
