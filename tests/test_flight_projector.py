"""Stage 4a -- the asynchronous projector reassembles ring cells into traces.

These drive :class:`wreath._projector.Projector` over a fake recorder whose
``drain`` hands back pre-encoded cell buffers, so assembly, settling, metric
aggregation, bounded retention, and export isolation are all exercised
deterministically without a real ring. One test drives the background thread.
"""

from __future__ import annotations

import threading
import time
from collections import deque

import pytest

from wreath._flight_schema import (
    CELL_SIZE,
    FLAG_HAS_CORRELATION,
    FLAG_SLOW_PROMOTED,
    CompletionCell,
    CorrelationCell,
    LossReason,
    PhaseBatchCell,
    PhaseKind,
    PhaseRecord,
    Protocol,
    TerminalStatus,
    histogram_bucket,
)
from wreath._projector import ProjectedTrace, Projector


class FakeRecorder:
    """A drainable stand-in: ``feed`` queues one drain's worth of cells;
    ``drain`` returns the head buffer, honoring ``max_cells`` by splitting it."""

    def __init__(self) -> None:
        self._queue: deque[bytes] = deque()
        self._loss: dict[int, int] = {}

    def feed(self, *cells: bytes) -> None:
        self._queue.append(b"".join(cells))

    def drain(self, max_cells: int = 4096) -> bytes:
        if not self._queue:
            return b""
        buf = self._queue.popleft()
        limit = max_cells * CELL_SIZE
        if len(buf) > limit:
            self._queue.appendleft(buf[limit:])
            return buf[:limit]
        return buf

    def set_loss(self, reason: int, value: int) -> None:
        self._loss[reason] = value

    def loss(self, reason: int) -> int:
        return self._loss.get(reason, 0)

    def histogram(self) -> tuple[int, ...]:
        return tuple([0] * 64)


def completion(request_id: int, **kw: object) -> bytes:
    fields: dict[str, object] = dict(
        request_id=request_id,
        connection_id=1,
        route_id=7,
        plan_id=3,
        duration_us=1000,
        status=200,
        bytes_in=10,
        bytes_out=20,
    )
    fields.update(kw)
    return CompletionCell(**fields).encode()  # type: ignore[arg-type]


def correlation(request_id: int, trace_id: int = 0xABC, span_id: int = 0xDEF) -> bytes:
    return CorrelationCell(
        request_id=request_id, trace_id=trace_id, span_id=span_id, parent_span_id=0x111
    ).encode()


def phases(request_id: int, *kinds: PhaseKind) -> bytes:
    records = tuple(
        PhaseRecord(phase_id=k, duration_us=100 + i, sequence=i)
        for i, k in enumerate(kinds)
    )
    return PhaseBatchCell(request_id=request_id, records=records).encode()


def drain_until_settled(proj: Projector, cycles: int = 3) -> None:
    for _ in range(cycles):
        proj.poll()


# --- basic assembly --------------------------------------------------------


def test_completion_only_settles_after_a_cycle() -> None:
    rec = FakeRecorder()
    rec.feed(completion(1))
    proj = Projector(rec)

    assert proj.poll() == 0  # first seen: not yet settled
    snap = proj.snapshot()
    assert snap.assembled == 0 and snap.pending == 1

    assert proj.poll() == 1  # a full cycle later: settled
    snap = proj.snapshot()
    assert snap.assembled == 1 and snap.pending == 0
    (trace,) = snap.recent
    assert trace.request_id == 1
    assert trace.route_id == 7
    assert not trace.has_correlation
    assert trace.phases == ()


class _CalibratedRecorder(FakeRecorder):
    """A FakeRecorder that also advertises a clock calibration, like a real one."""

    def __init__(self, epoch_mono_ns: int, epoch_unix_ns: int) -> None:
        super().__init__()
        self.clock_calibration = (epoch_mono_ns, epoch_unix_ns)


def test_span_time_is_derived_from_the_clock_calibration() -> None:
    # epoch_unix is a fixed wall instant; a completion whose monotonic end offset
    # is 5000 ms must map to epoch_unix + 5000 ms, drift-free -- not the wall clock
    # at finalize.
    epoch_unix = 1_700_000_000_000_000_000  # a fixed ns wall instant
    rec = _CalibratedRecorder(epoch_mono_ns=42, epoch_unix_ns=epoch_unix)
    rec.feed(completion(1, end_offset_ms=5000, duration_us=1000))
    proj = Projector(rec)
    proj.poll()
    proj.poll()
    (trace,) = proj.snapshot().recent
    assert trace.observed_unix_nano == epoch_unix + 5000 * 1_000_000


def test_completion_correlation_phases_join_in_order() -> None:
    rec = FakeRecorder()
    # The real ring order: completion, then correlation, then phase batch.
    rec.feed(
        completion(9, flags=FLAG_HAS_CORRELATION),
        correlation(9, trace_id=0x1234, span_id=0x55),
        phases(9, PhaseKind.HANDLER, PhaseKind.SERIALIZE),
    )
    proj = Projector(rec)
    drain_until_settled(proj)

    (trace,) = proj.snapshot().recent
    assert trace.trace_id == 0x1234 and trace.span_id == 0x55
    assert trace.parent_span_id == 0x111
    assert trace.has_correlation
    assert [p.phase_id for p in trace.phases] == [PhaseKind.HANDLER, PhaseKind.SERIALIZE]


def test_reordered_tail_before_completion_still_joins() -> None:
    """A batch boundary can put the correlation/phases ahead of their completion.
    The projector still settles on the quiet cycle with the whole tail joined."""
    rec = FakeRecorder()
    rec.feed(correlation(4), phases(4, PhaseKind.DB_QUERY), completion(4))
    proj = Projector(rec)

    assert proj.poll() == 0  # all present, but settle waits one quiet cycle
    assert proj.poll() == 1
    (trace,) = proj.snapshot().recent
    assert trace.has_correlation
    assert [p.phase_id for p in trace.phases] == [PhaseKind.DB_QUERY]


def test_tail_split_across_a_later_cycle_still_joins() -> None:
    rec = FakeRecorder()
    rec.feed(completion(2, flags=FLAG_HAS_CORRELATION))  # cycle 1
    rec.feed(correlation(2), phases(2, PhaseKind.AUTH))  # cycle 2
    proj = Projector(rec)

    assert proj.poll() == 0  # completion seen, tail not here yet
    assert proj.poll() == 0  # tail arrives this cycle: bumps last-seen, waits
    assert proj.poll() == 1  # a quiet cycle later: settled with its tail
    (trace,) = proj.snapshot().recent
    assert trace.has_correlation
    assert [p.phase_id for p in trace.phases] == [PhaseKind.AUTH]


def test_max_cells_split_does_not_lose_the_tail() -> None:
    rec = FakeRecorder()
    rec.feed(
        completion(3, flags=FLAG_HAS_CORRELATION),
        correlation(3),
        phases(3, PhaseKind.HANDLER),
    )
    proj = Projector(rec, max_cells=1)  # one cell per drain: 3 drains for one req
    for _ in range(6):
        proj.poll()
    snap = proj.snapshot()
    assert snap.assembled == 1
    (trace,) = snap.recent
    assert trace.has_correlation and len(trace.phases) == 1


# --- orphans and loss ------------------------------------------------------


def test_orphan_correlation_without_completion_is_counted() -> None:
    rec = FakeRecorder()
    rec.feed(correlation(5))  # a head-dropped request: correlation, no completion
    proj = Projector(rec)
    drain_until_settled(proj)

    snap = proj.snapshot()
    assert snap.assembled == 0
    assert snap.loss.orphan_correlation == 1
    assert snap.pending == 0  # retired, not held forever


def test_orphan_phase_without_completion_is_counted() -> None:
    rec = FakeRecorder()
    rec.feed(phases(6, PhaseKind.DB_QUERY))
    proj = Projector(rec)
    drain_until_settled(proj)

    assert proj.snapshot().loss.orphan_phase == 1


def test_corrupt_cell_counts_a_decode_error() -> None:
    rec = FakeRecorder()
    bad = bytearray(completion(8))
    bad[0] ^= 0xFF  # wrong schema version, kind byte still COMPLETION
    rec.feed(bytes(bad))
    proj = Projector(rec)
    drain_until_settled(proj)

    snap = proj.snapshot()
    assert snap.assembled == 0
    assert snap.loss.decode_error == 1


def test_pending_overflow_evicts_oldest_and_counts_it() -> None:
    rec = FakeRecorder()
    rec.feed(*(completion(i) for i in range(1, 6)))  # 5 completions, one drain
    proj = Projector(rec, pending=3)

    proj.poll()  # ingest all 5; table capped at 3 -> 2 evicted
    snap = proj.snapshot()
    assert snap.loss.pending_evicted == 2
    assert snap.pending == 3


def test_pending_overflow_categorizes_an_evicted_correlation_orphan() -> None:
    rec = FakeRecorder()
    rec.feed(correlation(1), correlation(2))
    proj = Projector(rec, pending=1)

    proj.poll()
    snap = proj.snapshot()

    assert snap.loss.orphan_correlation == 1
    assert snap.loss.pending_evicted == 0
    assert snap.pending == 1


def test_unknown_kind_cells_are_ignored() -> None:
    rec = FakeRecorder()
    control = bytearray(CELL_SIZE)
    control[0] = 1  # schema version
    control[1] = 4  # EventKind.CONTROL -- no projector payload
    rec.feed(bytes(control), completion(1))
    proj = Projector(rec)
    drain_until_settled(proj)

    snap = proj.snapshot()
    assert snap.assembled == 1
    assert snap.loss.decode_error == 0


# --- failures and metrics --------------------------------------------------


def test_failures_are_retained_separately() -> None:
    rec = FakeRecorder()
    rec.feed(
        completion(1, status=200, terminal=TerminalStatus.OK),
        completion(2, status=500, terminal=TerminalStatus.ERROR),
        completion(3, status=200, flags=FLAG_SLOW_PROMOTED),
    )
    proj = Projector(rec)
    drain_until_settled(proj)

    snap = proj.snapshot()
    assert snap.assembled == 3
    failure_ids = {t.request_id for t in snap.failures}
    assert failure_ids == {2, 3}  # error terminal and slow-promoted, not the 200/OK


def test_route_metrics_aggregate_counts_errors_and_duration() -> None:
    rec = FakeRecorder()
    rec.feed(
        completion(1, route_id=100, duration_us=500, status=200),
        completion(2, route_id=100, duration_us=1500, status=500,
                   terminal=TerminalStatus.ERROR),
        completion(3, route_id=200, duration_us=300, status=200),
    )
    proj = Projector(rec)
    drain_until_settled(proj)

    routes = {m.route_id: m for m in proj.snapshot().routes}
    assert routes[100].count == 2
    assert routes[100].errors == 1
    assert routes[100].duration_us_sum == 2000
    assert routes[100].duration_us_max == 1500
    assert routes[100].buckets[histogram_bucket(500)] == 1
    assert routes[100].buckets[histogram_bucket(1500)] == 1
    assert routes[200].count == 1 and routes[200].errors == 0


def test_route_cardinality_ceiling_is_bounded() -> None:
    rec = FakeRecorder()
    rec.feed(*(completion(i, route_id=i) for i in range(1, 11)))
    proj = Projector(rec, max_routes=4)
    drain_until_settled(proj)

    snap = proj.snapshot()
    assert snap.assembled == 10  # every request is still assembled and retained
    assert len(snap.routes) == 4  # but the metric table is capped


def test_recent_window_is_bounded_and_counts_eviction() -> None:
    rec = FakeRecorder()
    rec.feed(*(completion(i) for i in range(1, 6)))
    proj = Projector(rec, recent=2)
    drain_until_settled(proj)

    snap = proj.snapshot()
    assert len(snap.recent) == 2
    assert {t.request_id for t in snap.recent} == {4, 5}  # newest kept
    assert snap.loss.recent_evicted == 3


def test_recorder_loss_is_read_through() -> None:
    rec = FakeRecorder()
    rec.set_loss(int(LossReason.RING_FULL), 42)
    proj = Projector(rec)
    assert proj.recorder_loss()[LossReason.RING_FULL] == 42


# --- export hook -----------------------------------------------------------


def test_export_hook_receives_each_finished_trace() -> None:
    seen: list[ProjectedTrace] = []
    rec = FakeRecorder()
    rec.feed(completion(1), completion(2))
    proj = Projector(rec, on_trace=seen.append)
    drain_until_settled(proj)

    assert {t.request_id for t in seen} == {1, 2}


def test_export_hook_failure_is_isolated_and_counted() -> None:
    def boom(_trace: ProjectedTrace) -> None:
        raise RuntimeError("exporter down")

    rec = FakeRecorder()
    rec.feed(completion(1), completion(2))
    proj = Projector(rec, on_trace=boom)
    drain_until_settled(proj)

    snap = proj.snapshot()
    assert snap.assembled == 2  # assembly and retention are unaffected
    assert snap.loss.export_error == 2


# --- background thread -----------------------------------------------------


def test_background_thread_drains_and_stop_flushes() -> None:
    rec = FakeRecorder()
    for i in range(1, 21):
        rec.feed(completion(i))
    seen: list[int] = []
    lock = threading.Lock()

    def on_trace(t: ProjectedTrace) -> None:
        with lock:
            seen.append(t.request_id)

    proj = Projector(rec, interval=0.005, on_trace=on_trace)
    proj.start()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with lock:
            if len(seen) >= 20:
                break
        time.sleep(0.01)
    proj.stop()

    assert set(seen) == set(range(1, 21))


def test_start_is_idempotent() -> None:
    rec = FakeRecorder()
    proj = Projector(rec, interval=0.01)
    proj.start()
    proj.start()  # must not spawn a second thread or raise
    proj.stop()


def test_construction_validates_tuning() -> None:
    rec = FakeRecorder()
    with pytest.raises(ValueError):
        Projector(rec, interval=0)
    with pytest.raises(ValueError):
        Projector(rec, max_cells=0)


# --- integration over a real native recorder -------------------------------

_flight = pytest.importorskip("wreath._native._flight")
_TRACEPARENT = b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_projects_a_real_detailed_recorder_end_to_end() -> None:
    """Drive an armed Detailed request through the real ring -- completion,
    correlation, and phase cells -- and prove the projector reassembles it."""
    rec = _flight.Recorder(
        _flight.MODE_DETAILED,
        ring_records=1024,
        active_requests=64,
        detailed_sample_rate=1.0,  # arm every request
        phase_slots=8,
    )
    req = rec.begin(connection_id=42, protocol=_flight.PROTO_HTTP1, start_ns=0)
    req.route(101, 55)
    req.propagate(_TRACEPARENT)
    req.phase(phase_id=int(PhaseKind.HANDLER), duration_us=300)
    req.phase(phase_id=int(PhaseKind.SERIALIZE), duration_us=40)
    req.finish(now_ns=1_000_000, status=200, bytes_in=11, bytes_out=22)

    proj = Projector(rec)
    drain_until_settled(proj)

    snap = proj.snapshot()
    assert snap.assembled == 1
    (trace,) = snap.recent
    assert trace.route_id == 101 and trace.plan_id == 55
    assert trace.connection_id == 42
    assert trace.protocol == Protocol.HTTP1
    assert trace.bytes_in == 11 and trace.bytes_out == 22
    assert trace.has_correlation
    assert trace.trace_id == 0x4BF92F3577B34DA6A3CE929D0E0E4736
    assert trace.span_id != 0  # a fresh span id was generated
    assert [p.phase_id for p in trace.phases] == [PhaseKind.HANDLER, PhaseKind.SERIALIZE]
    assert {m.route_id for m in snap.routes} == {101}


def test_projects_many_real_completions() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=8192, active_requests=256)
    for i in range(200):
        rec.record(
            start_ns=0, end_ns=(i + 1) * 1000, status=200,
            connection_id=i, route_id=(i % 5), plan_id=0,
        )
    proj = Projector(rec)
    drain_until_settled(proj)

    snap = proj.snapshot()
    assert snap.assembled == 200
    assert sum(m.count for m in snap.routes) == 200
    assert len(snap.routes) == 5
