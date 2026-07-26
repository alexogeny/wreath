# Caching responses

Some endpoints do real work — a rollup query, a fan-out to upstreams — and return
the same answer to everyone for a while. `@cached` keeps that answer in a small
in-process store and serves the repeats for free.

No Redis, no adapter, no background worker: just a bounded, evictable cache you
can size, time-limit, inspect, and clear with certainty.

## User story: an expensive dashboard endpoint

> *As an API author, I have a `/reports/summary` endpoint that aggregates a slow
> query. It's the same for every caller for ~30 seconds, and it's getting hammered.
> I want to compute it at most once per 30 seconds.*

```python
from wreath.response_cache import cached

@app.get("/reports/summary")
@cached(ttl=30)
async def summary(request):
    return await expensive_rollup()      # runs at most once per 30s per key
```

The default key is `method + path + query`, so `?range=7d` and `?range=30d` cache
separately. Past `max_entries` (default 1024) the least-recently-used entry is
evicted; there is no unbounded growth.

## User story: invalidating on write

> *When someone POSTs a new record, the cached summary is stale. I want to drop it.*

```python
@app.post("/reports")
async def create(request):
    await save(...)
    summary.invalidate()                 # clear all; or .invalidate(request) for one key
    return {"created": True}
```

The decorated handler exposes `.invalidate(request=None)` and `.cache_store` (a
[`BoundedCache`](../reference/cache.md)) so you can read `summary.cache_store.stats`
— hits, misses, evictions, hit rate.

## Safe by default

`@cached` refuses to cache anything that would leak one caller's data to another:

- only the methods you allow (`GET` by default),
- only success responses (status < 400),
- **never** a response that sets a cookie,
- **never** one marked `Cache-Control: no-store` or `private`.

The default key has no notion of *who* is asking, so it is a **shared/public**
cache. If a response depends on the caller, either don't cache it, or pass a key
that includes the principal:

```python
@cached(ttl=10, key=lambda r: f"{r.identity.id}:{r.path}")
async def my_widgets(request):
    ...
```

## Sharing one store across endpoints

Pass an explicit store to give a group of endpoints one budget and one
invalidation surface:

```python
from wreath.cache import BoundedCache

catalog_cache = BoundedCache(max_entries=500, ttl=60)

@app.get("/products")
@cached(store=catalog_cache)
async def products(request): ...

@app.get("/categories")
@cached(store=catalog_cache)
async def categories(request): ...
```

Streaming, file, and SSE responses are never cached — their bodies aren't
materialized — so they pass straight through.
