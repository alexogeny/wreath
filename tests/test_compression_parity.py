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
    gzip_decompress,
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


# --- the bounded decoder -----------------------------------------------------
#
# The only entry point in this module that *decodes*, and so the only one with
# an adversary on the other end: a gzip member's input length says nothing about
# its output length.


def test_a_gzip_member_round_trips_through_the_decoder() -> None:
    payload = (b"abcdef" * 1000) + bytes(range(256))
    assert gzip_decompress(gzip_compress(payload), max_output_bytes=1 << 20) == payload


def test_an_empty_member_round_trips() -> None:
    assert gzip_decompress(gzip_compress(b""), max_output_bytes=1) == b""


def test_a_member_of_exactly_the_limit_is_accepted() -> None:
    """The off-by-one that would refuse what the caller said was allowed.

    Asking zlib for exactly `max_output_bytes` can leave the member's trailer in
    `unconsumed_tail`, which reads as "there is more" for a payload that is
    exactly the size permitted.
    """
    payload = b"a" * 1024
    assert gzip_decompress(gzip_compress(payload), max_output_bytes=1024) == payload


def test_a_member_one_byte_past_the_limit_is_refused() -> None:
    payload = b"a" * 1025
    with pytest.raises(ValueError, match="expands past the 1024-byte limit"):
        gzip_decompress(gzip_compress(payload), max_output_bytes=1024)


def test_a_bomb_is_refused_on_its_decoded_size_not_its_wire_size() -> None:
    """Two kilobytes in, two megabytes out. Only the output bound catches it."""
    bomb = gzip_compress(b"\x00" * 2_000_000)
    assert len(bomb) < 8192
    with pytest.raises(ValueError, match="expands past"):
        gzip_decompress(bomb, max_output_bytes=8192)


def test_a_limit_of_zero_is_refused_because_zlib_reads_it_as_unbounded() -> None:
    """The trap this keyword exists to close, asserted rather than commented.

    `zlib`'s `max_length=0` means *no limit*, so a caller that computed a
    ceiling of zero would get the exact opposite of the guarantee. Refusing is
    the only answer that cannot be silently wrong.
    """
    with pytest.raises(ValueError, match="must be positive"):
        gzip_decompress(gzip_compress(b"anything"), max_output_bytes=0)


def test_a_truncated_member_is_refused_rather_than_returning_a_prefix() -> None:
    whole = gzip_compress(b"abcdef" * 100)
    with pytest.raises(ValueError, match="truncated"):
        gzip_decompress(whole[:-4], max_output_bytes=1 << 20)


def test_bytes_after_the_member_are_refused() -> None:
    """A second member, or a smuggled tail, is not silently dropped."""
    with pytest.raises(ValueError, match="trailing bytes"):
        gzip_decompress(gzip_compress(b"abc") + b"junk", max_output_bytes=1 << 20)


def test_something_that_is_not_gzip_at_all_raises_value_error() -> None:
    """`zlib.error` is not a `ValueError`, and a caller of this facade should
    not have to import zlib to catch what it raises."""
    with pytest.raises(ValueError, match="not a readable gzip member"):
        gzip_decompress(b"nowhere near a gzip member", max_output_bytes=1 << 20)
