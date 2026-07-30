"""Gzip and zstd compression, on CPython's maintained native codecs.

Wreath does not ship a deflate or a zstd implementation. Every entry point is a
thin facade over `zlib` or over `compression.zstd`, both of which are C
extensions in the interpreter, so the compression itself already runs at native
speed and inherits the security fixes of the interpreter it is installed with.
Selecting a content encoding from `Accept-Encoding` is a separate concern and
lives in `wreath.middleware.CompressionMiddleware`; this module only turns bytes
into compressed bytes.

**zstd needs no third-party dependency, and brotli would.** `compression.zstd`
arrived in the standard library in Python 3.14 (PEP 784), which is the whole
reason Wreath can offer a second coding at all — `brotli` and `brotlicffi` are
PyPI packages, and a mandatory runtime dependency in `src/wreath` is not
something a content encoding is worth.

Each coding has a whole-buffer form and a streaming form:

- `gzip_compress(data, level=5)` / `GzipCompressor(level=5)`, levels 0-9.
- `zstd_compress(data, level=3)` / `ZstdCompressor(level=3)`, levels
  `ZSTD_MIN_LEVEL` to `ZSTD_MAX_LEVEL`, read from libzstd rather than
  hardcoded. Note there is no zstd equivalent of gzip's `0`: levels below 1 are
  libzstd's *fast* modes, not a store mode.

The streaming forms are the same three-state machine — open, finished, closed.
`compress(chunk)` may return `b""` while the encoder buffers, `finish()` emits
the remainder plus the trailer or frame epilogue, and `close()` drops a
still-open encoder without emitting one. A compressor answers `compress` and
`finish` only while it is open; after `finish()` or `close()` either raises
`RuntimeError`, so a stream cannot be silently continued past its own end.
`close()` is idempotent and never raises.

That refusal is not symmetric in what it prevents. A second `flush()` on a
`zlib` object is harmless, but a second `flush(FLUSH_FRAME)` on a stdlib
`zstd.ZstdCompressor` emits a second, *empty* 9-byte frame rather than raising —
valid zstd that no decoder complains about, so the failure surfaces as a
`Content-Length` 9 bytes too long rather than as an error. `ZstdCompressor.finish`
raising is what keeps that attributable.

These are re-exports from `wreath._pure.compression`, which is private and so has
no reference page of its own; the `:::` generator renders them under this module
for that reason, and their signatures are below.
"""

from __future__ import annotations

from ._pure.compression import (
    ZSTD_DEFAULT_LEVEL,
    ZSTD_MAX_LEVEL,
    ZSTD_MIN_LEVEL,
    GzipCompressor,
    ZstdCompressor,
    gzip_compress,
    zstd_compress,
)

__all__ = [
    "ZSTD_DEFAULT_LEVEL",
    "ZSTD_MAX_LEVEL",
    "ZSTD_MIN_LEVEL",
    "GzipCompressor",
    "ZstdCompressor",
    "gzip_compress",
    "zstd_compress",
]
