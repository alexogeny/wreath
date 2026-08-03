# Cache tags and edge purge

[Response caching](response-cache.md) is exact because Wreath owns both halves of
the problem: the ORM knows what committed, and the cache knows what it holds. Put
a CDN in front of the application and it is the same problem displaced by one
hop — except that now the two halves are in different companies.

The usual arrangement is entirely manual. Somebody tags responses with surrogate
keys. Somebody else, in a different file and often in a different quarter,
remembers to purge those keys when the data changes. Every hand-rolled cache-tag
setup fails at the same point, and it is always the second half: a purge that
nobody added, for a page nobody thought about, discovered during an incident.

`Tags` and `CDNPurge` close that loop from one declaration.

## The declaration you already wrote

A cached handler already names the models its response depends on:

```python
from wreath.response_cache import Tags, cached

tags = Tags(secret=SURROGATE_SECRET)

@app.get("/reports/summary")
@cached(ttl=30, invalidate_on=[Report, Invoice], tags=tags)
async def summary(request):
    return Response(await render_rollup())
```

`invalidate_on` is what the response *read*. Adding `tags=` turns the same list
into surrogate keys on the way out:

```
Cache-Tag: 3f2a91c4d8e05b17 8b1e7f04a2c93d66
Surrogate-Key: 3f2a91c4d8e05b17 8b1e7f04a2c93d66
Edge-Cache-Tag: 3f2a91c4d8e05b17 8b1e7f04a2c93d66
```

All three headers, because the CDNs disagree about the name and each ignores the
others. Fastly reads `Surrogate-Key`, Cloudflare reads `Cache-Tag`, Akamai reads
`Edge-Cache-Tag`. Emitting all three costs a few dozen bytes and removes a
deployment-time decision from your application code.

## The purge, from the other side of the same signal

```python
from wreath.response_cache import CDNPurge

purge = CDNPurge(app.jobs, tags=tags)
purge.watch(Report, Invoice)
```

`invalidate_on` was what the response read; the ORM's commit signal is what the
transaction *wrote*. Deriving the tag from the first and the purge from the
second makes correct edge invalidation a property of the schema rather than of
anybody's memory.

Then a task that talks to your CDN:

```python
@app.jobs.task("wreath_cdn_purge")
async def purge_tag(job, tag: str):
    await http.post(f"{CDN_API}/purge", json={"surrogate_keys": [tag]})
```

## Why the purge is a job

`CDNPurge` never calls your CDN inline. The ORM announces a write once the
transaction has committed, and a slow CDN API reached from that callback would
turn a slow edge into a slow write — the pathology where your checkout page gets
slower because someone else's cache is having a bad afternoon.

Instead the call is handed to [`wreath.jobs`](jobs.md), which is durable, retried
and observable. A purge that fails at three in the morning is a job somebody can
see and replay, rather than a signal that evaporated.

The job is enqueued with a **deduplication key** derived from the tag, so a
thousand rows written in a loop enqueue one purge per tag rather than a thousand.
That is the difference between an invalidation and a denial-of-service attack on
your own CDN.

## Why the keys are hashed

A surrogate key travels in a response header, which means anyone who can read one
learns whatever it spells out. An unhashed key named `Report` tells a reader your
table names; a key with row identity in it tells them which rows a page was built
from, and therefore that a given row exists.

Worse, a key anyone can *compute* is a key anyone can *purge*. If your tag is
`sha256("Report")`, emptying your edge cache requires knowing one word.

So `Tags` takes a secret and requires it:

```python
tags = Tags(secret=SURROGATE_SECRET)
```

There is no default and no derived fallback, because a defaulted security
parameter gets defaulted. The secret must be the same in every worker; rotate it
the way you would a cache version — deliberately, with a full purge behind it,
because a rotation renames every tag and orphans the old ones until they expire.

If several applications share one CDN account, give each a `prefix` so one
cannot purge the other's responses.

## Counting what is lost

The failure mode here is silence. A purge that never happened leaves the edge
serving a page the database no longer agrees with, and nothing in your
application looks wrong. So every dropped purge is counted:

```python
purge.enqueued()   # purges handed to the job queue
purge.dropped()    # purges that could not be, and were therefore lost
```

A `dropped()` that is not zero means the edge is stale and nobody was told.
Watch its rate the way you would watch a dead-letter queue.

## The limit worth knowing

The tag is **model-grained**, matching the signal it is derived from. A write to
any `Report` purges every response tagged with `Report`, not only the ones that
read the row that changed.

Row-grained tagging would need the response's read set recorded per request —
real bookkeeping on the hot path to save some over-purging on the cold one. It is
the same trade [`wreath._orm_events`](response-cache.md) declined for the local
cache, declined again here for the same reason. If a handler is expensive enough
that over-purging hurts, give it its own narrower `invalidate_on`.

Reference: [`wreath.response_cache`](../reference/response_cache.md)
