from __future__ import annotations

import zlib
from compression import zstd

import pytest

from wreath._pure.compression import (
    ZSTD_DEFAULT_LEVEL,
    ZSTD_MAX_LEVEL,
    ZSTD_MIN_LEVEL,
    GzipCompressor,
    ZstdCompressor,
    gzip_compress,
    zstd_compress,
)


@pytest.mark.parametrize("level", range(10))
def test_pure_gzip_levels(level: int) -> None:
    payload = (b"abcdef" * 1000) + bytes(range(256))
    assert zlib.decompress(gzip_compress(payload, level), wbits=31) == payload


def test_pure_stream_empty() -> None:
    compressor = GzipCompressor()
    result = compressor.compress(b"") + compressor.finish()
    assert zlib.decompress(result, wbits=31) == b""


@pytest.mark.parametrize("level", [ZSTD_MIN_LEVEL, -5, 1, ZSTD_DEFAULT_LEVEL, 9, ZSTD_MAX_LEVEL])
def test_pure_zstd_levels(level: int) -> None:
    payload = (b"abcdef" * 1000) + bytes(range(256))
    assert zstd.decompress(zstd_compress(payload, level)) == payload


def test_pure_zstd_stream_empty() -> None:
    compressor = ZstdCompressor()
    result = compressor.compress(b"") + compressor.finish()
    assert zstd.decompress(result) == b""


@pytest.mark.parametrize("level", [ZSTD_MIN_LEVEL - 1, ZSTD_MAX_LEVEL + 1])
def test_zstd_refuses_level_outside_libzstd_range(level: int) -> None:
    with pytest.raises(ValueError, match="zstd level"):
        zstd_compress(b"x", level)
    with pytest.raises(ValueError, match="zstd level"):
        ZstdCompressor(level)


def test_zstd_second_finish_refuses_rather_than_emitting_an_empty_frame() -> None:
    """The whole reason `ZstdCompressor` exists rather than the stdlib object.

    `zstd.ZstdCompressor.flush(FLUSH_FRAME)` does not raise on a finished
    encoder -- it emits a second, empty, *valid* frame. Nothing downstream
    complains, so the bug surfaces as a `Content-Length` too long by exactly
    those bytes. Assert the stdlib behaviour too, so this test explains itself if
    CPython ever changes it.
    """
    raw = zstd.ZstdCompressor(level=3)
    raw.compress(b"payload")
    raw.flush(zstd.ZstdCompressor.FLUSH_FRAME)
    assert len(raw.flush(zstd.ZstdCompressor.FLUSH_FRAME)) > 0

    compressor = ZstdCompressor()
    compressor.compress(b"payload")
    compressor.finish()
    with pytest.raises(RuntimeError, match="not open"):
        compressor.finish()
    with pytest.raises(RuntimeError, match="not open"):
        compressor.compress(b"more")


def test_zstd_close_is_idempotent_and_never_raises() -> None:
    compressor = ZstdCompressor()
    compressor.compress(b"payload")
    compressor.close()
    compressor.close()
    with pytest.raises(RuntimeError, match="not open"):
        compressor.compress(b"more")
    finished = ZstdCompressor()
    finished.finish()
    finished.close()


def test_zstd_streaming_matches_whole_buffer_output() -> None:
    payload = b"wreath" * 5_000
    compressor = ZstdCompressor(ZSTD_DEFAULT_LEVEL)
    streamed = b"".join(
        compressor.compress(payload[i : i + 512]) for i in range(0, len(payload), 512)
    )
    streamed += compressor.finish()
    assert zstd.decompress(streamed) == payload
    assert zstd.decompress(zstd_compress(payload, ZSTD_DEFAULT_LEVEL)) == payload
