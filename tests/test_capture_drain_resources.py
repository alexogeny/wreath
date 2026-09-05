import tracemalloc

import pytest

from wreath._flight_schema import CaptureFieldClass
from wreath._native import _flight


def _recorder():
    return _flight.Recorder(
        _flight.MODE_FORENSIC,
        ring_records=8,
        active_requests=8,
        capture_slabs=8,
        slab_bytes=1024 * 1024,
        detailed_sample_rate=1.0,
        capture_hash_key=(1, 2),
    )


def test_idle_capture_drain_does_not_allocate_a_slab():
    recorder = _recorder()
    tracemalloc.start()
    try:
        result = recorder.drain_captures()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == []
    assert peak < 1024


@pytest.mark.parametrize("limit", [-1, 0, 1, 2, 256])
def test_capture_drain_limits_and_order(limit):
    recorder = _recorder()
    for index in range(3):
        request = recorder.begin(connection_id=index, start_ns=index)
        request.capture(int(CaptureFieldClass.REQUEST_BODY), 0, _flight.CAP_RAW, bytes([index]))
        request.finish(now_ns=index + 1, status=200)
    count = min(max(limit, 0), 3)
    slabs = recorder.drain_captures(limit)
    assert len(slabs) == count
    assert recorder.capture_committed == 3 - count
    slabs.extend(recorder.drain_captures())
    assert len(slabs) == 3
    assert recorder.capture_committed == 0
    assert recorder.drain_captures() == []
    assert [slab[-4] for slab in slabs] == [0, 1, 2]
