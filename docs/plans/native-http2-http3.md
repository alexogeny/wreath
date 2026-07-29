# Prescriptive plan: native HTTP/2, HTTP/3, and protocol benchmarks

Status: ready for test-first implementation

Related material:

- `docs/plans/native-http-server.md`
- `docs/plans/native-server-sanitizers.md`
- `docs/decisions/0008-native-http-server-boundary.md`
- `benchmarks/README.md`

## Goal

Extend Neo's native server from HTTP/1.1 to production-shaped HTTP/2 and HTTP/3 while preserving Neo as a dependency-free ASGI framework. HTTP parsing, compression state, stream state, flow control, and response encoding remain below the Python application boundary. Add a reproducible benchmark matrix that compares HTTP/1.1, HTTP/2, and HTTP/3 without confusing protocol performance with framework performance.

The work is test driven: add the complete executable compliance suite for a protocol slice before implementing that slice. New compliance tests must initially collect and fail for the missing behavior. Do not write placeholder tests, weaken assertions, mark implemented behavior `xfail`, or make malformed input tests pass by merely closing the connection when a specific protocol error is required.

## Repository constraints

- Target CPython 3.14 only.
- Keep `src/neo` free of mandatory third-party runtime dependencies.
- Preserve `serve(app, config, ssl=...)` and HTTP/1.1 behavior.
- Preserve the pure HTTP/1.1 fallback and `NEO_PURE=1` behavior.
- Keep HTTP/2 and HTTP/3 optional native server capabilities. Requesting an unavailable protocol must raise a clear startup error; it must never silently downgrade.
- Continue using asyncio or uvloop for TCP/UDP polling. Do not add a custom event loop.
- Implement HTTP/2 framing, HPACK, stream state, and flow control in repo-native C.
- Do not implement QUIC or TLS cryptography from scratch. HTTP/3 uses optional native `ngtcp2` and `nghttp3` libraries with OpenSSL's supported QUIC/TLS integration. Their use is isolated in the HTTP/3 extension and does not affect a normal Neo install.
- Cross into Python once per request stream to invoke the ASGI application. Frame processing, header compression, flow control, and output scheduling must not bounce through Python.
- Optimize only after correctness, sanitizer, and benchmark baselines exist. Every optimization must retain raw before/after results from multiple runs.

## Standards and initial feature boundary

Treat these as normative:

- HTTP semantics: RFC 9110
- HTTP/2: RFC 9113
- HPACK: RFC 7541
- QUIC transport: RFC 9000
- QUIC TLS: RFC 9001
- QUIC loss detection: RFC 9002
- HTTP/3: RFC 9114
- QPACK: RFC 9204

The first HTTP/2 release supports TLS ALPN `h2`. The native protocol type may be driven directly without TLS in unit tests. Do not add prior-knowledge h2c or HTTP/1.1 Upgrade support in this change.

The first HTTP/3 release supports QUIC v1 and ALPN `h3` with TLS 1.3. Disable 0-RTT, connection migration, server push, WebTransport, QUIC DATAGRAM, HTTP/2 extended CONNECT, and HTTP/3 extended CONNECT. Existing HTTP/1.1 WebSockets remain supported; protocol benchmark scenarios must mark WebSockets unavailable for HTTP/2 and HTTP/3.

## Public contracts

### Server configuration

Extend `src/neo/server.py` with these public types:

```python
HttpProtocolName = Literal["http/1.1", "h2", "h3"]

@dataclass(frozen=True, slots=True)
class TLSConfig:
    certfile: str | os.PathLike[str]
    keyfile: str | os.PathLike[str]
    password: str | None = None

@dataclass(frozen=True, slots=True)
class ServerConfig:
    # Existing fields remain unchanged.
    protocols: tuple[HttpProtocolName, ...] = ("http/1.1",)
    max_concurrent_streams: int = 100
    initial_stream_window: int = 65_535
    initial_connection_window: int = 1_048_576
    max_header_list_bytes: int = 32 * 1024
    hpack_table_bytes: int = 4 * 1024
    qpack_table_bytes: int = 4 * 1024
    qpack_blocked_streams: int = 16
```

Validation rules:

- `protocols` is non-empty, ordered, contains no duplicates, and only contains the three documented values.
- All limits are positive except compression table sizes and blocked-stream count, which may be zero.
- `h3` requires `TLSConfig` and an available `neo._native._http3` module.
- `h2` in network serving requires TLS and ALPN; direct protocol tests are exempt.
- `ssl=` remains valid when `h3` is absent.
- Passing both `ssl=` and `tls=` is an error.
- `TLSConfig` builds the TCP `SSLContext` and also supplies the certificate/key to the QUIC backend. Do not attempt to extract private-key material from a Python `SSLContext`.

Add `tls: TLSConfig | None = None` to `serve()` and `run()`. Do not remove or reinterpret `ssl=`.

### Server ownership

`Server` continues to own lifespan and shutdown. Extend it to own:

- one TCP `asyncio.Server` when HTTP/1.1 or HTTP/2 is enabled;
- one UDP datagram transport when HTTP/3 is enabled;
- the active TCP protocol registry;
- the active QUIC connection registry.

When `port=0` and TCP plus UDP are enabled, bind TCP first, read its assigned port, then bind UDP to the same numeric port. `Server.sockets` continues to report TCP sockets for compatibility. Add a read-only `datagram_addresses` property for HTTP/3 endpoints.

### Native module boundary

Do not continue growing all protocols in `src/neo/_native/_servermodule.c`.

Refactor the existing extension into these compilation units while preserving the extension name and `HttpProtocol` compatibility alias:

```text
src/neo/_native/server.h
src/neo/_native/server_common.c
src/neo/_native/server_http1.c
src/neo/_native/server_http2.c
src/neo/_native/server_hpack.c
src/neo/_native/_servermodule.c
```

`neo._native._server` exports:

- `Http1Protocol`
- `Http2Protocol`
- `NegotiatingHttpProtocol`
- `HttpProtocol`, retained as an alias of `Http1Protocol`

Build HTTP/3 separately so missing QUIC libraries cannot break HTTP/1.1 or HTTP/2 imports:

```text
src/neo/_native/http3.h
src/neo/_native/http3_connection.c
src/neo/_native/http3_asgi.c
src/neo/_native/_http3module.c
```

`neo._native._http3` exports a datagram endpoint type used only by `neo.server`. Configure this extension only when `NEO_BUILD_HTTP3=1`; detect the required native packages during the build and fail the requested HTTP/3 build with an actionable error. A default build must remain compiler-and-CPython-headers only.

`NegotiatingHttpProtocol.connection_made()` reads the selected TLS ALPN protocol after the handshake and delegates to HTTP/1.1 or HTTP/2. Missing, unknown, or unconfigured ALPN values close the transport without invoking ASGI. Never inspect the first application bytes to guess a TLS protocol.

## Test-first implementation sequence

Each numbered item is a merge/checkpoint boundary. At every boundary:

1. add all tests listed for that item;
2. run them and record the expected failures with `update_feature_tdd(..., "red")`;
3. implement only enough production code to satisfy that item;
4. run focused tests, full pytest, Ruff, and `ty`;
5. run native parity and sanitizer checks when C changed;
6. record green/refactored state before proceeding.

Do not combine multiple red/green boundaries into one large implementation commit.

### 1. Freeze HTTP/1.1 behavior and split the C source

Add no new protocol behavior here. Extend existing HTTP/1.1 tests only where needed to lock module exports, protocol selection, startup, shutdown, TLS, and error behavior before moving code.

Required tests:

- `test_http_protocol_alias_remains_http1`
- `test_default_config_enables_only_http11`
- `test_http11_ssl_api_remains_supported`
- `test_protocol_config_rejects_empty_unknown_and_duplicate_values`
- `test_requesting_unbuilt_http3_fails_without_downgrade`

Move code mechanically into the listed compilation units. The complete existing server suite must remain green under pure and native implementations. Compare benchmark and allocation baselines before and after the split; the split itself must not claim a performance improvement.

### 2. Add the complete HTTP/2 codec compliance suite

Create:

```text
tests/http2/conftest.py
tests/http2/test_frames.py
tests/http2/test_hpack_vectors.py
tests/http2/test_hpack_errors.py
tests/http2/test_connection_state.py
tests/http2/test_header_validation.py
tests/http2/test_flow_control.py
tests/http2/test_asgi.py
tests/http2/test_shutdown.py
tests/http2/test_network.py
```

Build a test-only frame encoder/decoder in `tests/http2/support.py`. It must be an independent, obvious reference helper, not imported by production code. Use the RFC 7541 examples as fixtures and include their source section in fixture comments.

The full HTTP/2 suite must cover before implementation:

- exact client preface validation, including every truncated prefix;
- the 9-byte frame header split at every byte boundary;
- payload split at every byte boundary for every supported frame type;
- reserved-bit handling and stream-ID constraints;
- SETTINGS validation, ACK rules, duplicates, and value ranges;
- HEADERS plus every legal CONTINUATION fragmentation point;
- rejection of interleaved frames during a header block;
- HPACK indexed, literal, never-indexed, dynamic-size-update, eviction, and Huffman forms;
- invalid Huffman padding/EOS, integer overflow, truncated integers/strings, and table-size violations;
- pseudo-header ordering, uniqueness, required fields, CONNECT rules, lowercase names, and forbidden connection headers;
- stream state transitions for idle, open, half-closed, reserved, and closed streams;
- odd client stream IDs and monotonically increasing stream creation;
- connection and stream flow-control exhaustion, overflow, WINDOW_UPDATE, and recovery;
- SETTINGS_MAX_CONCURRENT_STREAMS enforcement;
- DATA/content-length agreement and request-body limit enforcement;
- PING, RST_STREAM, GOAWAY, and unknown-frame handling;
- connection errors versus stream errors with the exact RFC error code;
- ASGI scope, request-body delivery, disconnect, cancellation, response start/body/trailers, and application exceptions;
- multiplexed streams completing out of order without response corruption;
- slow consumers and transport `pause_writing()`/`resume_writing()`;
- graceful GOAWAY using the last processed stream ID and draining accepted streams;
- TLS ALPN selection and rejection paths.

Tests must assert emitted bytes, stream/connection state, ASGI messages, and exact protocol error codes. A generic “transport closed” assertion is insufficient where the RFC defines an error frame.

Only after this entire suite collects and fails may `server_http2.c` and `server_hpack.c` be implemented.

### 3. Implement HTTP/2 beneath one ASGI crossing per stream

Use one native connection object and a native stream table keyed by the 31-bit stream ID. Each accepted request creates exactly one ASGI task. Store request body chunks natively until `receive()` consumes them; do not create one Python callback/task per DATA frame.

Required implementation rules:

- Parse incrementally without concatenating the full connection buffer for every frame.
- Keep a read cursor and compact only when consumed prefix size justifies it.
- Bound frame payload, compressed header block, decoded header list, dynamic table, queued request body, and queued response body memory.
- Use checked `size_t`/`Py_ssize_t` arithmetic before every allocation and length addition.
- Preserve header bytes as bytes in ASGI scopes; do not decode and re-encode ordinary fields.
- Allocate stream state once and release it when both protocol and ASGI ownership end.
- Do not retain completed tasks, futures, scopes, header lists, exception tracebacks, or transport references.
- Apply connection and stream flow control before accepting more DATA and before emitting response DATA.
- Schedule writable streams fairly. A large response must not starve small responses.
- Disable server push by advertising `SETTINGS_ENABLE_PUSH=0` and treat illegal client values according to RFC 9113.
- Ignore legacy priority information safely; do not build a priority tree in this change.
- Emit one HEADERS block followed by the minimum necessary CONTINUATION frames.
- Batch adjacent frame bytes with `writelines()` or a single contiguous write only when measurement shows it reduces overhead without unbounded copying.

HTTP/2 scope requirements:

```python
scope["type"] == "http"
scope["http_version"] == "2"
scope["scheme"] == "https"
```

Pseudo-headers do not appear in `scope["headers"]`. `:authority` maps to a
`host` header when no regular host field is present. An explicit regular `host`
is preserved only when it identifies the same normalized authority; a mismatch
is a stream protocol error so routing and application policy cannot select two
different tenants. Preserve duplicate ordinary headers and their order, except
for `host`, which is singular.

Run `h2spec` against the network server after the in-process suite passes. Check its version into benchmark/test metadata and store raw output under the test artifact directory. No mandatory h2spec section may fail or be excluded without a written scope justification in this plan.

### 4. Add HTTP/3 build isolation and the complete failing suite

First add an ADR documenting the optional QUIC boundary: Neo owns ASGI integration, limits, lifecycle, and server configuration; `ngtcp2` owns QUIC transport/TLS integration; `nghttp3` owns HTTP/3 framing and QPACK. Keep calls behind `http3.h` so the backend can be replaced without changing `neo.server` or ASGI behavior.

Create:

```text
tests/http3/conftest.py
tests/http3/test_availability.py
tests/http3/test_settings.py
tests/http3/test_headers.py
tests/http3/test_stream_state.py
tests/http3/test_asgi.py
tests/http3/test_limits.py
tests/http3/test_timeouts.py
tests/http3/test_shutdown.py
tests/http3/test_network.py
tests/http3/test_interop.py
```

The HTTP/3 CI job must build the optional extension and install fixed, recorded versions of `ngtcp2`, `nghttp3`, OpenSSL, and the conformance clients. Tests in that job must not skip because the backend is unavailable. The default no-HTTP/3 job separately proves clean import and explicit unavailable errors.

Before implementing the endpoint, add failing tests for:

- QUIC v1 and `h3` ALPN negotiation;
- rejection of unsupported versions without ASGI invocation;
- client/server unidirectional control streams;
- one control stream and one QPACK encoder/decoder stream per connection;
- SETTINGS uniqueness and valid ranges;
- request pseudo-header and ordinary-header validation equivalent to HTTP/2 semantics;
- QPACK static/dynamic references, blocked streams, unblocking, cancellation, and configured bounds;
- request streams ending normally, resetting, and stopping;
- ASGI request/response mapping with `http_version == "3"`;
- concurrent streams completing out of order;
- connection close and stream reset propagation to ASGI;
- request-body, header-list, QPACK table, blocked-stream, and concurrent-stream limits;
- idle, handshake, request, and shutdown timeouts using deterministic loop time where possible;
- anti-amplification behavior before address validation;
- malformed packets and malformed HTTP/3 frames never invoking ASGI;
- graceful shutdown refusing new requests while draining accepted streams;
- `port=0` TCP/UDP same-port binding;
- repeated endpoint create/close cycles leaving no tasks, handles, native connections, or reference growth.

Do not mock `ngtcp2` or `nghttp3` in the network and lifecycle tests. Small callback-unit tests may use a narrow fake only to force otherwise unreachable backend error returns.

### 5. Implement HTTP/3 endpoint and lifecycle

Use `asyncio.DatagramProtocol` only as the polling boundary. Datagram receipt enters the native endpoint once; packet parsing, ACK/loss state, stream dispatch, HTTP/3 framing, and QPACK remain native until an ASGI request is ready.

Required implementation rules:

- Maintain a native connection table keyed by connection IDs with explicit ownership.
- Drive QUIC expiry using one endpoint timer for the nearest connection deadline, not one Python timer per packet or stream.
- Re-arm that timer only when the nearest deadline changes.
- Batch outgoing UDP datagrams where the transport/platform API permits, but retain a correct single-datagram path.
- Never keep borrowed datagram memory after the callback returns.
- Enforce anti-amplification limits before address validation.
- Disable 0-RTT and migration explicitly rather than accepting them accidentally.
- Map QUIC stream reset/stop events to ASGI disconnect exactly once.
- On application failure before response start, emit a minimal 500 where the stream is still writable; after response start, reset only that stream.
- Free QPACK state, stream buffers, timers, connection IDs, TLS objects, ASGI tasks, and endpoint references on every normal and exceptional close path.
- Keep native dependency errors out of the hot path. Resolve required symbols and create backend configuration at endpoint startup.

Run the available HTTP/3 conformance suite plus interoperability requests from at least two independent clients. Record exact tool/library versions and raw outputs. A successful request from Neo's own benchmark client is not interoperability evidence.

### 6. Integrate mixed-protocol startup and shutdown

Add `tests/test_server_protocols.py` covering every supported combination:

- HTTP/1.1 only;
- HTTP/2 only;
- HTTP/1.1 plus HTTP/2 with ALPN preference matching `config.protocols`;
- HTTP/3 only;
- all three protocols;
- missing TLS, missing HTTP/3 extension, UDP bind failure, partial startup cleanup, and lifespan startup failure;
- repeated `close()` and `wait_closed()` calls;
- shutdown with idle connections, active HTTP/2 streams, and active HTTP/3 streams.

Startup is atomic: if any requested listener fails, close every listener already created, tear down endpoint state, run lifespan shutdown when startup completed, and re-raise. Do not leave a TCP-only server running after requested HTTP/3 startup fails.

Shutdown order is fixed:

1. stop TCP accepts and reject new QUIC connections;
2. ask HTTP/1.1 connections to stop accepting requests;
3. send HTTP/2 GOAWAY and HTTP/3 graceful-shutdown signals;
4. drain accepted requests until `shutdown_timeout`;
5. cancel remaining ASGI tasks and close transports/endpoints;
6. wait for native protocol registries to become empty;
7. run lifespan shutdown;
8. resolve `wait_closed()`.

## Memory safety and performance rules

These are release blockers, not cleanup work:

- No unbounded buffer, header table, stream table, pending-body queue, blocked-stream queue, or output queue.
- No unchecked multiplication/addition used for allocation sizes, frame lengths, offsets, or flow-control windows.
- No native pointer may outlive the Python/native object that owns it.
- Every task callback must tolerate connection teardown occurring first.
- Clear stored exceptions and tracebacks immediately after logging/handling them.
- Cycles involving protocol, bound methods, tasks, futures, transport, and registry must be broken on close.
- Every `Py_INCREF`, native allocation, QUIC object, HTTP/3 object, TLS object, and timer registration must have one documented owner and one cleanup path.
- Do not use private CPython struct fields, undefined behavior, busy polling, sleeps to hide races, oversized preallocation, disabled validation, hard-coded benchmark responses, or route-specific fast paths.
- Do not recognize benchmark clients or scenarios in server code.
- Do not skip ASGI, header validation, flow control, compression, TLS, or response framing in benchmark mode.
- Environment switches may disable logging or access logs, but may not disable protocol correctness or safety limits.
- Unsafe optimizations require a focused benchmark, sanitizer coverage, an explanatory comment, and a safe fallback when practical.

Required leak checks:

- repeated valid requests on reused HTTP/2 and HTTP/3 connections;
- repeated malformed handshakes/frames/packets;
- stream reset during request and response bodies;
- application cancellation and exception paths;
- timeout and forced shutdown paths;
- endpoint startup failure after partial allocation;
- 10,000 create/use/close cycles in the dedicated leak runner;
- stable native allocation and Python reference counts after warmup, allowing only documented allocator caching.

Extend `docs/plans/native-server-sanitizers.md` and sanitizer tooling to cover both extensions. Run ASan, UBSan, and available leak detection against focused protocol tests, fuzz harnesses, and a network smoke run. Add fuzz targets for HTTP/2 frame parsing, HPACK decoding, HTTP/3 callback/input boundaries not already covered by upstream libraries, and lifecycle event sequences.

## Benchmark suite changes

The benchmark protocol is an orthogonal result dimension. Do not add `neo-native-h2` or `neo-native-h3` as framework names.

### Contracts

Extend `benchmarks/scenarios.py` so `Scenario` can declare supported protocols independently of frameworks:

```python
protocols: frozenset[str] = frozenset({"http/1.1", "h2", "h3"})
```

Mark `ws-echo` as HTTP/1.1-only initially. Existing scenarios should support all three unless their semantics are unsupported.

Extend every result row from `benchmarks/run.py` with:

```text
protocol
transport                 # tcp or udp
secure                    # always true in three-protocol comparisons
alpn
connections
max_streams_per_connection
trial
load_generator
load_generator_version
server_tls_version
```

Preserve raw per-trial rows. Aggregation belongs in `benchmarks/report.py`; never replace raw trials with averages.

### Load-generator boundary

Refactor `benchmarks/load.py` behind a small adapter contract while retaining its current HTTP/1.1 implementation as the dependency-free development generator. Add subprocess adapters for protocol-capable independent tools rather than importing client protocol libraries into the server process.

Use an HTTP/3-enabled `h2load` build for HTTP/2 and HTTP/3 development measurements. Record its build features and version. Keep HTTP/1.1 results from the current client clearly labeled as a different generator; do not declare a cross-protocol winner from mixed-generator results.

For a publishable three-protocol comparison, require one independent generator/build that has been verified to exercise all three protocols, or publish separate protocol results without ranking them. The report must display a warning whenever compared rows use different generators.

Add CLI options:

```text
--protocol http/1.1 h2 h3
--trials N                 # default 5 for protocol comparisons
--connections N
--streams-per-connection N
--tls-cert PATH
--tls-key PATH
--load-generator auto|builtin|h2load
```

`benchmarks/neo_server.py` receives protocol and TLS arguments and starts exactly the requested protocol set. Readiness checks must perform a valid request over the selected protocol; opening a TCP socket does not prove HTTP/2 readiness, and HTTP/3 has no TCP readiness path.

### Fair comparison matrix

The default three-protocol comparison runs only `neo-native` and uses:

- the same application and scenario definitions;
- TLS 1.3 for HTTP/1.1, HTTP/2, and HTTP/3;
- equal total in-flight requests;
- explicit connection and stream counts;
- identical request/response bodies and headers;
- fixed warmup followed by at least five measured trials;
- randomized protocol order with the chosen order stored in metadata;
- zero-error rows only in winner/summary calculations.

Run two separate modes:

1. steady-state connections, excluding handshake from request latency;
2. cold connections, explicitly including TCP/TLS or QUIC handshake cost.

Add protocol proof scenarios:

- small static response;
- dynamic parameter route;
- JSON request body;
- 64 KiB response;
- 1 MiB response;
- concurrent delayed streams demonstrating multiplexing;
- streaming response;
- cold connection request.

Report throughput, median, p95, p99, errors, measured duration, process RSS where available, protocol, TLS/ALPN, connection count, streams per connection, Python version, loop, platform, server revision, generator version, warmup, trial count, and scenario body sizes.

Loopback remains a development measurement. Any publishable HTTP/3 claim additionally requires an independent load-generator host and recorded network latency/loss configuration. Keep raw files and do not claim a win from one run.

### Benchmark tests

Extend `tests/test_benchmark_scenarios.py` and add `tests/test_benchmark_protocols.py` before changing the runner. Required tests:

- protocol support is independent from framework support;
- WebSocket is unavailable for h2/h3;
- every result includes the protocol metadata fields;
- unavailable protocol/tool combinations are reported, not mislabeled as zero throughput;
- readiness uses the selected protocol;
- HTTP/2 and HTTP/3 request counts represent completed streams, not frames or packets;
- concurrency maps to connections and streams as configured;
- reports never choose an error row as winner;
- reports warn and avoid cross-protocol ranking for mixed generators;
- raw trials remain present and aggregate values are derived from them;
- protocol order randomization is seeded and recorded for reproducibility.

## Files expected to change

```text
setup.py
pyproject.toml
src/neo/server.py
src/neo/_native/_servermodule.c
src/neo/_native/server.h
src/neo/_native/server_common.c
src/neo/_native/server_http1.c
src/neo/_native/server_http2.c
src/neo/_native/server_hpack.c
src/neo/_native/http3.h
src/neo/_native/http3_connection.c
src/neo/_native/http3_asgi.c
src/neo/_native/_http3module.c
tests/http2/*
tests/http3/*
tests/test_server.py
tests/test_server_protocol.py
tests/test_server_protocols.py
tests/test_benchmark_scenarios.py
tests/test_benchmark_protocols.py
benchmarks/scenarios.py
benchmarks/load.py
benchmarks/run.py
benchmarks/report.py
benchmarks/neo_server.py
benchmarks/README.md
docs/native/README.md
docs/plans/native-server-sanitizers.md
docs/decisions/<next>-native-multiprotocol-server.md
docs/decisions/<next>-optional-quic-backend.md
```

Do not modify framework routing, request, response, middleware, or authentication APIs unless a failing ASGI semantics test proves a protocol-independent defect.

## Required checks at completion

```bash
uv run pytest
uv run ruff check .
uv run ty check
```

Additionally run and retain artifacts for:

- pure/native HTTP/1.1 parity;
- HTTP/2 focused suite;
- HTTP/3 focused suite in the optional-backend CI image;
- `h2spec` with no unexplained mandatory failures;
- HTTP/3 conformance and two-client interoperability runs;
- ASan/UBSan/leak runs for HTTP/1.1, HTTP/2, and HTTP/3;
- parser and lifecycle fuzz smoke runs;
- repeated three-protocol benchmark matrix.

## Acceptance checks

- Existing HTTP/1.1 and framework tests pass unchanged under pure and native modes.
- A TLS listener negotiates HTTP/1.1 or HTTP/2 strictly through ALPN and sets the correct ASGI `http_version`.
- One HTTP/2 connection serves concurrent requests that complete out of order with correct bodies and bounded memory.
- One HTTP/3 endpoint serves concurrent QUIC request streams and sets ASGI `http_version` to `3`.
- Invalid HTTP/2 frames, HPACK blocks, QUIC events, HTTP/3 frames, and QPACK blocks never reach the application and produce the required scoped error.
- Flow-control exhaustion pauses work without busy looping and resumes after valid credit.
- Graceful shutdown drains accepted streams, rejects new work, and leaves no active tasks, transports, timers, native connections, or growing references.
- Default installation remains dependency-free and imports without HTTP/3 libraries.
- Explicitly requesting unavailable HTTP/3 fails at startup without downgrading.
- The benchmark suite stores raw repeated results for all three protocols with protocol, TLS, connection, stream, generator, and environment metadata.
- The report does not rank errored rows or mixed-generator protocol rows as a fair winner.
- No benchmark-specific response, validation bypass, disabled safety limit, leaked allocation, sanitizer finding, or unexplained conformance failure is accepted as a performance optimization.
