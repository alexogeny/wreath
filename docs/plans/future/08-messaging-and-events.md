# Messaging and event integration plan

## Status

Future proposal; broker-neutral contracts with optional adapters.

## Objective

Integrate external event systems without selecting a mandatory broker or pretending different brokers have identical guarantees. Provide common lifecycle, backpressure, acknowledgement, telemetry, and handler ownership while exposing adapter-specific capabilities explicitly.

## Core contracts

Define `Publisher`, `Consumer`, `Message`, `Delivery`, `Subscription`, and capability metadata. A message carries payload bytes, content type/schema version, message ID, timestamp, correlation/causation IDs, ordering key, and bounded headers. Delivery exposes explicit acknowledge, retry/nack, and terminal reject operations.

Consumers are supervised services. They stop fetching before shutdown, drain bounded in-flight handlers, settle deliveries where possible, and then close by deadline. Prefetch and handler concurrency are finite configuration values.

## Guarantee model

Adapters declare ordering scope, delivery guarantee, acknowledgement mode, transaction support, replay/offset behavior, maximum message size, and dead-letter support. The common API promises no stronger behavior than the selected adapter. At-least-once is the expected baseline; application inbox deduplication is the portable defense against duplicates.

Broker publication cannot generally share a transaction with PostgreSQL. Reliable publication therefore uses the command/outbox plan: commit an outbox row with business state, then let a supervised relay publish and mark progress.

## Optional adapters

Potential packages may target NATS, Kafka, RabbitMQ, Redis Streams, or PostgreSQL notifications/queues. Adapter SDKs remain optional dependencies outside the dependency-free core. Each adapter supplies a capability declaration, health snapshot, metrics, fault tests, and deployment guide.

## C and pure split

Core C primitives may validate bounded metadata, frame internal event envelopes, maintain bounded delivery-state tables, and accelerate checksums or local queues. Python owns adapter SDK calls, rebalance callbacks, user handlers, retry policy, and transactions. Broker protocol implementations should not be written in Neo merely to satisfy a C-first label.

Pure twins cover internal envelope and state primitives. Adapter behavior is tested against real broker versions and deterministic fakes; native/pure parity does not replace broker integration tests.

## Phases

1. Define envelope, delivery, capabilities, shutdown, and error taxonomy.
2. Build deterministic in-memory test adapter with strict finite bounds; document it as non-durable.
3. Integrate supervision and observability.
4. Implement one production adapter based on an actual target workload.
5. Connect transactional outbox relay and inbox deduplication.
6. Add additional adapters only with maintainers and conformance suites.

## Verification

Test duplicate delivery, out-of-order messages, handler cancellation, poison messages, redelivery, prefetch pressure, broker disconnect/reconnect, credential rotation, consumer rebalance, shutdown with in-flight work, outbox publish uncertainty, and adapter capability rejection.

## Completion criteria

Applications can reason about the selected broker's exact guarantees, no consumer has unbounded in-flight work, shutdown does not silently abandon owned deliveries, and broker failures are visible without blocking unrelated request handling.

## Risks

An over-normalized API hides critical semantics. Keep the shared contract narrow and expose native adapter handles or extension interfaces for broker-specific features.
