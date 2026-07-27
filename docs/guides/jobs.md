# Durable jobs, messaging, and services

Some work outlives the request that asked for it — send the receipt, rebuild the index, fan out the webhook. Wreath runs that work durably on the database you already have, with no broker to operate: **PostgreSQL is the queue.**

## User story: fire-and-forget work that survives a redeploy

> *As an API author, my `POST /webhooks/test` kicks off a slow delivery to a
> customer endpoint. I want to return immediately, have the delivery retried on
> failure, and — crucially — not lose it if the box redeploys mid-flight. No
> broker to run.*

```python
jobs = app.jobs("work", database="main", concurrency=16)

@jobs.task("deliver_webhook", retries=8, backoff="exp")
async def deliver_webhook(ctx, endpoint: str, payload: dict) -> None:
    await http.post(endpoint, json=payload)     # retried with backoff; dead-lettered when exhausted

@app.post("/webhooks/test")
async def test_webhook(request):
    body = await request.json()
    await jobs.enqueue("deliver_webhook", body["url"], body["payload"])
    return {"queued": True}
```

The enqueued job is a durable row, so a redeploy drains in-flight work and picks
the rest up on restart rather than dropping it. The handler's first argument is a
`JobContext`; the rest are the arguments you enqueued. The next sections cover
retry tuning, transactional enqueue, and scheduling.

### Retention

Finished rows are kept until you delete them — nothing sweeps in the background,
for the same reason nothing purges `wreath.store`: a background sweep duplicates
across workers and swallows its own failures. Run it from a scheduled job:

```python
@runner.task("purge_jobs")
async def purge_jobs(ctx):
    await runner.purge(older_than=14 * 86_400)     # done + dead rows

@bus_runner.task("purge_messages")
async def purge_messages(ctx):
    await bus.purge(older_than=14 * 86_400)
    await bus.prune_groups(unseen_for=30 * 86_400)  # consumers long gone
```

A bus deregisters its own durable groups on drain, so an orderly shutdown leaves
no registration behind; `prune_groups` is the backstop for a consumer that was
killed rather than drained. A group that stays registered keeps every publisher
enqueueing one copy per message into a queue nobody reads.


## A durable job runner

Configure a runner on an existing `app.postgres()` database. Its workers, lease sweeper, and cron scheduler run for the process lifetime, started during lifespan:

```python
db = app.postgres("main", dsn=DSN)
jobs = app.jobs("work", database="main", concurrency=16, lease=30.0)

@jobs.task("send_receipt", retries=5, backoff="exp")
async def send_receipt(ctx, order_id: str) -> None:
    await mailer.send(order_id)
```

The handler's first argument is a `JobContext`; the rest are the arguments you enqueued. Retry behaviour is tunable per task:

```python
@jobs.task(
    "reindex",
    retries=8,
    backoff="exp",          # "exp" | "linear" | "fixed"
    backoff_base=1.0,       # seconds
    backoff_factor=2.0,     # exp/linear growth
    backoff_cap=3600.0,     # ceiling
    backoff_jitter=0.2,     # ±20% spread, so retries don't thundering-herd
)
async def reindex(ctx, doc_id: str) -> None:
    ...
```

When a task raises, it is retried with backoff; once `retries` is exhausted it is **dead-lettered** (moved to a terminal state you can inspect) rather than lost or retried forever.

## Transactional enqueue

Enqueue from a handler on the *same transaction* as the business row, so the job commits atomically with the work that spawned it. Pass a `key` for idempotency:

```python
@app.post("/orders")
async def create(request):
    async with db.pool("write").acquire() as conn, conn.transaction() as tx:
        order = await place_order(tx, await request.json())
        await jobs.enqueue("send_receipt", order.id, tx=tx, key=f"receipt:{order.id}")
    return {"id": order.id}
```

Claiming uses `SELECT … FOR UPDATE SKIP LOCKED` with a per-row **fence token**: a stale worker whose lease expired can never complete a job that was reclaimed. Delivery is **at-least-once**, so the idempotency `key` is your defence against duplicate side effects — and it's on by default, not an afterthought. Enqueue without a `tx` and it runs on its own write connection.

## Work the caller wants to watch

A job the client is waiting on needs an id to watch and a percentage to read.
Give the runner a [progress registry](progress.md) and both come with it:

```python
jobs = app.jobs("work", database="app", progress=ProgressRegistry(bus))

handle = await jobs.launch("import_herd", path)   # {"task_id": "8821", ...}
```

The task id *is* the job id, `ctx.report(...)` inside the handler updates it,
and the runner sets `done`/`failed` itself — a retry stays `running`, because a
retry is not an ending. See [Reporting task progress](progress.md#user-story-the-mutation-that-takes-ninety-seconds).

`launch(..., key=...)` that deduplicates hands back the *surviving* row's
handle, so submitting the same work twice yields the same task to watch. In the
narrow race where that row is purged between the conflict and the lookup there
is genuinely nothing to watch, and `launch` raises `wreath.jobs.JobVanished`
rather than returning a task id that would 404 on the status endpoint and stream
forever on the SSE one. Re-launch — nothing is holding the key any more. When
the surviving row *is* found and this worker has no progress entry for it — the
original ran elsewhere, and progress fan-out is at-most-once with no replay —
the task is seeded as `running` so the handle is watchable immediately, without
overwriting a real percentage this worker already knows about.

### When the runner's doorbell drops

`NOTIFY` is a latency doorbell, never a correctness dependency: workers poll as
well, so losing it costs latency rather than jobs. That is exactly why it is
worth supervising — nothing breaks, so nothing tells you. The held `LISTEN`
connection is reconnected with jittered backoff (50 ms up to 5 s, the default
`poll_interval`), and a runner whose database is down at startup still starts,
still claims work by polling, and picks the doorbell up when the database comes
back.

Two counters make the quiet states countable:

- **`runner.doorbell_reconnects`** — connections lost, plus every failed attempt
  to open one (including at startup). Climbing means jobs are being claimed at
  poll latency rather than on notification.
- **`runner.pass_drive_errors`** — chunked passes that could not be given their
  first shift. A pass that is never driven does nothing at all, and its ledger
  row looks the same as one with no work to do.

## Scheduled work

```python
jobs.schedule("nightly_rollup", cron="0 3 * * *")
```

Every instance runs the scheduler, but each minute's enqueue carries a deterministic key, so the unique index elects exactly one winner — **cron without leader election** or a separate beat process.

## Publish / subscribe

`app.messaging()` gives two tiers over the same database:

- **Ephemeral fan-out** — a `NOTIFY` to every live subscriber, at-most-once, sub-millisecond.
- **Durable subscriptions** — a work queue with the same claim/fence machinery, at-least-once, replayable — with `NOTIFY` used purely as a wake-up doorbell so consumers never poll a hot loop.

```python
bus = app.messaging("events", database="main")

await bus.publish("order.created", {"id": order_id})          # fan-out
await bus.publish("order.created", payload, tx=tx, durable=True)  # transactional + durable

@bus.subscribe("order.created", group="fulfilment", concurrency=8)   # durable consumer
async def on_order(msg):
    await start_fulfilment(msg.payload)     # payload is already decoded
    msg.ack()                               # ack | nack (retry) | reject (dead-letter)
```

A delivered `Message` carries `channel`, `group`, `tenant`, and the decoded `payload`. For durable messages, `ack()` completes it, `nack()` retries it with backoff, and `reject()` dead-letters it immediately.

### When the doorbell's connection drops

Both tiers lean on one `LISTEN` connection held for the life of the process — it carries ephemeral fan-out, and it is what saves durable consumers from polling. Held connections do not last forever: a failover, an idle timeout, a `pg_terminate_backend`, a network blip, and it is gone. **The bus reconnects and re-`LISTEN`s on every channel by itself**, with exponential backoff from 50 ms to 5 s, jittered so a fleet that lost the same database does not come back at it in lockstep. A connection that is accepted and then dies immediately does not reset that backoff — flapping is not recovery.

That is a repair, not a save: ephemeral fan-out is at-most-once, so whatever was published during the gap is gone, and durable consumers fall back to `poll_interval` until the doorbell returns. So the outage is countable rather than silent:

- **`bus.doorbell_reconnects`** — connections lost, plus failed attempts to open one (including at startup, since a bus that came up against a dead database has no doorbell either). Zero is healthy; climbing means it is down *now* and durable delivery is running on the poll interval.
- **`bus.handler_errors`** — exceptions raised by ephemeral subscriber callbacks, counted separately and deliberately. Fire-and-forget delivery has nowhere else to put them (a durable handler's failure lands in the row's `last_error` and its retry state), and a bug in a handler must never read as a flapping database.

A bus whose database is down at startup still starts, still consumes its own durable work, and picks up the doorbell when the database comes back.

### Consumers in another service

A durable publish enqueues one copy per subscriber **group**, and the groups are discovered fleet-wide: every bus writes its durable subscriptions into a shared `message_groups` table at startup, and every publisher reads it. So the consumer does not have to live in — or even be known to — the process doing the publishing.

That matters because the alternative fails quietly. Discovering groups only from local registrations means a producer shipped before its consumer, or a producer in a different service, enqueues **nothing** for that group: no error, no dead letter, just a queue that never fills. A team finds that three days later.

```python
# service A — publishes, subscribes to nothing
await bus.publish("order_placed", payload, durable=True)

# service B — deployed later, against the same database
@bus.subscribe("order_placed", group="analytics", durable=True)
async def on_order(msg): ...
```

Service B's group is registered when its bus starts, and service A picks it up within `group_refresh` seconds (30 by default). Discovery is a timer, not a query on the publish path — a round trip in front of every durable publish would be a poor trade for a fact that only changes at deploy time.

Two properties worth knowing:

- **Local registrations are unioned with the persisted ones**, never replaced. A duplicate copy goes to a group that demonstrably has a consumer, and durable delivery is at-least-once anyway, so handlers already tolerate one; a *missing* copy is silent. It also means this works before you apply the new table — with no registry, a bus behaves exactly as it did when groups were local-only.
- **A publish that reaches nobody is counted**, not hidden: `bus.unrouted_publishes`. Publishing to a channel no consumer exists for yet is legitimate, so it stays a no-op — but pass `require_group=True` when you know a consumer must exist and want `NoSubscriberGroup` instead of silence.

`bus.known_groups("order_placed")` answers "will anything actually receive this?" before you ship, which is the check that used to require reading someone else's deployment.

## Running the workers

The same process serves requests and runs the workers; a `Supervisor` owns the tasks — started after the databases in lifespan, and **drained cleanly on shutdown** (stop fetching, finish in-flight, release leases) so a redeploy never abandons work mid-flight.

Multi-tenant deployments keep the queue in a dedicated **system schema with a tenant column** — never relying on `search_path`, because `NOTIFY` channel names are database-global and would otherwise wake the wrong tenant.

This is a durable queue, not a distributed message bus: it replaces the common 80% — background work and fan-out notifications — and is honest about the rest. See also [distributed locks](distributed-locks.md) for the `run_singleton` primitive underneath fleet-wide leaders.
