"""Records made from a thread that must not write to the ring.

The ring is single-writer by construction: `ring_publish` reads the head, copies
a cell, and stores the head back with no interlock, because exactly one thread
does it. A `wreath.jobs` worker or a thread-pool task calling a log site is a
second writer, and two of them interleaved do not merely lose a record -- they
overwrite one and advance the head anyway, corrupting every cell after it.

So an off-loop record takes a counted slow path: staged in a bounded queue,
published by the loop on its next drain, flagged `off-loop` so a reader can tell
a late record from a reordered one, and dropped-with-a-count when the stage is
full. These tests hold that shape, including the parts that only matter when
something is already going wrong.
"""

from __future__ import annotations

import threading

import pytest

from wreath import logging as log
from wreath._flight_schema import LOG_FLAG_OFF_LOOP, LogCell, Severity
from wreath._logscratch import LogSamplingPolicy, OffLoopStage


def _runtime(sink: list[LogCell], *, capacity: int = 8) -> log.LogRuntime:
    """A bound runtime with per-call-site limiting off.

    Every record here comes from one interned template, and the default policy
    passes the first 100 from a site per second and then one in 100 -- so a
    burst of 400 stages 103 and a test that expected 400 would be reading the
    limiter's answer while claiming to read the stage's.
    """
    runtime = log.LogRuntime(
        sink.append,
        level=log.INFO,
        off_loop_capacity=capacity,
        sampling=LogSamplingPolicy(enabled=False),
    )
    runtime.bind_writer()
    return runtime


def _emit_on_another_thread(fn: object) -> None:
    thread = threading.Thread(target=fn)  # type: ignore[arg-type]
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive()


def test_a_record_from_the_loop_reaches_the_sink_immediately() -> None:
    written: list[LogCell] = []
    previous = log.install(_runtime(written))
    try:
        log.info("on the loop {n}", n=1)
    finally:
        log.install(previous)
    assert len(written) == 1
    assert not written[0].flags & LOG_FLAG_OFF_LOOP


def test_a_record_from_another_thread_is_staged_rather_than_published() -> None:
    written: list[LogCell] = []
    runtime = _runtime(written)
    previous = log.install(runtime)
    try:
        _emit_on_another_thread(lambda: log.info("off the loop {n}", n=1))
        # Nothing reached the sink: the other thread may not write to the ring.
        assert written == []
        assert log.off_loop_counts() == {"staged": 1, "dropped": 0, "held": 1}
        drained = runtime.drain_off_loop()
    finally:
        log.install(previous)
    assert drained == 1
    assert len(written) == 1
    assert written[0].flags & LOG_FLAG_OFF_LOOP, "a late record must say it was late"


def test_the_native_emitter_is_refused_off_the_loop() -> None:
    """The fast path is a ring write; off the loop it must not be taken.

    A native emitter that ran here would pack straight into a ring slot from the
    wrong thread, which is the exact corruption the slow path exists to prevent.
    """
    _flight = pytest.importorskip("wreath._native._flight", exc_type=ImportError)
    recorder = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    runtime = log.LogRuntime(
        log.recorder_sink(recorder),
        level=log.INFO,
        native=log.recorder_emitter(recorder),
    )
    runtime.bind_writer()
    previous = log.install(runtime)
    try:
        assert runtime.native is not None
        _emit_on_another_thread(lambda: log.info("off the loop {n}", n=1))
        assert len(recorder.drain(8)) == 0, "the ring was written from the wrong thread"
        assert log.off_loop_counts()["staged"] == 1
        runtime.drain_off_loop()
    finally:
        log.install(previous)
    cell = LogCell.decode(bytes(recorder.drain(1)))
    assert cell.flags & LOG_FLAG_OFF_LOOP
    assert cell.severity == Severity.INFO


def test_a_full_stage_drops_and_counts_rather_than_growing() -> None:
    written: list[LogCell] = []
    runtime = _runtime(written, capacity=3)
    previous = log.install(runtime)
    try:

        def burst() -> None:
            for index in range(10):
                log.info("burst {n}", n=index)

        _emit_on_another_thread(burst)
        counts = log.off_loop_counts()
    finally:
        log.install(previous)
    assert counts == {"staged": 3, "dropped": 7, "held": 3}


def test_an_overflowing_stage_keeps_the_oldest_records() -> None:
    """Same rule as the request buffer: a burst is legible from its beginning."""
    stage = OffLoopStage(capacity=2)
    for index in range(5):
        stage.stage(LogCell(request_id=0, site_id=1, severity=Severity.INFO,
                            args=(), dropped_siblings=index))
    held = stage.drain()
    assert [cell.dropped_siblings for cell in held] == [0, 1]
    assert stage.dropped == 3


def test_draining_twice_is_harmless() -> None:
    written: list[LogCell] = []
    runtime = _runtime(written)
    previous = log.install(runtime)
    try:
        _emit_on_another_thread(lambda: log.info("once {n}", n=1))
        assert runtime.drain_off_loop() == 1
        assert runtime.drain_off_loop() == 0
    finally:
        log.install(previous)
    assert len(written) == 1


def test_a_runtime_without_a_bound_writer_publishes_from_any_thread() -> None:
    """A sink that is not a ring has no single-writer rule to keep.

    Every test capture, every plain-callable sink, and every process that never
    booted a server is in this state, and paying for a thread check there would
    be a tax on the common case to serve the rare one.
    """
    written: list[LogCell] = []
    runtime = log.LogRuntime(written.append, level=log.INFO)
    assert runtime.off_loop is None
    previous = log.install(runtime)
    try:
        _emit_on_another_thread(lambda: log.info("anywhere {n}", n=1))
    finally:
        log.install(previous)
    assert len(written) == 1
    assert not written[0].flags & LOG_FLAG_OFF_LOOP


def test_concurrent_threads_all_stage_without_losing_a_record() -> None:
    written: list[LogCell] = []
    runtime = _runtime(written, capacity=1024)
    previous = log.install(runtime)
    try:

        def burst() -> None:
            for index in range(50):
                log.info("burst {n}", n=index)

        threads = [threading.Thread(target=burst) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
        counts = log.off_loop_counts()
        drained = runtime.drain_off_loop()
    finally:
        log.install(previous)
    assert counts["dropped"] == 0
    assert counts["staged"] == 400
    assert drained == 400
    assert len(written) == 400


def test_a_buffered_record_promoted_off_the_loop_still_takes_the_slow_path() -> None:
    """Promotion republishes held records, and it can happen anywhere.

    A job that opens a log scope, holds DEBUG records and then fails is the case
    this covers: the promotion runs on the worker thread, so every record it
    publishes has to be staged rather than written.
    """
    written: list[LogCell] = []
    runtime = _runtime(written, capacity=16)
    previous = log.install(runtime)
    try:

        def work() -> None:
            with log.request_scope(7) as scope:
                log.debug("step {name}", name="charge")
                log.debug("step {name}", name="settle")
                scope.promote()

        runtime.capture_level = log.DEBUG
        _emit_on_another_thread(work)
        assert written == []
        assert log.off_loop_counts()["staged"] == 2
        runtime.drain_off_loop()
    finally:
        log.install(previous)
    assert len(written) == 2
    assert all(cell.flags & LOG_FLAG_OFF_LOOP for cell in written)
    assert all(cell.request_id == 7 for cell in written)
