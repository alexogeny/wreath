# `wreath.metrics`

What every registered subsystem is counting, in one place a scrape can read.

Two dozen objects in this tree keep counters — `jobs` counts run errors and
lease expiries, `messaging` counts unrouted publishes and doorbell reconnects,
`entity` counts names lost under load, the pool records how deep its wait queue
ever got. Each was added with a written reason an operator would want it, and
each was reachable only by holding the object and knowing the method's name.

The bridges in [Observability](../guides/observability.md) export the *flight
projector's* snapshot — per-route aggregates. They did not read a subsystem.
This module is the other half:

```python
from wreath import metrics

for reading in metrics.collect(app):
    print(reading.subsystem, reading.instance, reading.values)
```

Collected by **asking**, exactly as `Wreath.schema_components` collects DDL
claims: anything the application holds that offers `counters()` contributes one
reading. A hand-maintained registry would be one more place to forget a new
subsystem, and forgetting is the defect this exists to remove.

`instance` is not decoration. A deployment runs several queues and several
buses, and a reading that cannot say which one it came from is a reading nobody
can act on — so `PrometheusBridge(source, app=app)` renders
`wreath_jobs_run_errors{instance="work"}` and keeps two queues as two series.

::: wreath.metrics
