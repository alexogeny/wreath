import gzip
import tracemalloc

import pytest

from wreath._compression import _gzip_decoder_new, _gzip_decompress_with


@pytest.mark.parametrize("size", [0, 1100, 65536])
def test_gzip_output_reservation_tracks_member_size(size: int) -> None:
    payload = (b"hello world" * ((size + 10) // 11))[:size]
    encoded = gzip.compress(payload)
    workspace = _gzip_decoder_new()
    tracemalloc.start()
    try:
        result = _gzip_decompress_with(workspace, encoded, 64 << 20)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == payload
    assert peak < size + (64 << 10)


@pytest.mark.parametrize("size", [0, 1, 1100, 65536])
@pytest.mark.parametrize("buffer_type", [bytes, bytearray, memoryview])
def test_gzip_owned_results_survive_workspace_reuse(size: int, buffer_type: type) -> None:
    payload = (b"hello world" * ((size + 10) // 11))[:size]
    encoded = buffer_type(gzip.compress(payload))
    workspace = _gzip_decoder_new()
    results = [
        _gzip_decompress_with(workspace, encoded, maximum)
        for maximum in (max(1, size), 64 << 20, max(1, size))
    ]
    assert all(result == payload for result in results)
    assert all(type(result) is bytes for result in results)
