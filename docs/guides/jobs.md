# Durable jobs, messaging, and services

Some work outlives the request that asked for it — send the receipt, rebuild the index, fan out the webhook. Wreath runs that work durably on the database you already have, with no broker to operate: PostgreSQL is the queue.

## A durable job runner

Configure a runner on an existing `app.postgres()` database. Its workers, lease sweeper, and cron scheduler run for the process lifetime, started during lifespan:

```python
db = app.postgres("main", dsn=DSN)
jobs = app.jobs("work", database="main", concurrency=16, lease=30.0)

@jobs.task("send_receipt", retries=5, backoff="exp")
async def send_receipt(ctx, order_id: str) -> None:
    await mailer.send(order_id)
```

Enqueue from a handler — **transactionally**, so the job commits atomically with the business row it belongs to. Pass a `key` for idempotency:

```python
@app.post("/orders")
async def create(request):
    async with db.pool("write").acquire() as conn, conn.transaction() as tx:
        order = await place_order(tx, await request.json())
        await jobs.enqueue("send_receipt", order.id, tx=tx, key=f"receipt:{order.id}")
    return {"id": order.id}
```

Claiming uses `SELECT … FOR UPDATE SKIP LOCKED` with a per-row **fence token**, so a stale worker can never complete a job that was reclaimed after its lease expired. Delivery is **at-least-once**; the idempotency `key` is your defence against duplicate side effects, and it ships on by default rather than as an afterthought.

## Scheduled work

```python
jobs.schedule("nightly_rollup", cron="0 3 * * *")
```

Every instance runs the scheduler, but each minute's enqueue carries a deterministic key, so the unique index elects exactly one winner — cron without leader election.

## Publish / subscribe

`app.messaging()` gives ephemeral fan-out (a `NOTIFY` to every live subscriber) and durable subscriptions (a work queue with the same claim machinery), with `NOTIFY` used purely as a wake-up doorbell so consumers don't poll:

```python
bus = app.messaging("events", database="main")

await bus.publish("order.created", {"id": order_id})          # fan-out

@bus.subscribe("order.created", group="fulfilment")           # durable
async def on_order(msg):
    await start_fulfilment(msg.json())
```

## Running the workers

The same process serves requests and runs the workers; a `Supervisor` owns the tasks and drains them cleanly on shutdown. Multi-tenant deployments keep the queue in a dedicated system schema with a tenant column — never relying on `search_path`, since `NOTIFY` names are database-global.

This is a durable queue, not a distributed message bus: it replaces the common 80% (background work, fan-out notifications), and is honest about the rest.
