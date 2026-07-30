"""Gzip compression, on CPython's maintained native zlib.

Wreath does not ship a deflate implementation. Both entry points are thin
facades over `zlib`, which is a C extension in every supported CPython build, so
the compression itself already runs at native speed and inherits the security
fixes of the interpreter it is installed with. Selecting a content encoding from
`Accept-Encoding` is a separate concern and lives in
`wreath.middleware.CompressionMiddleware`; this module only turns bytes into
gzip bytes.

`gzip_compress(data, level=5)` returns one complete gzip member for `data`.
`GzipCompressor(level=5)` is the streaming form: `compress(chunk)` may return
`b""` while the encoder buffers, `finish()` emits the remainder plus the gzip
trailer, and `close()` drops a still-open encoder without emitting one. Both
constructors refuse a `level` outside
0-9 with `ValueError`. A compressor answers `compress` and `finish` only
while it is open; after `finish()` or `close()` either raises
`RuntimeError`, so a stream cannot be silently continued past its trailer.
`close()` is idempotent and never raises.

Both are re-exports from `wreath._pure.compression`, which is private and so has
no reference page of its own; the `:::` generator renders them under this module
for that reason, and their signatures are below.
"""

from __future__ import annotations

from ._pure.compression import GzipCompressor, gzip_compress

__all__ = ["GzipCompressor", "gzip_compress"]
