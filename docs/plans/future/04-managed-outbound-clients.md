# Managed outbound clients plan

## Status

Future proposal; optional runtime capability preserving the dependency-free framework core.

## Objective

Add lifespan-managed outbound HTTP and WebSocket clients with strict limits, cancellation-safe pooling, TLS, resilience policy, and observability. Do not infer client support from Neo's server-side protocol implementations.

## Public model

Applications configure named clients at startup and inject or retrieve them explicitly:

```python
partners = app.http_client(
    "partners",
    base_url="https://partner.example",
    limits=ClientLimits(max_connections=50, max_response_bytes=2_000_000),
    timeout=ClientTimeout(connect=2.0, response=10.0, total=15.0),
    retry=RetryPolicy(attempts=3, idempotent_only=True),
    tls=ClientTLS(...),
)
```

Pools are application-owned, open during startup, reject new work during drain, and close by a bounded shutdown deadline. Responses are streaming by default; collecting a body requires an enforced limit.

## Required semantics

- Per-origin and total connection/waiter limits.
- Connect, TLS, headers, body-idle, and total deadlines.
- Duplicate headers and raw byte preservation.
- Strict response framing and decompression limits.
- Cancellation either proves connection synchronization or discards it.
- Redirects, retries, and authentication replay are explicit and bounded.
- Retries default to idempotent operations and respect total deadlines.
- DNS, proxy, TLS trust, mTLS, and certificate reload have explicit owners.
- Outbound WebSockets provide reconnect policy, heartbeat, bounded send queues, subprotocol validation, and drain behavior.

## C and pure split

The native client backend may own HTTP response parsing, chunk decoding, header validation, WebSocket framing/masking, receive buffers, and compact pool-slot bookkeeping. Python owns DNS, asyncio transports, SSL contexts, policy, JSON conversion, callbacks, and async context management.

The pure twin uses `asyncio.open_connection` and strict streaming parsers. HTTP/1.1 is the first milestone. HTTP/2 multiplexing is separate and cannot reuse server-side claims without client-specific conformance work.

## Phases

1. Define request/response, deadlines, limits, and pool ownership contracts.
2. Implement pure HTTP/1.1 streaming client and fault test server.
3. Add native response parser and buffered protocol with parity tests.
4. Add TLS, proxy, mTLS, DNS, and drain behavior.
5. Add managed outbound WebSockets using shared framing primitives.
6. Add resilience policy and observability.
7. Evaluate HTTP/2 only from measured workloads.

## Verification

Test malformed and ambiguous framing, fragmented responses, early EOF, cancellation at every phase, slow headers/body, pool exhaustion, stale keep-alive connections, TLS failure, proxy failure, redirect loops, decompression bombs, reconnect storms, and pure/native parity. Compare with independent servers and clients.

## Completion criteria

Connections and waiters are bounded, cancellation cannot return a desynchronized connection to the pool, every request has a total deadline option, and pool state is visible through health and metrics.

## Risks

An HTTP client is a large security and interoperability commitment. If evidence or maintenance is insufficient, provide first-class lifecycle adapters for established optional clients instead of presenting a partial native client as production-ready.
