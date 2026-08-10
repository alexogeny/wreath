# Prescriptive plan: native outbound HTTP client

Status: ready for test-first implementation

Related material:

- `AGENTS.md`
- `repo-map.md`
- `docs/agents/manifest.json`
- `docs/decisions/0008-native-http-server-boundary.md`
- `docs/plans/native-http-server.md`
- `wreath.services`
- `wreath.http_client`
- `wreath.telemetry` / `wreath.logging`
- `docs/internals/performance.md`
- `benchmarks/README.md`

## Goal

Add a dependency-free, lifespan-managed outbound HTTP/1.1 client with pure-Python and optional native C protocol implementations. Reuse Neo's HTTP token validation, framing, buffering, byte-building, transport, and backpressure techniques without coupling client state to ASGI server state. Preserve bounded connection ownership, cancellation safety, SSRF defenses, TLS verification, pure/native parity, and honest retry semantics.

## Fixed constraints

- Target CPython 3.14 and keep `src/neo` free of mandatory third-party runtime dependencies.
- Preserve Neo as a conforming ASGI framework independent of its server and client implementations.
- Follow the native server boundary: asyncio or uvloop owns polling, DNS integration, sockets, and TLS; C owns measured protocol work.
- Provide a pure-Python client protocol with observable parity. `NEO_PURE=1` disables the native client.
- Keep client, server, and `_core` independently importable. Client build/import failure must not disable framework accelerators or the server.
- Measure existing server primitives before extraction and rerun server protocol, native lint, request-boundary, benchmark, and sanitizer gates afterward.
- Do not infer client correctness from server support. HTTP response parsing has distinct framing and lifecycle semantics.
- Keep HTTP/2, HTTP/3, outbound WebSockets, CONNECT tunnels, general-purpose proxies, decompression, and a custom event loop out of the first implementation.

## Architecture

```text
HTTPClient facade -------- Python policy, pooling, DNS, TLS, deadlines
      |
      +-- neo._pure.http_client ---- portable asyncio.Protocol
      |
      +-- neo._native._client ------ native asyncio.Protocol
                    |
                    +-- shared HTTP/1 C primitives
                               |
                               +-- compiled into neo._native._server too
```

Python owns policy, lifecycle, pool scheduling, retries, redirects, destination validation, and user-facing objects. C owns bounded byte processing and connection protocol state.

## Shared HTTP C boundary

Extract byte-level utilities from `src/neo/_native/http.c`, `server_http1.c`, and `server.h` into protocol-neutral files:

```text
src/neo/_native/http1_common.h
src/neo/_native/http1_head.c
src/neo/_native/http1_framing.c
src/neo/_native/http_buffer.c
src/neo/_native/http_builder.c
```

The shared surface should include only:

- RFC 9110 token and field-value validation;
- header-name normalization and common-header recognition;
- incremental header-terminator scanning;
- bounded decimal `Content-Length` parsing;
- transfer-encoding token parsing;
- chunk-size and chunk-terminator parsing;
- duplicate/conflicting framing helpers;
- geometric receive-buffer growth and compaction;
- private bytes-builder growth, exact resize, and ownership transfer;
- high/low-water accounting helpers without server assumptions.

Use plain C structs and return codes below the extension boundary. Shared helpers must not create ASGI messages, client response objects, routes, tasks, or policy decisions.

Do not reuse `NeoHttpProtocol`. Keep ASGI state, request-body queues, routing, server error responses, native response emission, and inbound pipelining inside `_server`. Add a separate `NeoHttpClientProtocol` for request method, response status, informational responses, body framing, cancellation uncertainty, and pool reuse state.

Add a separate extension in `setup.py`:

```text
neo._native._client
  src/neo/_native/_clientmodule.c
  src/neo/_native/client_http1.c
  src/neo/_native/http1_head.c
  src/neo/_native/http1_framing.c
  src/neo/_native/http_buffer.c
  src/neo/_native/http_builder.c
```

Compile the same shared sources into `_server`; do not dynamically expose internal symbols between extensions.

## Public contracts

Add `src/neo/http_client.py` with frozen/slotted configuration:

```python
@dataclass(frozen=True, slots=True)
class ClientLimits:
    max_connections: int = 20
    max_keepalive_connections: int = 10
    max_waiters: int = 100
    max_request_header_bytes: int = 32 * 1024
    max_response_header_bytes: int = 32 * 1024
    max_response_bytes: int = 2 * 1024 * 1024
    read_high_water: int = 256 * 1024


@dataclass(frozen=True, slots=True)
class ClientTimeout:
    pool: float = 1.0
    connect: float = 5.0
    tls: float = 5.0
    response_headers: float = 10.0
    response_body: float = 30.0
    total: float | None = 45.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 1
    idempotent_only: bool = True
    statuses: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class RedirectPolicy:
    enabled: bool = False
    max_hops: int = 0
    allow_cross_origin: bool = False
```

Register named clients explicitly:

```python
partners = app.http_client(
    "partners",
    base_url="https://partner.example",
    limits=ClientLimits(max_connections=50),
    timeout=ClientTimeout(connect=2.0, total=15.0),
    retry=RetryPolicy(attempts=3, idempotent_only=True),
)
```

The client starts with application lifespan, rejects work before successful startup and after shutdown begins, and exposes bounded health snapshots.

Request API:

```python
response = await partners.request(
    "POST",
    "/events",
    headers=((b"content-type", b"application/json"),),
    body=payload,
    idempotency_key=event_id,
)
```

Support replayable `bytes`, `bytearray`, and `memoryview` first. Support bounded async iterables only through explicit streaming; never retry a non-replayable stream transparently.

Response API:

```python
response.status
response.headers
response.body
response.http_version
response.extensions
response.raise_for_status()
```

Streaming:

```python
async with partners.stream("GET", "/export") as response:
    async for chunk in response.body:
        ...
```

Exiting without consuming the framed body closes the connection unless a bounded drain succeeds under explicit policy.

Stable exceptions:

```text
ClientError
ClientClosed
PoolTimeout
ConnectError
DNSFailure
TLSFailure
RequestTimeout
ResponseTimeout
ProtocolError
ResponseTooLarge
RedirectError
DestinationRejected
ProxyError
```

Exceptions retain phase, origin, attempt count, and a secret-free cause summary, never request bodies or credential headers.

## Request serialization

Pure and native serializers must:

- validate method and header tokens;
- use origin-form request targets;
- derive exactly one `Host` header from normalized authority;
- reject CR/LF and control bytes in targets and values;
- reject conflicting `Content-Length` and `Transfer-Encoding`;
- add content length for known replayable bodies;
- use chunked framing only for explicitly streamed HTTP/1.1 bodies;
- reject unsafe hop-by-hop headers;
- preserve duplicates only where HTTP semantics permit them;
- build request head and small body into transport-owned bytes only when measurement supports it.

Base-URL clients accept origin-relative paths, not network-path references or embedded credentials. `Expect: 100-continue` is deferred until it has explicit timeout/fallback tests.

## Response parser

The pure/native state machines must handle:

- response heads fragmented at every byte boundary;
- one or more informational `1xx` responses;
- bodyless `HEAD`, `204`, `304`, and successful `CONNECT` semantics;
- identical repeated content lengths and rejection of conflicts;
- transfer-encoding precedence and malformed tokens;
- chunk extensions, split chunks, terminal chunks, and bounded trailers;
- content-length bodies;
- close-delimited bodies;
- early EOF and unexpected extra bytes;
- sequential keep-alive responses without state contamination.

Associate each response with its request method because status alone does not determine body presence. Any ambiguous framing makes the connection non-reusable.

## Pool ownership

Pool by:

```text
scheme + normalized host + effective port + TLS policy + proxy policy
```

Non-negotiable invariants:

- active connections, idle connections, and waiters are independently bounded;
- waiters use a head/index queue, never front deletion;
- assignment has one owner and is cancellation-safe;
- idle connections have maximum age and idle deadlines;
- only complete, unambiguous responses permit reuse;
- `Connection: close`, close-delimited bodies, protocol errors, cancellation after send, and unread streams prevent reuse;
- shutdown stops acquisition, fails queued waiters, drains owned work to a deadline, then closes transports;
- pool state is visible through bounded counters and snapshots.

## Deadlines, cancellation, retries, and redirects

Use one total deadline plus phase deadlines, clamping each phase to remaining total budget.

- Cancellation before request assignment has no remote effect.
- Failure before request bytes are accepted may retry under policy.
- Failure after bytes may have been sent creates an uncertain remote outcome.
- Uncertain non-idempotent requests are not retried automatically.
- Response cancellation closes the connection unless framing is complete.
- `CancelledError` propagates after cleanup.

Retry requires remaining deadline/attempt budget, a replayable body, policy-approved method/idempotency, and a retryable phase/status. Use bounded exponential backoff with jitter. Parse and clamp `Retry-After`.

Redirects default off. When enabled, cap hops, detect loops, revalidate destination/DNS on every hop, remove authorization/cookie/signature headers across origins, and do not rewrite non-idempotent methods without explicit policy.

## Destination and SSRF policy

Add a validated `DestinationPolicy`:

```python
DestinationPolicy(
    schemes=frozenset({"https"}),
    hosts=("partner.example", "*.partner.example"),
    ports=frozenset({443}),
    allow_private=False,
    allow_loopback=False,
    allow_link_local=False,
)
```

Rules:

- prefer origins registered at startup;
- reject URL credentials and unsupported schemes;
- normalize internationalized hostnames before matching;
- validate the selected resolved address immediately before connection;
- block loopback, unspecified, private, link-local, multicast, and metadata ranges unless explicitly allowed;
- revalidate every DNS result and redirect;
- bind reuse to validated origin and TLS policy;
- disable environment proxies unless explicitly configured;
- use stdlib TLS hostname and certificate verification by default;
- exclude credentials and query strings from logs, labels, and exceptions.

## Observability

Expose bounded metrics/events for DNS, connect, TLS, pool wait, write, response headers/body, total duration, active/idle connections, waiters, reuse, stale closes, protocol errors, retries, redirects, and destination rejection.

Configured client name, phase, bounded outcome, method, and status may be dimensions. Full URLs, query strings, destination values from untrusted input, credentials, and bodies are not metric labels.

## Test-first work

### Pure codec foundation

- [x] Add `tests/test_http_client_protocol.py` with fixed-request serialization, injection/framing rejection, incomplete response heads, fragmentation, and malformed response cases.
- [x] Add `src/neo/_pure/http_client.py` with fixed-length HTTP/1.1 request serialization and incremental response-head parsing.
- [x] Extend the pure transport with content-length, chunked, close-delimited, informational, and bodyless response framing before exposing the public client API.
- [x] Add optional native request serialization and response-head parsing in the shared HTTP C tooling with pure/native parity fixtures.
- [ ] Move complete connection framing/state into the independent native client only after retained baselines are complete.

### Preserve and extract server primitives

- [ ] Add characterization tests for header validation, length/chunk parsing, buffer compaction, and builder growth.
- [ ] Retain server ingress/response, fragmentation, pipelining, pressure, request-boundary, and sanitizer baselines.
- [ ] Extract shared C helpers without adding client behavior in the same change.
- [ ] Prove existing pure/native server tests and native lints remain clean.

### Build the pure protocol

- [ ] Add a pure asyncio protocol and fake-transport harness.
- [x] Test request serialization and injection rejection.
- [x] Exercise fixed, chunked, close-delimited, bodyless, informational, and HEAD responses fragmented one byte at a time.
- [x] Test `1xx`, `HEAD`, conflicting lengths, unsupported transfer encodings, malformed chunks, and early EOF.
- [x] Add explicit `204`, `304`, close-framing, and unsolicited-extra-byte tests.
- [ ] Test streaming backpressure and unread-body cleanup.
- [x] Test bounded pool waiter saturation and waiter cancellation cleanup.
- [x] Cancel during response headers, response body, and retry backoff without pool leakage.
- [ ] Cancel at DNS, connect, TLS, and request write.

### Add native parity

- [x] Add independently importable `_client` initialization and select it ahead of `_core`.
- [x] Implement native fixed-request serialization and response-head parsing.
- [ ] Move incremental response body/framing and transport state into `_client`.
- [x] Run identical codec fixtures against pure/native paths.
- [x] Add deterministic randomized codec fragmentation and malformed-input parity tests.
- [x] Keep memory/error/GIL/complexity lints clean for the independent native codec module.
- [ ] Run server/client sanitizer variants after incremental framing/transport state moves into C.
- [x] Prove `NEO_PURE=1` selects the pure outbound codecs.

### Add managed policy

- [x] Add named registration and lifespan ownership.
- [x] Add bounded origin pools and waiter queues.
- [x] Add phase/attempt deadlines, stale handling, conservative retries, and bounded same-origin redirects.
- [x] Clamp retries and same-origin redirects to one end-to-end total deadline.
- [ ] Complete cross-origin redirect policy through separately configured clients.
- [x] Add TLS and SSRF policy with resolved-address validation.
- [x] Add shutdown draining for owned requests and bounded pool health snapshots.
- [ ] Test pool exhaustion, stale reuse, reconnect storms, DNS rebinding simulation, redirects, and TLS failures.

## Benchmark plan

- [x] Add `benchmarks/bench_http_client.py` with fixed serialization, pure/native response parsing, integrity checks, raw repeated samples, and managed keep-alive loopback timing.
- [x] Retain a small development baseline at `benchmark-results-http-client/baseline-small.json`.
- [x] Add a focused pure framing decomposition; the small run prices it at ~0.7 µs versus ~75 µs for managed loopback, so native framing classification is not yet justified.
- [ ] Add A/A noise calibration, independent peer coverage, TLS/new-connection decomposition, RSS, and publishable repeated trials.

Retain current server measurements before extracting C:

- `benchmarks/bench_native_http1_storage.py`;
- `benchmarks/bench_native_pressure.py`;
- `benchmarks/bench_native_request_bridge.py`;
- `neo-decomp`, request trace, native lint, and sanitizer results.

Add `benchmarks/bench_http_client.py` and price separately:

- request-head serialization;
- fragmented response-head parsing;
- content-length and chunked framing;
- small buffered and large streamed bodies;
- pool acquire/release and waiter wakeup;
- pure/native protocol execution;
- new versus reused TLS connections;
- complete one-request and keep-alive loops.

Record repeated raw trials, A/A noise, median/p95/p99, throughput, bytes per second, errors, RSS, pool wait, DNS/connect/TLS/header/body durations, Python version, loop, compiler flags, and native module path.

Do not test only Neo against itself. Use an independent server for the Neo client, an independent client for the Neo server, and Neo-to-Neo only as integration/combined ceiling. Cover fragmentation, stale keep-alive, ambiguous framing, slow peers, cancellation, pool exhaustion, TLS failure, redirect loops, proxy rejection, and reconnect storms.

## Likely files touched

```text
src/neo/http_client.py
src/neo/app.py
src/neo/__init__.py
src/neo/_pure/http_client.py
src/neo/_native/_clientmodule.c
src/neo/_native/client_http1.c
src/neo/_native/client.h
src/neo/_native/http1_common.h
src/neo/_native/http1_head.c
src/neo/_native/http1_framing.c
src/neo/_native/http_buffer.c
src/neo/_native/http_builder.c
src/neo/_native/http.c
src/neo/_native/server_http1.c
src/neo/_native/server.h
setup.py
tests/test_http_client.py
tests/test_http_client_protocol.py
benchmarks/bench_http_client.py
benchmarks/README.md
docs/reference/http-client.md
docs/agents/manifest.json
repo-map.md
```

## Out of scope

- HTTP/2, HTTP/3, outbound WebSockets, CONNECT tunnels, and custom event loops.
- Exactly-once effects or durable request queues.
- Arbitrary user-controlled destinations by default.
- Unbounded pooling, buffering, waiters, retries, redirects, or history.
- Transparent retries of non-replayable or uncertain non-idempotent requests.
- C-based DNS, TLS, retry policy, or user callbacks.

## Acceptance checks

- A named client starts/stops with the application and rejects work outside its lifecycle.
- Pure/native clients serialize requests and parse fragmented HTTP/1.1 responses with parity.
- Existing server behavior, request-boundary baseline, lints, benchmarks, and sanitizers remain clean after extraction.
- Connection, idle, waiter, header, and body memory remain bounded under saturation.
- No incomplete, ambiguous, cancelled-after-send, or unread-stream connection returns to the pool.
- Total/phase deadlines bound connection, response, retry, and shutdown work.
- SSRF policy rejects unsafe schemes, credentials, hosts, ports, addresses, and redirects.
- TLS verification is enabled by default.
- Logs, metrics, health, and exceptions contain no credentials or unrestricted payloads.
- Benchmark artifacts include repeated trials, A/A noise, environment metadata, errors, percentiles, throughput, and memory.
- Any native claim clears noise and reports complete-client impact, not parser-only timing.
- Focused/default/full tests, Ruff, ty, native lints, request trace, sanitizers, and strict docs build pass.
