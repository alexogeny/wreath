"""Response compression on Wreath's native gzip and CPython's native zstd."""

from __future__ import annotations

import importlib
from base64 import b64encode
from hashlib import sha256
from typing import Any

from ._native import _core

_zstd: Any = None
try:
    _zstd = importlib.import_module("compression.zstd")
except ImportError:
    # ``compression.zstd`` is part of Python 3.14, but the extension itself is
    # optional at interpreter build time.  In particular, the CPython 3.14
    # manylinux images used by cibuildwheel currently omit ``_zstd``.  Gzip and
    # the rest of Wreath must remain importable in such a build; selecting zstd
    # is the boundary where the missing capability becomes an error.
    pass

# The valid range is asked of the stdlib rather than written down, because it is
# libzstd's range and not ours; a build linked against a different libzstd would
# otherwise have Wreath refusing a level that library accepts. Currently
# (-131072, 22): the negatives are libzstd's "fast" modes.
if _zstd is None:
    # Public defaults remain introspectable even when the optional codec is not
    # present.  These are the ranges defined by the Python 3.14 API; a build
    # with the extension asks its linked libzstd below instead.
    ZSTD_MIN_LEVEL, ZSTD_MAX_LEVEL = -131072, 22
    ZSTD_DEFAULT_LEVEL = 3
else:
    ZSTD_MIN_LEVEL, ZSTD_MAX_LEVEL = _zstd.CompressionParameter.compression_level.bounds()
    ZSTD_DEFAULT_LEVEL = _zstd.COMPRESSION_LEVEL_DEFAULT

_DCZ_MAGIC = b"\x5e\x2a\x4d\x18\x20\x00\x00\x00"


def require_zstd() -> Any:
    """Return the codec or refuse when this CPython build omitted ``_zstd``."""
    if _zstd is None:
        raise RuntimeError(
            "zstd compression requires a CPython build with the optional "
            "_zstd module; use gzip or install Python 3.14 with libzstd support"
        )
    return _zstd


def _gzip_encoder_new() -> object:
    """Allocate workspace owned by one compression policy or stream."""
    return _core.gzip_encoder_new()


def _gzip_compress_with(workspace: object, data: bytes, level: int, format: str | bytes) -> bytes:
    """Encode with application-owned workspace, keeping dispatch inside C."""
    return _core.gzip_compress_with(workspace, data, level, format)


def _gzip_fragment_compress_with(
    workspace: object,
    data: bytes,
    level: int,
    format: str | bytes,
    fragments: tuple[object | None, ...],
) -> bytes:
    """Use an exact prepared gzip member when this format's stable span matches."""
    return _core.gzip_fragment_compress_with(workspace, data, level, format, fragments)


def _prepare_dcz_dictionary(dictionary: bytes) -> tuple[bytes, bytes, Any]:
    """Prepare one raw RFC 9842 dictionary outside the request path."""
    codec = require_zstd()
    content = bytes(dictionary)
    digest = sha256(content).digest()
    prepared = codec.ZstdDict(content, is_raw=True)
    return b":" + b64encode(digest) + b":", digest, prepared


def _dcz_compress(prepared: tuple[bytes, bytes, Any], data: bytes, level: int) -> bytes:
    """Emit one RFC 9842 Dictionary-Compressed Zstandard stream."""
    _token, digest, dictionary = prepared
    payload = require_zstd().compress(
        data,
        level=level,
        zstd_dict=dictionary.as_digested_dict,
    )
    return _DCZ_MAGIC + digest + payload


def _dcz_decompress(data: bytes, dictionary: bytes, *, max_output_bytes: int) -> bytes:
    """Decode one DCZ stream for tests and benchmark verification."""
    if max_output_bytes < 1:
        raise ValueError(f"max_output_bytes must be positive, got {max_output_bytes}")
    prepared = _prepare_dcz_dictionary(dictionary)
    _token, digest, zstd_dictionary = prepared
    if len(data) < 40 or data[:8] != _DCZ_MAGIC or data[8:40] != digest:
        raise ValueError("not a readable dcz stream for this dictionary")
    decoder = require_zstd().ZstdDecompressor(zstd_dict=zstd_dictionary)
    output = decoder.decompress(data[40:], max_output_bytes)
    if not decoder.eof or decoder.unused_data:
        raise ValueError(f"dcz stream expands past the {max_output_bytes}-byte limit")
    return output


def _gzip_decoder_new() -> object:
    """Allocate workspace owned by one compressed-input stream."""
    return _core.gzip_decoder_new()


def _gzip_decompress_with(
    workspace: object,
    data: bytes,
    max_output_bytes: int,
    format: str | bytes = "unknown",
) -> bytes:
    """Decode with stream-owned workspace, retaining native table state."""
    if max_output_bytes < 1:
        raise ValueError(f"max_output_bytes must be positive, got {max_output_bytes}")
    return _core.gzip_decompress_with(workspace, data, max_output_bytes, format)


def gzip_compress(data: bytes, level: int = 5, format: str | bytes = "unknown") -> bytes:
    """One complete gzip member for `data`, header and trailer included.

    `format` is optional out-of-band knowledge: `json`, `chaotic-json`, `html`,
    `graphql`, `log`, `plaintext`, or an HTTP Content-Type. It selects a native
    parser policy but never changes the standard gzip wire format.

    `level` runs from 0 (store, no compression) to 9 (smallest, slowest), and
    defaults to 5. Compression is Wreath's independent native encoder.

    Raises:
        ValueError: `level` is outside 0-9.
    """
    if not 0 <= level <= 9:
        raise ValueError("gzip level must be between 0 and 9")
    return _core.gzip_compress(data, level, format)


def gzip_decompress(
    data: bytes, *, max_output_bytes: int, format: str | bytes = "unknown"
) -> bytes:
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
            be positive.
        format: Optional content knowledge, with the same values as
            `gzip_compress`. It is not read from the member or written to it.

    Returns:
        The decoded bytes.

    Raises:
        ValueError: `max_output_bytes` is not positive; the member expands past
            it; the member is truncated; bytes follow it; or it is not a gzip
            member at all. One exception type, because every one of these is
            "the bytes were not what the caller was promised".
    """
    if max_output_bytes < 1:
        raise ValueError(f"max_output_bytes must be positive, got {max_output_bytes}")
    return _core.gzip_decompress(data, max_output_bytes, format)


class GzipCompressor:
    """Collect one response body and encode one gzip member at `finish()`.

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

    __slots__ = ("_chunks", "_format", "_level", "_state", "_workspace")

    def __init__(self, level: int = 5, format: str | bytes = "unknown") -> None:
        if not 0 <= level <= 9:
            raise ValueError("gzip level must be between 0 and 9")
        self._chunks: list[bytes] | None = []
        self._format = format
        self._level = level
        self._state = "open"
        self._workspace = _gzip_encoder_new()

    def compress(self, data: bytes) -> bytes:
        """Encode one chunk, returning whatever is ready to send.

        **`b""` is a normal answer, not an error.** deflate buffers to find
        matches, so a chunk often produces no output at all; the bytes it
        contributed come out of a later `compress` or out of `finish`. A caller
        that treats an empty return as end-of-stream will truncate the body.

        Raises:
            RuntimeError: this compressor has already been finished or closed.
        """
        chunks = self._chunks
        if self._state != "open" or chunks is None:
            raise RuntimeError("gzip compressor is not open")
        if data:
            chunks.append(data)
        return b""

    def finish(self) -> bytes:
        """Flush what is still buffered, plus the gzip trailer, and close the member.

        The return value is the last thing to send: the trailer carries the CRC
        and the uncompressed length, so a body that stops before it is
        incomplete no matter how many chunks preceded it. Afterwards the
        compressor is finished, and `compress` and `finish` both raise.

        Raises:
            RuntimeError: this compressor has already been finished or closed.
        """
        chunks = self._chunks
        if self._state != "open" or chunks is None:
            raise RuntimeError("gzip compressor is not open")
        self._state = "finished"
        self._chunks = None
        return _gzip_compress_with(self._workspace, b"".join(chunks), self._level, self._format)

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
            self._chunks = None


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
    and which is a C extension when the interpreter was built with libzstd.

    Raises:
        RuntimeError: this CPython build omitted the optional `_zstd` module.
        ValueError: `level` is outside `ZSTD_MIN_LEVEL`-`ZSTD_MAX_LEVEL`.
    """
    codec = require_zstd()
    if not ZSTD_MIN_LEVEL <= level <= ZSTD_MAX_LEVEL:
        raise ValueError(f"zstd level must be between {ZSTD_MIN_LEVEL} and {ZSTD_MAX_LEVEL}")
    return codec.compress(data, level=level)


class ZstdCompressor:
    """The streaming form of `zstd_compress`: feed it chunks, then finish.

    The same three-state machine as `GzipCompressor` — open, finished, closed,
    and only *open* accepts work — over `compression.zstd`, so
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
        RuntimeError: this CPython build omitted the optional `_zstd` module.
        ValueError: `level` is outside `ZSTD_MIN_LEVEL`-`ZSTD_MAX_LEVEL`.
    """

    __slots__ = ("_compressor", "_state")

    def __init__(self, level: int = ZSTD_DEFAULT_LEVEL) -> None:
        codec = require_zstd()
        if not ZSTD_MIN_LEVEL <= level <= ZSTD_MAX_LEVEL:
            raise ValueError(f"zstd level must be between {ZSTD_MIN_LEVEL} and {ZSTD_MAX_LEVEL}")
        self._compressor = codec.ZstdCompressor(level=level)
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
        compressor = self._compressor
        if self._state != "open" or compressor is None:
            raise RuntimeError("zstd compressor is not open")
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
        compressor = self._compressor
        if self._state != "open" or compressor is None:
            raise RuntimeError("zstd compressor is not open")
        self._state = "finished"
        codec = require_zstd()
        return compressor.flush(codec.ZstdCompressor.FLUSH_FRAME)

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
    "require_zstd",
    "zstd_compress",
]
