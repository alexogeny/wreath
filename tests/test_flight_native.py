"""Stage 1 Native Flight Recorder core: ring, active table, completion, losses.

Drives the native ``_flight.Recorder`` directly (no server yet) and checks it
against the pure oracle in ``wreath._pure.flight``. The extension is optional;
tests skip cleanly if it was not built.
"""

from __future__ import annotations

import random

import pytest

from wreath import _flight_schema as fs
from wreath._pure.flight import PureRecorder

_flight = pytest.importorskip("wreath._native._flight")


def test_off_mode_does_nothing() -> None:
    rec = _flight.Recorder(_flight.MODE_OFF)
    rec.record(start_ns=0, end_ns=10_000, status=200)
    assert rec.requests == 0
    assert rec.completions == 0
    assert rec.drain() == b""
    assert rec.active_count == 0


def test_pulse_emits_one_completion_cell() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, active_requests=8)
    rec.record(
        start_ns=1_000,
        end_ns=2_000_000,
        connection_id=9,
        protocol=_flight.PROTO_HTTP1,
        route_id=4,
        plan_id=1,
        status=200,
        terminal=fs.TerminalStatus.OK,
        bytes_in=64,
        bytes_out=1024,
    )
    assert rec.requests == 1 and rec.completions == 1
    blob = rec.drain()
    assert len(blob) == fs.CELL_SIZE
    cell = fs.CompletionCell.decode(blob)
    assert cell.status == 200
    assert cell.route_id == 4
    assert cell.protocol is fs.Protocol.HTTP1
    assert cell.bytes_out == 1024
    assert cell.duration_us == (2_000_000 - 1_000) // 1000


def test_completion_summaries_off_keeps_counters_without_cells() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, completion_summaries=False)
    rec.record(start_ns=0, end_ns=5_000, status=200)
    assert rec.completions == 1
    assert rec.drain() == b""
    assert sum(rec.histogram()) == 1


def test_terminal_status_and_error_class_round_trip() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=8)
    rec.record(
        start_ns=0, end_ns=1000, status=500,
        terminal=fs.TerminalStatus.ERROR, error_class=7,
    )
    cell = fs.CompletionCell.decode(rec.drain())
    assert cell.terminal is fs.TerminalStatus.ERROR
    assert cell.error_class == 7


# --- ring behavior ----------------------------------------------------------


def test_ring_full_drops_and_counts_loss() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=4, active_requests=64)
    for i in range(10):  # 4 fit, 6 dropped
        rec.record(start_ns=i, end_ns=i + 1000, status=200)
    assert rec.ring_occupancy == 4
    assert rec.loss(_flight.LOSS_RING_FULL) == 6
    assert rec.completions == 10  # counters still count every request
    assert rec.ring_high_water == 4


def test_ring_wraps_and_preserves_sequence() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=4, active_requests=64)
    seen: list[int] = []
    for i in range(20):
        rec.record(start_ns=0, end_ns=1000, status=200 + i)
        blob = rec.drain()  # drain each so the ring never fills
        for j in range(0, len(blob), fs.CELL_SIZE):
            seen.append(fs.CompletionCell.decode(blob[j : j + fs.CELL_SIZE]).status)
    assert seen == [200 + i for i in range(20)]
    assert rec.loss(_flight.LOSS_RING_FULL) == 0


# --- active table -----------------------------------------------------------


def test_active_slots_reserve_release_and_reuse() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=8, active_requests=2)
    a = rec.begin(start_ns=1)
    b = rec.begin(start_ns=2)
    assert rec.active_count == 2
    assert {a.active_slot, b.active_slot} == {0, 1}
    # table full -> next reservation fails and is counted
    c = rec.begin(start_ns=3)
    assert c.active_slot == -1
    assert rec.loss(_flight.LOSS_ACTIVE_TABLE_FULL) == 1
    a.finish(now_ns=100, status=200)
    assert rec.active_count == 1
    d = rec.begin(start_ns=4)  # reuses the freed slot
    assert d.active_slot == 0
    assert rec.active_count == 2


def test_abandoned_request_releases_its_slot() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, active_requests=4)
    r = rec.begin(start_ns=1)
    assert rec.active_count == 1
    r.abandon()
    assert rec.active_count == 0
    assert rec.drain() == b""  # abandon emits no cell


def test_dropped_request_releases_its_slot() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, active_requests=4)
    r = rec.begin(start_ns=1)
    assert rec.active_count == 1
    del r  # GC/teardown must release the slot
    assert rec.active_count == 0


# --- histograms -------------------------------------------------------------


def test_histogram_records_log2_buckets() -> None:
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=64)
    rec.record(start_ns=0, end_ns=1000, status=200)  # ~1us -> bucket 0
    rec.record(start_ns=0, end_ns=1_000_000, status=200)  # ~1000us -> bucket 9
    hist = rec.histogram()
    assert hist[0] == 1
    assert hist[fs.histogram_bucket(1000)] == 1
    assert sum(hist) == 2


# --- differential against the pure oracle -----------------------------------


def _run_sequence(rec, seed: int) -> tuple[bytes, tuple]:
    rng = random.Random(seed)
    for _ in range(500):
        start = rng.randrange(0, 1_000_000)
        dur = rng.randrange(1, 5_000_000)
        rec.record(
            start_ns=start,
            end_ns=start + dur,
            connection_id=rng.randrange(0, 1000),
            protocol=rng.choice(
                [_flight.PROTO_HTTP1, _flight.PROTO_HTTP2, _flight.PROTO_HTTP3]
            ),
            route_id=rng.randrange(0, 50),
            plan_id=rng.randrange(0, 10),
            status=rng.choice([200, 204, 404, 500]),
            terminal=rng.choice([0, 1, 2]),
            bytes_in=rng.randrange(0, 10_000),
            bytes_out=rng.randrange(0, 100_000),
        )
    cells = rec.drain(10_000)
    snapshot = (
        rec.requests,
        rec.completions,
        rec.ring_high_water,
        rec.loss(_flight.LOSS_RING_FULL),
        rec.histogram(),
    )
    return cells, snapshot


def test_native_matches_pure_oracle() -> None:
    native = _flight.Recorder(_flight.MODE_PULSE, ring_records=256, active_requests=64)
    pure = PureRecorder(fs.Mode.PULSE, ring_records=256, active_requests=64)
    native_cells, native_snap = _run_sequence(native, seed=1234)
    pure_cells, pure_snap = _run_sequence(pure, seed=1234)
    assert native_cells == pure_cells
    assert native_snap == pure_snap


def test_native_matches_pure_oracle_under_ring_pressure() -> None:
    # A tiny ring so most completions drop: loss accounting must agree exactly.
    native = _flight.Recorder(_flight.MODE_PULSE, ring_records=8, active_requests=64)
    pure = PureRecorder(fs.Mode.PULSE, ring_records=8, active_requests=64)
    n_cells, n_snap = _run_sequence(native, seed=99)
    p_cells, p_snap = _run_sequence(pure, seed=99)
    assert n_cells == p_cells
    assert n_snap == p_snap
    assert native.loss(_flight.LOSS_RING_FULL) > 0


# --- Stage 3: Detailed-mode arming ------------------------------------------


def _armed_fraction(rec, n: int) -> float:
    for i in range(n):
        rec.record(start_ns=i * 1000, end_ns=i * 1000 + 500)
    blob = rec.drain(n)
    cells = [
        fs.CompletionCell.decode(blob[i * fs.CELL_SIZE : (i + 1) * fs.CELL_SIZE])
        for i in range(len(blob) // fs.CELL_SIZE)
    ]
    return sum(bool(c.flags & fs.FLAG_DETAILED_ARMED) for c in cells) / n


@pytest.mark.parametrize("rate", [0.0, 0.25, 0.5, 1.0])
def test_detailed_arming_tracks_sample_rate(rate: float) -> None:
    rec = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=8192, active_requests=64,
        detailed_sample_rate=rate,
    )
    fraction = _armed_fraction(rec, 4000)
    if rate in (0.0, 1.0):
        assert fraction == rate  # deterministic bounds are exact
    else:
        assert abs(fraction - rate) < 0.05  # deterministic sample, ~uniform


def test_pulse_never_arms_even_at_full_rate() -> None:
    # Arming is a Detailed-mode concept; Pulse must stay crossing/flag identical.
    rec = _flight.Recorder(
        _flight.MODE_PULSE, ring_records=1024, detailed_sample_rate=1.0
    )
    assert _armed_fraction(rec, 500) == 0.0


def test_detailed_arming_is_deterministic_per_request() -> None:
    # The same request id makes the same decision across independent recorders.
    def flags(seed_rate: float) -> list[int]:
        rec = _flight.Recorder(
            _flight.MODE_DETAILED, ring_records=2048, detailed_sample_rate=seed_rate
        )
        for i in range(300):
            rec.record(start_ns=i, end_ns=i + 1)
        blob = rec.drain(300)
        return [
            fs.CompletionCell.decode(
                blob[i * fs.CELL_SIZE : (i + 1) * fs.CELL_SIZE]
            ).flags & fs.FLAG_DETAILED_ARMED
            for i in range(len(blob) // fs.CELL_SIZE)
        ]

    assert flags(0.5) == flags(0.5)


def test_invalid_sample_rate_is_rejected() -> None:
    with pytest.raises(ValueError):
        _flight.Recorder(_flight.MODE_DETAILED, detailed_sample_rate=1.5)
    with pytest.raises(ValueError):
        PureRecorder(fs.Mode.DETAILED, detailed_sample_rate=-0.1)


def _run_detailed_sequence(rec, seed: int) -> bytes:
    rng = random.Random(seed)
    for _ in range(500):
        start = rng.randrange(0, 1_000_000)
        rec.record(start_ns=start, end_ns=start + rng.randrange(1, 5_000_000))
    return rec.drain(10_000)


def test_native_matches_pure_oracle_detailed_arming() -> None:
    # The armed flag rides the completion cell, so drained cells must be
    # byte-identical between the native worker and the pure oracle.
    native = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=1024, active_requests=64,
        detailed_sample_rate=0.5,
    )
    pure = PureRecorder(
        fs.Mode.DETAILED, ring_records=1024, active_requests=64,
        detailed_sample_rate=0.5,
    )
    assert _run_detailed_sequence(native, seed=7) == _run_detailed_sequence(pure, seed=7)


# --- Stage 3 slice 2: phase scratch + batch commit --------------------------


def _phase_cells(blob: bytes) -> list:
    out = []
    for i in range(len(blob) // fs.CELL_SIZE):
        cell = blob[i * fs.CELL_SIZE : (i + 1) * fs.CELL_SIZE]
        if cell[1] == fs.EventKind.PHASE:
            out.append(fs.PhaseBatchCell.decode(cell))
    return out


def test_phases_commit_as_batches_after_the_completion() -> None:
    rec = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=64, active_requests=8,
        detailed_sample_rate=1.0, phase_slots=4,
    )
    req = rec.begin(connection_id=9, protocol=fs.Protocol.HTTP1, start_ns=1000)
    kinds = [
        fs.PhaseKind.BINDING, fs.PhaseKind.AUTH, fs.PhaseKind.HANDLER,
        fs.PhaseKind.DB_QUERY, fs.PhaseKind.SERIALIZE,
    ]
    for seq, kind in enumerate(kinds):
        req.phase(phase_id=int(kind), dependency_id=seq, coverage=int(fs.PhaseCoverage.PYTHON),
                  start_offset_us=seq * 10, duration_us=seq + 1)
    assert req.phase_count == 5
    req.finish(now_ns=1000 + 200_000, status=200)

    blob = rec.drain()
    # completion first, then 2 phase batch cells (3 + 2 records).
    assert blob[1] == fs.EventKind.COMPLETION
    batches = _phase_cells(blob)
    assert [len(b.records) for b in batches] == [3, 2]
    assert all(b.request_id == 1 for b in batches)
    records = [r for b in batches for r in b.records]
    assert [r.phase_id for r in records] == kinds
    assert [r.sequence for r in records] == [0, 1, 2, 3, 4]
    assert records[3].dependency_id == 3


def test_phase_budget_overflow_drops_and_counts_loss() -> None:
    rec = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=64, detailed_sample_rate=1.0, phase_slots=2,
    )
    req = rec.begin(start_ns=0)
    for i in range(fs.PHASE_CELL_BUDGET + 5):  # five past the budget
        req.phase(phase_id=int(fs.PhaseKind.HANDLER), duration_us=i)
    assert req.phase_count == fs.PHASE_CELL_BUDGET  # capped
    assert rec.loss(int(fs.LossReason.PHASE_SCRATCH_FULL)) == 5
    req.finish(now_ns=1000, status=200)
    records = [r for b in _phase_cells(rec.drain()) for r in b.records]
    assert len(records) == fs.PHASE_CELL_BUDGET


def test_phase_pool_exhaustion_counts_loss_and_arms_without_scratch() -> None:
    # Two scratch blocks, three concurrent armed requests: the third arms but
    # records no phases (loss counted), and completes normally.
    rec = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=64, active_requests=8,
        detailed_sample_rate=1.0, phase_slots=2,
    )
    a = rec.begin(start_ns=1)
    b = rec.begin(start_ns=2)
    c = rec.begin(start_ns=3)
    for r in (a, b, c):
        r.phase(phase_id=int(fs.PhaseKind.HANDLER), duration_us=1)
    assert (a.phase_count, b.phase_count) == (1, 1)
    assert c.phase_count == 0  # armed, but no scratch block was available
    assert rec.loss(int(fs.LossReason.PHASE_SCRATCH_FULL)) == 1
    for r in (a, b, c):
        r.finish(now_ns=1000, status=200)
    # a and b each commit one batch; c commits none.
    assert len(_phase_cells(rec.drain())) == 2


def test_unarmed_requests_record_no_phases() -> None:
    # Pulse never arms, so phase() is a no-op and no phase cells are emitted.
    rec = _flight.Recorder(_flight.MODE_PULSE, ring_records=64, phase_slots=4)
    req = rec.begin(start_ns=0)
    req.phase(phase_id=int(fs.PhaseKind.HANDLER), duration_us=1)
    assert req.phase_count == 0
    req.finish(now_ns=1000, status=200)
    assert _phase_cells(rec.drain()) == []


def test_phase_slot_is_released_and_reused() -> None:
    # One scratch block, reused across sequential requests.
    rec = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=64, detailed_sample_rate=1.0, phase_slots=1,
    )
    for _ in range(5):
        req = rec.begin(start_ns=0)
        req.phase(phase_id=int(fs.PhaseKind.INGRESS), duration_us=1)
        assert req.phase_count == 1  # the single slot is free each time
        req.finish(now_ns=1000, status=200)
    assert rec.loss(int(fs.LossReason.PHASE_SCRATCH_FULL)) == 0


def test_abandoned_armed_request_commits_no_phases_but_frees_the_slot() -> None:
    rec = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=64, detailed_sample_rate=1.0, phase_slots=1,
    )
    req = rec.begin(start_ns=0)
    req.phase(phase_id=int(fs.PhaseKind.HANDLER), duration_us=1)
    req.abandon()
    assert _phase_cells(rec.drain()) == []  # nothing committed
    # The slot returned to the pool: a later request can record phases.
    req2 = rec.begin(start_ns=0)
    req2.phase(phase_id=int(fs.PhaseKind.HANDLER), duration_us=1)
    assert req2.phase_count == 1
    req2.finish(now_ns=1000, status=200)
    assert len(_phase_cells(rec.drain())) == 1


def _run_phase_sequence(rec, seed: int) -> tuple[bytes, tuple]:
    rng = random.Random(seed)
    for i in range(400):
        r = rec.begin(
            connection_id=rng.randrange(1000),
            protocol=rng.choice([_flight.PROTO_HTTP1, _flight.PROTO_HTTP2]),
            start_ns=i * 1000,
        )
        for _ in range(rng.randrange(0, 14)):  # sometimes exceeds the budget
            r.phase(
                phase_id=rng.randrange(0, 13),
                dependency_id=rng.randrange(0, 20),
                coverage=rng.randrange(0, 4),
                start_offset_us=rng.randrange(0, 500),
                duration_us=rng.randrange(0, 1000),
            )
        if rng.random() < 0.9:
            r.finish(now_ns=i * 1000 + rng.randrange(1, 900_000),
                     status=rng.choice([200, 404, 500]), terminal=rng.randrange(0, 3))
        else:
            r.abandon()
    snap = (
        rec.requests, rec.completions,
        rec.loss(int(fs.LossReason.PHASE_SCRATCH_FULL)),
        rec.loss(int(fs.LossReason.RING_FULL)),
    )
    return rec.drain(20_000), snap


def test_native_matches_pure_oracle_with_phases_under_pressure() -> None:
    # Small ring and phase pool so both ring-full and scratch-full losses fire;
    # drained cells (completions + phase batches) and counters must agree exactly.
    native = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=64, active_requests=64,
        detailed_sample_rate=0.5, phase_slots=8,
    )
    pure = PureRecorder(
        fs.Mode.DETAILED, ring_records=64, active_requests=64,
        detailed_sample_rate=0.5, phase_slots=8,
    )
    n_cells, n_snap = _run_phase_sequence(native, seed=2024)
    p_cells, p_snap = _run_phase_sequence(pure, seed=2024)
    assert n_cells == p_cells
    assert n_snap == p_snap
    assert native.loss(int(fs.LossReason.PHASE_SCRATCH_FULL)) > 0
    assert native.loss(int(fs.LossReason.RING_FULL)) > 0


# --- Stage 3 slice 3: completion promotion (slow / error) -------------------


def _only_cell(rec) -> fs.CompletionCell:
    return fs.CompletionCell.decode(rec.drain()[: fs.CELL_SIZE])


def test_slow_completion_is_promoted() -> None:
    rec = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=16, detailed_sample_rate=0.0,
        detailed_slow_us=1000,
    )
    rec.record(start_ns=0, end_ns=2_000_000, status=200, terminal=0)  # 2000us
    cell = _only_cell(rec)
    assert cell.flags & fs.FLAG_SLOW_PROMOTED
    assert not cell.flags & fs.FLAG_ERROR_PROMOTED


def test_fast_completion_is_not_promoted() -> None:
    rec = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=16, detailed_sample_rate=0.0,
        detailed_slow_us=1000,
    )
    rec.record(start_ns=0, end_ns=500_000, status=200, terminal=0)  # 500us < 1000
    assert not _only_cell(rec).flags & fs.FLAG_SLOW_PROMOTED


def test_error_and_timeout_completions_are_promoted() -> None:
    rec = _flight.Recorder(_flight.MODE_DETAILED, ring_records=16, detailed_sample_rate=0.0)
    rec.record(start_ns=0, end_ns=1000, status=500,
               terminal=int(fs.TerminalStatus.ERROR))
    assert _only_cell(rec).flags & fs.FLAG_ERROR_PROMOTED
    rec.record(start_ns=0, end_ns=1000, status=504,
               terminal=int(fs.TerminalStatus.TIMEOUT))
    assert _only_cell(rec).flags & fs.FLAG_ERROR_PROMOTED


def test_pulse_never_promotes() -> None:
    # Promotion is a Detailed concept; Pulse cells stay byte-identical to Stage 2.
    rec = _flight.Recorder(
        _flight.MODE_PULSE, ring_records=16, detailed_sample_rate=0.0, detailed_slow_us=1,
    )
    rec.record(start_ns=0, end_ns=9_000_000, status=500,
               terminal=int(fs.TerminalStatus.ERROR))
    cell = _only_cell(rec)
    assert not cell.flags & fs.FLAG_SLOW_PROMOTED
    assert not cell.flags & fs.FLAG_ERROR_PROMOTED


def test_promotion_matches_pure_oracle() -> None:
    def drive(rec) -> bytes:
        rng = random.Random(11)
        for i in range(300):
            rec.record(
                start_ns=i * 1000,
                end_ns=i * 1000 + rng.randrange(1, 4_000_000),
                status=rng.choice([200, 500, 504]),
                terminal=rng.randrange(0, 5),
            )
        return rec.drain(10_000)

    native = _flight.Recorder(
        _flight.MODE_DETAILED, ring_records=512, detailed_sample_rate=0.3,
        detailed_slow_us=1500,
    )
    pure = PureRecorder(
        fs.Mode.DETAILED, ring_records=512, detailed_sample_rate=0.3,
        detailed_slow_us=1500,
    )
    assert drive(native) == drive(pure)


def test_invalid_slow_threshold_config_is_rejected() -> None:
    from wreath.telemetry import Mode as TMode
    from wreath.telemetry import TelemetryConfig

    with pytest.raises(Exception):  # noqa: B017 - TelemetryConfigError
        TelemetryConfig(mode=TMode.DETAILED, detailed_slow_us=-1)
