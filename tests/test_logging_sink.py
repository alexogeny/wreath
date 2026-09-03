from __future__ import annotations

import json
from collections import deque

import pytest

from wreath import _flight_schema as fs
from wreath import logging as log
from wreath._logsink import (
    JsonRenderer,
    LogPipeline,
    ProjectedLog,
    TextRenderer,
    default_renderer,
)
from wreath._projector import Projector


class FakeRecorder:
    """A drainable stand-in: `feed` queues one drain's worth of cells.

    Deliberately local rather than imported from the projector suite: these
    tests must keep passing if that one restructures its helpers.
    """

    def __init__(self) -> None:
        self._queue: deque[bytes] = deque()

    def feed(self, *cells: bytes) -> None:
        self._queue.append(b"".join(cells))

    def drain(self, max_cells: int = 4096) -> bytes:
        if not self._queue:
            return b""
        buf = self._queue.popleft()
        limit = max_cells * fs.CELL_SIZE
        if len(buf) > limit:
            self._queue.appendleft(buf[limit:])
            return buf[:limit]
        return buf

    def loss(self, reason: int) -> int:
        return 0

    def histogram(self) -> tuple[int, ...]:
        return tuple([0] * 64)


@pytest.fixture
def runtime() -> log.LogRuntime:
    with log.testing_runtime(lambda _c: None, level=log.TRACE):
        yield log.installed()


def _site(runtime: log.LogRuntime) -> log.LogEvent:
    return log.event(
        "sink.denied",
        "user {user} denied {resource}",
        level=log.WARN,
        fields=(log.field("user", int), log.field("resource", str, log.RAW)),
    )


def _recorder() -> FakeRecorder:
    return FakeRecorder()


def _publish(recorder: FakeRecorder, cells: list[bytes]) -> None:
    """Queue one drain's worth of cells, as the ring would hand them over."""
    recorder.feed(*cells)


def test_a_log_record_joins_its_trace_by_request_id(runtime: log.LogRuntime) -> None:
    recorder = _recorder()
    projector = Projector(recorder)
    request_id = 4242
    completion = fs.CompletionCell(
        request_id=request_id,
        connection_id=1,
        route_id=2,
        plan_id=0,
        duration_us=100,
        status=200,
        bytes_in=0,
        bytes_out=0,
    ).encode()
    record = fs.LogCell(request_id=request_id, site_id=1, severity=fs.Severity.WARN).encode()
    _publish(recorder, [completion, record])

    projector.poll()  # ingest
    projector.poll()  # quiet cycle settles it
    (trace,) = projector.snapshot().recent
    assert len(trace.logs) == 1
    assert trace.logs[0].site_id == 1


def test_a_record_arriving_after_its_completion_still_joins(
    runtime: log.LogRuntime,
) -> None:
    recorder = _recorder()
    projector = Projector(recorder)
    request_id = 7
    _publish(
        recorder,
        [
            fs.CompletionCell(
                request_id=request_id,
                connection_id=1,
                route_id=2,
                plan_id=0,
                duration_us=1,
                status=200,
                bytes_in=0,
                bytes_out=0,
            ).encode()
        ],
    )
    projector.poll()
    _publish(
        recorder,
        [fs.LogCell(request_id=request_id, site_id=3, severity=fs.Severity.INFO).encode()],
    )
    projector.poll()
    projector.poll()
    (trace,) = projector.snapshot().recent
    assert [c.site_id for c in trace.logs] == [3]


def test_an_unscoped_record_is_delivered_without_a_trace(
    runtime: log.LogRuntime,
) -> None:
    recorder = _recorder()
    seen: list[ProjectedLog] = []
    projector = Projector(recorder, on_log=seen.append)
    _publish(
        recorder,
        [fs.LogCell(request_id=0, site_id=5, severity=fs.Severity.ERROR).encode()],
    )
    projector.poll()
    assert [p.cell.site_id for p in seen] == [5]
    assert seen[0].trace_id == 0


def test_a_scoped_record_reaches_the_hook_with_its_correlation(
    runtime: log.LogRuntime,
) -> None:
    recorder = _recorder()
    seen: list[ProjectedLog] = []
    projector = Projector(recorder, on_log=seen.append)
    request_id = 11
    _publish(
        recorder,
        [
            fs.CompletionCell(
                request_id=request_id,
                connection_id=1,
                route_id=9,
                plan_id=0,
                duration_us=1,
                status=200,
                bytes_in=0,
                bytes_out=0,
                flags=fs.FLAG_HAS_CORRELATION,
            ).encode(),
            fs.CorrelationCell(request_id=request_id, trace_id=(1 << 64) | 2, span_id=3).encode(),
            fs.LogCell(request_id=request_id, site_id=6, severity=fs.Severity.INFO).encode(),
        ],
    )
    projector.poll()
    projector.poll()
    assert len(seen) == 1
    assert seen[0].trace_id == (1 << 64) | 2
    assert seen[0].span_id == 3
    assert seen[0].route_id == 9


def test_records_whose_completion_never_arrives_are_counted(
    runtime: log.LogRuntime,
) -> None:
    recorder = _recorder()
    projector = Projector(recorder)
    _publish(
        recorder,
        [fs.LogCell(request_id=999, site_id=1, severity=fs.Severity.INFO).encode()],
    )
    projector.poll()
    projector.poll()
    projector.poll()
    assert projector.snapshot().loss.orphan_log >= 1


def _projected(site_id: int = 1, severity: fs.Severity = fs.Severity.INFO) -> ProjectedLog:
    return ProjectedLog(cell=fs.LogCell(request_id=0, site_id=site_id, severity=severity))


def test_pipeline_hands_records_to_the_sink(runtime: log.LogRuntime) -> None:
    written: list[str] = []
    pipeline = LogPipeline(runtime.registry, write=written.append)
    pipeline.on_log(_projected())
    pipeline.flush()
    assert len(written) == 1


def test_pipeline_uses_the_supplied_renderer(runtime: log.LogRuntime) -> None:
    written: list[str] = []

    def renderer(_registry, _record) -> str:
        return "custom-rendering"

    pipeline = LogPipeline(runtime.registry, write=written.append, renderer=renderer)
    pipeline.on_log(_projected())
    pipeline.flush()

    assert written == ["custom-rendering"]


def test_a_full_queue_drops_and_counts_rather_than_blocking(
    runtime: log.LogRuntime,
) -> None:
    pipeline = LogPipeline(runtime.registry, write=lambda _line: None, capacity=2)
    for _ in range(5):
        pipeline.on_log(_projected())
    assert pipeline.stats()["dropped"] == 3
    pipeline.flush()
    assert pipeline.stats()["written"] == 2


def test_a_raising_sink_is_counted_and_the_pipeline_keeps_draining(
    runtime: log.LogRuntime,
) -> None:
    written: list[str] = []

    def flaky(line: str) -> None:
        if "1" in line or len(written) == 0:
            raise OSError("disk went away")
        written.append(line)

    pipeline = LogPipeline(runtime.registry, write=flaky)
    for _ in range(3):
        pipeline.on_log(_projected())
    pipeline.flush()
    assert pipeline.stats()["write_error"] >= 1


def test_the_writer_thread_drains_without_being_polled(
    runtime: log.LogRuntime,
) -> None:
    written: list[str] = []
    pipeline = LogPipeline(runtime.registry, write=written.append, interval=0.01)
    pipeline.start()
    try:
        pipeline.on_log(_projected())
        deadline = 2.0
        step = 0.01
        waited = 0.0
        while not written and waited < deadline:
            import time

            time.sleep(step)
            waited += step
    finally:
        pipeline.stop()
    assert len(written) == 1


def test_stop_flushes_what_is_queued(runtime: log.LogRuntime) -> None:
    written: list[str] = []
    pipeline = LogPipeline(runtime.registry, write=written.append, interval=60.0)
    pipeline.start()
    pipeline.on_log(_projected())
    pipeline.stop()
    assert len(written) == 1


def test_text_renderer_shows_severity_and_the_rendered_message(
    runtime: log.LogRuntime,
) -> None:
    site = _site(runtime)
    site(17, "orders")
    cell = fs.LogCell(
        request_id=0,
        site_id=site.site_id,
        severity=fs.Severity.WARN,
        args=(fs.LogArg.integer(17), fs.LogArg.text("orders")),
    )
    line = TextRenderer()(runtime.registry, ProjectedLog(cell=cell))
    assert "WARN" in line
    assert "user 17 denied orders" in line


def test_text_renderer_includes_the_trace_id_when_there_is_one(
    runtime: log.LogRuntime,
) -> None:
    site = _site(runtime)
    cell = fs.LogCell(
        request_id=1,
        site_id=site.site_id,
        severity=fs.Severity.WARN,
        args=(fs.LogArg.integer(1), fs.LogArg.text("x")),
    )
    line = TextRenderer()(runtime.registry, ProjectedLog(cell=cell, trace_id=0xABC, span_id=0xDEF))
    assert "abc" in line.lower()


def test_json_renderer_emits_one_parseable_object_per_record(
    runtime: log.LogRuntime,
) -> None:
    site = _site(runtime)
    cell = fs.LogCell(
        request_id=1,
        site_id=site.site_id,
        severity=fs.Severity.WARN,
        args=(fs.LogArg.integer(17), fs.LogArg.text("orders")),
    )
    line = JsonRenderer()(runtime.registry, ProjectedLog(cell=cell, trace_id=5, span_id=6))
    payload = json.loads(line)
    assert payload["severity"] == "WARN"
    assert payload["event"] == "sink.denied"
    assert payload["message"] == "user 17 denied orders"
    assert payload["attributes"] == {"user": 17, "resource": "orders"}
    assert payload["trace_id"] == f"{5:032x}"
    assert payload["span_id"] == f"{6:016x}"
    assert "\n" not in line


def test_json_renderer_omits_correlation_when_there_is_none(
    runtime: log.LogRuntime,
) -> None:
    site = _site(runtime)
    cell = fs.LogCell(request_id=0, site_id=site.site_id, severity=fs.Severity.WARN)
    payload = json.loads(JsonRenderer()(runtime.registry, ProjectedLog(cell=cell)))
    assert "trace_id" not in payload


def test_a_redacted_argument_never_reaches_either_renderer(
    runtime: log.LogRuntime,
) -> None:
    site = log.event("sink.token", "token {token}", fields=(log.field("token", str),))
    site("hunter2")
    cell = fs.LogCell(
        request_id=0,
        site_id=site.site_id,
        severity=fs.Severity.INFO,
        args=(fs.LogArg.hashed(0x1234),),
    )
    projected = ProjectedLog(cell=cell)
    assert "hunter2" not in TextRenderer()(runtime.registry, projected)
    assert "hunter2" not in JsonRenderer()(runtime.registry, projected)


def test_default_renderer_is_text_on_a_tty_and_json_otherwise() -> None:
    assert isinstance(default_renderer(is_tty=True), TextRenderer)
    assert isinstance(default_renderer(is_tty=False), JsonRenderer)
