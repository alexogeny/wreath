# Make a POST safe to retry

Networks retry. A user double-clicks, a proxy resends, a mobile client reconnects
and replays its last request. For a `GET` that's harmless; for `POST /orders` it
charges the card twice. `IdempotencyMiddleware` makes the retry safe: when a
request carries an `Idempotency-Key`, the first response is remembered and
replayed for any repeat of that key — from a small, bounded, in-process store:

```python
from wreath.middleware import IdempotencyMiddleware

app.add_global_middleware(IdempotencyMiddleware(ttl=24 * 60 * 60))
```

The client generates one key per logical operation and sends it as a header:

```
POST /orders
Idempotency-Key: 4b5f...-once-per-checkout
```

The first request runs the handler and stores the response; a retry with the same
key returns the stored response verbatim plus `Idempotency-Replayed: true`, and
the handler never runs again. It's safe by construction: only unsafe methods
(`POST`, `PUT`, `PATCH`, `DELETE`) are guarded; the key is scoped by method, path,
*and* authenticated principal, so two users can't collide; an unauthenticated
request is skipped entirely rather than guarded, because there is no principal to
scope it by and a shared anonymous keyspace would let a client-chosen header
replay one caller's response to another; a concurrent duplicate
gets `409 Conflict` rather than racing; and a `5xx` is never stored, so a
transient failure stays retryable. Tune `ttl`, `max_entries` (LRU past the
ceiling), `methods`, and `header` for your write volume and retry window.
