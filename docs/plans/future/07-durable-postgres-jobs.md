# Durable PostgreSQL jobs plan

## Status

Future proposal; depends on supervised services and observability.

## Objective

Provide persisted one-shot, delayed, recurring, and retryable work coordinated across application replicas using Neo's PostgreSQL stack. The contract is at-least-once execution with leases and fencing, never exactly-once execution.

## Public model

Applications create a named job service for a configured database workload, register task names at startup, and enqueue versioned payloads transactionally:

```python
jobs = app.jobs(
    "control",
    database=database,
    workload="jobs",
    lease_duration=30.0,
    concurrency=16,
)

@jobs.task("reconcile", payload_version=1)
async def reconcile(context: JobContext, payload: ReconcilePayload) -> None: ...
```

Enqueue supports run time, idempotency key, priority, partition/ordering key, retry policy, and optional existing transaction/session.

## Storage contract

The schema records job ID, task name, payload bytes and version, state, priority, run time, attempts, retry policy, lease owner/expiry, monotonically increasing fencing token, idempotency key, partition key, timestamps, result summary, and structured last error. Tables and indexes are returned through `schema_sql()` and are never applied automatically.

Workers claim due rows with a short transaction using `FOR UPDATE SKIP LOCKED`, update owner/lease/fence in the same transaction, then execute outside the claim transaction. Completion and heartbeat updates require the current fencing token. A stale worker cannot complete work after lease loss.

## Scheduling semantics

- One-shot jobs become due at their persisted `run_at`.
- Recurring schedules persist the next occurrence.
- Misfire policy is skip, run once, or bounded catch-up.
- Overlap policy is allow, forbid globally, or forbid per partition.
- Retry delay and attempt limits are persisted.
- Terminal failures enter queryable dead-letter state.
- Administrative retry/cancel operations are authorized and audited.
- Payload evolution uses explicit version decoders; importing old application code is not required.

## Runtime architecture

A supervised claimer maintains a bounded number of leased jobs. Execution slots are bounded independently from claim batch size. Heartbeats, lease recovery, cleanup, and schedule materialization are named services with visible health. Database time is authoritative for cross-process leases; local monotonic time controls only process-local waits.

## C and pure split

Python owns task invocation, transactions, retry policy, payload adaptation, and cancellation. Existing native PostgreSQL code accelerates wire and decode work. Optional native primitives may later provide a local due-time heap, compact state validation, and batched payload framing. PostgreSQL remains the durable source of truth and all logic works through the pure driver.

## Phases

1. State machine, schema, lease, fence, and cancellation specification.
2. Pure coordinator on the public PostgreSQL API.
3. Supervision, readiness, metrics, and deterministic-clock tests.
4. Recurrence, misfires, overlap, dead letters, and administration.
5. Payload versioning and schema migration guide.
6. Contention, failover, soak, and optional native optimization.

## Verification

Cover process death after claim and before/after side effects, lease expiry, stale completion, database disconnect, cancellation, clock differences, duplicate enqueue, retry exhaustion, recurring misfires, partition overlap, many competing workers, and subsequent pool reuse.

## Completion criteria

Restarting any worker loses no committed job, duplicate execution is documented and manageable, stale workers are fenced, all queues and claims are bounded, and job lag/failure/lease health is observable.
