# Reliable command delivery plan

## Status

Future proposal; depends on PostgreSQL, supervised services, managed transports, and observability.

## Objective

Provide generic transactional outbox/inbox, dispatch, acknowledgement, retry, ordering, and reconciliation primitives for commands sent over unreliable networks. Domain command names and payload semantics remain outside Neo.

## Guarantees

The system provides durable intent and at-least-once dispatch. It does not promise exactly-once remote effects. A stable command ID and application-defined idempotency or reconciliation are required to handle uncertain outcomes.

## Outbox transaction

Applications enqueue a command in the same PostgreSQL transaction that changes desired business state. The row records command ID, destination, kind, payload/version, idempotency key, ordering key/sequence, state, attempts, next attempt, lease/fencing data, acknowledgement deadline, and structured failure.

The enqueue API accepts an existing Neo connection or ORM session. It never commits an application transaction implicitly.

## State machine

The baseline progression is `pending -> leased -> sent -> acknowledged -> confirmed`, with bounded transitions to `retry_wait`, `failed`, or `cancelled`. Transport loss after send and before acknowledgement creates an explicit `unknown` outcome. Unknown commands are reconciled or retried only under declared idempotency policy.

Every update validates the current state and fencing token. Terminal states are immutable except through an authorized administrative operation that creates an audit record.

## Dispatcher

A supervised dispatcher claims bounded batches using leases and fencing. Destinations have independent concurrency and queue limits. Ordering keys serialize only the streams that require ordering; one slow destination cannot block unrelated streams. Admission stops during drain, and in-flight sends finish or return to recoverable persisted state by deadline.

Transport adapters map send acceptance and acknowledgement events into generic delivery outcomes. An active WebSocket, managed outbound client, or broker can be a transport without changing outbox semantics.

## Inbox and deduplication

Inbound commands/events use a unique `(source, message_id)` record. The inbox insert, application side effects, and recorded result share one transaction. Duplicate input returns or references the prior result. Retention and purge are explicit because an expired deduplication record weakens the guarantee.

## C and pure split

Python owns transactions, policy, payload versions, transport callbacks, and reconciliation. C may accelerate transition validation, sequence checks, destination queue indexes, envelope framing, and checksums. In-memory native queues are rebuildable caches and never the durable source of truth. Pure twins execute identical transitions.

## Phases

1. Formal state, uncertainty, idempotency, ordering, and fence specification.
2. Schema and transactional outbox/inbox APIs.
3. Single-process supervised dispatcher with a deterministic fake transport.
4. Managed HTTP/WebSocket and broker transport adapters.
5. Reconciliation, administration, audit, and retention.
6. Multi-replica contention, failure, soak, and native profiling.

## Verification

Inject failure before commit, after commit, after claim, before send, after remote acceptance, before acknowledgement persistence, during lease renewal, and during shutdown. Test duplicate acknowledgements, stale fences, reordered responses, destination overload, poison payloads, retention expiry, and process restart.

## Completion criteria

A committed intent is recoverable after process death, stale dispatchers cannot finalize it, uncertainty is represented rather than guessed away, duplicate inputs are transactionally contained, and operators can inspect backlog, age, attempts, and terminal cause.
