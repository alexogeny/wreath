# 0013. PostgreSQL is the queue; there is no broker

Date: 2026-07-27
Status: Accepted

## Context

Durable jobs, a message bus, rate limiting, idempotency keys, session storage
and task progress all need shared state across workers. The reflex is Redis for
the fast paths and RabbitMQ or Kafka for the durable ones.

An application using Wreath's ORM already has PostgreSQL, already has a
connection pool, and already has transactions. Adding a broker adds an operational
component, a failure mode, and — decisively — a **second durability boundary**: a
job enqueued in a broker inside a database transaction is not enqueued
transactionally, and reconciling the two is the outbox pattern, which needs the
database anyway.

## Decision

PostgreSQL is the queue. `wreath.jobs` runs a job table with `SKIP LOCKED`
claiming; `wreath.messaging` uses `LISTEN`/`NOTIFY` for the doorbell with a
durable subscriber-group table; `wreath.store` is one keyed-table primitive
behind rate limiting, idempotency and sessions. No broker, no Redis.

The supervisor (`src/wreath/services.py`) starts after databases and drains
in-flight work on shutdown.

## Consequences

- One operational dependency. `docker run postgres` is the whole infrastructure.
- Enqueue is transactional with the write that caused it, which is what makes
  the exactly-once recipe composable rather than aspirational.
- Throughput is bounded by PostgreSQL, which is far below a dedicated broker.
  For the workload this targets — application background work, not event
  streaming — that ceiling is not reached.
- `LISTEN`/`NOTIFY` has a trap this project hit: `Connection.notifications()`
  *returns* when the connection closes rather than raising, so a supervisor
  written around `except` sees nothing at all. A dropped doorbell ended
  cross-worker fan-out for the process lifetime, silently, in both `MessageBus`
  and `JobRunner`. The reconnect and its counter exist because of that, and it
  is one of the incidents behind ADR 0018.
- Durable subscriber groups are discovered fleet-wide from a table rather than
  from local registration, so a consumer in another service is not silently
  skipped. `publish(..., require_group=True)` raises rather than no-opping.

## Alternatives rejected

- **Redis for the fast paths.** Rejected: it splits durability, and the
  in-process bounded stores cover the single-worker case without a network hop.
- **A broker for durable jobs.** Rejected: the outbox needed to make it
  transactional puts the state back in the database, so the broker becomes a
  relay rather than the source of truth.
- **An abstract queue interface with pluggable backends.** Rejected: the
  guarantees differ per backend, so the interface would either promise the
  weakest or lie. `wreath.store` is a shared *primitive*, not a portability
  layer.

## What would reverse this

A measured workload where PostgreSQL is the throughput bottleneck and the
application does not otherwise need transactional enqueue. That is a real
scenario — it is just not the one this framework is for, and it should be met by
documenting the ceiling rather than by weakening the guarantee.
