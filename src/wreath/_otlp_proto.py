"""The OTLP messages, declared, plus the OTLP/JSON dict -> protobuf bridge.

`_otlp.py` builds OTLP **JSON** dicts, and those are the shape every builder,
test and reader in this repository already speaks. Rather than fork those
builders to emit two shapes, this module declares the wire messages once and
converts the dict it is handed.

That keeps the JSON path exactly as it was -- it is still a supported encoding
and still selectable -- and puts the whole protobuf question in one place.

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
a list. `_from_json` reads those rules off the plan rather than off a second
declaration that could drift from it.

## What is declared here

Only what `_otlp.py` emits. `AnyValue` carries its four scalar arms and not
`array_value`/`kvlist_value`, because nothing builds those -- and a field that
is never set is better absent than declared and untested. An emitter that
starts producing them adds them here, and the omission is loud rather than
silent because the converter refuses a key it has no field for.
"""

from __future__ import annotations

from typing import Any

from ._protobuf_plan import (
    FLAG_REPEATED,
    KIND_BYTES,
    KIND_FIXED64,
    KIND_INT64,
    KIND_MESSAGE,
    KIND_SFIXED64,
    KIND_SINT64,
    KIND_UINT64,
)
from .protobuf import encode as _encode
from .protobuf import field, message

# -- the JSON representation of each wire kind -------------------------------
#
# Read off the compiled plan, so the rule lives next to the kinds it applies to
# rather than being restated per field.

#: Kinds whose JSON form is a decimal *string* (proto3 JSON, because a JSON
#: number is a double and cannot hold the whole 64-bit range).
_STRING_ENCODED = frozenset(
    {
        KIND_INT64,
        KIND_UINT64,
        KIND_SINT64,
        KIND_FIXED64,
        KIND_SFIXED64,
    }
)


def _camel(name: str) -> str:
    """`start_time_unix_nano` -> `startTimeUnixNano`, the proto3 JSON name."""
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _scalar(kind: int, value: Any) -> Any:
    if kind == KIND_BYTES:
        # OTLP sends ids as hex, not the base64 proto3 JSON would use.
        return bytes.fromhex(value) if isinstance(value, str) else bytes(value)
    if kind in _STRING_ENCODED:
        return int(value)
    return value


def _from_json(cls: type, data: dict[str, Any]) -> Any:
    """Build `cls` from an OTLP/JSON dict, driven by the compiled plan.

    A key with no matching field is a **refusal**, not something to skip: it
    means a builder emitted a field this module has not declared, and silently
    dropping it would export telemetry that quietly lost data.
    """
    # `getattr`, because the plan is attached by `@message` at class creation
    # and a bare `type` carries no static knowledge of it.
    plan, names, holders, _oneofs = getattr(cls, "__wreath_protobuf_plan__")  # noqa: B009
    known = {_camel(name): index for index, name in enumerate(names)}
    unexpected = set(data) - set(known)
    if unexpected:
        raise ValueError(
            f"{cls.__name__} has no field for OTLP/JSON key(s) "
            f"{sorted(unexpected)}; declare them in _otlp_proto.py rather than "
            "exporting a request that drops them"
        )
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        index = known[key]
        _number, kind, flags, _sub = plan[index]
        name = names[index]
        nested = holders[index]
        if flags & FLAG_REPEATED:
            if kind == KIND_MESSAGE:
                kwargs[name] = [_from_json(nested, item) for item in value]
            else:
                kwargs[name] = [_scalar(kind, item) for item in value]
        elif kind == KIND_MESSAGE:
            kwargs[name] = _from_json(nested, value)
        else:
            kwargs[name] = _scalar(kind, value)
    return cls(**kwargs)


# -- common.proto ------------------------------------------------------------


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


# -- resource.proto ----------------------------------------------------------


@message
class Resource:
    attributes: list[KeyValue] = field(1)


# -- trace.proto -------------------------------------------------------------


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


# -- metrics.proto -----------------------------------------------------------


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


# -- logs.proto --------------------------------------------------------------


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


# -- the three entry points --------------------------------------------------


def encode_traces(request: dict[str, Any]) -> bytes:
    """An OTLP/JSON trace request dict as `ExportTraceServiceRequest` bytes."""
    return _encode(_from_json(ExportTraceServiceRequest, request))


def encode_metrics(request: dict[str, Any]) -> bytes:
    """An OTLP/JSON metrics request dict as `ExportMetricsServiceRequest` bytes."""
    return _encode(_from_json(ExportMetricsServiceRequest, request))


def encode_logs(request: dict[str, Any]) -> bytes:
    """An OTLP/JSON logs request dict as `ExportLogsServiceRequest` bytes."""
    return _encode(_from_json(ExportLogsServiceRequest, request))
