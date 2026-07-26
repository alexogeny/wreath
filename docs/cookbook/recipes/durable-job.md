# Enqueue a durable background job with retries

Some work outlives the request that asked for it — send the receipt, rebuild the
index, deliver a webhook. Wreath runs that durably on the Postgres you already
have, with no broker to operate: the database *is* the queue. Configure a runner
on an existing `app.postgres()` database, register a task, and enqueue it:

```python
db = app.postgres("main", dsn=DSN)
jobs = app.jobs("work", database="main", concurrency=16)

@jobs.task("send_receipt", retries=5, backoff="exp")
async def send_receipt(ctx, order_id: str) -> None:
    await mailer.send(order_id)          # retried with backoff; dead-lettered when exhausted

@app.post("/orders")
async def create(request):
    order = await place_order(await request.json())
    await jobs.enqueue("send_receipt", order.id)
    return {"id": order.id}
```

The handler's first argument is a `JobContext`; the rest are the arguments you
enqueued. The enqueued job is a durable row, so a redeploy drains in-flight work
and picks the rest up on restart rather than dropping it.

Retry behaviour is tunable per task:

```python
@jobs.task(
    "reindex",
    retries=8,
    backoff="exp",          # "exp" | "linear" | "fixed"
    backoff_base=1.0,       # seconds
    backoff_cap=3600.0,     # ceiling
    backoff_jitter=0.2,     # ±20% spread, so retries don't thundering-herd
)
async def reindex(ctx, doc_id: str) -> None:
    ...
```

When a task raises it is retried with backoff; once `retries` is exhausted it is
dead-lettered rather than lost or retried forever. Delivery is at-least-once, so
pass a `key` for idempotency — and enqueue on the same transaction as the
business row (`jobs.enqueue(..., tx=tx, key=f"receipt:{order.id}")`) when you
need the job to commit atomically with the work that spawned it.
