# `wreath.series`

Chart data as a declaration. Reach for it the moment a handler starts fetching
rows in order to bucket them by day in Python — a `Series` says *count these per
interval, in the reader's timezone, over this range, with the quiet buckets
showing as zero* once, and lets PostgreSQL do the arithmetic. `Aggregate` is the
same machinery without a time axis, for a bar chart, a KPI, or a scatter.

The bucket vocabulary itself lives in [`wreath.temporal`](temporal.md), because
correct zone-aware bucketing is useful on its own and `Series` is not its only
caller.

`.seal(after=...)` makes a bucket final once that long has passed since it
closed, so a settled value is computed once and afterwards read. A settled
bucket is deliberately **not** a cache: no TTL, no eviction, no recomputation.
A row that lands behind the watermark is recorded as a correction beside the
settled value rather than rewriting it, and `reconcile()` is what finds one —
the ORM's write events are model-grained by design and cannot say which bucket a
late row belongs to. See
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
