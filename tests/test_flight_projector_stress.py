from __future__ import annotations

import random
import threading
import time
from collections import deque

from wreath._flight_schema import (
    CELL_SIZE,
    CompletionCell,
    CorrelationCell,
    PhaseBatchCell,
    PhaseKind,
    PhaseRecord,
)
from wreath._projector import Projector


class ChunkRecorder:
    """A recorder whose ``drain`` returns pre-chunked buffers in order."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = deque(chunks)

    def drain(self, max_cells: int = 4096) -> bytes:
        if not self._chunks:
            return b""
        buf = self._chunks.popleft()
        limit = max_cells * CELL_SIZE
        if len(buf) > limit:
            self._chunks.appendleft(buf[limit:])
            return buf[:limit]
        return buf

    def loss(self, reason: int) -> int:
        return 0

    def empty(self) -> bool:
        return not self._chunks


def _completion(rid: int) -> bytes:
    return CompletionCell(
        request_id=rid,
        connection_id=1,
        route_id=rid % 4,
        plan_id=0,
        duration_us=100,
        status=200,
        bytes_in=0,
        bytes_out=0,
    ).encode()


def _correlation(rid: int) -> bytes:
    return CorrelationCell(request_id=rid, trace_id=rid or 1, span_id=rid or 1).encode()


def _phase_batch(rid: int, n: int, seq0: int) -> bytes:
    records = tuple(
        PhaseRecord(phase_id=PhaseKind.HANDLER, duration_us=1, sequence=seq0 + i) for i in range(n)
    )
    return PhaseBatchCell(request_id=rid, records=records).encode()


def _drain_all(proj: Projector, recorder: ChunkRecorder) -> None:
    # Feed every chunk, then poll quiet cycles until nothing is pending.
    while not recorder.empty():
        proj.poll()
    for _ in range(4):  # settle + retire orphans
        proj.poll()


def test_reassembly_is_exact_over_random_interleavings() -> None:
    for seed in range(25):
        rng = random.Random(seed)
        n_ids = rng.randint(1, 60)

        expected_assembled = 0
        expected_phase_counts: dict[int, int] = {}
        orphan_corr = 0
        orphan_phase = 0

        # Build each id's cells contiguously so a chunk boundary never opens an
        # interior quiet gap that would settle a completion before its tail.
        buffer = bytearray()
        for rid in range(1, n_ids + 1):
            has_completion = rng.random() < 0.75
            has_correlation = rng.random() < 0.5
            n_phase_records = rng.randint(0, 6) if rng.random() < 0.6 else 0

            cells: list[bytes] = []
            if has_completion:
                cells.append(_completion(rid))
            if has_correlation:
                cells.append(_correlation(rid))
            seq = 0
            remaining = n_phase_records
            while remaining > 0:
                take = min(remaining, 3)
                cells.append(_phase_batch(rid, take, seq))
                seq += take
                remaining -= take
            rng.shuffle(cells)  # trailing order within the id is arbitrary
            for cell in cells:
                buffer += cell

            if has_completion:
                expected_assembled += 1
                expected_phase_counts[rid] = n_phase_records
            else:
                if has_correlation:
                    orphan_corr += 1
                if n_phase_records:
                    orphan_phase += 1

        # Random chunking of the (id-contiguous) buffer.
        chunks: list[bytes] = []
        pos = 0
        total_cells = len(buffer) // CELL_SIZE
        while pos < total_cells:
            take = rng.randint(1, max(1, total_cells - pos))
            chunks.append(bytes(buffer[pos * CELL_SIZE : (pos + take) * CELL_SIZE]))
            pos += take

        recorder = ChunkRecorder(chunks)
        proj = Projector(recorder, max_cells=rng.choice([1, 3, 4096]))
        _drain_all(proj, recorder)
        snap = proj.snapshot(recent=10_000)

        assert snap.assembled == expected_assembled, seed
        assert snap.pending == 0, seed
        assert snap.loss.orphan_correlation == orphan_corr, seed
        assert snap.loss.orphan_phase == orphan_phase, seed
        assert snap.loss.decode_error == 0, seed
        # Every assembled trace carries exactly the phases fed for its id.
        for trace in snap.recent:
            assert len(trace.phases) == expected_phase_counts[trace.request_id], (
                seed,
                trace.request_id,
            )


def test_snapshots_are_safe_under_concurrent_draining() -> None:
    chunks = [b"".join(_completion(i) for i in range(k * 50, k * 50 + 50)) for k in range(40)]
    recorder = ChunkRecorder(chunks)
    proj = Projector(recorder, interval=0.001)

    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        prev = 0  # per-thread: sequential reads on one thread see assembled grow
        try:
            while not stop.is_set():
                snap = proj.snapshot(recent=64)
                # Internal consistency of every read.
                assert len(snap.recent) <= 64
                assert snap.assembled >= prev  # monotonic non-decreasing
                assert snap.assembled >= len(snap.recent)
                prev = snap.assembled
                assert snap.pending >= 0
        except BaseException as exc:  # noqa: BLE001 -- surface to the test thread
            errors.append(exc)

    proj.start()
    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and proj.snapshot().assembled < 2000:
        time.sleep(0.01)
    stop.set()
    for t in threads:
        t.join(2.0)
    proj.stop()

    assert not errors, errors[0]
    assert proj.snapshot().assembled == 2000  # 40 chunks * 50 completions
