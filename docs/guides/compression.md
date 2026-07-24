# Compression

`wreath.compression` holds the reusable codec pieces of response compression —
`GzipCompressor` and `gzip_compress`, built on CPython's well-maintained zlib.
Keeping the codec as a real, importable module — rather than burying it inside
one middleware — means you can compress a payload anywhere you need to, not
only on the response path. The content-encoding negotiation that decides
whether and how to compress belongs to `CompressionMiddleware` itself.

For the ordinary case, though, you don't touch the codec directly. You add
`CompressionMiddleware` from the [middleware](middleware.md) module, which reads
the request's `Accept-Encoding`, checks that the content type is worth
compressing, and does the rest:

```python
from wreath.middleware import CompressionMiddleware
app.add_middleware(CompressionMiddleware())
```

**Reference:** [`wreath.compression`](../reference/compression.md).
