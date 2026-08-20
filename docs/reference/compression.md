# `wreath.compression`

The reusable compression codecs: gzip (`GzipCompressor`, `gzip_compress`,
`gzip_decompress`) and zstd (`ZstdCompressor`, `zstd_compress`).
Content-encoding negotiation lives in `wreath.policy`'s
`CompressionPolicy`.

`gzip_decompress` is the only entry point here that decodes, and so the only one
with an adversary on the other end. Its `max_output_bytes` is a required
keyword: a gzip member's input length says nothing about its output length, so a
bound on the compressed bytes cannot catch a decompression bomb and the caller
has to name the decoded ceiling it is willing to hold. A limit of zero is
refused rather than interpreted as unbounded.

Gzip is Wreath's native, format-aware RFC 1951/1952 implementation. Its encoder
and decoder are separate kernels, and its optional format hint changes internal
policy without changing the standard wire format. Zstd uses Python 3.14's
`compression.zstd` from PEP 784. The `_zstd` extension is optional when CPython
is built. Wreath and gzip remain usable without it; configuring
`CompressionPolicy` or calling a zstd encoder refuses immediately and names the
required CPython capability. No third-party fallback is installed silently.

Three module constants carry zstd's level range. A capable build reads it from
libzstd; an interpreter without `_zstd` exposes Python 3.14's documented values
for introspection while refusing zstd use:

- `ZSTD_MIN_LEVEL` — currently `-131072`. Levels below 1 are libzstd's *fast*
  modes; there is no zstd equivalent of gzip's `0` store mode.
- `ZSTD_MAX_LEVEL` — currently `22`.
- `ZSTD_DEFAULT_LEVEL` — `3`, the level whose speed is comparable to gzip's
  default while compressing appreciably better.

::: wreath.compression
