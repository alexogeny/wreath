# `wreath.series`

Chart data as a declaration. Reach for it the moment a handler starts fetching
rows in order to bucket them by day in Python — a `Series` says *count these per
interval, in the reader's timezone, over this range, with the quiet buckets
showing as zero* once, and lets PostgreSQL do the arithmetic. `Aggregate` is the
same machinery without a time axis, for a bar chart, a KPI, or a scatter.

The bucket vocabulary itself lives in [`wreath.temporal`](temporal.md), because
correct zone-aware bucketing is useful on its own and `Series` is not its only
caller.

The data operations underneath a declaration are public and storage-neutral.
`wreath.temporal.spine(start, end, bucket=..., in_zone=...)` produces the same
local-wall-clock bucket run PostgreSQL's `generate_series` does, including DST
boundaries. `reconcile(buckets, sparse, fills)` accepts ordinary iterables and
maps, so an in-memory producer uses exactly the dense-bucket and per-measure
fill rules as a PostgreSQL-backed declaration—without a model or connection.
When only cardinality is needed, `spine_length(...)` walks the same calendar
without constructing one `Instant` per boundary.

Rendering stays data-only too. `lttb(x, y, threshold)` returns indices into
parallel arrays, `nice_ticks(minimum, maximum, target)` chooses a readable
1/2/5 axis, and `series_path(x, y)` emits an SVG path where `None` begins a new
segment. None of these operations knows about a declaration, ORM type, SQL, or
renderer object.

For a complete chart boundary, `project_chart(...)` reconciles only the rows a
renderer asks for and returns stable identities, paths, and tick axes. If the
dense run is described by a range rather than already held as an iterable,
`project_chart_spine(start, end, bucket=..., in_zone=..., sparse=..., fills=...,
...)` keeps that run native-owned: sparse timestamps map directly to ordinal
positions and only compact final outputs materialise. SVG coordinates carry
nine significant digits, which is well beyond display resolution while keeping
the path locale-independent and bounded.

```python
from wreath.series import (
    lttb,
    nice_ticks,
    project_chart_spine,
    reconcile,
    series_path,
)
from wreath.temporal import Day, spine

buckets = spine(start, end, bucket=Day, in_zone="Pacific/Auckland")
readings = {("north", False): {buckets[0]: {"count": 4}}}
dense = reconcile(buckets, readings, {"count": 0})
values = dense[0][2]
selected = lttb(range(len(values)), values, min(96, len(values)))
path = series_path(selected, tuple(values[index] for index in selected))
ticks = nice_ticks(min(values), max(values))

row_count, keys, paths, axes = project_chart_spine(
    start,
    end,
    bucket=Day,
    in_zone="Pacific/Auckland",
    sparse=readings,
    fills={"count": 0},
    downsample_rows=(0,),
)
```

`.seal(after=...)` makes a bucket final once that long has passed since it
closed, so a settled value is computed once and afterwards read. A settled
bucket is deliberately **not** a cache: no TTL, no eviction, no recomputation.
A row that lands behind the watermark is recorded as a correction beside the
settled value rather than rewriting it, and `reconcile()` is what finds one —
the ORM's write events are model-grained by design and cannot say which bucket a
late row belongs to.

**Reading a sealed view never writes.** `settle()` is the write half and it
belongs in a scheduled job; `reconcile()` runs it first, so one job covers both.
A `run()` over a range nobody has settled returns the same numbers and does not
keep them, which is what lets a chart route use a read-workload session, a
replica, or a role with no `INSERT`. The two tables behind it are created by the
lifespan once the application declares `app.series(database=...)` — a `Series`
is a declaration the application never holds, so the claim needs an owner. See
[the guide](../guides/calculated-views.md#sealing-when-a-bucket-stops-being-able-to-change).

`.retain(raw=..., day=..., month=None)` keeps the same view at more than one
grain, so a long range reads stored coarse buckets instead of re-aggregating the
source table. A tier **is** this view at a coarser grain — same table, same key
shape, with the grain already part of the key — which is why `rollup()` and
sealing share one watermark and one insert. `retain` deletes nothing and this
release adds no way to; it says how long a grain stays warm, and the read path
honours that even while raw is still present. Two refusals travel with it: a
measure that cannot be recombined from parts (an average) against a bounded raw
window, and a read whose zone a materialised grain cannot serve.

A result can be returned from a handler directly; the JSON encoder asks it for
`__jsonable__`. `as_dict()` is the explicit form and is what the generated
TypeScript is written against.

::: wreath.series
