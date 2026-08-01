"""Pure-Python facade over the stdlib native zlib and zstd implementations."""

from __future__ import annotations

import zlib
from compression import zstd

# The valid range is asked of the stdlib rather than written down, because it is
# libzstd's range and not ours; a build linked against a different libzstd would
# otherwise have Wreath refusing a level that library accepts. Currently
# (-131072, 22): the negatives are libzstd's "fast" modes.
ZSTD_MIN_LEVEL, ZSTD_MAX_LEVEL = zstd.CompressionParameter.compression_level.bounds()
ZSTD_DEFAULT_LEVEL = zstd.COMPRESSION_LEVEL_DEFAULT


def gzip_compress(data: bytes, level: int = 5) -> bytes:
    """One complete gzip member for `data`, header and trailer included.

    The whole-buffer form: hand it the bytes you have and get back something a
    client can decompress on its own. Use `GzipCompressor` instead when the body
    arrives in pieces and you do not want to hold all of it.

    `level` runs from 0 (store, no compression) to 9 (smallest, slowest), and
    defaults to 5. Compression itself is CPython's `zlib`, a C extension in
    every supported build.

    Raises:
        ValueError: `level` is outside 0-9.
    """
    if not 0 <= level <= 9:
        raise ValueError("gzip level must be between 0 and 9")
    return zlib.compress(data, level=level, wbits=31)


def gzip_decompress(data: bytes, *, max_output_bytes: int) -> bytes:
    """The inverse of `gzip_compress`, refusing an output past `max_output_bytes`.

    **The bound is not optional, which is why it has no default.** A gzip
    member's *input* length says nothing about its output length -- a few
    hundred bytes of zeros expand to megabytes, and that ratio is the whole
    mechanism of a decompression bomb. Anything reading a compressed body off
    the network already has a ceiling on the decoded size it is willing to hold;
    this makes the caller name it rather than discover it as memory pressure.

    Args:
        data: One complete gzip member, header and trailer included.
        max_output_bytes: The largest decoded result to produce, in bytes. Must
            be positive: `zlib` reads a `max_length` of zero as *unbounded*, so
            a caller that computed a limit of zero would silently get the
            opposite of the guarantee this function exists to make.

    Returns:
        The decoded bytes.

    Raises:
        ValueError: `max_output_bytes` is not positive; the member expands past
            it; the member is truncated; bytes follow it; or it is not a gzip
            member at all. One exception type, because every one of these is
            "the bytes were not what the caller was promised", and `zlib.error`
            is not something a caller of this facade should have to know about.
    """
    if max_output_bytes < 1:
        raise ValueError(
            f"max_output_bytes must be positive, got {max_output_bytes}: zlib "
            "reads a limit of zero as unbounded"
        )
    decompressor = zlib.decompressobj(wbits=31)
    try:
        # One byte of headroom, then a length check: asking for exactly the
        # limit can leave the member's own trailer sitting in `unconsumed_tail`
        # for a payload that is *exactly* the size allowed, which would refuse a
        # message the caller said was acceptable.
        #
        # The length is the whole test. `unconsumed_tail` is non-empty only when
        # the output limit was hit, and hitting a limit of `max + 1` means the
        # length check has already fired -- so testing both is one check written
        # twice. (A mutation pass found that second clause redundant, which is
        # how it came to be deleted rather than tested.)
        out = decompressor.decompress(data, max_output_bytes + 1)
        if len(out) > max_output_bytes:
            raise ValueError(
                f"gzip member expands past the {max_output_bytes}-byte limit"
            )
        if not decompressor.eof:
            raise ValueError("gzip member is truncated")
        if decompressor.unused_data:
            raise ValueError("trailing bytes follow the gzip member")
    except zlib.error as error:
        raise ValueError(f"not a readable gzip member: {error}") from error
    return out


class GzipCompressor:
    """The streaming form of `gzip_compress`: feed it chunks, then finish.

    One instance encodes exactly one gzip member, and it is a small state
    machine with three states — open, finished, closed — of which only *open*
    accepts work:

    ```python
    encoder = GzipCompressor(level=5)
    for chunk in body:
        yield encoder.compress(chunk)
    yield encoder.finish()
    ```

    `compress` and `finish` both raise `RuntimeError` once the compressor has
    left the open state, so a stream cannot be continued past its own trailer
    and a second `finish()` cannot emit a second one — which would produce
    bytes no decompressor agrees on.

    Abandon a stream that will never be finished with `close()`. It emits
    nothing, drops the encoder, is idempotent, and never raises, so it is safe
    in a `finally`.

    Args:
        level: 0 (store) to 9 (smallest, slowest); 5 by default.

    Raises:
        ValueError: `level` is outside 0-9.
    """

    __slots__ = ("_compressor", "_state")

    def __init__(self, level: int = 5) -> None:
        if not 0 <= level <= 9:
            raise ValueError("gzip level must be between 0 and 9")
        self._compressor = zlib.compressobj(level, zlib.DEFLATED, 31)
        self._state = "open"

    def compress(self, data: bytes) -> bytes:
        """Encode one chunk, returning whatever is ready to send.

        **`b""` is a normal answer, not an error.** deflate buffers to find
        matches, so a chunk often produces no output at all; the bytes it
        contributed come out of a later `compress` or out of `finish`. A caller
        that treats an empty return as end-of-stream will truncate the body.

        Raises:
            RuntimeError: this compressor has already been finished or closed.
        """
        if self._state != "open":
            raise RuntimeError("gzip compressor is not open")
        compressor = self._compressor
        assert compressor is not None
        return compressor.compress(data)

    def finish(self) -> bytes:
        """Flush what is still buffered, plus the gzip trailer, and close the member.

        The return value is the last thing to send: the trailer carries the CRC
        and the uncompressed length, so a body that stops before it is
        incomplete no matter how many chunks preceded it. Afterwards the
        compressor is finished, and `compress` and `finish` both raise.

        Raises:
            RuntimeError: this compressor has already been finished or closed.
        """
        if self._state != "open":
            raise RuntimeError("gzip compressor is not open")
        self._state = "finished"
        compressor = self._compressor
        assert compressor is not None
        return compressor.flush(zlib.Z_FINISH)

    def close(self) -> None:
        """Abandon a still-open compressor without emitting a trailer.

        For the stream that will not be completed — a client that disconnected,
        a handler that raised part-way through a body. An open compressor
        releases its encoder and becomes closed, after which `compress` and
        `finish` raise; a compressor that is already finished or closed is left
        exactly as it was.

        Never raises, in any of the three states, which is what makes it safe in
        a `finally` that does not know which one it is in.
        """
        if self._state == "open":
            self._state = "closed"
            self._compressor = None


def zstd_compress(data: bytes, level: int = ZSTD_DEFAULT_LEVEL) -> bytes:
    """One complete zstd frame for `data`, header and checksum included.

    The whole-buffer counterpart of `ZstdCompressor`, and the same shape as
    `gzip_compress`: hand it the bytes you have, get back something a client can
    decompress on its own.

    `level` runs from `ZSTD_MIN_LEVEL` to `ZSTD_MAX_LEVEL` — libzstd's range,
    read from the stdlib rather than hardcoded — and defaults to
    `ZSTD_DEFAULT_LEVEL` (3), which is the level whose speed is comparable to
    gzip's default while compressing appreciably better. Levels below 1 are
    libzstd's "fast" modes, not gzip's `0` store mode: **there is no zstd level
    that stores without compressing.**

    Compression itself is `compression.zstd`, which Python 3.14 added in PEP 784
    and which is a C extension in every build that has it, so this needs no
    third-party dependency.

    Raises:
        ValueError: `level` is outside `ZSTD_MIN_LEVEL`-`ZSTD_MAX_LEVEL`.
    """
    if not ZSTD_MIN_LEVEL <= level <= ZSTD_MAX_LEVEL:
        raise ValueError(f"zstd level must be between {ZSTD_MIN_LEVEL} and {ZSTD_MAX_LEVEL}")
    return zstd.compress(data, level=level)


class ZstdCompressor:
    """The streaming form of `zstd_compress`: feed it chunks, then finish.

    The same three-state machine as `GzipCompressor` — open, finished, closed,
    and only *open* accepts work — over `compression.zstd` instead of `zlib`, so
    the two are interchangeable to a caller that only compresses a body:

    ```python
    encoder = ZstdCompressor(level=3)
    for chunk in body:
        yield encoder.compress(chunk)
    yield encoder.finish()
    ```

    The state machine is load-bearing here in a way it is not for gzip, and this
    is the reason this class exists rather than the stdlib object being used
    directly. `zstd.ZstdCompressor.flush(FLUSH_FRAME)` called a second time does
    not raise — it emits a **second, empty frame** (9 bytes). Those bytes are
    valid zstd, so nothing downstream complains; a client decoding the response
    just sees a body that ends where the first frame did, and a `Content-Length`
    computed over both is 9 bytes too long. `finish()` raising `RuntimeError`
    instead turns that into a visible error at the one place it can still be
    attributed.

    Abandon a stream that will never be finished with `close()`. It emits
    nothing, drops the encoder, is idempotent, and never raises, so it is safe
    in a `finally`.

    Args:
        level: `ZSTD_MIN_LEVEL` to `ZSTD_MAX_LEVEL`; `ZSTD_DEFAULT_LEVEL` (3) by
            default.

    Raises:
        ValueError: `level` is outside `ZSTD_MIN_LEVEL`-`ZSTD_MAX_LEVEL`.
    """

    __slots__ = ("_compressor", "_state")

    def __init__(self, level: int = ZSTD_DEFAULT_LEVEL) -> None:
        if not ZSTD_MIN_LEVEL <= level <= ZSTD_MAX_LEVEL:
            raise ValueError(f"zstd level must be between {ZSTD_MIN_LEVEL} and {ZSTD_MAX_LEVEL}")
        self._compressor = zstd.ZstdCompressor(level=level)
        self._state = "open"

    def compress(self, data: bytes) -> bytes:
        """Encode one chunk, returning whatever is ready to send.

        **`b""` is a normal answer, not an error**, and zstd returns it more
        often than gzip does: the encoder fills a block before emitting
        anything, so a small chunk routinely produces no output at all and its
        bytes come out of a later `compress` or out of `finish`. A caller that
        treats an empty return as end-of-stream will truncate the body.

        Raises:
            RuntimeError: this compressor has already been finished or closed.
        """
        if self._state != "open":
            raise RuntimeError("zstd compressor is not open")
        compressor = self._compressor
        assert compressor is not None
        return compressor.compress(data)

    def finish(self) -> bytes:
        """Flush what is still buffered and close the frame.

        The return value is the last thing to send: the frame epilogue carries
        the content checksum, so a body that stops before it is incomplete no
        matter how many chunks preceded it. Afterwards the compressor is
        finished, and `compress` and `finish` both raise — see the class
        docstring for why that refusal matters more here than it does for gzip.

        Raises:
            RuntimeError: this compressor has already been finished or closed.
        """
        if self._state != "open":
            raise RuntimeError("zstd compressor is not open")
        self._state = "finished"
        compressor = self._compressor
        assert compressor is not None
        return compressor.flush(zstd.ZstdCompressor.FLUSH_FRAME)

    def close(self) -> None:
        """Abandon a still-open compressor without closing its frame.

        For the stream that will not be completed — a client that disconnected,
        a handler that raised part-way through a body. An open compressor
        releases its encoder and becomes closed, after which `compress` and
        `finish` raise; a compressor that is already finished or closed is left
        exactly as it was.

        Never raises, in any of the three states, which is what makes it safe in
        a `finally` that does not know which one it is in.
        """
        if self._state == "open":
            self._state = "closed"
            self._compressor = None


__all__ = [
    "ZSTD_DEFAULT_LEVEL",
    "ZSTD_MAX_LEVEL",
    "ZSTD_MIN_LEVEL",
    "GzipCompressor",
    "ZstdCompressor",
    "gzip_compress",
    "gzip_decompress",
    "zstd_compress",
]
