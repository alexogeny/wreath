# Caching

"Caching" gets used for two genuinely different things, and Wreath refuses to
blur them, because confusing a store with a header is how caches go wrong. So
there are two modules, each doing exactly what its name says.

**`wreath.cache`** is an actual in-process application cache — `SnapshotCache`.
It is read-mostly and bounded, refreshed as a whole and published atomically, so
readers always see a consistent snapshot. Reads never touch I/O, and a miss is
explicit rather than a silent fetch. A cache defaults to 65,536 entries and a
64 MiB shallow retained-size budget; pass `max_entries=None` or `max_bytes=None`
only when another layer supplies a tighter bound:

```python
from wreath.cache import SnapshotCache

widgets: SnapshotCache[int, Widget] = SnapshotCache()
widget = widgets.get(widget_id)         # no I/O; None on a miss
```

## User story: a read-mostly lookup table kept in memory

> *As an API author, I have a small table of plan records that every request reads
> and almost nothing writes. I want it in memory as one consistent snapshot,
> refreshed as a whole, so a read is never a database round-trip and never catches
> the table half-updated.*

```python
from wreath.cache import SnapshotCache

plans: SnapshotCache[str, Plan] = SnapshotCache()

plans.replace({p.code: p for p in loaded_plans})   # publish a whole generation atomically
plan = plans.get("pro")                            # no I/O; None on a miss
```

`replace` materializes and size-checks the new generation before publishing it,
then swaps it in with a single reference — a reader always sees either the whole
old snapshot or the whole new one, never a partial update. Use `refresh(loader)`
to load-and-publish with concurrent callers coalesced into one load.

**`wreath.cache_control`** is about the HTTP `Cache-Control` header — typed
policy objects that describe how *clients and proxies* should cache a response.
It stores nothing itself. Apply those policies to responses with
`CacheControlMiddleware` from the [middleware](middleware.md) module.

Reach for the first when you want to keep computed data close; reach for the
second when you want to tell the network what it may keep.

**Reference:** [`wreath.cache`](../reference/cache.md),
[`wreath.cache_control`](../reference/cache_control.md).
