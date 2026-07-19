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

**`wreath.cache_control`** is about the HTTP `Cache-Control` header — typed
policy objects that describe how *clients and proxies* should cache a response.
It stores nothing itself. Apply those policies to responses with
`CacheControlMiddleware` from the [middleware](middleware.md) module.

Reach for the first when you want to keep computed data close; reach for the
second when you want to tell the network what it may keep.

**Reference:** [`wreath.cache`](../reference/cache.md),
[`wreath.cache_control`](../reference/cache_control.md).
