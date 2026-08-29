---
description: Declare a paced, resumable and observable walk over a large PostgreSQL table.
keywords: guide backfill purge migration chunk keyset durable pass pacing
---

# Chunked data passes

A backfill should not be a terminal loop with `OFFSET`, one huge transaction and a
sleep chosen on a laptop. Declare the ordered domain, chunk budget, frontier and work.

```python title="passes.py"
from wreath.passes import (
    ChunkedPass,
    DutyCycle,
    Key,
    Purge,
    Rows,
    Sealed,
    Table,
)

purge_replays = ChunkedPass(
    "idempotency_purge",
    over=Table("wreath_idempotency"),
    units=Rows(
        key=(
            Key("expires", "timestamptz", indexed=True),
            Key("key", "text", unique=True),
        ),
        limit=1_000,
        within="2s",
    ),
    frontier=Sealed(),
    work=Purge(),
    pace=DutyCycle(0.25),
    shift="10s",
)
```

```python title="test_passes.py"
from passes import purge_replays


def test_the_walk_has_a_bounded_keyset_contract() -> None:
    assert purge_replays.name == "idempotency_purge"
    assert purge_replays.units.limit == 1_000
    assert purge_replays.units.within == 2.0
    assert purge_replays.shift == 10.0
    assert purge_replays.work.writes == ()
```

Drive it through the existing durable job runner:

```python title="worker.py"
from app import app
from passes import purge_replays

jobs = app.jobs("maintenance", database="main", concurrency=2, lease=30)
jobs.drive(purge_replays, cron="*/5 * * * *")
```

Each cursor advance and chunk mutation commit together. Work resumes by keyset rather
than rescanning from the beginning. The declaration refuses an unindexed leading key,
an unprovable unique boundary, a chunk longer than its shift and a shift longer than
the job lease. `passes_check` reports blocked or stalled walks on the alerts endpoint
without removing a healthy server from rotation.

See [passes API](../reference/data.md) and [operations](operations.md).
