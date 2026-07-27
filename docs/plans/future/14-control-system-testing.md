# Control-system testing toolkit plan

## Status

Future proposal; generic fault and time testing, not domain simulation.

## Objective

Make long-lived connections, polling, leases, retries, delivery uncertainty, and multi-replica ownership deterministic enough to test without wall-clock sleeps or unreliable external infrastructure.

## Components

### Virtual clock

A clock protocol supplies monotonic and wall/database-like time, sleep, and deadline scheduling. Production uses real time; tests advance explicitly. Code that participates in supervision, retries, jobs, heartbeat, leases, or command timeouts receives the clock rather than calling time functions directly.

### Scripted peers

Provide bounded HTTP, WebSocket, and raw-stream test peers that can fragment frames, delay bytes, close at selected points, violate framing, withhold acknowledgements, replay messages, and assert outbound ordering. These peers are test utilities and do not become production protocol implementations.

### Fault injection

Named fault points cover database borrow/query/commit, transport connect/write/read, acknowledgement persistence, lease renewal, exporter flush, and shutdown. Faults can raise, delay, cancel, disconnect, or return an ambiguous result. Production builds keep only zero/near-zero-overhead disabled checks if fault points are present at all.

### Multi-instance harness

Run multiple application instances against shared test PostgreSQL and optional brokers, expose instance lifecycle controls, and inspect ownership/job/command state. Support abrupt termination separately from graceful shutdown.

### State-machine models

Reference models generate operation sequences for supervisor transitions, job leases/fences, outbox delivery, inbox deduplication, and connection generations. Native and pure backends execute the same traces.

## C and pure split

The test API is primarily Python. Native protocol parsers expose deterministic bounded feed points already used by fuzzers and parity tests. C state primitives accept serialized operation traces where useful; pure models remain the readable oracle. Test-only instrumentation must not introduce production hidden globals.

## Phases

1. Define clock and deterministic random/jitter protocols.
2. Add scripted network peers and failure-point vocabulary.
3. Add subsystem state-machine models and trace replay.
4. Add multi-instance PostgreSQL harness.
5. Add reconnect-storm, soak, and resource accounting tools.
6. Integrate fuzz corpus retention and regression minimization.

## Verification targets

- No tests depend on arbitrary sleeps where virtual time is applicable.
- Every durable transition has a crash point before and after it.
- Every network parser sees complete, fragmented, malformed, and disconnected input.
- Every bounded queue is tested at capacity and overflow.
- Process death, stale fences, duplicate messages, and uncertain sends are reproducible.
- Native failures are exercised under ASan/UBSan and suitable fuzzers.

## Completion criteria

A regression involving timeout, reconnect, duplicate delivery, stale ownership, or shutdown can be expressed as a deterministic focused test and replayed against pure and native implementations.

## Risks

An all-purpose simulator becomes more complex than production. Keep primitives composable, model only documented guarantees, and retain real socket/database/broker integration and soak tests.
