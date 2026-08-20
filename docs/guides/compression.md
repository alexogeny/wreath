# Compression

`wreath.compression` holds the reusable codec pieces of response compression —
`GzipCompressor` and `gzip_compress` on Wreath's native, format-aware gzip
implementation, and `ZstdCompressor` and `zstd_compress` on `compression.zstd`. Keeping the codecs as
a real, importable module — rather than burying them inside one middleware —
means you can compress a payload anywhere you need to, not only on the response
path. The content-encoding negotiation that decides whether and how to compress
belongs to `CompressionPolicy` itself.

## User story: shrink a large JSON list without paying on small ones

> *As an API author, my `/products` endpoint returns a big JSON array that's
> costing bandwidth, and mobile clients feel it. I want it compressed when the
> client can decode it — but I don't want tiny responses paying compression
> overhead.*

```python
from wreath.policy import CompressionPolicy, HttpPolicy

app.configure_http_policy(
    HttpPolicy(compression=CompressionPolicy(minimum_size=1024))
)
```

The middleware reads `Accept-Encoding`, compresses only when the client accepts a
coding it offers *and* the content type is worth compressing, and skips any body
under `minimum_size` (1024 bytes by default) where the header would cost more
than it saves. It sets `Content-Encoding`, suffixes any `ETag` so the two
encodings never share a tag, and fixes the length for you — the handler still
just returns data.

## Negotiation and the prepared-response ladder

Python 3.14 added `compression.zstd` to the standard library (PEP 784), so Wreath
can offer zstd without a third-party package. CPython can be built without its
optional `_zstd` extension; in that environment Wreath and gzip still import,
while constructing `CompressionPolicy` refuses and asks for Python 3.14 built
with libzstd support. brotli is *not* installed as a silent fallback.

zstd is served only to a client that **names** it. `gzip` is served to a client
that names it *or* that sends a bare `*`:

| `Accept-Encoding` | Coding served |
| --- | --- |
| `gzip` | gzip |
| `*` | gzip — not zstd |
| `zstd` | zstd |
| `gzip, zstd` | zstd (a tie goes to the better coding) |
| `gzip;q=1.0, zstd;q=0.5` | gzip (the client said it prefers gzip) |
| `dcz, gzip, zstd` plus the exact registered dictionary | dcz |
| `dcz, gzip, zstd` with no matching dictionary | gzip |
| `zstd;q=0` | none — the response is sent uncompressed |

RFC 9110 would let `*` stand for consent to zstd. Wreath does not read it that
way, because a request carrying `*` and no explicit list is far more likely to
come from an old client than a new one, and a client with no zstd decoder
receives a body it reports as corrupt rather than as a negotiation failure. The
practical consequence is worth stating plainly: **no request that used to receive
gzip receives a coding it did not ask for by name.**

DCZ is a prepared-response optimization, not a fourth general-purpose encoder.
It is eligible only for an in-memory HTTPS response when the client names `dcz`
and its `Available-Dictionary` is an exact hash match for the dictionary Wreath
registered for that response family. When those conditions and the client's
quality values select DCZ, Wreath uses this order:

1. RFC 9842 DCZ with the exact client-held dictionary.
2. Standard gzip using an exact prepared stable fragment, when one matches.
3. Standard format-aware gzip when the fragment does not match.

The second and third steps have the same `Content-Encoding: gzip`; prepared
fragments are independently readable concatenated gzip members, as RFC 1952
allows. A strictly higher client q-value still overrides this order. A request
that names ordinary `gzip, zstd` without opting into DCZ keeps the established
zstd tie-break.

Levels are separate knobs, because the scales are unrelated — `gzip_level` runs
0–9, and `zstd_level` runs over libzstd's own range with `ZSTD_DEFAULT_LEVEL`
(3) as the default:

```python
app.configure_http_policy(
    HttpPolicy(compression=CompressionPolicy(gzip_level=6, zstd_level=6))
)
```

Note there is no zstd equivalent of gzip's `0`: levels below 1 are libzstd's
*fast* modes, not a store mode.

Wreath passes the response `Content-Type` to its gzip encoder automatically.
JSON, HTML, GraphQL, log-like streams, and plain text therefore take distinct
parser policies while producing ordinary gzip that any RFC 1952 decoder reads.
The compression policy owns reusable native encoder workspace. With Wreath's
native server, response negotiation, format selection, gzip encoding, and
header rewriting stay on the native response path rather than calling back to
Python for each compressed response. The same policy remains usable on any
conforming ASGI server.
Direct callers can provide the same knowledge explicitly:

```python
encoded = gzip_compress(payload, level=5, format="application/json")
```

Identified responses are not compressed by default. A compressed body containing
both a secret and attacker-controlled reflection exposes a length oracle (the
BREACH class of attacks). If a protected endpoint is known not to mix those,
opt in deliberately with `compress_authenticated=True`; public responses keep
the ordinary compression fast path.

## Reading one back, with a ceiling you have to name

`gzip_decompress` is the only entry point here that decodes, and it takes a
required `max_output_bytes`:

```python
from wreath.compression import gzip_decompress

payload = gzip_decompress(chunk, max_output_bytes=4 * 1024 * 1024)
```

The keyword has no default on purpose. **A gzip member's input length says
nothing about its output length** — a couple of kilobytes of zeros expand to
megabytes, and that ratio is the whole mechanism of a decompression bomb. A
length check on the compressed bytes cannot catch one, so anything reading a
compressed body off the network has to bound the decoded size as well, and
making the caller say it is the difference between a refusal and memory
pressure. `wreath.grpc` decodes `grpc-encoding: gzip` through this, applying its
`max_message_bytes` on both sides of the decoder for exactly that reason. Each
gRPC unframer owns reusable native decoder workspace, so consecutive messages
retain validated table state without sharing mutable process-global state.

Every failure is a `ValueError` — past the limit, truncated, trailing bytes, or
not gzip at all. A `max_output_bytes` of zero is refused rather than treated as
an unbounded request.

For the ordinary response case, though, you don't touch the codec directly. You add
`CompressionPolicy` from the [middleware](middleware.md) module, which reads
the request's `Accept-Encoding`, checks that the content type is worth
compressing, and does the rest:

```python
from wreath.policy import CompressionPolicy, HttpPolicy
app.configure_http_policy(HttpPolicy(compression=CompressionPolicy()))
```

**Reference:** [`wreath.compression`](../reference/compression.md).
