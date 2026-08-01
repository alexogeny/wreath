"""Stage 4c -- the OTLP export pipeline: bounded queue, batching, isolation.

These drive :class:`wreath._export.ExportPipeline` with a collecting fake
transport (so no network is required for the core behaviors) and exercise the
concrete :class:`wreath._export.OtlpHttpExporter` against a localhost HTTP server
in one integration test.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from wreath._export import ExportPipeline, OtlpHttpExporter
from wreath._flight_schema import Protocol, TerminalStatus
from wreath._projector import (
    ProjectedTrace,
    ProjectorLoss,
    ProjectorSnapshot,
    RouteMetric,
)


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
        assembled=3, recent=(), failures=(), routes=(metric,),
        loss=ProjectorLoss(), pending=0,
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


# --- synchronous pipeline behavior (no thread) -----------------------------


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
    sizes = [
        len(req["resourceSpans"][0]["scopeSpans"][0]["spans"])
        for req in transport.traces
    ]
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


# --- threaded lifecycle ----------------------------------------------------


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
    """Many threads offering while the exporter drains: nothing is created or
    lost -- offered == exported + dropped, exactly."""
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


# --- concrete OTLP/HTTP exporter -------------------------------------------


class _OtlpHandler(BaseHTTPRequestHandler):
    #: The raw body, deliberately not parsed. This handler used to
    #: `json.loads` it, which quietly made every test through it a JSON-encoding
    #: test as well as a routing one. The exporter now defaults to protobuf, and
    #: the assertions below are about *which paths get posted to* and *that
    #: empty requests are skipped* -- neither of which is about the encoding.
    #: `tests/test_otlp_protobuf.py` covers the bodies in both encodings.
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


def test_otlp_http_exporter_posts_to_traces_and_metrics() -> None:
    _OtlpHandler.posts = []
    server = HTTPServer(("127.0.0.1", 0), _OtlpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
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


def test_otlp_http_exporter_failure_propagates_for_pipeline_isolation() -> None:
    # An unroutable endpoint: export raises, which the pipeline (not this test)
    # is responsible for catching. Here we assert it does raise.
    exporter = OtlpHttpExporter("http://127.0.0.1:1", timeout=0.2)
    with pytest.raises(Exception):  # noqa: B017 -- urllib error type varies
        exporter.export_traces({"resourceSpans": [{"scopeSpans": []}]})
