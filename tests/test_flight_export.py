from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from wreath import _export
from wreath._export import ExportPipeline, OtlpHttpExporter
from wreath._flight_schema import Protocol, TerminalStatus
from wreath._projector import (
    ProjectedTrace,
    ProjectorLoss,
    ProjectorSnapshot,
    RouteMetric,
)
from wreath.http_client import DestinationRejected


def _trace(request_id: int, **kw: object) -> ProjectedTrace:
    fields: dict[str, object] = dict(
        request_id=request_id,
        connection_id=1,
        route_id=7,
        plan_id=0,
        worker_id=0,
        duration_us=1000,
        status=200,
        terminal=TerminalStatus.OK,
        protocol=Protocol.HTTP1,
        error_class=0,
        flags=0,
        bytes_in=1,
        bytes_out=2,
        observed_unix_nano=5_000_000_000,
    )
    fields.update(kw)
    return ProjectedTrace(**fields)  # type: ignore[arg-type]


def _snapshot() -> ProjectorSnapshot:
    metric = RouteMetric(route_id=7, count=3, errors=1, duration_us_sum=30)
    return ProjectorSnapshot(
        assembled=3,
        recent=(),
        failures=(),
        routes=(metric,),
        loss=ProjectorLoss(),
        pending=0,
    )


class CollectingTransport:
    """A fake transport recording every request it is handed."""

    def __init__(self) -> None:
        self.traces: list[dict] = []
        self.metrics: list[dict] = []
        self._lock = threading.Lock()

    def export_traces(self, request: dict) -> None:
        with self._lock:
            self.traces.append(request)

    def export_metrics(self, request: dict) -> None:
        with self._lock:
            self.metrics.append(request)

    def span_count(self) -> int:
        with self._lock:
            return sum(
                len(sp["spans"])
                for req in self.traces
                for rs in req["resourceSpans"]
                for sp in rs["scopeSpans"]
            )


class BoomTransport:
    def export_traces(self, request: dict) -> None:
        raise RuntimeError("collector down")

    def export_metrics(self, request: dict) -> None:
        raise RuntimeError("collector down")


def test_on_trace_enqueues_and_tick_exports() -> None:
    transport = CollectingTransport()
    pipe = ExportPipeline(transport, snapshot_provider=_snapshot)
    for i in range(5):
        pipe.on_trace(_trace(i))
    pipe._tick()  # one manual cycle instead of the thread

    assert transport.span_count() == 5
    assert len(transport.metrics) == 1  # one metrics export per tick
    assert pipe.stats["exported_traces"] == 5


def test_batching_splits_into_multiple_requests() -> None:
    transport = CollectingTransport()
    pipe = ExportPipeline(transport, batch_size=2)
    for i in range(5):
        pipe.on_trace(_trace(i))
    pipe._tick()

    # 5 traces at batch_size 2 -> requests of 2, 2, 1.
    sizes = [len(req["resourceSpans"][0]["scopeSpans"][0]["spans"]) for req in transport.traces]
    assert sizes == [2, 2, 1]
    assert transport.span_count() == 5


def test_trace_export_failure_is_isolated_and_counted() -> None:
    pipe = ExportPipeline(BoomTransport(), snapshot_provider=_snapshot)
    pipe.on_trace(_trace(1))
    pipe._tick()  # must not raise

    stats = pipe.stats
    assert stats["exported_traces"] == 0
    assert stats["trace_errors"] == 1
    assert stats["metric_errors"] == 1


def test_full_queue_drops_and_counts() -> None:
    transport = CollectingTransport()
    pipe = ExportPipeline(transport, queue_capacity=2)
    assert pipe.on_trace(_trace(1)) is None  # hook returns None; drop tracked in stats
    pipe.on_trace(_trace(2))
    pipe.on_trace(_trace(3))  # full -> dropped
    assert pipe.stats["dropped"] == 1
    pipe._tick()
    assert transport.span_count() == 2  # only the two that fit


def test_no_snapshot_provider_exports_no_metrics() -> None:
    transport = CollectingTransport()
    pipe = ExportPipeline(transport)  # traces only
    pipe.on_trace(_trace(1))
    pipe._tick()
    assert transport.span_count() == 1
    assert transport.metrics == []


def test_validates_tuning() -> None:
    transport = CollectingTransport()
    with pytest.raises(ValueError):
        ExportPipeline(transport, interval=0)
    with pytest.raises(ValueError):
        ExportPipeline(transport, batch_size=0)


def test_background_thread_exports_then_stop_flushes() -> None:
    transport = CollectingTransport()
    pipe = ExportPipeline(transport, interval=0.01, snapshot_provider=_snapshot)
    pipe.start()
    try:
        for i in range(10):
            pipe.on_trace(_trace(i))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and transport.span_count() < 10:
            time.sleep(0.01)
    finally:
        pipe.stop()
    # Enqueue after the loop is asked to stop: the final flush in stop() catches it.
    pipe.on_trace(_trace(999))
    pipe.stop()
    assert transport.span_count() == 11


def test_start_is_idempotent() -> None:
    pipe = ExportPipeline(CollectingTransport(), interval=0.01)
    pipe.start()
    pipe.start()
    pipe.stop()


def test_concurrent_producers_conserve_every_trace() -> None:
    transport = CollectingTransport()
    pipe = ExportPipeline(transport, interval=0.001, queue_capacity=256, batch_size=32)
    pipe.start()

    per_thread = 500
    n_threads = 4

    def producer(base: int) -> None:
        for i in range(per_thread):
            pipe.on_trace(_trace(base + i))

    threads = [threading.Thread(target=producer, args=(k * per_thread,)) for k in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Let the exporter drain whatever is queued, then stop (final flush).
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and len(pipe.queue) > 0:
        time.sleep(0.01)
    pipe.stop()

    stats = pipe.stats
    offered = pipe.queue.offered
    assert offered == n_threads * per_thread
    # Every offered trace is either exported or counted as a drop -- never lost.
    assert stats["exported_traces"] + stats["dropped"] == offered
    assert transport.span_count() == stats["exported_traces"]


class _OtlpHandler(BaseHTTPRequestHandler):
    posts: list[tuple[str, bytes]] = []

    def do_POST(self) -> None:  # http.server API
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        type(self).posts.append((self.path, body))
        self.send_response(200)
        self.send_header("content-type", "application/x-protobuf")
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:  # silence the test server
        pass


class _InternalCanaryHandler(BaseHTTPRequestHandler):
    gets: list[str] = []

    def do_GET(self) -> None:  # http.server API
        type(self).gets.append(self.path)
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


class _RedirectingCollectorHandler(BaseHTTPRequestHandler):
    gets: list[str] = []
    location = ""

    def do_POST(self) -> None:  # http.server API
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        self.send_response(302)
        self.send_header("location", type(self).location)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # http.server API
        type(self).gets.append(self.path)
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def test_otlp_http_exporter_posts_to_traces_and_metrics() -> None:
    _OtlpHandler.posts = []
    server = HTTPServer(("127.0.0.1", 0), _OtlpHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        host, port = server.server_address
        exporter = OtlpHttpExporter(f"http://{host}:{port}", timeout=5.0)
        exporter.export_traces({"resourceSpans": [{"scopeSpans": []}]})
        exporter.export_metrics({"resourceMetrics": [{"scopeMetrics": []}]})
        # Empty requests are skipped (no POST).
        exporter.export_traces({"resourceSpans": []})
    finally:
        server.shutdown()
        thread.join(2.0)

    paths = [path for path, _ in _OtlpHandler.posts]
    assert paths == ["/v1/traces", "/v1/metrics"]


@pytest.mark.parametrize(
    "endpoint",
    (
        "collector.invalid/v1/traces",
        "ftp://collector.invalid",
        "http:///v1/traces",
    ),
)
def test_otlp_exporter_refuses_an_endpoint_without_an_http_origin(endpoint: str) -> None:
    with pytest.raises(ValueError, match=r"absolute HTTP\(S\) URL"):
        OtlpHttpExporter(endpoint)


@pytest.mark.parametrize(
    ("url", "origin"),
    (
        ("http://COLLECTOR.invalid/path", ("http", "collector.invalid", 80)),
        ("https://COLLECTOR.invalid/path", ("https", "collector.invalid", 443)),
        ("https://collector.invalid:8443/path", ("https", "collector.invalid", 8443)),
    ),
)
def test_otlp_redirect_origin_normalizes_scheme_host_and_default_port(
    url: str, origin: tuple[str, str, int]
) -> None:
    assert _export._otlp_origin(url) == origin


def test_otlp_redirect_refuses_a_non_http_location() -> None:
    handler = _export._SameOriginRedirectHandler(("https", "collector.invalid", 443))
    request = _export.urllib.request.Request(
        "https://collector.invalid/v1/traces", data=b"payload", method="POST"
    )

    with pytest.raises(DestinationRejected, match="cross-origin"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "file:///etc/passwd",
        )


def test_otlp_collector_can_redirect_within_its_pinned_origin() -> None:
    _RedirectingCollectorHandler.gets = []
    collector = HTTPServer(("127.0.0.1", 0), _RedirectingCollectorHandler)
    thread = threading.Thread(
        target=collector.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    host, port = collector.server_address
    _RedirectingCollectorHandler.location = f"http://{host}:{port}/accepted"

    try:
        exporter = OtlpHttpExporter(f"http://{host}:{port}", timeout=5.0)
        exporter.export_traces({"resourceSpans": [{"scopeSpans": []}]})
    finally:
        collector.shutdown()
        thread.join(2.0)

    assert _RedirectingCollectorHandler.gets == ["/accepted"]


def test_otlp_collector_cannot_redirect_export_to_an_internal_origin() -> None:
    _InternalCanaryHandler.gets = []
    internal = HTTPServer(("127.0.0.1", 0), _InternalCanaryHandler)
    internal_thread = threading.Thread(
        target=internal.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    internal_thread.start()

    internal_host, internal_port = internal.server_address
    _RedirectingCollectorHandler.location = f"http://{internal_host}:{internal_port}/internal"
    collector = HTTPServer(("127.0.0.1", 0), _RedirectingCollectorHandler)
    collector_thread = threading.Thread(
        target=collector.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    collector_thread.start()
    collector_host, collector_port = collector.server_address

    refusal: DestinationRejected | None = None
    try:
        exporter = OtlpHttpExporter(f"http://{collector_host}:{collector_port}", timeout=5.0)
        try:
            exporter.export_traces({"resourceSpans": [{"scopeSpans": []}]})
        except DestinationRejected as error:
            refusal = error
    finally:
        collector.shutdown()
        internal.shutdown()
        collector_thread.join(2.0)
        internal_thread.join(2.0)

    assert _InternalCanaryHandler.gets == [], (
        "the configured collector redirected OTLP export into the internal canary"
    )
    assert refusal is not None
    assert "cross-origin" in str(refusal)


def test_otlp_http_exporter_failure_propagates_for_pipeline_isolation() -> None:
    # An unroutable endpoint: export raises, which the pipeline (not this test)
    # is responsible for catching. Here we assert it does raise.
    exporter = OtlpHttpExporter("http://127.0.0.1:1", timeout=0.2)
    with pytest.raises(Exception):  # noqa: B017 -- urllib error type varies
        exporter.export_traces({"resourceSpans": [{"scopeSpans": []}]})
