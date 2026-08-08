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
It stores nothing itself. Apply those policies with first-class
`wreath.policy.CachePolicy`; a static default is frozen into native egress.

Reach for the first when you want to keep computed data close; reach for the
second when you want to tell the network what it may keep.

**Reference:** [`wreath.cache`](../reference/cache.md),
[`wreath.cache_control`](../reference/cache_control.md).

## User story: reference data that reloads itself

> *Countries, plans, feature definitions — read on nearly every request, changed
> a few times a year, and always by someone forgetting to restart the workers
> afterwards.*

```python
from wreath.cache import SnapshotCache, refresh_on

countries: SnapshotCache[str, Country] = SnapshotCache()
await countries.refresh(load_countries)
refresh_on(countries, [Country], load=load_countries)
```

Now a committed write to `Country` — from an admin page, a migration, a job —
reloads the snapshot. Note it *reloads* rather than dropping: this cache holds
reference data, and a dropped generation would leave readers with an explicit
miss on data that has not gone anywhere.

It rides the same announcement the [response cache](response-cache.md) listens
to, so one call makes both fleet-wide:

```python
invalidate_across_workers(app.messaging("bus", database="app"))
```

A write on any worker then reloads every worker's reference data over one bus
channel. Two properties keep it safe to leave on: the refresh is single-flight,
so a burst of writes is one reload; and a failing loader leaves the previous
generation in place, so a database blip cannot empty a cache readers depend on.

### Check that the reloads are working

Surviving a failing loader is the right behaviour, and it has an uncomfortable
consequence: this cache degrades *upwards*. Every read keeps succeeding against
the last good generation, so errors, latency and hit rate all stay flat while
the data quietly ages. Nothing else in the system will tell you.

`refresh_on` returns a `RefreshWatch` for exactly that. Call it to stop
watching, and read the two attributes to find out whether it is still working:

```python
watch = refresh_on(countries, [Country], load=load_countries)

if watch.refresh_errors:
    # Serving data older than the last write we were told about.
    logger.warning("country reload failing: %r", watch.last_error)

watch()          # stop watching
```

Export `refresh_errors` next to your other counters; a non-zero value that stays
non-zero is a cache serving stale reference data indefinitely.
