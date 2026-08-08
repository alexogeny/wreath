# `wreath.compression`

The reusable compression codecs: gzip (`GzipCompressor`, `gzip_compress`,
`gzip_decompress`) and zstd (`ZstdCompressor`, `zstd_compress`).
Content-encoding negotiation lives in `wreath.middleware`'s
`CompressionPolicy`.

`gzip_decompress` is the only entry point here that decodes, and so the only one
with an adversary on the other end. Its `max_output_bytes` is a required
keyword: a gzip member's input length says nothing about its output length, so a
bound on the compressed bytes cannot catch a decompression bomb and the caller
has to name the decoded ceiling it is willing to hold. A limit of zero is
refused, because `zlib` reads a `max_length` of zero as unbounded.

Both codings are the interpreter's own — `zlib` and, since Python 3.14 and
PEP 784, `compression.zstd`. Neither needs a third-party package, which is why
zstd is offered and brotli is not: `brotli` and `brotlicffi` are PyPI
distributions, and a content coding does not justify a mandatory runtime
dependency in `src/wreath`.

Three module constants carry zstd's level range, read from libzstd at import
rather than written down here, so a build linked against a different libzstd
does not have Wreath refusing a level that library accepts:

- `ZSTD_MIN_LEVEL` — currently `-131072`. Levels below 1 are libzstd's *fast*
  modes; there is no zstd equivalent of gzip's `0` store mode.
- `ZSTD_MAX_LEVEL` — currently `22`.
- `ZSTD_DEFAULT_LEVEL` — `3`, the level whose speed is comparable to gzip's
  default while compressing appreciably better.

::: wreath.compression
