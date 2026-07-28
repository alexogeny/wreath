# Compression

`wreath.compression` holds the reusable codec pieces of response compression —
`GzipCompressor` and `gzip_compress`, built on CPython's well-maintained zlib.
Keeping the codec as a real, importable module — rather than burying it inside
one middleware — means you can compress a payload anywhere you need to, not
only on the response path. The content-encoding negotiation that decides
whether and how to compress belongs to `CompressionMiddleware` itself.

## User story: shrink a large JSON list without paying on small ones

> *As an API author, my `/products` endpoint returns a big JSON array that's
> costing bandwidth, and mobile clients feel it. I want it gzipped when the client
> accepts gzip — but I don't want tiny responses paying compression overhead.*

```python
from wreath.middleware import CompressionMiddleware

app.add_middleware(CompressionMiddleware(minimum_size=1024))
```

The middleware reads `Accept-Encoding`, compresses only when the client accepts
gzip *and* the content type is worth compressing, and skips any body under
`minimum_size` (1024 bytes by default) where the gzip header would cost more than
it saves. It sets `Content-Encoding: gzip` and fixes the length for you — the
handler still just returns data.

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
