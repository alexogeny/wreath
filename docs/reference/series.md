# `wreath.series`

Chart data as a declaration. Reach for it the moment a handler starts fetching
rows in order to bucket them by day in Python — a `Series` says *count these per
interval, in the reader's timezone, over this range, with the quiet buckets
showing as zero* once, and lets PostgreSQL do the arithmetic. `Aggregate` is the
same machinery without a time axis, for a bar chart, a KPI, or a scatter.

The bucket vocabulary itself lives in [`wreath.temporal`](temporal.md), because
correct zone-aware bucketing is useful on its own and `Series` is not its only
caller.

::: wreath.series
