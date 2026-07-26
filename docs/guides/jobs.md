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

## Running the workers

The same process serves requests and runs the workers; a `Supervisor` owns the tasks — started after the databases in lifespan, and **drained cleanly on shutdown** (stop fetching, finish in-flight, release leases) so a redeploy never abandons work mid-flight.

Multi-tenant deployments keep the queue in a dedicated **system schema with a tenant column** — never relying on `search_path`, because `NOTIFY` channel names are database-global and would otherwise wake the wrong tenant.

This is a durable queue, not a distributed message bus: it replaces the common 80% — background work and fan-out notifications — and is honest about the rest. See also [distributed locks](distributed-locks.md) for the `run_singleton` primitive underneath fleet-wide leaders.
