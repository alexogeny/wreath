from __future__ import annotations

import pytest

import wreath
from wreath._flight_metadata import build_metadata_image
from wreath._flight_schema import CELL_SIZE, EventKind
from wreath._projector import Projector
from wreath._recording_format import RecordingSink, read_recording

_flight = pytest.importorskip("wreath._native._flight", exc_type=ImportError)


def _sink(tmp_path, recorder):
    app = wreath.Wreath()

    @app.get("/ping")
    async def ping(request: wreath.Request) -> wreath.Response:
        return wreath.response.TextResponse("pong")

    path = str(tmp_path / "recording.wfr1")
    sink = RecordingSink(recorder, build_metadata_image(app), path)
    sink.start()
    return sink, path


def _serve(recorder, count: int) -> None:
    for _ in range(count):
        request = recorder.begin(1, 1, 0)
        request.route(7, 3)
        request.finish(1_000, 200, 0, 0, 0, 12)


def test_drained_cells_are_filed_as_events(tmp_path) -> None:
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=16)
    sink, path = _sink(tmp_path, recorder)
    projector = Projector(recorder, on_cells=sink.archive_cells)
    try:
        _serve(recorder, 5)
        projector.poll()
        projector.poll()
    finally:
        sink.stop()

    decoded = read_recording(open(path, "rb").read())
    assert decoded.events, "the drained cells should have reached the recording"
    kinds = {cell[1] for cell in decoded.events}
    assert kinds == {int(EventKind.COMPLETION)}
    assert len(decoded.events) == 5


def test_the_archive_outlives_a_ring_that_has_already_wrapped(tmp_path) -> None:
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=4, active_requests=16)
    sink, path = _sink(tmp_path, recorder)
    projector = Projector(recorder, on_cells=sink.archive_cells)
    try:
        for _ in range(5):
            _serve(recorder, 2)
            projector.poll()
        projector.poll()
    finally:
        sink.stop()

    decoded = read_recording(open(path, "rb").read())
    assert len(decoded.events) == 10
    assert recorder.loss(_flight.LOSS_RING_FULL) == 0, "the drains should have kept up"


def test_an_empty_drain_files_nothing(tmp_path) -> None:
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=8, active_requests=8)
    sink, path = _sink(tmp_path, recorder)
    projector = Projector(recorder, on_cells=sink.archive_cells)
    try:
        projector.poll()
        projector.poll()
    finally:
        sink.stop()
    assert read_recording(open(path, "rb").read()).events == ()


def test_a_failing_archive_is_counted_and_never_stalls_the_drain() -> None:
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=16)

    def explode(_cells: bytes) -> None:
        raise OSError("the disk is full")

    projector = Projector(recorder, on_cells=explode)
    _serve(recorder, 3)
    projector.poll()
    settled = projector.poll()

    assert settled == 3, "assembly must have run despite the archive failing"
    assert projector.snapshot().loss.export_error == 1


def test_a_degraded_sink_drops_and_counts_rather_than_raising(tmp_path) -> None:
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=16)
    sink, _path = _sink(tmp_path, recorder)
    try:
        _serve(recorder, 2)
        cells = recorder.drain(64)
        sink._degraded = True  # what a write error leaves behind
        sink.archive_cells(cells)
        assert sink.stats["dropped"] == 2
    finally:
        sink.stop()


def test_a_partial_cell_degrades_rather_than_filing_an_unsplittable_chunk(
    tmp_path,
) -> None:
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=16)
    sink, _path = _sink(tmp_path, recorder)
    try:
        sink.archive_cells(b"\x00" * (CELL_SIZE + 7))
        assert sink.stats["degraded"] is True
        assert sink.stats["write_errors"] == 1
    finally:
        sink.stop()
