from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from wreath import _otlp_proto as proto
from wreath._export import OtlpHttpExporter
from wreath._otlp import build_logs_request, build_metrics_request
from wreath.protobuf import decode

#: (message, python field name, number) transcribed from opentelemetry-proto:
#: common/v1/common.proto, resource/v1/resource.proto, trace/v1/trace.proto,
#: metrics/v1/metrics.proto, logs/v1/logs.proto and the three service protos.
FIELD_NUMBERS = [
    (proto.AnyValue, "string_value", 1),
    (proto.AnyValue, "bool_value", 2),
    (proto.AnyValue, "int_value", 3),
    (proto.AnyValue, "double_value", 4),
    (proto.KeyValue, "key", 1),
    (proto.KeyValue, "value", 2),
    (proto.InstrumentationScope, "name", 1),
    (proto.InstrumentationScope, "version", 2),
    (proto.Resource, "attributes", 1),
    (proto.Status, "message", 2),
    (proto.Status, "code", 3),
    (proto.Span, "trace_id", 1),
    (proto.Span, "span_id", 2),
    (proto.Span, "parent_span_id", 4),
    (proto.Span, "name", 5),
    (proto.Span, "kind", 6),
    (proto.Span, "start_time_unix_nano", 7),
    (proto.Span, "end_time_unix_nano", 8),
    (proto.Span, "attributes", 9),
    (proto.Span, "status", 15),
    (proto.ScopeSpans, "scope", 1),
    (proto.ScopeSpans, "spans", 2),
    (proto.ResourceSpans, "resource", 1),
    (proto.ResourceSpans, "scope_spans", 2),
    (proto.ExportTraceServiceRequest, "resource_spans", 1),
    (proto.NumberDataPoint, "start_time_unix_nano", 2),
    (proto.NumberDataPoint, "time_unix_nano", 3),
    (proto.NumberDataPoint, "as_int", 6),
    (proto.NumberDataPoint, "attributes", 7),
    (proto.Sum, "data_points", 1),
    (proto.Sum, "aggregation_temporality", 2),
    (proto.Sum, "is_monotonic", 3),
    (proto.Buckets, "offset", 1),
    (proto.Buckets, "bucket_counts", 2),
    (proto.ExponentialHistogramDataPoint, "attributes", 1),
    (proto.ExponentialHistogramDataPoint, "start_time_unix_nano", 2),
    (proto.ExponentialHistogramDataPoint, "time_unix_nano", 3),
    (proto.ExponentialHistogramDataPoint, "count", 4),
    (proto.ExponentialHistogramDataPoint, "sum", 5),
    (proto.ExponentialHistogramDataPoint, "scale", 6),
    (proto.ExponentialHistogramDataPoint, "zero_count", 7),
    (proto.ExponentialHistogramDataPoint, "positive", 8),
    (proto.ExponentialHistogramDataPoint, "max", 13),
    (proto.ExponentialHistogram, "data_points", 1),
    (proto.ExponentialHistogram, "aggregation_temporality", 2),
    (proto.Metric, "name", 1),
    (proto.Metric, "unit", 3),
    (proto.Metric, "sum", 7),
    (proto.Metric, "exponential_histogram", 10),
    (proto.ScopeMetrics, "scope", 1),
    (proto.ScopeMetrics, "metrics", 2),
    (proto.ResourceMetrics, "resource", 1),
    (proto.ResourceMetrics, "scope_metrics", 2),
    (proto.ExportMetricsServiceRequest, "resource_metrics", 1),
    (proto.LogRecord, "time_unix_nano", 1),
    (proto.LogRecord, "severity_number", 2),
    (proto.LogRecord, "severity_text", 3),
    (proto.LogRecord, "body", 5),
    (proto.LogRecord, "attributes", 6),
    (proto.LogRecord, "trace_id", 9),
    (proto.LogRecord, "span_id", 10),
    (proto.LogRecord, "observed_time_unix_nano", 11),
    (proto.LogRecord, "event_name", 12),
    (proto.ScopeLogs, "scope", 1),
    (proto.ScopeLogs, "log_records", 2),
    (proto.ResourceLogs, "resource", 1),
    (proto.ResourceLogs, "scope_logs", 2),
    (proto.ExportLogsServiceRequest, "resource_logs", 1),
]


@pytest.mark.parametrize(("cls", "name", "number"), FIELD_NUMBERS)
def test_field_numbers_match_opentelemetry_proto(cls: type, name: str, number: int) -> None:
    plan, names, _holders, _oneofs = cls.__wreath_protobuf_plan__
    assert name in names, f"{cls.__name__} has no field {name!r}"
    assert plan[names.index(name)][0] == number


def test_the_framing_matches_a_hand_computed_vector() -> None:
    raw = proto.encode_traces({"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "x"}]}]}]})
    assert raw == bytes.fromhex("0a07120512032a0178")


def test_ids_travel_as_bytes_not_hex_text() -> None:
    span = {"traceId": "ff" * 16, "spanId": "ab" * 8}
    raw = proto.encode_traces({"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]})
    request = decode(proto.ExportTraceServiceRequest, raw)
    span = request.resource_spans[0].scope_spans[0].spans[0]
    assert span.trace_id == b"\xff" * 16
    assert span.span_id == b"\xab" * 8


def test_sixty_four_bit_values_survive_past_the_double_range() -> None:
    stamp = 1_753_900_000_123_456_789
    assert stamp > 2**53
    raw = proto.encode_traces(
        {"resourceSpans": [{"scopeSpans": [{"spans": [{"startTimeUnixNano": str(stamp)}]}]}]}
    )
    request = decode(proto.ExportTraceServiceRequest, raw)
    assert request.resource_spans[0].scope_spans[0].spans[0].start_time_unix_nano == stamp


def test_a_real_metrics_request_survives_the_conversion() -> None:
    from wreath._projector import ProjectorLoss, ProjectorSnapshot, RouteMetric

    metric = RouteMetric(route_id=3, count=7, errors=2, duration_us_sum=1234, duration_us_max=900)
    metric.buckets[2] = 5
    metric.buckets[3] = 2
    snapshot = ProjectorSnapshot(
        assembled=7,
        recent=(),
        failures=(),
        routes=(metric,),
        loss=ProjectorLoss(),
        pending=0,
    )
    request = build_metrics_request(
        snapshot, start_unix_nano=1_000_000_000, now_unix_nano=2_000_000_000
    )
    decoded = decode(proto.ExportMetricsServiceRequest, proto.encode_metrics(request))

    metrics = decoded.resource_metrics[0].scope_metrics[0].metrics
    assert [m.name for m in metrics] == [
        "http.server.request.count",
        "http.server.request.duration",
    ]
    counts = metrics[0].sum
    assert counts.is_monotonic is True
    assert counts.aggregation_temporality == 2
    assert [p.as_int for p in counts.data_points] == [7, 2]
    assert counts.data_points[0].time_unix_nano == 2_000_000_000

    histogram = metrics[1].exponential_histogram.data_points[0]
    assert histogram.count == 7
    assert histogram.scale == 0
    assert histogram.positive.offset == 2
    assert histogram.positive.bucket_counts == [5, 2]


def test_direct_metric_writer_matches_the_declared_mapping() -> None:
    from wreath._projector import ProjectorLoss, ProjectorSnapshot, RouteMetric

    first = RouteMetric(route_id=3, count=7, errors=2, duration_us_sum=1234, duration_us_max=900)
    first.buckets[2] = 5
    first.buckets[5] = 2
    second = RouteMetric(route_id=9, errors=-1)
    third = RouteMetric(
        route_id=11,
        count=(1 << 63) - 1,
        errors=(1 << 63) - 1,
        duration_us_sum=1 << 60,
        duration_us_max=1 << 59,
    )
    third.buckets[63] = (1 << 64) - 1
    snapshot = ProjectorSnapshot(
        assembled=7,
        recent=(),
        failures=(),
        routes=(first, second, third),
        loss=ProjectorLoss(),
        pending=0,
    )
    image = SimpleNamespace(
        routes=(
            SimpleNamespace(route_id=3, path="/users/{id}"),
            SimpleNamespace(route_id=9, path=""),
        ),
        databases=(),
        clients=(),
        dependencies=(),
    )
    kwargs = {
        "image": image,
        "start_unix_nano": 1_000_000_000,
        "now_unix_nano": 2_000_000_000,
        "resource_attributes": {
            "service.name": "shop",
            "region": "east",
            "empty": "",
        },
    }
    defined = proto.encode_metrics(build_metrics_request(snapshot, **kwargs))
    assert proto.encode_projected_metrics(snapshot, **kwargs) == defined


def test_direct_metric_writer_keeps_an_empty_request_empty() -> None:
    from wreath._projector import ProjectorLoss, ProjectorSnapshot

    snapshot = ProjectorSnapshot(
        assembled=0,
        recent=(),
        failures=(),
        routes=(),
        loss=ProjectorLoss(),
        pending=0,
    )
    assert proto.encode_projected_metrics(snapshot, start_unix_nano=1, now_unix_nano=2) == b""


def test_direct_trace_writer_matches_the_declared_mapping() -> None:
    from wreath._flight_schema import (
        ClientFactFlag,
        ClientFactsCell,
        PhaseCoverage,
        PhaseKind,
        PhaseRecord,
        Protocol,
        TerminalStatus,
    )
    from wreath._otlp import build_trace_request
    from wreath._projector import ProjectedTrace

    traces = (
        ProjectedTrace(
            request_id=1,
            connection_id=1,
            route_id=3,
            plan_id=7,
            worker_id=2,
            duration_us=500,
            status=503,
            terminal=TerminalStatus.ERROR,
            protocol=Protocol.HTTP2,
            error_class=4,
            flags=0,
            bytes_in=12,
            bytes_out=34,
            trace_id=(1 << 127) | 99,
            span_id=(1 << 63) | 7,
            parent_span_id=5,
            client_facts=ClientFactsCell(
                request_id=1,
                flags=(
                    ClientFactFlag.UA_KNOWN
                    | ClientFactFlag.BOT_CLAIMED
                    | ClientFactFlag.AGENT_VERIFIED
                    | ClientFactFlag.IP_KNOWN
                    | ClientFactFlag.GEO_KNOWN
                ),
                user_agent_rule_id=4,
                country="AU",
            ),
            phases=(
                PhaseRecord(
                    phase_id=PhaseKind.DB_QUERY,
                    duration_us=80,
                    start_offset_us=20,
                    dependency_id=11,
                    sequence=0,
                    coverage=PhaseCoverage.EXTERNAL,
                ),
                PhaseRecord(
                    phase_id=PhaseKind.HANDLER,
                    duration_us=100,
                    start_offset_us=120,
                    sequence=1,
                    coverage=PhaseCoverage.PYTHON,
                ),
            ),
            observed_unix_nano=2_000_000_000,
        ),
        ProjectedTrace(
            request_id=42,
            connection_id=2,
            route_id=0,
            plan_id=0,
            worker_id=3,
            duration_us=10,
            status=0,
            terminal=TerminalStatus.OK,
            protocol=Protocol.WEBSOCKET,
            error_class=0,
            flags=0,
            bytes_in=0,
            bytes_out=0,
            observed_unix_nano=3_000_000_000,
        ),
    )
    image = SimpleNamespace(
        routes=(SimpleNamespace(route_id=3, method="GET", path="/users/{id}"),),
        databases=(SimpleNamespace(entry_id=11, name="primary"),),
        clients=(),
        dependencies=(),
    )
    attributes = {"service.name": "shop", "empty": ""}
    defined = proto.encode_traces(
        build_trace_request(
            traces,
            image=image,
            resource_attributes=attributes,
        )
    )
    assert (
        proto.encode_projected_traces(
            traces,
            image=image,
            resource_attributes=attributes,
        )
        == defined
    )


def test_direct_trace_writer_keeps_an_empty_request_empty() -> None:
    assert proto.encode_projected_traces(()) == b""


def test_a_real_logs_request_survives_the_conversion() -> None:
    from wreath._logsite import SiteRegistry

    registry = SiteRegistry()
    request = build_logs_request([], registry=registry)
    assert request == {"resourceLogs": []}
    # An empty request still encodes to a valid (empty) message.
    assert proto.encode_logs(request) == b""


def test_direct_log_writer_matches_the_declared_mapping() -> None:
    from wreath._flight_schema import (
        CaptureDisposition,
        LogArg,
        LogCell,
        Severity,
    )
    from wreath._logsite import SiteRegistry, declare
    from wreath._projector import ProjectedLog

    registry = SiteRegistry()
    values = registry.register(
        "bench.values",
        "i={integer} s={text} b={boolean} f={real} n={none}",
        Severity.WARN,
        (
            declare("integer", int),
            declare("text", str, CaptureDisposition.RAW),
            declare("boolean", bool),
            declare("real", float),
            declare("none", type(None)),
        ),
    )
    redacted = registry.register(
        "bench.redacted",
        "h={hashed} l={length}",
        Severity.ERROR,
        (
            declare("hashed", str),
            declare("length", str),
        ),
    )
    records = (
        ProjectedLog(
            cell=LogCell(
                request_id=1,
                site_id=values.site_id,
                severity=Severity.WARN,
                args=(
                    LogArg.integer(-7),
                    LogArg.text("hello"),
                    LogArg.boolean(True),
                    LogArg.real(1.25),
                    LogArg.none(),
                ),
                dropped_siblings=2,
            ),
            trace_id=(1 << 127) | 3,
            span_id=(1 << 63) | 5,
            route_id=9,
            observed_unix_nano=2_000_000_000,
        ),
        ProjectedLog(
            cell=LogCell(
                request_id=2,
                site_id=redacted.site_id,
                severity=Severity.ERROR,
                args=(LogArg.hashed(0xABC), LogArg.length(42)),
            ),
            observed_unix_nano=3_000_000_000,
        ),
        ProjectedLog(
            cell=LogCell(
                request_id=3,
                site_id=999,
                severity=0,
                args=(LogArg.none(), LogArg.integer(8)),
            ),
            observed_unix_nano=4_000_000_000,
        ),
    )
    attributes = {"service.name": "shop", "empty": ""}
    defined = proto.encode_logs(
        build_logs_request(
            records,
            registry=registry,
            resource_attributes=attributes,
        )
    )
    assert (
        proto.encode_projected_logs(
            records,
            registry=registry,
            resource_attributes=attributes,
        )
        == defined
    )


def test_direct_log_writer_keeps_an_empty_request_empty() -> None:
    from wreath._logsite import SiteRegistry

    assert proto.encode_projected_logs((), registry=SiteRegistry()) == b""


class _Handler(BaseHTTPRequestHandler):
    posts: list[tuple[str, str, bytes]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        type(self).posts.append((self.path, self.headers.get("content-type", ""), body))
        self.send_response(200)
        self.send_header("content-type", "application/x-protobuf")
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def _serve(fn) -> list[tuple[str, str, bytes]]:
    _Handler.posts = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        host, port = server.server_address
        fn(f"http://{host}:{port}")
    finally:
        server.shutdown()
        thread.join(2.0)
    return _Handler.posts


TRACES = {"resourceSpans": [{"scopeSpans": [{"spans": [{"name": "GET /x"}]}]}]}


def test_the_exporter_sends_protobuf_by_default() -> None:
    posts = _serve(lambda base: OtlpHttpExporter(base, timeout=5.0).export_traces(TRACES))
    ((path, content_type, body),) = posts
    assert path == "/v1/traces"
    assert content_type == "application/x-protobuf"
    # And the body really is the message it claims to be.
    request = decode(proto.ExportTraceServiceRequest, body)
    assert request.resource_spans[0].scope_spans[0].spans[0].name == "GET /x"


def test_json_remains_selectable() -> None:
    posts = _serve(
        lambda base: OtlpHttpExporter(base, timeout=5.0, encoding="json").export_traces(TRACES)
    )
    ((_path, content_type, body),) = posts
    assert content_type == "application/json"
    assert json.loads(body) == TRACES


def test_an_unknown_encoding_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="protobuf"):
        OtlpHttpExporter("http://127.0.0.1:1", encoding="capnproto")


def test_empty_requests_are_still_skipped_in_both_encodings() -> None:
    def drive(base: str) -> None:
        for encoding in ("protobuf", "json"):
            exporter = OtlpHttpExporter(base, timeout=5.0, encoding=encoding)
            exporter.export_traces({"resourceSpans": []})
            exporter.export_metrics({"resourceMetrics": []})
            exporter.export_logs({"resourceLogs": []})

    assert _serve(drive) == []


def test_an_undeclared_otlp_key_is_refused_rather_than_dropped() -> None:
    with pytest.raises(ValueError) as excinfo:
        proto.encode_traces({"resourceSpans": [{"scopeSpans": [{"spans": [{"links": []}]}]}]})
    assert "links" in str(excinfo.value)
    assert "Span" in str(excinfo.value)
