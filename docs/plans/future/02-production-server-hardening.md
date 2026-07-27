# Production server hardening plan

## Status

Future proposal; evidence program, not a production-readiness claim.

## Objective

Define the work required before Neo's native server can be recommended for persistent, adversarial, and high-concurrency production traffic. Neo applications must remain deployable on another conforming ASGI server throughout this program.

## Scope

- HTTP/1.1, HTTP/2, optional HTTP/3, WebSocket, TLS, ALPN, proxy, and lifespan behavior.
- Slow clients, malformed framing, cancellation, backpressure, disconnects, shutdown, and resource exhaustion.
- Single-process operation, supervisor integration, rolling drain, and socket inheritance decisions.
- Native memory ownership, free-threading, sanitizers, fuzzing, and long-duration soak evidence.

## Required semantics

All configured limits are enforced before unbounded allocation. Paused transports do not accumulate unbounded application output. Disconnect and timeout cancellation release request tasks and borrowed resources. Graceful shutdown stops admission, drains bounded in-flight work, closes persistent connections with protocol-appropriate signals, and exits by a configured deadline.

A protocol feature is production-candidate only when its independent conformance, fault, and soak evidence is published for the exact configuration. HTTP/1.1 evidence does not imply HTTP/2 or HTTP/3 readiness.

## C and pure split

Native protocol implementations retain parser, framing, buffer, and transport hot paths. Pure implementations remain executable semantic references. Server-level lifecycle, signal, configuration, and application task ownership stay in Python unless profiling proves a bounded primitive belongs in C.

Every native ownership change requires focused parity tests and ASan/UBSan coverage. Shared state introduced for free-threading must have explicit synchronization and race tests.

## Workstreams

1. Independent protocol conformance suites and differential tests.
2. Structure-aware fuzzers for framing, HPACK/QPACK, WebSocket, and state transitions.
3. Slowloris, fragmented-body, pipelining, flow-control, and cancellation tests.
4. TLS handshake, client-certificate, rotation, ALPN, and proxy deployment validation.
5. Backpressure and bounded-memory tests with slow readers and writers.
6. Graceful drain and rolling-restart behavior for HTTP and WebSocket connections.
7. Reconnect-storm, resource exhaustion, and multi-hour soak campaigns.
8. Multi-process/supervisor design or an explicit external-supervisor contract.
9. Independent load generation with retained latency, error, memory, and environment data.

## Verification matrix

Test plain TCP and TLS, pure and native protocols, default and free-threaded CPython, supported event loops, direct and trusted-proxy deployment, normal and constrained file descriptors, clean and forced shutdown, and every advertised protocol independently.

Skipped tests cannot count as evidence. Failures must distinguish connection errors, stream errors, application errors, and expected limit rejection.

## Completion criteria

- No known unbounded queue or buffer under configured operation.
- Published conformance, fuzz, sanitizer, fault, soak, and independent-load reports.
- Documented deployment and drain procedures.
- Actionable diagnostics for unsupported or unsafe configurations.
- A narrowly stated production-candidate matrix rather than a blanket claim.

## Risks

Native protocol code has a large security surface. Scope should contract rather than weaken evidence requirements; mature external ASGI servers remain the safe fallback.
