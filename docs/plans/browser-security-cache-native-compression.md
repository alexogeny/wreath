# Prescriptive plan: CSRF, HSTS, cache policy, and native compression

Status: implemented (direct-zlib extension rejected by the measured retention gate)

Related material:

- `AGENTS.md`
- `docs/plans/middleware-auth-rbac-cedar-comforts.md`
- `docs/plans/native-c-hotspots.md`
- `docs/native/README.md`
- `benchmarks/README.md`
- `src/neo/middleware/security.py`
- `src/neo/middleware/sessions.py`
- `src/neo/middleware/cors.py`

## Goal

Add production-shaped browser request forgery protection, structured HSTS, explicit cache-control policies, and gzip response compression while preserving Neo as a portable ASGI framework. Put genuinely hot parsing, header mutation, and compression work in optional native C accelerators with exact pure-Python twins. Keep cryptographic orchestration in audited stdlib primitives rather than inventing cryptography in C. Benchmark policy helpers, compression kernels, middleware overhead, throughput, compression ratio, allocations, and peak RSS before claiming a native win.

## Existing seams and constraints

- `SecurityHeadersPolicy` already emits HSTS from an unvalidated raw string. Extend it; do not add a second HSTS middleware.
- Global hook middleware covers route hits, misses, static files, and authorization failures. It is the correct seam for CSRF ingress checks and response policy.
- Middleware runs `before` hooks in sorted registration order and `after` hooks in reverse order. Integration tests must lock ordering.
- `Response` owns an in-memory `bytes` body and mutable header list. `StreamingResponse` owns an `AsyncIterable[bytes]`. `FileResponse` streams a path and computes length at send time.
- The default `_core` extension must continue to build with only a C compiler and CPython headers. Do not link zlib into `_core`.
- The framework must behave identically on Neo’s native server and any conforming ASGI server. Compression belongs in middleware, not the server protocol.
- Every native policy helper must have an exact implementation under `src/neo/_pure/`, selected by a thin public facade and covered by parity tests.
- CPython’s `hmac.digest`, `hmac.compare_digest`, `secrets`, and `binascii` already execute their expensive work in maintained native implementations. Do not write SHA-256 or a random generator in Neo C.
- Brotli and Zstandard are outside the dependency-free core. Do not add them in this change.

## Architectural decision: two native surfaces

### Core policy accelerator

Add dependency-free request/header policy helpers to the existing `_core` extension:

```text
src/neo/_native/webpolicy.c
src/neo/_pure/webpolicy.py
src/neo/webpolicy.py
```

The public facade selects `_core` unless `NEO_PURE=1` or the extension is unavailable.

Native/pure API:

```python
select_content_encoding(accept_encoding: bytes) -> str | None
is_compressible_content_type(content_type: bytes) -> bool
cache_control_flags(value: bytes) -> int
origin_matches(origin: bytes, allowed: tuple[bytes, ...]) -> bool
append_vary(headers: list[tuple[bytes, bytes]], token: bytes) -> None
replace_content_length(headers: list[tuple[bytes, bytes]], length: int | None) -> None
find_response_header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None
```

Only retain helpers that benchmarks or repeated middleware use. Do not move startup-only policy serialization into C.

### Optional zlib accelerator

Add a separate extension:

```text
src/neo/_native/_compressionmodule.c
src/neo/_native/gzip.c
src/neo/_native/gzip.h
src/neo/_pure/compression.py
src/neo/compression.py
```

Exports:

```python
gzip_compress(data: bytes, level: int = 5) -> bytes
GzipCompressor(level: int = 5)
GzipCompressor.compress(data: bytes) -> bytes
GzipCompressor.finish() -> bytes
GzipCompressor.close() -> None
```

`neo.compression` selects the optional native extension when built and not forced pure; otherwise it uses the stdlib `zlib` twin.

Build the extension only when explicitly requested:

```text
NEO_BUILD_COMPRESSION=1
```

`setup.py` must detect zlib headers and linker flags and fail the requested build with an actionable message when unavailable. A normal build must not probe, link, or silently depend on zlib. Package metadata gains no mandatory dependency.

## Benchmark gate before implementation

Create `benchmarks/bench_web_policy_compression.py` before changing production behavior.

Required command:

```bash
uv run python -m benchmarks.bench_web_policy_compression \
  --implementation native pure stdlib \
  --warmup 2 --trials 9 \
  --output benchmark-results-web-policy/before.json
```

Required cases:

```text
accept-encoding-selection
origin-match
vary-merge
content-length-replace
csrf-safe-request
csrf-unsafe-valid
csrf-unsafe-invalid
security-headers
cache-policy
compress-1k-text
compress-16k-text
compress-1m-text
compress-16k-incompressible
compress-1m-incompressible
stream-compress-1m
middleware-json-16k
middleware-json-1m
```

For compression, record levels 1, 5, and 9, compressed bytes, ratio, elapsed time, throughput, allocations where available, and peak RSS in a fresh subprocess. For middleware cases, drive the compiled Neo pipeline rather than calling the compression function directly.

Every result includes full Python/platform/compiler/zlib metadata, body generation seed, warmups, all nine raw trials, median, p95, errors, compressed size, and RSS normalization. Store untouched baseline and after-results separately:

```text
benchmark-results-web-policy/before.json
benchmark-results-web-policy/after.json
```

Native retention gate:

- keep the optional C gzip extension only if it improves median `gzip_compress` time by at least 5% over `gzip.compress(..., mtime=0)` at 16 KiB or improves peak allocation/RSS materially;
- it must not regress 1 MiB median by more than 3%;
- compressed output must be standards-valid and its size no worse than the stdlib output at the same level except for documented deterministic-header differences;
- retain C policy helpers only when they are reused by middleware and beat the pure twin repeatably. Correct but unhelpful one-off C wrappers should be removed.

These gates prevent “native” code that only adds maintenance cost around work already performed by zlib/OpenSSL in C.

## Shared contracts

### Cache policy

Add `src/neo/cache.py`:

```python
@dataclass(frozen=True, slots=True)
class CacheControl:
    public: bool = False
    private: bool = False
    no_store: bool = False
    no_cache: bool = False
    no_transform: bool = False
    must_revalidate: bool = False
    proxy_revalidate: bool = False
    immutable: bool = False
    max_age: int | None = None
    shared_max_age: int | None = None
    stale_while_revalidate: int | None = None
    stale_if_error: int | None = None

    def to_header(self) -> bytes: ...
```

Validation:

- `public` and `private` are mutually exclusive;
- all durations are non-negative integers;
- `immutable` requires a finite positive `max_age`;
- emit directives in one deterministic order;
- no user-controlled quoting or extension directives in this first version.

Define:

```python
type CachePolicy = Callable[[Request, ResponseValue], CacheControl | None]
```

### CSRF state and helper

Add `src/neo/middleware/csrf.py`:

```python
CSRF_STATE_KEY = "csrf_token"

def csrf_token(request: Request) -> str:
    ...
```

`csrf_token()` returns the token prepared by middleware on safe requests. It raises a clear `RuntimeError` when `CsrfPolicy` is not installed or has not run; it never creates a token independently.

Token wire format:

```text
v1.<issued-unix-seconds>.<base64url-32-byte-nonce>.<base64url-hmac-sha256>
```

The MAC input is the ASCII bytes through the nonce component. Base64url components omit padding and use a strict alphabet. The cookie and submitted token must be byte-for-byte equal after strict parsing.

## 1. Implement native web-policy primitives

Files:

```text
src/neo/_native/webpolicy.c
src/neo/_native/neocore.h
src/neo/_native/_coremodule.c
src/neo/_pure/webpolicy.py
src/neo/webpolicy.py
setup.py
tests/test_webpolicy_parity.py
tests/test_native_perf.py
```

### Accept-Encoding selection

Implement a single-pass parser over raw bytes:

- comma-separated codings, optional OWS, case-insensitive token comparison;
- parse `q=0` through `q=1` with at most three decimal digits;
- malformed quality values make that coding unacceptable rather than being treated as 1;
- select `gzip` when explicitly acceptable;
- wildcard permits gzip only when gzip is not explicitly disabled;
- `identity;q=0` does not make gzip acceptable by itself;
- return `"gzip"` or `None`; do not advertise unsupported encodings;
- no list, dict, substring, or Unicode allocation while scanning in C.

### Cache-Control flags

Return a bitmask for directives needed on the hot path:

```text
NO_TRANSFORM
NO_STORE
PRIVATE
PUBLIC
```

Parsing is case-insensitive, comma-delimited, and ignores extension values safely. Compression uses `NO_TRANSFORM`; cache middleware may use the remaining flags for conflict checks.

### Origin matcher

At middleware construction, Python normalizes configured trusted origins to exact ASCII bytes:

```text
scheme://host[:non-default-port]
```

The native matcher strictly parses one request `Origin` and compares exact normalized scheme, host, and effective port against the tuple. Reject userinfo, paths other than an optional trailing slash, query, fragment, invalid ports, control bytes, and comma/multiple-origin input. Treat the literal `null` as disallowed unless explicitly configured as `b"null"`.

The pure twin uses the same normalization and rejection vectors. Do not use suffix matching.

### Header mutation

`append_vary` must merge case-insensitively across all existing `Vary` fields, avoid duplicate tokens, preserve unrelated tokens, and collapse to `Vary: *` when present. `replace_content_length` removes every existing Content-Length and appends one exact decimal value unless `length is None`. Both functions mutate the existing list and return `None`.

Validate every list entry as a two-item bytes pair before mutation. On error, leave the original list unchanged by validating first.

## 2. Implement signed double-submit CSRF middleware

Files:

```text
src/neo/middleware/csrf.py
src/neo/middleware/__init__.py
src/neo/webpolicy.py
tests/test_csrf_middleware.py
docs/guides/security.md
docs/reference/middleware.md
```

Public API:

```python
CsrfPolicy(
    secret: str | bytes,
    *,
    cookie_name: str = "neo_csrf",
    header_name: str = "x-csrf-token",
    max_age: int = 2 * 60 * 60,
    secure: bool = True,
    same_site: Literal["strict", "lax", "none"] = "lax",
    trusted_origins: Iterable[str] = (),
    exempt: Callable[[Request], bool] | None = None,
)
```

The middleware is global and exposes `before` and `after`.

Construction rules:

- secret must contain at least 32 bytes after encoding;
- cookie/header names must satisfy conservative ASCII token syntax;
- `max_age` must be positive;
- `same_site="none"` requires `secure=True`;
- precompute cookie/header byte names and normalized trusted origins;
- never log tokens, cookie values, the secret, or MAC diagnostics.

Safe methods are exactly `GET`, `HEAD`, and `OPTIONS`. Treat every other method as unsafe, including unknown extension methods.

Safe request behavior:

1. Read and strictly validate an existing CSRF cookie.
2. Reuse an unexpired valid token; otherwise generate a new nonce with `secrets.token_bytes(32)`, timestamp it, and sign with `hmac.digest(secret, message, "sha256")`.
3. Store the token on request state for `csrf_token(request)`.
4. In `after`, set the cookie only when newly issued or near expiry.

Cookie attributes:

```text
Path=/
Secure=<configured, default true>
SameSite=<configured>
HttpOnly=false
Max-Age=<max_age>
```

Do not set `Domain`; host-only cookies resist sibling-subdomain injection.

Unsafe request behavior:

1. Apply `exempt` first. Exceptions from the predicate fail closed with a generic 403 and go through the application error hook without token detail.
2. Require a valid, unexpired signed cookie token.
3. Require the configured header; compare its ASCII bytes to the cookie token with `hmac.compare_digest`.
4. Validate the MAC independently even when cookie and header match.
5. Validate `Origin` when present against the request origin plus configured trusted origins.
6. For HTTPS requests without Origin, require a same-origin HTTPS Referer. For HTTP development requests, make missing Origin/Referer configurable only through `secure=False`; production defaults fail closed.
7. Return `ProblemResponse(status=403, title="Forbidden", detail="CSRF validation failed")` for every rejection reason.

Use `request.scope.get("scheme", "http")`, Host from `request.header("host")`, and the native/pure origin matcher. Never consume the request body. Header transport is the supported first-version submission mechanism; automatic multipart/form token extraction is intentionally excluded because it forces body buffering before routing.

CSRF remains independent of `SessionMiddleware`, so login and session-creation POSTs are protected. SameSite is defense-in-depth, not a substitute for token validation.

Tests cover tampering, expiry boundaries, wrong/missing header, hostile subdomain cookie injection, Origin/Referer mismatches, default ports, IPv6 hosts, `Origin: null`, trusted origins, exemptions, early errors, login-style unauthenticated POST, cookie attributes, and no secret/token disclosure.

## 3. Harden structured HSTS in existing middleware

Files:

```text
src/neo/middleware/security.py
tests/test_security_middleware.py
docs/guides/security.md
docs/reference/middleware.md
```

Extend `SecurityHeadersPolicy`:

```python
SecurityHeadersPolicy(
    ...,
    hsts_max_age: int | None = None,
    hsts_include_subdomains: bool = False,
    hsts_preload: bool = False,
    strict_transport_security: str | None = None,  # temporary advanced override
)
```

Rules:

- structured and raw HSTS settings are mutually exclusive;
- HSTS remains opt-in;
- max age is non-negative;
- preload requires include-subdomains and a max age of at least 31,536,000 seconds;
- serialize the exact header once at construction;
- append HSTS only when `request.scope["scheme"] == "https"`;
- preserve a handler-provided HSTS field;
- never infer HTTPS directly from `X-Forwarded-Proto`;
- update the existing test that currently expects HSTS on an HTTP TestClient request.

Keep other security headers precompiled. Replace the per-response Python set only if `append_missing_headers` becomes a measured, reusable native helper; do not add a one-off C function solely for HSTS.

Document preload irreversibility and the need for trusted proxy scheme rewriting before HSTS middleware.

## 4. Add typed cache policy and middleware

Files:

```text
src/neo/cache.py
src/neo/middleware/cache.py
src/neo/middleware/__init__.py
src/neo/response.py
src/neo/staticfiles.py
tests/test_cache_control.py
tests/test_client_sessions_forms.py
docs/guides/caching.md
docs/reference/responses.md
docs/reference/middleware.md
```

Add to `Response`:

```python
def set_cache_control(self, policy: CacheControl) -> None:
    ...
```

It removes all existing Cache-Control fields and appends the deterministic serialized policy. Add the same method to `StreamingResponse` and `FileResponse` through a small shared helper, not inheritance restructuring.

Middleware API:

```python
CacheControlMiddleware(
    default: CacheControl | None = None,
    policy: CachePolicy | None = None,
)
```

`after()` behavior:

- preserve an explicit response Cache-Control header;
- call `policy(request, response)` when provided, then fall back to `default`;
- no policy means no header;
- if the selected policy is public and the response carries Set-Cookie, replace it with conservative `private, no-store` rather than emitting public caching;
- apply policy to errors, static responses, and 304 responses;
- never infer authenticated/public status from the presence of an Authorization request header alone;
- do not cache policy decisions globally.

Extend `StaticFiles`:

```python
StaticFiles(..., cache_control: CacheControl | None = None)
```

and the public `app.static(...)` facade with the same option. Keep the existing default (`None`) to avoid silently changing deployments. Ensure 200 and matching 304 responses carry the same configured policy.

Examples in documentation:

```python
CacheControl(private=True, no_store=True)
CacheControl(public=True, max_age=3600)
CacheControl(public=True, max_age=31_536_000, immutable=True)
```

Use the native cache-control flag parser only for hot conflict/`no-transform` checks. Serialization remains Python startup/response policy work because C would not materially improve it.

## 5. Implement optional native gzip kernel and pure twin

Files:

```text
src/neo/_native/_compressionmodule.c
src/neo/_native/gzip.c
src/neo/_native/gzip.h
src/neo/_pure/compression.py
src/neo/compression.py
setup.py
tests/test_compression_parity.py
tests/test_compression_native.py
benchmarks/bench_web_policy_compression.py
docs/native/compression.md
```

### Build configuration

Add `_compression_extension()` to `setup.py`. Under `NEO_BUILD_COMPRESSION=1`:

- detect zlib with `pkg-config zlib` on Unix-like systems;
- allow explicit compiler/linker environment overrides using the repository’s existing build conventions;
- on Windows, use a documented supported zlib library configuration rather than guessing a library name;
- compile `_compressionmodule.c` and `gzip.c` as `neo._native._compression`;
- fail loudly when requested prerequisites are absent;
- expose the linked/runtime zlib version for benchmark metadata.

Default `ext_modules` remains unchanged when the flag is absent.

### One-shot compression

Use `deflateInit2(level, Z_DEFLATED, 15 + 16, 8, Z_DEFAULT_STRATEGY)` to produce gzip framing directly. Validate level in `0..9`.

Implementation requirements:

- accept exact bytes-like input through a read-only `Py_buffer`;
- check every `Py_ssize_t`/`uLong` conversion;
- allocate geometrically or from a safe `deflateBound` estimate;
- never expose uninitialized bytes;
- release the GIL around deflate for inputs at or above a measured threshold, initially 32 KiB;
- keep all Python references and buffer exports alive while the GIL is released;
- on zlib error, clean up stream/buffer and raise a stable Python exception;
- shrink output to exact length once;
- emit deterministic gzip headers matching `mtime=0` semantics.

### Streaming compressor type

`GzipCompressor` owns one `z_stream` and has explicit states `OPEN`, `FINISHED`, `CLOSED`, `RUNNING`.

- `.compress(data)` uses `Z_NO_FLUSH` and returns any currently produced bytes;
- `.finish()` uses `Z_FINISH`, returns trailer/final bytes, calls `deflateEnd`, and is idempotence-safe by rejecting a second finish with `RuntimeError`;
- `.close()` releases native state without emitting output and is idempotent;
- deallocation calls `deflateEnd` when needed;
- concurrent/reentrant calls while `RUNNING` raise `RuntimeError` before touching zlib;
- methods release the GIL for sufficiently large chunks;
- no `Z_SYNC_FLUSH` per ASGI chunk by default, because that damages ratio and CPU. Document that output chunk boundaries need not match input boundaries.

The pure twin uses `zlib.compress(data, level=..., wbits=31)` or `compressobj(level, wbits=31)`, matching output semantics and state errors. Parity compares decompressed bytes and state behavior; exact compressed bytes are required only when both implementations deliberately use identical zlib framing/version behavior.

Run the existing sanitizer policy plus repeated create/compress/finish/close/deallocate cycles. Add tests for empty input, incompressible input, huge input, all levels, memoryview input, reentrancy, exceptions, finish/close ordering, and GIL-safe parallel calls on separate compressor instances.

## 6. Add compression middleware using native policy and kernel facades

Files:

```text
src/neo/middleware/compression.py
src/neo/middleware/__init__.py
src/neo/compression.py
src/neo/webpolicy.py
tests/test_compression_middleware.py
tests/test_request_pipeline.py
docs/guides/compression.md
docs/reference/middleware.md
```

API:

```python
CompressionMiddleware(
    *,
    minimum_size: int = 1024,
    gzip_level: int = 5,
    compress_streaming: bool = True,
)
```

Precompile configuration and compressible media-type rules at construction.

Eligibility requires all of:

- request method is not HEAD;
- native/pure `select_content_encoding` selects gzip;
- status permits a payload and is not 204, 206, or 304;
- no Content-Encoding is present;
- no Content-Range is present;
- Cache-Control does not contain `no-transform`;
- content type is compressible;
- for in-memory responses, body length is at least `minimum_size`;
- response is not `FileResponse` in this implementation.

Default compressible types:

```text
text/*
application/json
application/problem+json
application/javascript
application/xml
application/*+json
application/*+xml
image/svg+xml
```

Do not compress known archive, image, audio, video, font, or already-compressed types.

### In-memory Response

- call `neo.compression.gzip_compress`;
- replace `response.body` only after successful compression;
- replace Content-Length with the exact compressed length through the native/pure header helper;
- append Content-Encoding: gzip;
- merge `Accept-Encoding` into Vary;
- preserve background tasks and every unrelated header.

If the response carries a syntactically valid ETag, make it representation-specific by appending `--gzip` inside the quoted opaque tag while preserving a `W/` prefix. If the ETag is malformed, skip compression rather than emitting one validator for two representations.

### StreamingResponse

Wrap the original async iterator with an async generator that creates `GzipCompressor` at iteration start:

- feed each non-empty source chunk to `.compress()`;
- yield only non-empty compressed output;
- after source exhaustion, yield non-empty `.finish()` output;
- in `finally`, call `.close()` so cancellation releases native state;
- remove Content-Length;
- add Content-Encoding and Vary before response start;
- preserve backpressure by awaiting/yielding one source chunk at a time;
- never gather the stream into memory.

Skip `FileResponse` initially. Transparent file compression conflicts with ranges, strong ETags, sendfile/pathsend, and precompressed assets. Document serving `.gz` assets or using a reverse proxy for that case.

On compression failure before response start, propagate through Neo’s error path. Once streaming response start has been sent, close the stream/connection according to existing ASGI behavior; do not attempt to replace it with a second response.

## 7. Lock middleware ordering and integration

Files:

```text
src/neo/middleware/base.py (tests first; production change only if needed)
tests/test_request_pipeline.py
tests/test_csrf_middleware.py
tests/test_cache_control.py
tests/test_compression_middleware.py
tests/test_security_middleware.py
```

Recommended priority ordering, from low to high:

```text
SecurityHeadersPolicy     0
CompressionMiddleware        10
CacheControlMiddleware       20
Session/CsrfPolicy       30
```

Because after-hooks execute in reverse, the response path becomes:

1. session/CSRF cookies are added;
2. cache policy sees Set-Cookie and can force private/no-store;
3. compression sees final Cache-Control, including no-transform;
4. security headers, including HSTS, are appended.

Before-hooks still run low-to-high. CSRF validation occurs before the endpoint and after cheaper trusted-host/ingress checks when configured accordingly.

Do not hard-code these numeric priorities inside middleware. Document them and add one integration test registering in the recommended order. Also test explicit response Cache-Control, session Set-Cookie, CSRF Set-Cookie, CORS Vary: Origin, compression Vary: Accept-Encoding, HSTS over HTTPS, early 403, 404, 304, and application exceptions.

If repeated middleware benchmark results show hook overhead materially dominates sub-1 KiB responses, propose a separate compiled composite response-policy hook. Do not merge unrelated public middleware APIs preemptively.

## Correctness and security rules

- CSRF uses maintained stdlib HMAC and randomness; no handwritten cryptography.
- CSRF tokens and secrets never enter logs, exception details, or response bodies.
- Unsafe methods fail closed; SameSite is supplementary only.
- Host-only CSRF cookies are mandatory; no Domain option in the first API.
- Origin matching is exact by scheme, host, and effective port.
- HSTS is emitted only for an HTTPS scope and never inferred from untrusted forwarding headers.
- Handler-provided security and cache headers win unless an explicitly documented safety conflict would emit public caching with Set-Cookie.
- Compression honors `no-transform`, range semantics, status/body rules, and Accept-Encoding quality values.
- Identity and gzip representations never share one strong ETag.
- Streaming compression remains bounded and preserves ASGI backpressure.
- Native and pure paths have identical externally visible policy decisions and error classes.
- Default installation/build remains dependency-free and usable without native extensions or zlib headers.

## Expected files touched

```text
setup.py
src/neo/cache.py
src/neo/compression.py
src/neo/webpolicy.py
src/neo/response.py
src/neo/staticfiles.py
src/neo/app.py
src/neo/middleware/__init__.py
src/neo/middleware/cache.py
src/neo/middleware/compression.py
src/neo/middleware/csrf.py
src/neo/middleware/security.py
src/neo/_pure/compression.py
src/neo/_pure/webpolicy.py
src/neo/_native/_coremodule.c
src/neo/_native/neocore.h
src/neo/_native/webpolicy.c
src/neo/_native/_compressionmodule.c
src/neo/_native/gzip.c
src/neo/_native/gzip.h
tests/test_webpolicy_parity.py
tests/test_csrf_middleware.py
tests/test_security_middleware.py
tests/test_cache_control.py
tests/test_compression_parity.py
tests/test_compression_native.py
tests/test_compression_middleware.py
tests/test_request_pipeline.py
tests/test_client_sessions_forms.py
tests/test_native_perf.py
benchmarks/bench_web_policy_compression.py
benchmarks/README.md
docs/guides/security.md
docs/guides/caching.md
docs/guides/compression.md
docs/reference/middleware.md
docs/reference/responses.md
docs/native/compression.md
docs/agents/manifest.json
```

Use the existing exact documentation filenames if a listed guide/reference is consolidated elsewhere; do not create duplicate navigation concepts.

## Verification

Focused checks:

```bash
uv run pytest tests/test_webpolicy_parity.py tests/test_native_perf.py
uv run pytest tests/test_csrf_middleware.py tests/test_security_middleware.py
uv run pytest tests/test_cache_control.py tests/test_client_sessions_forms.py
uv run pytest tests/test_compression_parity.py tests/test_compression_middleware.py
NEO_BUILD_COMPRESSION=1 uv run pytest tests/test_compression_native.py
uv run pytest tests/test_request_pipeline.py
```

Full checks:

```bash
uv run pytest
uv run pytest -m '' -n 4
uv run ruff check .
uv run ty check
uv run --group docs mkdocs build --strict
```

Build-matrix checks:

```bash
NEO_PURE=1 uv run pytest tests/test_webpolicy_parity.py tests/test_csrf_middleware.py tests/test_cache_control.py tests/test_compression_middleware.py
NEO_BUILD_COMPRESSION=1 uv sync --group dev
```

Run ASan/UBSan against `_core` policy parsing and the optional compression extension with malformed encodings, headers, and repeated compressor lifecycle tests. Use the repository sanitizer plan rather than raw untracked build commands.

After implementation:

```bash
uv run python -m benchmarks.bench_web_policy_compression \
  --implementation native pure stdlib \
  --warmup 2 --trials 9 \
  --output benchmark-results-web-policy/after.json
```

## Acceptance checks

- Native and pure Accept-Encoding, Cache-Control, Origin, Vary, and Content-Length helpers agree over fixed vectors and fuzz-generated bounded input.
- CSRF safe requests expose a signed token and set a host-only cookie; valid unsafe requests succeed; missing, mismatched, tampered, expired, cross-origin, and malformed requests receive the same generic 403.
- Login/session-creation POSTs are protected without requiring an existing authenticated session.
- HSTS is absent over HTTP, present over HTTPS when enabled, structured correctly, and never replaces a handler value.
- CacheControl rejects contradictory/negative policies, serializes deterministically, preserves explicit response headers, and prevents public caching with Set-Cookie.
- Static-file 200 and 304 responses carry the same configured cache policy.
- Gzip negotiation respects q-values, wildcard, explicit gzip disablement, identity-only requests, malformed values, and Vary merging.
- In-memory compression updates body, Content-Length, Content-Encoding, Vary, and ETag consistently and skips no-transform/range/bodyless/incompressible cases.
- Streaming compression decompresses to the exact source bytes, does not gather the source, preserves backpressure, and releases native state on cancellation.
- Native compression passes sanitizer and lifecycle stress tests and satisfies the stated performance retention gate; otherwise the optional extension is removed and the stdlib twin remains the production implementation.
- Raw before/after files contain nine timing and RSS trials plus ratio, errors, and environment/zlib metadata.
- No performance claim is made from one run or from compression throughput without reporting ratio, p95, errors, and memory.
- Full tests, pure-mode tests, lint, type checking, strict docs, and sanitizer checks pass.

## Implementation order

1. benchmark harness and untouched baseline;
2. pure web-policy reference functions and exhaustive vectors;
3. native `_core` policy helpers and parity/performance gates;
4. CSRF middleware and security tests;
5. structured HSTS hardening;
6. CacheControl contract, middleware, and static-file integration;
7. pure gzip facade and compression middleware correctness;
8. optional native zlib extension, one-shot and streaming APIs;
9. middleware ordering integration and end-to-end benchmarks;
10. retain or remove each native helper according to measured gates;
11. full checks, sanitizer runs, after-results, and documentation.

Keep cryptographic review, cache semantics, and compression kernel changes as separate review units even when implemented by one agent.
