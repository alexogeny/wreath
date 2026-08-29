from __future__ import annotations

import pytest

from wreath._flight_schema import (
    FLAG_AI_SCRAPING_REFUSED,
    FLAG_POLICY_REFUSED,
    MetadataImage,
    NamedMeta,
    PhaseCoverage,
    PhaseKind,
    PhaseRecord,
    Protocol,
    RouteMeta,
    TerminalStatus,
)
from wreath._otlp import (
    BoundedExportQueue,
    build_metrics_request,
    build_trace_request,
    resource,
)
from wreath._projector import ProjectedTrace, ProjectorLoss, ProjectorSnapshot, RouteMetric


def _image() -> MetadataImage:
    return MetadataImage(
        version=1,
        routes=(
            RouteMeta(
                route_id=101,
                method="GET",
                path="/users/{id}",
                operation_id="get_user",
                plan_id=55,
                tags=(),
                dependency_ids=(5,),
                middleware_ids=(),
                auth_policy_id=0,
                coverage="python",
            ),
        ),
        plans=(),
        dependencies=(),
        middleware=(),
        auth_policies=(),
        serializers=(),
        validators=(),
        limits=(),
        clients=(),
        databases=(NamedMeta(entry_id=5, name="maindb"),),
        models=(),
    )


def _trace(**kw: object) -> ProjectedTrace:
    fields: dict[str, object] = dict(
        request_id=1,
        connection_id=2,
        route_id=101,
        plan_id=55,
        worker_id=0,
        duration_us=1000,
        status=200,
        terminal=TerminalStatus.OK,
        protocol=Protocol.HTTP1,
        error_class=0,
        flags=0,
        bytes_in=11,
        bytes_out=22,
        observed_unix_nano=5_000_000_000,
    )
    fields.update(kw)
    return ProjectedTrace(**fields)  # type: ignore[arg-type]


def _only_span(request: dict) -> dict:
    spans = request["resourceSpans"][0]["scopeSpans"][0]["spans"]
    return spans[0]


def _attrs(span: dict) -> dict[str, object]:
    out: dict[str, object] = {}
    for kv in span["attributes"]:
        value = kv["value"]
        out[kv["key"]] = value.get("stringValue", value.get("intValue", value.get("boolValue")))
    return out


def test_server_span_shape_and_route_attributes() -> None:
    req = build_trace_request([_trace(trace_id=0xABC, span_id=0xDEF)], image=_image())
    span = _only_span(req)

    assert span["name"] == "GET /users/{id}"
    assert span["kind"] == 2  # SERVER
    assert span["traceId"] == format(0xABC, "032x")
    assert span["spanId"] == format(0xDEF, "016x")
    assert span["endTimeUnixNano"] == "5000000000"
    assert span["startTimeUnixNano"] == str(5_000_000_000 - 1000 * 1000)
    assert span["status"] == {"code": 0}  # UNSET on success

    attrs = _attrs(span)
    assert attrs["http.request.method"] == "GET"
    assert attrs["http.route"] == "/users/{id}"
    assert attrs["http.response.status_code"] == "200"
    assert attrs["network.protocol.name"] == "http"
    assert attrs["network.protocol.version"] == "1.1"
    assert attrs["http.request.body.size"] == "11"
    assert attrs["http.response.body.size"] == "22"
    assert attrs["wreath.terminal"] == "ok"


def test_compact_client_facts_enrich_server_span() -> None:
    from wreath._flight_schema import ClientFactFlag, ClientFactsCell

    trace = _trace(
        client_facts=ClientFactsCell(
            request_id=1,
            flags=(
                ClientFactFlag.UA_KNOWN
                | ClientFactFlag.BOT_CLAIMED
                | ClientFactFlag.AGENT_VERIFIED
                | ClientFactFlag.IP_KNOWN
                | ClientFactFlag.IP_FORWARDED
                | ClientFactFlag.GEO_KNOWN
            ),
            user_agent_rule_id=23,
            country="AU",
        )
    )
    attrs = _attrs(_only_span(build_trace_request([trace])))
    assert attrs["wreath.client.agent.claimed"] is True
    assert attrs["wreath.client.agent.verified"] is True
    assert attrs["wreath.user_agent.classified"] is True
    assert attrs["wreath.user_agent.rule_id"] == "23"
    assert attrs["user_agent.synthetic.type"] == "bot"
    assert attrs["network.type"] == "ipv4"
    assert attrs["wreath.client.address_source"] == "forwarded"
    assert attrs["geo.country.iso_code"] == "AU"


def test_failure_sets_error_status() -> None:
    req = build_trace_request([_trace(status=500, terminal=TerminalStatus.ERROR, error_class=7)])
    span = _only_span(req)
    assert span["status"]["code"] == 2  # ERROR
    assert span["status"]["message"] == "error"
    assert _attrs(span)["error.type"] == "class:7"


def test_policy_refusal_is_structured_without_becoming_an_error() -> None:
    trace = _trace(
        status=403,
        flags=FLAG_POLICY_REFUSED | FLAG_AI_SCRAPING_REFUSED,
    )
    span = _only_span(build_trace_request([trace]))
    attrs = _attrs(span)
    assert span["status"] == {"code": 0}
    assert attrs["wreath.policy.refused"] is True
    assert attrs["wreath.policy.disposition"] == "ai_scraping"


def test_parent_span_id_present_only_when_propagated() -> None:
    with_parent = _only_span(
        build_trace_request([_trace(trace_id=1, span_id=2, parent_span_id=0x99)])
    )
    assert with_parent["parentSpanId"] == format(0x99, "016x")
    without = _only_span(build_trace_request([_trace(trace_id=1, span_id=2)]))
    assert "parentSpanId" not in without


def test_unpropagated_request_gets_synthesized_nonzero_ids() -> None:
    span = _only_span(build_trace_request([_trace(request_id=42)]))  # no correlation
    assert int(span["traceId"], 16) != 0
    assert int(span["spanId"], 16) != 0
    # Deterministic: same request id yields the same ids.
    again = _only_span(build_trace_request([_trace(request_id=42)]))
    assert again["traceId"] == span["traceId"]
    assert again["spanId"] == span["spanId"]


def test_websocket_protocol_naming() -> None:
    span = _only_span(build_trace_request([_trace(route_id=0, protocol=Protocol.WEBSOCKET)]))
    assert span["name"] == "WEBSOCKET"
    assert _attrs(span)["network.protocol.name"] == "websocket"


def test_unknown_route_falls_back_without_route_attributes() -> None:
    span = _only_span(build_trace_request([_trace(route_id=999)]))  # no image
    assert span["name"] == "HTTP"
    attrs = _attrs(span)
    assert "http.route" not in attrs


def test_phases_become_child_spans_with_correct_kinds() -> None:
    trace = _trace(
        trace_id=0xAAA,
        span_id=0xBBB,
        duration_us=1000,
        phases=(
            PhaseRecord(
                phase_id=PhaseKind.HANDLER,
                duration_us=200,
                start_offset_us=10,
                sequence=0,
                coverage=PhaseCoverage.PYTHON,
            ),
            PhaseRecord(
                phase_id=PhaseKind.DB_QUERY,
                duration_us=50,
                start_offset_us=100,
                dependency_id=5,
                sequence=1,
                coverage=PhaseCoverage.EXTERNAL,
            ),
        ),
    )
    spans = build_trace_request([trace], image=_image())["resourceSpans"][0]["scopeSpans"][0][
        "spans"
    ]
    assert len(spans) == 3  # one server + two child

    server, handler, db = spans
    parent_start = 5_000_000_000 - 1000 * 1000
    assert handler["kind"] == 1  # INTERNAL
    assert handler["parentSpanId"] == format(0xBBB, "016x")
    assert handler["traceId"] == server["traceId"]
    assert handler["startTimeUnixNano"] == str(parent_start + 10 * 1000)
    assert handler["endTimeUnixNano"] == str(parent_start + 10 * 1000 + 200 * 1000)

    assert db["kind"] == 3  # CLIENT for a dependency call
    assert db["name"] == "db_query"
    db_attrs = _attrs(db)
    assert db_attrs["wreath.dependency_id"] == "5"
    assert db_attrs["wreath.dependency"] == "maindb"  # resolved from the image
    assert db_attrs["wreath.coverage"] == "external"
    # Child span ids are distinct per sequence.
    assert handler["spanId"] != db["spanId"]


def test_empty_traces_produce_empty_request() -> None:
    assert build_trace_request([]) == {"resourceSpans": []}


def test_resource_attributes_default_and_override() -> None:
    default = resource(None)
    keys = {kv["key"]: kv["value"]["stringValue"] for kv in default["attributes"]}
    assert keys["service.name"] == "wreath"

    override = resource({"service.name": "api", "deployment.environment": "prod"})
    keys = {kv["key"]: kv["value"]["stringValue"] for kv in override["attributes"]}
    assert keys["service.name"] == "api"
    assert keys["deployment.environment"] == "prod"


def _snapshot(*routes: RouteMetric) -> ProjectorSnapshot:
    return ProjectorSnapshot(
        assembled=sum(r.count for r in routes),
        recent=(),
        failures=(),
        routes=routes,
        loss=ProjectorLoss(),
        pending=0,
    )


def test_metrics_request_counts_and_histogram() -> None:
    metric = RouteMetric(
        route_id=101, count=10, errors=3, duration_us_sum=12345, duration_us_max=2000
    )
    metric.buckets[3] = 4
    metric.buckets[7] = 6
    snap = _snapshot(metric)

    req = build_metrics_request(snap, image=_image(), start_unix_nano=1000, now_unix_nano=2000)
    metrics = req["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    by_name = {m["name"]: m for m in metrics}

    count = by_name["http.server.request.count"]["sum"]
    assert count["isMonotonic"] is True
    assert count["aggregationTemporality"] == 2

    def _has_outcome(point: dict) -> bool:
        return any(a["key"] == "wreath.outcome" for a in point["attributes"])

    total_point = next(p for p in count["dataPoints"] if not _has_outcome(p))
    assert total_point["asInt"] == "10"
    error_point = next(p for p in count["dataPoints"] if _has_outcome(p))
    assert error_point["asInt"] == "3"

    duration = by_name["http.server.request.duration"]["exponentialHistogram"]
    point = duration["dataPoints"][0]
    assert point["scale"] == 0
    assert point["count"] == "10"
    assert point["sum"] == 12345.0
    # Buckets 3..7: offset 3, counts [4,0,0,0,6].
    assert point["positive"]["offset"] == 3
    assert point["positive"]["bucketCounts"] == ["4", "0", "0", "0", "6"]
    route_attr = next(a for a in point["attributes"] if a["key"] == "http.route")
    assert route_attr["value"]["stringValue"] == "/users/{id}"


def test_metrics_request_empty_when_no_routes() -> None:
    snap = _snapshot()
    req = build_metrics_request(snap, start_unix_nano=1, now_unix_nano=2)
    assert req == {"resourceMetrics": []}


def test_bounded_queue_offers_drains_and_counts_drops() -> None:
    q = BoundedExportQueue(capacity=2)
    assert q.offer(_trace(request_id=1)) is True
    assert q.offer(_trace(request_id=2)) is True
    assert q.offer(_trace(request_id=3)) is False  # full
    assert len(q) == 2
    assert q.dropped == 1
    assert q.offered == 3

    batch = q.drain()
    assert [t.request_id for t in batch] == [1, 2]
    assert len(q) == 0
    # Space frees up after draining.
    assert q.offer(_trace(request_id=4)) is True


def test_bounded_queue_partial_drain() -> None:
    q = BoundedExportQueue(capacity=8)
    for i in range(5):
        q.offer(_trace(request_id=i))
    first = q.drain(2)
    assert [t.request_id for t in first] == [0, 1]
    assert len(q) == 3


def test_bounded_queue_is_usable_as_on_trace_hook() -> None:
    from wreath._projector import Projector

    class FakeRec:
        def drain(self, max_cells: int = 4096) -> bytes:
            return b""

        def loss(self, reason: int) -> int:
            return 0

    q = BoundedExportQueue(capacity=16)
    proj = Projector(FakeRec(), on_trace=q.offer)
    # The hook signature matches; construction and wiring do not raise.
    assert proj is not None


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        BoundedExportQueue(capacity=0)


_flight = pytest.importorskip("wreath._native._flight")
_TRACEPARENT = b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


#: Every attribute key the OTLP mapping is allowed to emit. A key not on this
#: list is a cardinality/secrecy regression: the mapping must never surface a
#: concrete path, query value, header value, SQL, or user/tenant id.
_ALLOWED_ATTRIBUTE_KEYS = frozenset(
    {
        "wreath.route_id",
        "wreath.plan_id",
        "wreath.terminal",
        "http.request.body.size",
        "http.response.body.size",
        "network.protocol.name",
        "network.protocol.version",
        "http.response.status_code",
        "http.request.method",
        "http.route",
        "error.type",
        "wreath.phase",
        "wreath.coverage",
        "wreath.dependency_id",
        "wreath.dependency",
        "wreath.client.agent.claimed",
        "wreath.client.agent.verified",
        "wreath.user_agent.classified",
        "wreath.user_agent.rule_id",
        "user_agent.synthetic.type",
        "browser.mobile",
        "network.type",
        "wreath.client.address_source",
        "geo.country.iso_code",
    }
)


def _all_spans(request: dict) -> list[dict]:
    return [
        span for rs in request["resourceSpans"] for sp in rs["scopeSpans"] for span in sp["spans"]
    ]


def test_span_attributes_stay_within_the_low_cardinality_allowlist() -> None:
    traces = [
        _trace(trace_id=1, span_id=2, status=500, terminal=TerminalStatus.ERROR, error_class=4),
        _trace(route_id=0, protocol=Protocol.WEBSOCKET),
        _trace(
            trace_id=9,
            span_id=8,
            phases=(
                PhaseRecord(
                    phase_id=PhaseKind.DB_QUERY,
                    duration_us=5,
                    dependency_id=5,
                    sequence=0,
                    coverage=PhaseCoverage.EXTERNAL,
                ),
                PhaseRecord(phase_id=PhaseKind.HANDLER, duration_us=5, sequence=1),
            ),
        ),
    ]
    request = build_trace_request(traces, image=_image())
    for span in _all_spans(request):
        for kv in span["attributes"]:
            assert kv["key"] in _ALLOWED_ATTRIBUTE_KEYS, kv["key"]
        # The span name is method + route template, never a concrete path.
        assert "?" not in span["name"]  # no query string
        route_attr = next((a for a in span["attributes"] if a["key"] == "http.route"), None)
        if route_attr is not None:
            assert route_attr["value"]["stringValue"] == "/users/{id}"


def test_real_recorder_projects_to_serializable_otlp() -> None:
    import json

    from wreath._projector import Projector

    rec = _flight.Recorder(
        _flight.MODE_DETAILED,
        ring_records=1024,
        active_requests=64,
        detailed_sample_rate=1.0,
        phase_slots=8,
    )
    req = rec.begin(connection_id=1, protocol=_flight.PROTO_HTTP1, start_ns=0)
    req.route(101, 55)
    req.propagate(_TRACEPARENT)
    req.phase(phase_id=int(PhaseKind.HANDLER), duration_us=200)
    req.finish(now_ns=500_000, status=200, bytes_in=10, bytes_out=20)

    proj = Projector(rec)
    for _ in range(3):
        proj.poll()
    snap = proj.snapshot()

    request = build_trace_request(snap.recent, image=_image())
    blob = json.dumps(request)  # must be JSON-serializable
    assert json.loads(blob) == request

    spans = request["resourceSpans"][0]["scopeSpans"][0]["spans"]
    server = next(s for s in spans if s["kind"] == 2)
    assert server["traceId"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert server["name"] == "GET /users/{id}"
    child = next(s for s in spans if s["kind"] == 1)
    assert child["name"] == "handler"
    assert child["parentSpanId"] == server["spanId"]

    metrics = build_metrics_request(snap, image=_image(), start_unix_nano=1, now_unix_nano=2)
    assert json.loads(json.dumps(metrics)) == metrics
