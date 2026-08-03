# Purge a CDN when the data changes

You cache a page at the edge. When the rows behind it change, the edge should
drop it — without anybody having to remember.

## The whole thing

```python
from wreath.response_cache import CDNPurge, Tags, cached

tags = Tags(secret=settings.surrogate_secret)

@app.get("/reports/summary")
@cached(ttl=30, invalidate_on=[Report, Invoice], tags=tags)
async def summary(request):
    return Response(await render_rollup())


purge = CDNPurge(app.jobs, tags=tags)
purge.watch(Report, Invoice)

@app.jobs.task("wreath_cdn_purge")
async def purge_tag(job, tag: str):
    await http.post(
        f"{CDN_API}/purge",
        json={"surrogate_keys": [tag]},
        headers={"Authorization": f"Bearer {settings.cdn_token}"},
    )
```

That is the whole loop. `invalidate_on` is what the response read, so it becomes
the surrogate keys on the way out; the ORM's commit signal is what the
transaction wrote, so it triggers the purge. One declaration, both caches.

The response now carries:

```
Cache-Tag: 3f2a91c4d8e05b17 8b1e7f04a2c93d66
Surrogate-Key: 3f2a91c4d8e05b17 8b1e7f04a2c93d66
Edge-Cache-Tag: 3f2a91c4d8e05b17 8b1e7f04a2c93d66
```

All three, because Fastly, Cloudflare and Akamai each read a different one and
ignore the others.

## The secret is not optional

`Tags(secret=...)` has no default. A surrogate key travels in a response header,
so an unhashed one leaks your table names, and a *computable* one is worse:
if the tag is `sha256("Report")` then emptying your edge cache takes knowing one
word. The secret must be identical across workers; rotating it renames every tag,
so rotate it deliberately and purge everything behind it.

## Watch the drop counter

```python
purge.dropped()    # purges that could not be enqueued, and are therefore lost
```

The failure mode here is silence — the edge keeps serving the old page and
nothing in your application looks wrong. A non-zero `dropped()` means somebody is
being served stale content and nothing else will tell you. Alert on its rate the
way you would a dead-letter queue.

## Why you do not call the CDN inline

`CDNPurge` enqueues a durable job rather than calling your CDN from the commit
signal. A CDN having a slow afternoon would otherwise make your *writes* slow,
and a purge that failed would simply be gone. The job is enqueued under a per-tag
deduplication key, so a bulk import writing ten thousand rows produces one purge
per tag rather than ten thousand.

Reference: [`wreath.response_cache`](../../reference/response_cache.md) ·
Guide: [Cache tags and edge purge](../../guides/cache-tags.md)
