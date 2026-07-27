# Data-layer completion plan

## Status

Future proposal; complements rather than reverses Neo's explicit PostgreSQL and ORM omissions.

## Objective

Fill operational data-path gaps needed by write-heavy, concurrent, and event-driven applications while preserving raw SQL, explicit I/O, application-owned schema management, and cancellation-safe pool behavior.

## Scope

- Documented migration-tool workflow without Neo applying DDL.
- Optimistic concurrency/compare-and-set patterns.
- PostgreSQL upsert and returning support.
- Bounded batch execution and `COPY` ingestion.
- Transactional outbox/inbox helpers.
- Workload-separated pools and replica consistency guidance.
- High-volume retention/partitioning recipes.
- Driver recovery certification for cancellation and network failure.

## Schema management

Neo continues not to create, alter, or drop application schema. Provide a migration integration protocol, startup schema/version assertion, advisory-lock recipe, deployment ordering guide, and examples for at least one external migration tool. Neo-owned optional service tables expose versioned `schema_sql()` and explicit upgrade scripts.

## Concurrency

Add explicit compare-and-set operations based on declared version columns or caller-provided predicates. A stale write returns a distinct outcome or raises a documented concurrency error. No implicit session-wide optimistic behavior is introduced.

Upsert APIs must expose PostgreSQL conflict target, update set, predicate, and returned shape rather than guessing from model declarations. Raw SQL remains the escape hatch and reference behavior.

## Bulk paths

Provide bounded batch sizes and streaming `COPY` APIs with clear transaction ownership. Producers are backpressured; cancellation either restores protocol synchronization or discards the connection. Errors identify whether the transaction is aborted and whether the connection can be reused.

## C and pure split

Native PostgreSQL owns measured wire parsing, codecs, hydration, buffers, and COPY framing. Python owns SQL shape, transaction policy, model semantics, migration integration, iterators, and application callbacks. Pure protocol behavior remains equivalent for values, errors, nulls, cancellation, and pool reuse.

## Phases

1. Publish migration and deployment workflow.
2. Add schema-version assertions and concurrency primitives.
3. Add explicit upsert and bounded batch APIs.
4. Implement pure streaming COPY followed by native framing/codec acceleration.
5. Add outbox/inbox storage helpers shared by jobs and delivery.
6. Certify failure recovery, replicas, contention, and high-volume operation.

## Verification

Test stale versions, conflicting upserts, partial batches, producer failure, cancellation during COPY, transaction abort, reconnect, schema mismatch, migration lock contention, replica lag assumptions, pool partition starvation, and pure/native parity. Retain PostgreSQL version and server settings in benchmarks.

## Completion criteria

Applications can perform concurrency-safe updates and bounded bulk ingestion without hidden I/O, schema changes remain external and reviewable, and cancellation/failure leaves every pooled connection synchronized or discarded.

## Risks

Convenience APIs can become an implicit SQL abstraction or migration system. Keep PostgreSQL semantics visible and prefer focused primitives over cross-database generalization.
