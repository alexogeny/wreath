# Cache an expensive endpoint response

Some endpoints do real work — a rollup query, a fan-out to upstreams — and return
the same answer to everyone for a while. `@cached` keeps that answer in a small,
bounded in-process store and serves the repeats for free. No Redis, no adapter,
no background worker:

```python
from wreath.response_cache import cached

@app.get("/reports/summary")
@cached(ttl=30)
async def summary(request):
    return await expensive_rollup()      # runs at most once per 30s per key
```

The default key is `method + path + query`, so `?range=7d` and `?range=30d`
cache separately. Past `max_entries` (default 1024) the least-recently-used entry
is evicted, so there is no unbounded growth. When a write makes the answer stale,
drop it — the decorated handler carries the controls:

```python
@app.post("/reports")
async def create(request):
    await save(...)
    summary.invalidate()                 # clear all; or .invalidate(request) for one key
    return {"created": True}
```

## Safe by default

`@cached` refuses to cache anything that would leak one caller's data to another.
It caches only the methods you allow (`GET` by default), only success responses
(status < 400), **never** a response that sets a cookie, and **never** one marked
`Cache-Control: no-store` or `private`. Streaming, file, and SSE responses pass
straight through uncached — their bodies aren't materialized.

The default key has no notion of *who* is asking, so it is a shared, public
cache. If a response depends on the caller, either don't cache it, or pass a key
that includes the principal:

```python
@cached(ttl=10, key=lambda r: f"{r.identity.id}:{r.path}")
async def my_widgets(request):
    ...
```
