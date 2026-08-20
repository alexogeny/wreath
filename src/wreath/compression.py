"""Gzip and zstd compression on native codecs.

Gzip uses Wreath's independent, format-aware encoder and decoder. The optional
format hint changes parser and table policy, never the RFC 1951/1952 wire
format. Zstd uses Python 3.14's `compression.zstd` extension.
Selecting a content encoding from `Accept-Encoding` is a separate concern and
lives in `wreath.middleware.CompressionPolicy`; this module only turns bytes
into compressed bytes.

**zstd needs no third-party dependency, and brotli would.** `compression.zstd`
arrived in the standard library in Python 3.14 (PEP 784), which is the whole
reason Wreath can offer a second coding at all. The underlying `_zstd` extension
is an optional CPython build capability: Wreath and gzip remain importable when
it is absent, while zstd functions and `CompressionPolicy` refuse with the
required build form. `brotli` and `brotlicffi` remain third-party packages.

Each coding has a whole-buffer form and a chunk-accepting form:

- `gzip_compress(data, level=5, format="unknown")` /
  `GzipCompressor(level=5, format="unknown")`, levels 0-9. The format can be a
  name or an HTTP Content-Type.
- `gzip_decompress(data, max_output_bytes=...)` reads one back. It is the only
  entry point here that *decodes*, because decoding is the direction with an
  adversary in it: a gzip member's input length says nothing about its output
  length, so the bound is a required keyword rather than a default. It reads a
  compressed request body -- `wreath.grpc` decodes `grpc-encoding: gzip`
  through it -- and the ceiling is whatever the caller was already willing to
  hold decoded.
- `zstd_compress(data, level=3)` / `ZstdCompressor(level=3)`, levels
  `ZSTD_MIN_LEVEL` to `ZSTD_MAX_LEVEL`, read from libzstd rather than
  hardcoded. Note there is no zstd equivalent of gzip's `0`: levels below 1 are
  libzstd's *fast* modes, not a store mode.

The chunk-accepting forms are the same three-state machine — open, finished,
closed. Gzip holds chunks until `finish()` so the format-aware encoder sees the
whole member; zstd may emit blocks incrementally. `close()` drops a still-open
encoder, is idempotent, and never raises.

That refusal is load-bearing for zstd. A second `flush(FLUSH_FRAME)` on a
stdlib `zstd.ZstdCompressor` emits a second, *empty* 9-byte frame rather than raising —
valid zstd that no decoder complains about, so the failure surfaces as a
`Content-Length` 9 bytes too long rather than as an error. `ZstdCompressor.finish`
raising is what keeps that attributable.

These are re-exports from `wreath._compression`, which is private and so has
no reference page of its own; the `:::` generator renders them under this module
for that reason, and their signatures are below.
"""

from __future__ import annotations

from ._compression import (
    ZSTD_DEFAULT_LEVEL,
    ZSTD_MAX_LEVEL,
    ZSTD_MIN_LEVEL,
    GzipCompressor,
    ZstdCompressor,
    gzip_compress,
    gzip_decompress,
    zstd_compress,
)

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
