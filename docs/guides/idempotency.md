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
- A **concurrent** duplicate (the first is still in flight) gets `409 Conflict`
  rather than racing to run twice.
- A `5xx` is **not** stored — a transient failure stays retryable, so the client's
  retry actually re-runs instead of replaying the error.

## Tuning

```python
IdempotencyMiddleware(
    ttl=60 * 60,                 # how long a key is honoured (default 24h)
    max_entries=4096,            # hard ceiling; LRU eviction past it
    methods=("POST", "PATCH"),   # which methods to guard
    header="idempotency-key",    # the request header to read
)
```

The store is bounded and evictable, so it can't grow without limit; the trade-off
is that a key older than `ttl` (or evicted under load) is forgotten and its retry
runs fresh. Size `max_entries`/`ttl` for your write volume and retry window.
