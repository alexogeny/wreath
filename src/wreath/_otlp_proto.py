"""The OTLP messages, declared, plus the OTLP/JSON dict -> protobuf bridge.

`_otlp.py` builds OTLP JSON-shaped dictionaries. The C projection engine maps
those dictionaries through these declarations and the protobuf codec writes
the wire message.

## The mapping is stated once

OTLP/JSON is proto3 JSON with two OTLP-specific rules, and both are the kind of
thing that is easy to get subtly wrong thirty times if written out per message:

* **64-bit integers travel as decimal strings**, because JSON numbers are
  doubles and `2^53` is not far enough. Every 64-bit kind is read with `int()`.
* **Trace and span ids travel as lowercase hex**, not the base64 that proto3
  JSON specifies for `bytes`. This is an OTLP departure and it is what
  `_otlp.py` emits.

Everything else follows from the compiled plan: a 32-bit int or an enum is a
JSON number, a double is a number, a message recurses, and a repeated field is
a list.

## What is declared here

Only what `_otlp.py` emits. `AnyValue` carries its four scalar arms and not
`array_value`/`kvlist_value`, because nothing builds those -- and a field that
is never set is better absent than declared and untested. An emitter that
starts producing them adds them here, and the omission is loud rather than
silent because the converter refuses a key it has no field for.
"""

from __future__ import annotations

from typing import Any

from ._native import _core
from .protobuf import field, message


@message
class AnyValue:
    string_value: str | None = field(1)
    bool_value: bool | None = field(2)
    int_value: int | None = field(3, kind="int64")
    double_value: float | None = field(4)


@message
class KeyValue:
    key: str = field(1)
    value: AnyValue | None = field(2)


@message
class InstrumentationScope:
    name: str = field(1)
    version: str = field(2)


@message
class Resource:
    attributes: list[KeyValue] = field(1)


@message
class Status:
    message: str = field(2)
    code: int = field(3, kind="int32")


@message
class Span:
    trace_id: bytes = field(1)
    span_id: bytes = field(2)
    parent_span_id: bytes = field(4)
    name: str = field(5)
    kind: int = field(6, kind="int32")
    start_time_unix_nano: int = field(7, kind="fixed64")
    end_time_unix_nano: int = field(8, kind="fixed64")
    attributes: list[KeyValue] = field(9)
    status: Status | None = field(15)


@message
class ScopeSpans:
    scope: InstrumentationScope | None = field(1)
    spans: list[Span] = field(2)


@message
class ResourceSpans:
    resource: Resource | None = field(1)
    scope_spans: list[ScopeSpans] = field(2)


@message
class ExportTraceServiceRequest:
    resource_spans: list[ResourceSpans] = field(1)


@message
class NumberDataPoint:
    start_time_unix_nano: int = field(2, kind="fixed64")
    time_unix_nano: int = field(3, kind="fixed64")
    as_int: int | None = field(6, kind="sfixed64")
    attributes: list[KeyValue] = field(7)


@message
class Sum:
    data_points: list[NumberDataPoint] = field(1)
    aggregation_temporality: int = field(2, kind="int32")
    is_monotonic: bool = field(3)


@message
class Buckets:
    offset: int = field(1, kind="sint32")
    bucket_counts: list[int] = field(2, kind="uint64")


@message
class ExponentialHistogramDataPoint:
    attributes: list[KeyValue] = field(1)
    start_time_unix_nano: int = field(2, kind="fixed64")
    time_unix_nano: int = field(3, kind="fixed64")
    count: int = field(4, kind="fixed64")
    sum: float | None = field(5)
    scale: int = field(6, kind="sint32")
    zero_count: int = field(7, kind="uint64")
    positive: Buckets | None = field(8)
    max: float | None = field(13)


@message
class ExponentialHistogram:
    data_points: list[ExponentialHistogramDataPoint] = field(1)
    aggregation_temporality: int = field(2, kind="int32")


@message
class Metric:
    name: str = field(1)
    unit: str = field(3)
    sum: Sum | None = field(7)
    exponential_histogram: ExponentialHistogram | None = field(10)


@message
class ScopeMetrics:
    scope: InstrumentationScope | None = field(1)
    metrics: list[Metric] = field(2)


@message
class ResourceMetrics:
    resource: Resource | None = field(1)
    scope_metrics: list[ScopeMetrics] = field(2)


@message
class ExportMetricsServiceRequest:
    resource_metrics: list[ResourceMetrics] = field(1)


@message
class LogRecord:
    time_unix_nano: int = field(1, kind="fixed64")
    severity_number: int = field(2, kind="int32")
    severity_text: str = field(3)
    body: AnyValue | None = field(5)
    attributes: list[KeyValue] = field(6)
    trace_id: bytes = field(9)
    span_id: bytes = field(10)
    observed_time_unix_nano: int = field(11, kind="fixed64")
    event_name: str = field(12)


@message
class ScopeLogs:
    scope: InstrumentationScope | None = field(1)
    log_records: list[LogRecord] = field(2)


@message
class ResourceLogs:
    resource: Resource | None = field(1)
    scope_logs: list[ScopeLogs] = field(2)


@message
class ExportLogsServiceRequest:
    resource_logs: list[ResourceLogs] = field(1)


def encode_traces(request: dict[str, Any]) -> bytes:
    """An OTLP/JSON trace request dict as `ExportTraceServiceRequest` bytes."""
    return _core.protobuf_encode_otlp_json(ExportTraceServiceRequest, request)


def encode_projected_traces(
    traces: Any,
    *,
    image: Any = None,
    resource_attributes: dict[str, str] | None = None,
) -> bytes:
    """Projected traces encoded directly as OTLP trace protobuf bytes."""
    return _core.protobuf_encode_otlp_traces(traces, image, resource_attributes)


def encode_metrics(request: dict[str, Any]) -> bytes:
    """An OTLP/JSON metrics request dict as `ExportMetricsServiceRequest` bytes."""
    return _core.protobuf_encode_otlp_json(ExportMetricsServiceRequest, request)


def encode_projected_metrics(
    snapshot: Any,
    *,
    image: Any = None,
    start_unix_nano: int,
    now_unix_nano: int,
    resource_attributes: dict[str, str] | None = None,
) -> bytes:
    """A projector snapshot encoded directly as OTLP metric protobuf bytes."""
    return _core.protobuf_encode_otlp_metrics(
        snapshot,
        image,
        start_unix_nano,
        now_unix_nano,
        resource_attributes,
    )


def encode_logs(request: dict[str, Any]) -> bytes:
    """An OTLP/JSON logs request dict as `ExportLogsServiceRequest` bytes."""
    return _core.protobuf_encode_otlp_json(ExportLogsServiceRequest, request)


def encode_projected_logs(
    records: Any,
    *,
    registry: Any,
    resource_attributes: dict[str, str] | None = None,
) -> bytes:
    """Projected log records encoded directly as OTLP protobuf bytes."""
    return _core.protobuf_encode_otlp_logs(records, registry, resource_attributes)
