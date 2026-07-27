# WebSocket operational layer plan

## Status

Future proposal; builds above the existing portable `WebSocket` wrapper.

## Objective

Provide the connection lifecycle, flow control, heartbeat, task ownership, grouping, and diagnostics needed for long-lived production sessions without embedding any application subprotocol in Neo.

## Public model

A `WebSocketService` or connection manager is application-owned and registered through supervised services. Each accepted connection has a stable local ID, logical key, generation, selected subprotocol, authentication context, bounded outbound queue, heartbeat policy, and child task group.

Handlers may use direct request/response-style iteration for simple cases or attach to the operational manager for persistent routing. Portable ASGI remains the public wire contract; native fast paths cannot be required by application code.

## Lifecycle

1. Validate route, origin/authentication, and requested subprotocol.
2. Reserve bounded registry and queue capacity before acceptance.
3. Accept and publish local readiness/presence.
4. Run receive, send, heartbeat, and application tasks under one owner.
5. On any terminal failure, cancel siblings, close once, remove presence, and release queues.
6. During server drain, stop accepting, notify peers where supported, flush only a bounded queue, and close by deadline.

## Flow control

Outbound sends enter a bounded per-connection queue with configurable reject, disconnect, or application backpressure behavior. Broadcast/group operations iterate bounded snapshots and report partial delivery; they do not build an unbounded fan-out task list. Maximum message size and fragmentation budgets are enforced before complete allocation.

Heartbeat distinguishes transport ping/pong from application heartbeat. Timeouts, tolerated misses, and activity reset rules are explicit. Heartbeat does not declare durable peer state; it only updates connection liveness.

## Security

Subprotocol selection must come from the requested set. Origin policy, authentication, token expiry/renewal, authorization changes, and forced disconnect are explicit hooks. Log and close reasons are sanitized and bounded.

## C and pure split

Existing native WebSocket framing remains a lower layer. Additional C primitives may provide compact registry entries, queue rings, generation checks, group indexes, and heartbeat deadline heaps. Python owns ASGI calls, user handlers, policy, coroutine cancellation, and callbacks. The pure twin uses dictionaries, deques, and heaps with identical semantics.

## Phases

1. Specify connection state, ownership, queue, close, and heartbeat contracts.
2. Implement pure single-process manager and deterministic test harness.
3. Add supervision, health, metrics, and graceful drain.
4. Add bounded groups/broadcast and authentication renewal hooks.
5. Add native registry/queue/deadline primitives after profiling.
6. Integrate distributed ownership as a separate layer.

## Verification

Test disconnect during accept/send/receive, fragmented input, slow consumer, queue overflow, heartbeat races, simultaneous close, handler failure, token expiry, drain, broadcast churn, reconnect storms, pure/native parity, and behavior on another ASGI server.

## Completion criteria

Every connection and child task has one owner, all queues and deadlines are bounded, close occurs exactly once, drain has a finite endpoint, and operators can inspect active count, age, queue pressure, heartbeat latency, and disconnect cause.
