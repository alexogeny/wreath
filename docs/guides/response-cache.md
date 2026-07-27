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

## User story: letting the ORM invalidate for you

> *I keep forgetting to call `.invalidate()`, and a TTL is a guess. I want the
> cache to know when its data changed.*

Name the models the response depends on:

```python
@cached(ttl=300, invalidate_on=[Llama, Trek])
async def herd_report(request):
    ...
```

Now any committed write to `Llama` or `Trek` — from a handler, a CRUD route, a
GraphQL mutation, a background job, anywhere — clears this cache. You do not
call anything.

This works because Wreath owns both layers. The session announces the model
names it wrote; caches that named those models clear. A cache library bolted on
beside the framework cannot do this, because it cannot see your writes.

Three properties worth knowing:

* **It fires on commit, never before.** A write inside a transaction that then
  rolls back invalidates nothing — the data never changed, so nothing cached is
  stale.
* **Once per transaction**, not once per flush.
* **Model-grained, not row-grained.** Writing one llama drops the cached llama
  responses, not just the ones mentioning that llama. Row-grained invalidation
  would mean recording which rows fed which response — real work on every read
  to save a few misses on the rare write.

`invalidate_on` composes with `ttl`: the TTL becomes a backstop for staleness
Wreath cannot see (a row changed by another system, say), rather than the only
line of defence.

## User story: four workers, one truth

> *We run four workers behind a load balancer. Someone renames a llama, the
> POST lands on worker 2, and workers 1, 3 and 4 keep serving the old name until
> the TTL runs out. I want the write to clear all of them — and I do not want to
> stand up Redis to do it.*

One line, before startup:

```python
from wreath.cache import invalidate_across_workers

bus = app.messaging("bus", database="app")
invalidate_across_workers(bus)
```

Nothing else changes. `@cached(invalidate_on=[Llama])` now clears on *any*
worker's committed write to `Llama`, because the announcement the session
already makes is carried over the PostgreSQL message bus — the database you
already have, no new moving part.

```text
POST /llamas/7  ->  worker 2   COMMIT
                    worker 2   caches naming Llama clear          (immediately)
                    worker 2   NOTIFY wreath_writes {"models": ["Llama"]}
                       |
   PostgreSQL  --------+------> workers 1, 3, 4
                                caches naming Llama clear
```

The properties that make it safe to leave on:

* **The local clear happens first.** The worker that took the write is never the
  one waiting on a notification to stop being wrong.
* **A worker never relays what it received.** Announcements travel one hop, out
  from the writer, so adding workers cannot turn one write into a storm.
* **A bus that is down cannot fail a write.** The row is already committed;
  publishing is best-effort behind it.
* **Delivery is at-most-once**, as ephemeral fan-out is. A worker that missed
  the notification holds its entries until they expire — which is exactly what
  the `ttl` is for now: a backstop, not the mechanism.

`invalidate_across_workers` returns the bridge, whose `close()` stops it. Call it
once per process and before startup, since the bus collects its subscriptions
before it begins listening.

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

**One store is one invalidation domain.** That is the same sentence as "one
invalidation surface", read from the other side: invalidation clears the
*store*, not the handler's slice of it. So if the endpoints sharing a store name
different models, a write to any one of those models drops every entry in it:

```python
@cached(store=catalog_cache, invalidate_on=[Product])
async def products(request): ...

@cached(store=catalog_cache, invalidate_on=[Category])
async def categories(request): ...

# A Category write clears the cached /products responses too.
```

That is safe — you never serve stale data because of it — but it is wasted
recomputation, and it grows with the number of endpoints in the group. Share a
store across endpoints that watch the *same* models, or that are cheap enough
that recomputing them together does not matter. Give an expensive endpoint with
its own narrow `invalidate_on` its own store.

Streaming, file, and SSE responses are never cached — their bodies aren't
materialized — so they pass straight through.
