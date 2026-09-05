from dataclasses import replace

import pytest

from wreath._export import ExportPipeline
from wreath._flight_schema import MetadataImage, Protocol, RouteMeta, TerminalStatus
from wreath._otlp import build_trace_request
from wreath._projector import ProjectedTrace


def _image(routes):
    return MetadataImage(
        version=1,
        routes=routes,
        plans=(),
        dependencies=(),
        middleware=(),
        auth_policies=(),
        serializers=(),
        validators=(),
        limits=(),
        clients=(),
        databases=(),
        models=(),
    )


def _trace():
    return ProjectedTrace(1, 1, 101, 55, 0, 10, 200, TerminalStatus.OK, Protocol.HTTP1, 0, 0, 0, 2)


class _Transport:
    def __init__(self):
        self.requests = []

    def export_traces(self, request):
        self.requests.append(request)


def test_each_drain_indexes_metadata_once_and_preserves_route_attributes():
    traversals = []

    class Rows(tuple):
        def __iter__(self):
            traversals.append(1)
            return super().__iter__()

    route = RouteMeta(101, "GET", "/users/{id}", "get_user", 55, (), (), (), 0, "python")
    image = _image(Rows((route,)))
    transport = _Transport()
    pipeline = ExportPipeline(
        transport,
        image=image,
        batch_size=1,
        resource_attributes={"service.name": "lookup-test"},
    )
    for _ in range(3):
        pipeline.on_trace(_trace())
    pipeline._export_traces()
    assert len(traversals) == 1
    assert len(transport.requests) == 3
    for request in transport.requests:
        assert {"key": "service.name", "value": {"stringValue": "lookup-test"}} in request[
            "resourceSpans"
        ][0]["resource"]["attributes"]
        span = request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["name"] == "GET /users/{id}"
        assert {"key": "http.route", "value": {"stringValue": "/users/{id}"}} in span["attributes"]
    pipeline._export_traces()
    assert len(traversals) == 1
    pipeline.on_trace(_trace())
    pipeline._export_traces()
    assert len(traversals) == 2


def test_projected_transport_does_not_build_fallback_index():
    class Rows(tuple):
        def __iter__(self):
            raise AssertionError("projected path must not index metadata")

    class Transport:
        def __init__(self):
            self.batches = []

        def export_projected_traces(self, batch, **options):
            self.batches.append((batch, options))

    image = _image(Rows())
    transport = Transport()
    pipeline = ExportPipeline(transport, image=image, batch_size=1)
    for _ in range(3):
        pipeline.on_trace(_trace())
    pipeline._export_traces()
    assert len(transport.batches) == 3
    assert all(options["image"] is image for _, options in transport.batches)


def test_index_failure_is_counted_per_batch_and_retried():
    class Rows(tuple):
        def __iter__(self):
            raise ValueError("bad metadata")

    pipeline = ExportPipeline(_Transport(), image=_image(Rows()), batch_size=1)
    for _ in range(3):
        pipeline.on_trace(_trace())
    pipeline._export_traces()
    assert pipeline.stats["trace_errors"] == 3
    assert pipeline.stats["exported_traces"] == 0


@pytest.mark.parametrize("batch_size", [1, 2, 8])
def test_public_builder_and_pipeline_agree_with_bounded_queue(batch_size):
    transport = _Transport()
    pipeline = ExportPipeline(transport, queue_capacity=3, batch_size=batch_size)
    traces = [replace(_trace(), request_id=index + 1) for index in range(5)]
    for trace in traces:
        pipeline.on_trace(trace)
    pipeline._export_traces()
    assert transport.requests == [
        build_trace_request(traces[start : min(start + batch_size, 3)])
        for start in range(0, 3, batch_size)
    ]
    assert pipeline.stats["exported_traces"] == 3


def test_failed_transport_does_not_abort_remaining_batches():
    class Transport(_Transport):
        def export_traces(self, request):
            super().export_traces(request)
            if len(self.requests) == 1:
                raise OSError("collector unavailable")

    transport = Transport()
    pipeline = ExportPipeline(transport, batch_size=1)
    for _ in range(3):
        pipeline.on_trace(_trace())
    pipeline._export_traces()
    assert len(transport.requests) == 3
    assert pipeline.stats["trace_errors"] == 1
    assert pipeline.stats["exported_traces"] == 2
