# Future application-platform plans

## Status

Future proposals for discussion. None of these documents is an accepted compatibility promise or a claim that the feature exists. Each plan needs an ADR or an explicitly approved implementation issue before work starts.

## Purpose

These plans describe the generic capabilities needed to use Neo as the foundation of a distributed, connection-heavy control system. The motivating workload includes inbound APIs, persistent peers, outbound integrations, polling, durable control loops, distributed ownership, and reliable commands. Domain protocols and algorithms remain application or ecosystem concerns.

## Shared implementation model

All plans preserve Neo's existing constraints:

- `src/neo` has no mandatory third-party runtime dependencies.
- The framework remains usable on conforming ASGI servers.
- Python owns coroutine execution, cancellation, policy, transactions, and user callbacks.
- C owns measured hot primitives: strict parsers, bounded buffers and queues, compact state tables, counters, histograms, and deadline structures.
- Every native behavior has a pure-Python semantic twin selected by `NEO_PURE=1`.
- Native and pure implementations may differ in representation, never in public results, errors, limits, cancellation, or ownership.
- Durable truth lives in PostgreSQL or an explicitly configured external system, never only in a native in-memory queue.
- New crossings of the Python/native boundary are measured across realistic lifecycle paths.

A likely layering is:

```text
application and protocol adapters
    -> public Neo service APIs
    -> Python lifecycle/orchestration
    -> native or pure bounded primitives
    -> asyncio transports, PostgreSQL, and optional exporters/brokers
```

The default implementation sequence is contract and pure reference first, focused parity tests second, native acceleration third, and optimization claims only after retained repeated measurements. A C implementation may be developed alongside the reference, but it must not become the only executable specification.

## Plans

1. [Compatibility and support contract](01-compatibility-and-support.md)
2. [Production server hardening](02-production-server-hardening.md)
3. [Supervised application services](03-supervised-services.md)
4. [Managed outbound clients](04-managed-outbound-clients.md)
5. [Operational observability](05-operational-observability.md)
6. [Security extensions](06-security-extensions.md)
7. [Durable PostgreSQL jobs](07-durable-postgres-jobs.md)
8. [Messaging and event integration](08-messaging-and-events.md)
9. [Reliable command delivery](09-reliable-command-delivery.md)
10. [Distributed connection ownership](10-distributed-connection-ownership.md)
11. [WebSocket operational layer](11-websocket-operational-layer.md)
12. [Data-layer completion](12-data-layer-completion.md)
13. [API lifecycle tooling](13-api-lifecycle-tooling.md)
14. [Control-system testing toolkit](14-control-system-testing.md)
15. [Operational configuration](15-operational-configuration.md)
16. [Wreath-owned native event loop](16-native-event-loop.md)

## Dependency order

The recommended critical path is:

1. Compatibility contracts and production gates.
2. Supervised services and deterministic time.
3. Observability hooks, so every later subsystem is measurable.
4. Managed HTTP/1.1 and WebSocket clients.
5. Durable jobs and reliable command delivery.
6. Distributed connection ownership and broker adapters.
7. Data, API, testing, security, and configuration maturation in parallel where their prerequisites permit.

Server hardening proceeds independently and must not block use of Neo on another conforming ASGI server.

## Common release gates

Every implemented plan must include:

- documented public ownership and cancellation semantics;
- finite defaults or mandatory limits for queues, bodies, waiters, labels, leases, and retries;
- focused pure tests and native/pure differential tests;
- malformed input, disconnect, timeout, shutdown, and partial-failure tests;
- free-threaded testing where shared native state is introduced;
- sanitizer and fuzz coverage for changed native parsing or ownership;
- request-boundary baseline review where request execution changes;
- user guides, API reference, agent manifest, and migration notes where applicable;
- reproducible benchmarks before any performance claim.

## Non-goals

These plans do not add domain protocols, balancing algorithms, device models, forecasting, grid semantics, or vendor-specific payloads to Neo. They also do not promise exactly-once delivery, transparent database failover, invisible distributed coordination, or automatic schema mutation.
