# Prescriptive plan: Neo native HTTP/1.1 server

Status: ready for implementation

Related decision: `docs/decisions/0008-native-http-server-boundary.md`

## Goal

Implement an optional Neo-owned HTTP/1.1 server with protocol parsing, request-body framing, connection state, backpressure, and response encoding in handwritten C. Expose it through a thin Python facade while preserving Neo as a normal ASGI framework that remains usable behind Uvicorn or any other conforming server.

The first server must use the `asyncio.Protocol` transport boundary. Do not build a custom epoll/kqueue/IOCP event loop in this change. This keeps socket polling, TLS, and platform differences in asyncio or uvloop while moving the HTTP hot path into Neo's C extension.

## Fixed constraints

- Target CPython 3.14.
- Add no mandatory or optional third-party runtime dependency.
- Keep the framework accelerator `neo._native._core` separate from the server extension.
- Name the server extension `neo._native._server`.
- Provide a behaviorally equivalent reference implementation in `neo._pure.server`.
- Respect `NEO_PURE=1` when selecting the implementation.
- Support arbitrary ASGI 3 HTTP applications; do not special-case `Neo` or bypass `Neo.__call__` in the first implementation.
- Support HTTP/1.0 and HTTP/1.1 only.
- Do not implement HTTP/2 or WebSockets in this change.
- Process at most one application request at a time per connection. Preserve pipelined bytes and process the next request only after the current response finishes.
- Keep lifespan, signals, TLS configuration, and graceful-shutdown orchestration in Python because they are not request hot paths.
- Keep all ownership explicit. Do not add a global server or connection registry.
- Do not run or report benchmarks until all correctness checks pass. Run the native-server benchmark at the very end.

## Required public API

Create `src/neo/server.py`.

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from ssl import SSLContext
from typing import Any, Literal, Protocol

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApplication = Callable[[Scope, Receive, Send], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    backlog: int = 2048
    keep_alive_timeout: float = 5.0
    request_timeout: float = 30.0
    shutdown_timeout: float = 10.0
    max_request_line: int = 8 * 1024
    max_header_count: int = 100
    max_header_bytes: int = 32 * 1024
    max_body_bytes: int = 16 * 1024 * 1024
    read_high_water: int = 256 * 1024
    lifespan: Literal["auto", "on", "off"] = "auto"


async def serve(
    app: ASGIApplication,
    config: ServerConfig | None = None,
    *,
    ssl: SSLContext | None = None,
) -> Server: ...


def run(
    app: ASGIApplication,
    config: ServerConfig | None = None,
    *,
    ssl: SSLContext | None = None,
) -> None: ...
```

Implement `Server` with this minimum surface:

```python
class Server:
    async def close(self) -> None: ...
    async def wait_closed(self) -> None: ...
    async def serve_forever(self) -> None: ...

    @property
    def sockets(self) -> tuple[Any, ...]: ...
```

`Server` must explicitly own the `asyncio.AbstractServer`, active protocol instances, lifespan state, and shutdown state.

Do not export server symbols from `neo.__init__` initially. Users should explicitly import `neo.server`; this avoids prematurely enlarging the framework API.

## Required source layout

Create these files:

```text
src/neo/server.py
src/neo/_pure/server.py
src/neo/_native/server/server.h
src/neo/_native/server/_servermodule.c
src/neo/_native/server/protocol.c
src/neo/_native/server/http1.c
src/neo/_native/server/body.c
src/neo/_native/server/response.c
src/neo/_native/server/asgi.c
tests/test_server_protocol.py
tests/test_server.py
benchmarks/neo_server.py
```

Update:

```text
setup.py
src/neo/_native/http.c
src/neo/_native/neocore.h
tests/test_native_parity.py
benchmarks/run.py
benchmarks/report.py
docs/native/README.md
README.md
```

Keep each C file focused. Do not collapse the server into one large source file.

## Ordered implementation checklist

### 1. Extract an internal C request-head parser

Refactor `src/neo/_native/http.c` before implementing sockets or protocols.

Add internal parser declarations to a shared header. Prefer a small `src/neo/_native/http_parser.h`; include it from both `_core` and `_server`. Use `neocore.h` only if introducing a second internal header creates unnecessary duplication.

Required structures:

```c
typedef struct {
    const uint8_t *name;
    Py_ssize_t name_len;
    const uint8_t *value;
    Py_ssize_t value_len;
} neo_http_header;

typedef struct {
    const uint8_t *method;
    Py_ssize_t method_len;
    const uint8_t *target;
    Py_ssize_t target_len;
    int minor_version;
    neo_http_header *headers;
    Py_ssize_t header_count;
    Py_ssize_t consumed;
} neo_http_head;

typedef struct {
    Py_ssize_t max_request_line;
    Py_ssize_t max_header_count;
    Py_ssize_t max_header_bytes;
} neo_http_limits;

typedef enum {
    NEO_HTTP_OK,
    NEO_HTTP_INCOMPLETE,
    NEO_HTTP_BAD_REQUEST,
    NEO_HTTP_URI_TOO_LONG,
    NEO_HTTP_HEADERS_TOO_LARGE,
    NEO_HTTP_VERSION_NOT_SUPPORTED
} neo_http_result;
```

Required function:

```c
neo_http_result neo_http_parse_head(
    const uint8_t *buffer,
    Py_ssize_t length,
    const neo_http_limits *limits,
    neo_http_head *out
);
```

Rules:

- The internal parser must not allocate Python objects.
- Header names must be normalized to lowercase before constructing the ASGI list. Normalization may happen when copying into connection-owned storage.
- Preserve the existing `_core.http_parse_request(data)` result and exceptions exactly.
- Keep `_core.http_parse_request()` as a Python-object wrapper around the internal parser.
- Do not duplicate parser logic between `_core` and `_server`.

Add parity tests before proceeding. Existing parser tests must remain unchanged and pass.

### 2. Implement the pure-Python protocol first

Create `src/neo/_pure/server.py` as the executable behavioral specification.

Implement an `asyncio.Protocol` class with:

```python
class HttpProtocol(asyncio.Protocol):
    def connection_made(self, transport): ...
    def data_received(self, data: bytes): ...
    def eof_received(self): ...
    def connection_lost(self, exc): ...
    def pause_writing(self): ...
    def resume_writing(self): ...
```

The constructor must accept:

```python
HttpProtocol(app, config, loop, connection_registry)
```

Implement and test these states:

```text
READING_HEAD
READING_FIXED_BODY
READING_CHUNK_SIZE
READING_CHUNK_DATA
READING_CHUNK_TRAILERS
REQUEST_RUNNING
CLOSING
```

Use a bytearray input buffer and explicit cursor. Compact it only after consumed data becomes material; avoid deleting from its front after every read.

Do not dispatch the next pipelined request until the previous response has emitted its terminal body message.

This implementation is the reference for native parity. Prioritize explicit behavior over speed.

### 3. Implement ASGI scope, receive, and send behavior

Construct this HTTP scope:

```python
{
    "type": "http",
    "asgi": {"version": "3.0", "spec_version": "2.5"},
    "http_version": "1.0" | "1.1",
    "method": str,
    "scheme": "http" | "https",
    "path": str,
    "raw_path": bytes,
    "query_string": bytes,
    "headers": list[tuple[bytes, bytes]],
    "server": tuple[str, int],
    "client": tuple[str, int],
    "root_path": "",
}
```

Split the target at the first `?`. Preserve the original path bytes in `raw_path`. Decode `path` using ASGI-compatible UTF-8/percent-decoding semantics already established by Neo's codec functions. Malformed targets must produce 400 and must not call the application.

The receive callable must produce ordered messages:

```python
{"type": "http.request", "body": chunk, "more_body": True}
{"type": "http.request", "body": final_chunk, "more_body": False}
```

After a lost connection it must produce exactly one observable disconnect state:

```python
{"type": "http.disconnect"}
```

The send callable must accept only:

```text
http.response.start
http.response.body
```

Enforce:

- One response start only.
- No body before response start.
- No messages after a terminal body.
- Application exceptions before response start produce a minimal 500.
- Application exceptions after response start close the connection.
- Invalid ASGI message sequences close the response deterministically and surface an application error through the task exception handler or configured server logger.

### 4. Implement body framing

Support fixed-length and chunked bodies.

For `Content-Length`:

- Reject non-decimal, signed, negative, or overflowing values.
- Reject conflicting duplicate values.
- Either accept identical duplicate values or reject all duplicates; choose one policy and lock it with tests. Prefer accepting identical values and rejecting conflicts.
- Reject a value over `max_body_bytes` with 413 before dispatching body data.

For `Transfer-Encoding`:

- Support only a final `chunked` coding.
- Reject simultaneous `Transfer-Encoding` and `Content-Length` with 400 and close the connection.
- Reject unsupported transfer codings.
- Parse hexadecimal chunk sizes with overflow checks.
- Ignore syntactically valid chunk extensions.
- Validate every CRLF boundary.
- Parse and validate trailers under the configured header limits, but do not expose trailers until a separate ASGI extension is deliberately designed.
- Count decoded bytes against `max_body_bytes`.

Backpressure:

- Queue body chunks until consumed by ASGI `receive()`.
- Call `transport.pause_reading()` when queued data exceeds `read_high_water`.
- Call `transport.resume_reading()` after queued data falls below half the high-water value.
- Never continue unbounded buffering while the application is slow.

### 5. Implement response framing

Encode status lines and headers in the protocol implementation.

Required behavior:

- Strip a response body for `HEAD`, 1xx, 204, and 304.
- Reject invalid header names or values from the application.
- Preserve repeated response headers such as `set-cookie`.
- If a valid `Content-Length` is supplied, enforce that the emitted body length matches it.
- If no length is supplied for a completed non-streaming response, add one when possible.
- For HTTP/1.1 streaming responses without a length, use chunked transfer encoding.
- For HTTP/1.0 responses without a length, use connection-close framing.
- HTTP/1.1 defaults to keep-alive unless either side requests close.
- HTTP/1.0 defaults to close unless both framing and `Connection: keep-alive` permit reuse.
- Never reuse a connection after malformed or ambiguous framing.

Precompute common status-line fragments in C, but retain a safe generic encoder for valid uncommon status codes.

### 6. Implement timeouts and graceful connection closure

Each protocol instance owns its timeout handles.

- Start the request timeout when a request head begins arriving.
- Cancel it after the terminal response body or connection loss.
- Start the keep-alive timeout only while waiting for the next request.
- Cancel it as soon as new request bytes arrive.
- A request timeout closes the connection after a minimal 408 response when safe.
- A keep-alive timeout closes without invoking the application.
- `connection_lost()` cancels every pending timer and waiter.

Add assertions/tests that no timeout callback retains a closed protocol instance.

### 7. Port the reference protocol to C

Create `neo._native._server.HttpProtocol` with the same constructor and observable protocol methods as the pure implementation.

The native connection object must own:

- Application reference
- Transport reference
- Loop reference
- Connection-registry reference
- Input buffer and cursor
- Parser and body-decoder state
- Current application task
- Pending receive waiter
- Pending write-backpressure waiter
- Timeout handles
- Response state
- Close/disconnect flags

Create native callable types for ASGI receive and send:

```text
HttpReceive
HttpSend
```

Their calls return awaitables. Use loop-created futures for genuinely pending operations. Use already-resolved futures or a small native awaitable only after profiling proves that future allocation matters.

The native send adapter writes encoded bytes directly through the asyncio transport. If `pause_writing()` has been called, the returned awaitable must stay pending until `resume_writing()`.

C safety requirements:

- Every object field has clear owned/borrowed-reference documentation in `server.h`.
- Clear Python references during connection teardown.
- Make task callbacks tolerate connection teardown and interpreter shutdown.
- Guard every size addition and multiplication against `PY_SSIZE_T_MAX` overflow.
- Do not retain pointers into a resizable buffer across resize operations.
- Keep GIL assumptions explicit.
- Do not use `Py_MOD_GIL_NOT_USED` in this change.

### 8. Add the native extension build

Update `setup.py` with a second extension:

```python
Extension(
    "neo._native._server",
    sources=[
        "src/neo/_native/server/_servermodule.c",
        "src/neo/_native/server/protocol.c",
        "src/neo/_native/server/http1.c",
        "src/neo/_native/server/body.c",
        "src/neo/_native/server/response.c",
        "src/neo/_native/server/asgi.c",
        # Include the shared parser source if symbols cannot be shared
        # across independently loaded extension modules.
    ],
    depends=[
        "src/neo/_native/server/server.h",
        "src/neo/_native/http_parser.h",
    ],
    extra_compile_args=extra_compile_args,
)
```

Python extension modules cannot safely assume that private C symbols from `_core` are link-visible to `_server`. Compile the same shared parser source into both modules when necessary. Do not create a second parser implementation.

### 9. Implement the Python facade and selection

`src/neo/server.py` selects the implementation in this order:

1. If `NEO_PURE` is set, import `neo._pure.server`.
2. Otherwise try `neo._native._server`.
3. Fall back to `neo._pure.server` on `ImportError`.

Do not modify `src/neo/_native/__init__.py` to make server import failure affect `_core`. Framework accelerators and server availability must remain independent.

The facade owns:

- `ServerConfig` validation
- Event-loop lookup
- `loop.create_server()`
- Active-protocol registry
- Lifespan startup/shutdown
- TLS argument forwarding
- Signal handling in synchronous `run()`
- Graceful shutdown

`serve()` must not install process signal handlers. `run()` may install SIGINT/SIGTERM handlers when running in the main thread.

### 10. Implement lifespan

Support `lifespan="auto"`, `"on"`, and `"off"`.

Startup order:

1. Validate configuration.
2. Run lifespan startup unless off.
3. If startup succeeds, create the listening server.
4. Return the `Server` owner.

Shutdown order:

1. Stop accepting connections.
2. Ask active protocols to stop accepting new requests.
3. Allow active responses to drain until `shutdown_timeout`.
4. Close remaining transports.
5. Wait for protocol teardown.
6. Run lifespan shutdown.
7. Resolve `wait_closed()`.

In auto mode, an application that explicitly rejects lifespan may continue without it. An application startup failure must still fail server startup. In on mode, unsupported lifespan is an error.

### 11. Add protocol and integration tests

Add fake-transport tests to `tests/test_server_protocol.py` for both pure and native implementations:

- Header split at every byte boundary.
- Body split at every byte boundary.
- Chunk-size and chunk-data split at every byte boundary.
- Two complete requests in one `data_received()` call.
- Pipelined response ordering.
- Conflicting `Content-Length` headers.
- `Transfer-Encoding` plus `Content-Length`.
- Oversized request line, headers, and body.
- Read pause/resume watermarks.
- Write pause/resume awaitable behavior.
- Keep-alive timeout.
- Request timeout.
- Disconnect before body completion.
- Disconnect during streaming response.
- Application exception before response start.
- Application exception after response start.
- HEAD, 204, and 304 body suppression.
- HTTP/1.0 close and keep-alive behavior.
- Protocol object collectability after connection loss.

Parameterize each behavioral test over pure and native implementations. Mark native cases with the repository's existing native skip marker.

Add loopback socket tests to `tests/test_server.py`:

- Serve a `Neo` application.
- Serve a minimal non-Neo ASGI application.
- GET with keep-alive reuse.
- POST with fixed body.
- POST with chunked body.
- Streaming response.
- Pipelined requests preserve order.
- Malformed requests receive the expected status and do not call the app.
- Graceful shutdown drains an active response.
- TLS works through an asyncio SSL transport.
- `NEO_PURE=1` facade selection works in a subprocess or isolated import test.

Do not compare internal object layouts between pure and native implementations. Compare bytes on the wire, ASGI scopes/messages, closure behavior, and errors.

### 12. Add parser fuzzing and sanitizer entry points

Add a small native harness for:

- Request-head parser
- Chunk-size parser
- Chunk framing
- Header framing combinations

Document ASan/UBSan build commands in `docs/native/README.md`. Preserve every discovered crash input under a bounded test-fixture directory and add a regression test.

This work may land after the main pytest behavior is complete, but the server must not be described as production-ready until sanitizer and sustained soak checks pass.

### 13. Add separate benchmark execution

Create `benchmarks/neo_server.py` that starts the application using `neo.server` and accepts host/port arguments matching `benchmarks/sanic_server.py`.

Update benchmark selection so server identity is explicit. Do not silently replace Neo's Uvicorn run.

Required benchmark comparisons:

```text
Neo + Uvicorn          framework comparison
Neo + native server    Neo server comparison
Sanic + native server  end-to-end native-stack comparison
```

Record at minimum:

- Framework
- Server
- Event loop
- Native or pure server implementation
- Python version/build and free-threading mode
- Host/port policy
- Concurrency
- Warmup and measured requests
- Startup time
- Throughput
- Median, p95, and p99
- Errors/disconnects
- Suite duration

Keep Neo + Uvicorn in the same-server framework charts. Put Neo-native and Sanic-native in an explicitly labeled native-server/end-to-end section.

Run benchmarks only after all checks below pass. Use repeated runs before claiming an improvement.

## HTTP error mapping

Use this fixed mapping unless an existing Neo test requires a different result:

| Condition | Response | Connection |
| --- | --- | --- |
| Malformed request line/header/framing | 400 | close |
| Unsupported expectation | 417 | close |
| Body exceeds configured maximum | 413 | close |
| Request target exceeds maximum | 414 | close |
| Header count/bytes exceed maximum | 431 | close |
| Request timeout | 408 | close |
| Unsupported HTTP version | 505 | close |
| Application error before response start | 500 | close |
| Application error after response start | no second response | close |

All server-generated error responses must be minimal, have deterministic `Content-Length`, and include `Connection: close`.

## Correctness rules

- Never call the ASGI application for malformed or ambiguous requests.
- Never process requests after a framing error on the same connection.
- Never emit responses out of pipeline order.
- Never buffer an unbounded request body or response backlog.
- Never expose mutable connection buffers directly to application code.
- Never retain a pointer into a Python bytes/bytearray after releasing its buffer or resizing it.
- Deliver body chunks and disconnect messages in ASGI order.
- Preserve duplicate request and response headers where ASGI requires them.
- Keep response headers as byte pairs at the ASGI boundary.
- Preserve Neo's operation behind Uvicorn without importing `neo.server`.
- A missing `_server` extension must not disable `_core` JSON, routing, codec, or parser accelerators.
- TLS is owned by the asyncio transport; do not implement TLS in C.
- Do not claim free-threaded safety until tested separately.

## Verification commands

Run focused checks while implementing:

```bash
uv sync --group dev --reinstall-package neo-asgi
uv run pytest tests/test_native_parity.py
uv run pytest tests/test_server_protocol.py
uv run pytest tests/test_server.py
uv run ruff check .
uv run ty check
```

Before benchmarking, run the complete correctness suite:

```bash
uv sync --group dev --reinstall-package neo-asgi
uv run pytest
uv run ruff check .
uv run ty check
```

Only after all commands above pass, install benchmark dependencies and run the server comparison:

```bash
uv sync --group benchmark --reinstall-package neo-asgi
uv run python -m benchmarks.run --framework neo
```

If benchmark server selection is implemented as a separate argument, run both explicit variants instead:

```bash
uv run python -m benchmarks.run --framework neo --server uvicorn
uv run python -m benchmarks.run --framework neo --server neo-native
```

Retain timestamped raw JSON and HTML reports. Run each publishable comparison repeatedly with the same Python build, event loop, concurrency, and machine state.

## Completion criteria

The implementation is complete only when all of these are true:

- `await neo.server.serve(app)` serves an arbitrary conforming ASGI HTTP application.
- `neo.server.run(app)` starts and gracefully stops a Neo application.
- The native server is a separate extension from `_core`.
- `NEO_PURE=1` selects the pure protocol implementation.
- Missing `_server` falls back without affecting framework accelerators.
- Pure and native implementations pass the same protocol behavior tests.
- Fragmented fixed-length and chunked bodies reach ASGI receive correctly.
- Pipelined requests produce responses in request order.
- Streaming responses use correct HTTP framing and honor write backpressure.
- Read backpressure prevents unbounded request-body buffering.
- HEAD, 1xx, 204, and 304 responses emit no prohibited body bytes.
- HTTP/1.0 and HTTP/1.1 keep-alive behavior is covered by socket tests.
- Malformed and request-smuggling inputs never reach the application.
- Graceful shutdown drains active requests and closes at the configured deadline.
- Timeout callbacks and connection tasks do not retain closed protocols.
- Full pytest, Ruff, and type checks pass.
- Neo still runs normally behind Uvicorn.
- Native-server benchmark results are reported separately from framework-only comparisons.
- Documentation labels the server experimental until fuzzing, sanitizer, soak, and security review work is complete.
