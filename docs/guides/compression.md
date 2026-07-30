# Compression

`wreath.compression` holds the reusable codec pieces of response compression —
`GzipCompressor` and `gzip_compress` on CPython's well-maintained zlib, and
`ZstdCompressor` and `zstd_compress` on `compression.zstd`. Keeping the codecs as
a real, importable module — rather than burying them inside one middleware —
means you can compress a payload anywhere you need to, not only on the response
path. The content-encoding negotiation that decides whether and how to compress
belongs to `CompressionMiddleware` itself.

## User story: shrink a large JSON list without paying on small ones

> *As an API author, my `/products` endpoint returns a big JSON array that's
> costing bandwidth, and mobile clients feel it. I want it compressed when the
> client can decode it — but I don't want tiny responses paying compression
> overhead.*

```python
from wreath.middleware import CompressionMiddleware

app.add_middleware(CompressionMiddleware(minimum_size=1024))
```

The middleware reads `Accept-Encoding`, compresses only when the client accepts a
coding it offers *and* the content type is worth compressing, and skips any body
under `minimum_size` (1024 bytes by default) where the header would cost more
than it saves. It sets `Content-Encoding`, suffixes any `ETag` so the two
encodings never share a tag, and fixes the length for you — the handler still
just returns data.

## Two codings, and why they are not offered alike

Python 3.14 added `compression.zstd` to the standard library (PEP 784), so Wreath
can offer zstd without a third-party package. brotli is *not* offered, for the
same reason turned around: `brotli` and `brotlicffi` are PyPI distributions, and
a content coding does not justify a mandatory runtime dependency.

zstd is served only to a client that **names** it. `gzip` is served to a client
that names it *or* that sends a bare `*`:

| `Accept-Encoding` | Coding served |
| --- | --- |
| `gzip` | gzip |
| `*` | gzip — not zstd |
| `zstd` | zstd |
| `gzip, zstd` | zstd (a tie goes to the better coding) |
| `gzip;q=1.0, zstd;q=0.5` | gzip (the client said it prefers gzip) |
| `zstd;q=0` | none — the response is sent uncompressed |

RFC 9110 would let `*` stand for consent to zstd. Wreath does not read it that
way, because a request carrying `*` and no explicit list is far more likely to
come from an old client than a new one, and a client with no zstd decoder
receives a body it reports as corrupt rather than as a negotiation failure. The
practical consequence is worth stating plainly: **no request that used to receive
gzip receives a coding it did not ask for by name.**

Levels are separate knobs, because the scales are unrelated — `gzip_level` runs
0–9, and `zstd_level` runs over libzstd's own range with `ZSTD_DEFAULT_LEVEL`
(3) as the default:

```python
app.add_middleware(CompressionMiddleware(gzip_level=6, zstd_level=6))
```

Note there is no zstd equivalent of gzip's `0`: levels below 1 are libzstd's
*fast* modes, not a store mode.

Identified responses are not compressed by default. A compressed body containing
both a secret and attacker-controlled reflection exposes a length oracle (the
BREACH class of attacks). If a protected endpoint is known not to mix those,
opt in deliberately with `compress_authenticated=True`; public responses keep
the ordinary compression fast path.

For the ordinary case, though, you don't touch the codec directly. You add
`CompressionMiddleware` from the [middleware](middleware.md) module, which reads
the request's `Accept-Encoding`, checks that the content type is worth
compressing, and does the rest:

```python
from wreath.middleware import CompressionMiddleware
app.add_middleware(CompressionMiddleware())
```

**Reference:** [`wreath.compression`](../reference/compression.md).
