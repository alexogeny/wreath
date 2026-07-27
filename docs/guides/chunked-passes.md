# Chunked passes: backfills, rollups, and reindexes

Sooner or later a table gets big enough that you cannot change all of it at once.
Ten million rows need a new column filled in. A month of raw events needs folding
into a daily summary. Every row needs re-encrypting under a new key. Expired
sessions need deleting, forever.

The shape is always the same, and so is the script people write for it: a
`while True` loop with `OFFSET`, one transaction around the whole thing, a
`sleep` somebody tuned once on a laptop, a `print` for progress, and an
`except: continue` that turns a failed chunk into a silent hole. It runs in a
terminal on a jump host and it is nobody's job to watch it.

`wreath.passes` is that script, written once and correctly.

!!! note "Why it is not called `backfill`"

    A recurring rollup does not fill anything *back*; it settles a frontier
    forward. Naming shared machinery after one caller's purpose is how the second
    caller ends up explaining why the backfill module runs forever. A *pass* over
    data is ordinary engineering English — a compiler pass, a two-pass assembler
    — and it takes neither caller's side.

## User story: keep the replay table small, forever

> *As an operator, my idempotency table grows by every write my API serves. I
> need expired rows deleted continuously, without the delete ever being the
> reason a request waited for a database connection.*

```python
from wreath.passes import ChunkedPass, DutyCycle, Key, Purge, Rows, Sealed, Table

purge_replays = ChunkedPass(
    "purge_replays",
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
)

jobs.drive(purge_replays, cron="*/5 * * * *")
```

That is the whole declaration. The job runner drives it, the ledger remembers
where it got to, and `wreath passes status` answers *is it still going?*

For Wreath's own stores you do not even write that much — a `Keyed` declaration
already knows its table, its key, and its stamp:

```python
jobs.drive(store.purge_pass(), cron="*/5 * * * *")
```

## What you get, and what each one prevents

**Keyset ranges, never `OFFSET`.** `LIMIT c OFFSET k` produces and discards `k`
rows before returning any, so walking `N` rows in chunks of `c` touches
`N²/(2c)` rows in total. A keyset range is an index descent plus `c` rows per
chunk — `(N/c)·log N + N`. At ten million rows in ten-thousand-row chunks that is
5×10¹² against 10⁷. The walk is the same speed at row nine million as at row
nine. *(This is a complexity argument and needs no benchmark.)*

**One transaction per chunk.** A transaction held open for an hour holds its
snapshot open for an hour, so `VACUUM` reclaims nothing any other transaction
updated in that hour and a hot standby inherits the same bloat. The application
does not slow down *during* the backfill; it slows down for as long as the bloat
takes to work back out, which is the part nobody attributes correctly afterwards.

**The cursor advances inside that transaction.** The position and the data are
two rows in one database, so they commit together. A chunk is wholly applied or
wholly not, and a crash resumes exactly where it stopped. The alternatives are
broken asymmetrically: a cursor committed *after* the work re-runs a chunk, which
is recoverable; a cursor committed *before* it **skips** one, which is an
unrecorded hole the pass reports as success.

**Bounded shifts.** A job lease is thirty seconds and there is no heartbeat, so a
handler that runs for an hour gets reclaimed while it is still running and picked
up by a second worker. A pass instead works in stretches shorter than the lease,
ends at a chunk boundary, and re-enqueues itself. A redeploy mid-pass costs at
most one chunk.

**Pacing, from the first release.** There is no "off". A walk that goes as fast
as it can is the one that takes the site down while its own dashboard stays
green — every layer's queue inside its own limit, and p99 at thirty seconds
because each request waited for a connection the backfill was holding.

## The refusals

Every rule below is a data-loss or a never-terminates bug that the declaration
can see. Raising where you declared it costs a failed start; the same bug raising
during a walk costs a table.

**A boundary that cannot be proven unique is refused.** The cursor stores a key
value, so if two rows share the value that lands on a chunk boundary then `>`
skips the siblings — silent data loss whose counters still add up — and `>=`
re-processes them forever once one value has more rows than the chunk limit.
There is no third option. The fix is always the same and the error says it:
append the primary key as a tiebreaker.

```python
units=Rows(key=Key("expires", "timestamptz", indexed=True))          # refused
units=Rows(key=(EXPIRES, Key("key", "text", unique=True)))           # correct
```

**A leading key column with no index is refused.** Without one the database sorts
the whole table once per chunk — `N/c` sorts of `N` rows, which is *worse* than
the `OFFSET` paging a keyset walk exists to avoid. It is refused rather than
silently degraded.

**A composite key is one row comparison.** `(herd_id, id) > ($1, $2)` is answered
by a single index scan. The hand-expanded `herd_id > $1 OR (herd_id = $1 AND id >
$2)` means the same thing and is planned as a bitmap-or over two scans plus a
sort, so the row-comparison form is the only one emitted — and because a row
comparison has no mixed-direction form, `(a ASC, b DESC)` is refused with that as
the reason.

**A fixed ceiling over an unordered key is refused.** `Ceiling.at_launch()` is
sound only when a row inserted afterwards cannot land *beneath* it. An identity
key or a `now()` default gives that; `gen_random_uuid()` does not, and a row that
lands behind the cursor is one the pass will never see. ULIDs and UUIDv7 really
are monotone and nothing in a column declaration can see it, so the way past is a
sentence a reviewer reads rather than a flag:

```python
frontier=Ceiling.at_launch(monotone="UUIDv7, assigned by the application")
```

**A key the work itself writes is refused.** Walking by `expires` while updating
`expires` moves rows past the cursor, so they are processed twice or never.

**The time chain is checked.** `statement_timeout < within < shift < lease <
command_timeout`. A chunk budget that does not fit inside a shift, or a shift
that does not fit inside the runner's lease, is refused by
[`jobs.drive`](jobs.md).

## Passes that finish, and passes that do not

The frontier decides which kind you have.

`Ceiling.at_launch()` captures the largest key once, so *completion* means
something. Without a ceiling, a table written to faster than the walk moves never
terminates — it reports ninety-six percent forever while doing real work. Rows
written past the ceiling are not the pass's problem, and the reason they are not
is the precondition every pass is declared under:

> **A pass converts the past. The application writes the future in the shape the
> pass is converting to.**

`Sealed()` re-derives the frontier every cycle: everything the clock has already
passed. A recurring pass has no completion — a *cycle* completes, the frontier
moves, and the next cycle starts again from the beginning of the domain. That
rewind is what makes it sound where a fixed ceiling would need a monotone key: a
row that expired behind the cursor while a cycle ran is found by the next one.
`Sealed(after="1h")` holds the frontier back from the present, for work that must
not touch a row until it has settled.

A recurring pass needs `cron=`, and `jobs.drive` refuses without it. Nothing else
would start the next cycle, and a pass that quietly stops after one is worse than
one that refuses to be declared.

## What the work can be

- **`Purge()`** — delete every row in the chunk. Idempotent by nature.
- **`Rewrite({column: expression}, where=...)`** — update the rows that still
  need it. Re-running is a no-op because `where` excludes what is already
  converted.
- **`Apply(callback, idempotent=Declared("why"))`** — anything else, awaited
  inside the chunk transaction so what it writes commits with the cursor.

`Apply` requires a written reason, and there is deliberately no `strict=False`.
Job delivery is at-least-once, so a chunk *can* run twice; the question cannot be
avoided, only answered — and being wrong on purpose should at least be legible to
whoever reviews it.

```python
Apply(reencrypt, idempotent=Declared(
    "re-wrapping a key is idempotent: the row records the wrapping key id, and "
    "rows already carrying the new id are excluded by `where`"
))
```

## Watching one

```console
$ wreath passes status myapp:app
PASS                             PHASE        UNITS         ROWS  PACED
--------------------------------------------------------------------------
purge_replays                    walking         14        14000  duty cycle 0.25
                                 last advance 2026-07-27 12:00:00+00:00
```

The ledger row is the durable status, so this is honest at three in the morning:
a pass running for two hours is still there, and one that nothing is driving says
so instead of looking idle. `--json` emits the same thing for a machine.

## Applying the schema

The ledger lives in `"wreath".passes`, beside the jobs table. Nothing in Wreath
creates it — a table that appears because a process started is a schema change
with no history and no review — so apply it as a migration:

```python
from wreath.passes import schema_sql

print(schema_sql())
```

## What a pass deliberately does not know

It has no opinion on what a chunk *means* and no clock of its own; scheduling
belongs to [`wreath.jobs`](jobs.md), which already deduplicates a cron tick
fleet-wide. Its whole vocabulary is "a half-open range over one ordered domain",
and row counts are reported rather than structural — which is what keeps the door
open for a range source that counts no rows at all.

Reference: [`wreath.passes`](../reference/passes.md).
