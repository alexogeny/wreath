from __future__ import annotations

import zlib

import pytest

from wreath._pure.compression import GzipCompressor, gzip_compress


@pytest.mark.parametrize("level", range(10))
def test_pure_gzip_levels(level: int) -> None:
    payload = (b"abcdef" * 1000) + bytes(range(256))
    assert zlib.decompress(gzip_compress(payload, level), wbits=31) == payload


def test_pure_stream_empty() -> None:
    compressor = GzipCompressor()
    result = compressor.compress(b"") + compressor.finish()
    assert zlib.decompress(result, wbits=31) == b""
