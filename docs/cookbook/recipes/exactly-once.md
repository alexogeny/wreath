# Write it exactly once, end to end

A checkout does three things: it writes the order, it queues a receipt, and it
tells billing. Every one of those hops is *at-least-once* — the client retries,
the job runner redelivers on a lease expiry, the bus redelivers on a nack.
Composed naively that is three chances to charge someone twice.

Wreath owns all three, so they can be composed into one guarantee instead of
three hopeful ones. This recipe is that composition, and the reasoning that
makes it hold.

## The whole thing

```python
from wreath.middleware import IdempotencyMiddleware, PostgresIdempotencyStore

db = app.postgres("app", dsn=...)
jobs = app.jobs("work", database="app")
bus = app.messaging("events", database="app")

app.add_global_middleware(
    IdempotencyMiddleware(store=PostgresIdempotencyStore(db))
)

@app.post("/orders")
async def place_order(request, cart: Cart):
    key = request.header("idempotency-key")
    conn = await db.acquire("write")
    try:
        async with conn.transaction() as tx:
            order = await write_order(tx, cart, idempotency_key=key)
            await jobs.enqueue("send_receipt", order.id, tx=tx, key=key)
            await bus.publish("order_placed", {"id": order.id}, tx=tx, durable=True)
    finally:
        await db.release("write", conn)
    return {"id": order.id}, 201
```

Four lines of composition. What each one is actually doing:

## 1. The client's retry never reaches the handler

`IdempotencyMiddleware` claims the key before the handler runs and replays the
stored response for any repeat. The claim is atomic, so two simultaneous
retries do not both proceed — the second gets `409` while the first is in
flight.

**Use a shared store.** The default is in-process, which covers a retry that
lands on the worker that served the original and nothing else. Behind a load
balancer that is most retries, and "most" is not a guarantee.
`PostgresIdempotencyStore` puts the claim in a table every worker can see, in
one `INSERT … ON CONFLICT … RETURNING` — a read followed by a write would let
two workers both conclude they were first.

A `5xx` releases the key, so a transient failure stays retryable rather than
being replayed forever.

## 2. The database is what makes it true

This is the part worth internalising: **the middleware saves work; the unique
index provides the guarantee.** Suppose the store is unavailable, or the key
expired, or you are running the in-process default and the retry hit another
worker. The handler runs a second time — and nothing duplicates anyway,
because the key went into the `INSERT`:

* `jobs.enqueue(..., key=key)` hits a unique index on `(queue, dedup_key)` and
  the second insert is dropped.
* the order row should carry the same key in a unique column, so the second
  write conflicts instead of creating a second order.

Design it so that losing the idempotency store costs you *work*, never
*correctness*. If removing the middleware would let a duplicate through, the
key is not in the right place yet.

`jobs.launch` is the friendly form of this: a deduplicated launch looks up the
surviving row and hands back the **same** task id, so a retry gets the same
answer rather than nothing.

```python
handle = await jobs.launch("send_receipt", order.id, key=key)   # same id, twice
```

The lookup can lose a race: if the first job completed and was purged between the
conflict and the read, there is no surviving row to point at. `launch` raises
`JobVanished` rather than inventing a task id, because a `TaskHandle` *is* an id
and an unpollable one would 404 on status and hang on an SSE stream. Nothing
holds the key any more, so the recovery is to launch again.

## 3. All three commit together, or none do

Passing `tx=` to both `enqueue` and `publish` makes them part of the same
transaction as the order row. That is the transactional outbox, and it removes
the two failure modes people actually hit in production:

* an order with **no** receipt job, because the process died after the commit
  and before the enqueue;
* a receipt job for an order that **never existed**, because the enqueue
  succeeded and the transaction rolled back.

Neither is reachable here. There is one commit, and it covers all three rows.

## 4. The job runs at least once, so guard the side effect

Exactly-once *enqueue* is not exactly-once *execution*. A worker whose lease
expires mid-handler gets its job redelivered — that is what makes the queue
reliable, and it means the handler must be safe to run twice.

`ctx.key` is the same string the client sent, so the guard is the same
identity all the way down:

```python
@jobs.task("send_receipt")
async def send_receipt(ctx, order_id):
    conn = await db.acquire("write")
    try:
        async with conn.transaction() as tx:
            sent = await tx.fetchval(
                "INSERT INTO receipts (order_id, key) VALUES ($1, $2) "
                "ON CONFLICT (key) DO NOTHING RETURNING id",
                order_id, ctx.key,
            )
            if sent is None:
                return                   # a previous attempt already sent it
            await email(order_id)
    finally:
        await db.release("write", conn)
```

The same rule applies to a durable bus subscriber: it is delivered at least
once, and `message.payload` should carry whatever it needs to recognise a
repeat.

## What you are relying on

Stated plainly, so it can be checked:

| Hop | Mechanism | If it fails |
| --- | --- | --- |
| client retry | shared idempotency claim | handler re-runs; unique indexes absorb it |
| order write | unique column on the key | the second write conflicts |
| job enqueue | unique index on `(queue, dedup_key)` | the second insert is dropped |
| job execution | your guard on `ctx.key` | the side effect repeats — this one is yours |
| bus publish | same transaction as the write | nothing published for a rolled-back order |
| bus delivery | subscriber's own guard | the handler repeats — also yours |

The two rows the framework cannot cover are the ones where the side effect
leaves the database — sending an email, charging a card. Everything that stays
in Postgres is covered by construction.

## Proving it

`tests/test_exactly_once.py` pins each hop, including the one that is easy to
assume away: a retry arriving at a worker with no memory of the key still
enqueues once, and still returns the same response.
