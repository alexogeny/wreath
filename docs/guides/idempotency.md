# Idempotent writes

Networks retry. A user double-clicks, a proxy resends, a mobile client reconnects
and replays the last request. For a `GET` that's harmless; for a `POST /orders`
it charges the card twice.

`IdempotencyMiddleware` makes a retry safe: when a request carries an
`Idempotency-Key`, the first response is remembered and replayed for any repeat
of that key — from a small, bounded, in-process store. No Redis, no external
state to run.

## User story: don't create the order twice

> *As an API author, my `POST /orders` creates a charge. If the client retries
> because the connection dropped, I need the retry to return the original result,
> not create a second order.*

```python
from wreath.middleware import IdempotencyMiddleware

app.add_global_middleware(IdempotencyMiddleware(ttl=24 * 60 * 60))
```

The client generates a key per logical operation and sends it:

```
POST /orders
Idempotency-Key: 4b5f...-once-per-checkout
```

- **First request** → runs the handler, stores the response.
- **Retry (same key)** → returns the stored response verbatim, plus
  `Idempotency-Replayed: true`. The handler never runs again.

## Safe by construction

- Only unsafe methods are considered — `POST`, `PUT`, `PATCH`, `DELETE`.
- The key is scoped by **method, path, and the authenticated principal**, so two
  users cannot collide on the same key value.
- An **unauthenticated** request is left unguarded — see below.
- A **concurrent** duplicate (the first is still in flight) gets `409 Conflict`
  rather than racing to run twice.
- A `5xx` is **not** stored — a transient failure stays retryable, so the client's
  retry actually re-runs instead of replaying the error.

## Idempotency requires an authenticated principal

A key is only safe because of what it is scoped by, and an anonymous request has
no principal to scope it by. Were the middleware to treat "no principal" as a
principal, every anonymous caller of one endpoint would share a single keyspace
— and the key is a header the *client* chooses. On `POST /signup`, a password
reset, or anything else that answers an unauthenticated caller with a new id, a
token, or an email address, guessing or reusing one key value would be enough to
be handed back a stranger's response. That is cross-user disclosure, driven by
an attacker-supplied header.

So the middleware refuses rather than shares: **an unauthenticated request is not
idempotency-guarded at all.** Its `Idempotency-Key` is ignored, nothing is
reserved, nothing is stored, and the handler runs exactly as it would if the
middleware were not installed. The cost is that an anonymous retry re-runs the
handler — which is the behaviour you had before adding the middleware, and a far
smaller price than replaying one caller's response to another.

There is deliberately no fallback to the client address. A shared proxy, a
corporate NAT, or a mobile carrier gateway would put unrelated callers back in
one bucket, which is the same defect with a longer explanation.

If you need retries to be safe on an unauthenticated endpoint, put the guarantee
where it actually belongs: a unique index on something durable — a unique column
on the row you write, or the `key` you pass to `jobs.enqueue`. That is the
advice in [what this does and does not guarantee](#what-this-does-and-does-not-guarantee)
regardless of who is calling; for anonymous endpoints it is the whole of it.

## User story: the retry lands on a different worker

> *We run four workers. The client's retry does not necessarily come back to
> the one that served the original — and that worker is the only one that
> remembers the key.*

The default store is in-process, so that retry re-runs the handler. Give the
middleware a shared store and every worker honours every key:

```python
from wreath.middleware import IdempotencyMiddleware, PostgresIdempotencyStore

store = PostgresIdempotencyStore(app.postgres("app", dsn=...))
app.add_global_middleware(IdempotencyMiddleware(store=store))
```

One table, no Redis. Apply `store.schema_sql()` as a migration and call
`store.purge()` on a schedule to drop expired rows.

The claim is a **single** `INSERT … ON CONFLICT (key) DO UPDATE … RETURNING`.
That matters: a read followed by a write would let two workers both conclude
they were first, which is exactly the race the middleware exists to close. A
row comes back only when the insert succeeded or an expired row was reclaimed,
so "a row came back" *is* the claim — no owner column and no second round trip.
Postgres supplies the clock, so workers with drifting wall clocks cannot
disagree about when a key expires.

## What this does and does not guarantee

`IdempotencyMiddleware` is **response replay**. It saves the work and keeps the
answer consistent. What makes the *effect* happen once is a unique index on
something durable — the `key` you pass to `jobs.enqueue`, or a unique column on
the row you write.

Build it so that losing the idempotency store costs you work, never
correctness. If removing this middleware would let a duplicate order through,
the key is not yet in the right place. The
[exactly-once recipe](../cookbook/recipes/exactly-once.md) works that through
hop by hop, from the client's retry to the message bus.

## Tuning

```python
IdempotencyMiddleware(
    ttl=60 * 60,                 # how long a key is honoured (default 24h)
    max_entries=4096,            # hard ceiling; LRU eviction past it
    methods=("POST", "PATCH"),   # which methods to guard
    header="idempotency-key",    # the request header to read
    store=None,                  # shared store; in-process when omitted
)
```

The default store is bounded and evictable, so it can't grow without limit; the
trade-off is that a key older than `ttl` (or evicted under load) is forgotten
and its retry runs fresh. Size `max_entries`/`ttl` for your write volume and
retry window — or move the store to Postgres, where neither ceiling applies.

`ttl` is measured **from the first attempt**, not from whenever the handler
finished, in both stores. A slow request therefore cannot extend its own key,
and a key is honoured for the same length of time whichever store you configure.
