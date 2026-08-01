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

**A `numeric` key is refused.** The cursor round-trips through the ledger's
`jsonb`, and there is no decimal codec to read it back with — so the value would
return as a float, and `1.0000000000000000001` and `1.0000000000000000002` both
become `1.0`. Two boundaries a decimal place apart collapse into one and the walk
skips every row between them, which is the non-unique-boundary failure wearing a
different hat. Walk on an exact column instead, usually the primary key, and put
the decimal in the chunk's work rather than its key.

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

## Where the next range comes from

`Rows(key=...)` walks by keyset: the next chunk is the next *limit* rows after
the cursor, found by asking the table. Everything above about unique, indexed,
single-direction keys is about that — the boundary is a row that exists, so it
has to identify exactly one.

### How big should a chunk be?

Measured, not guessed — PostgreSQL 17 on loopback, ten million rows, an
`UPDATE`-per-chunk rewrite, unpaced, median of two full walks. The A/A noise
floor at this size is 0.087s, so everything below is resolved:

| `limit` | wall time | rows/s | per chunk |
| --- | --- | --- | --- |
| 1,000 | 52.98s | 188,745 | 5.3ms |
| 10,000 | 27.64s | 361,856 | 27.6ms |
| 100,000 | 26.82s | 372,794 | 268ms |

Fitting a fixed cost per chunk plus a marginal cost per row gives **2.8ms per
chunk** and **2.5µs per row**. That decomposition is the whole story: the
marginal cost is what the work costs, and it matches a single `UPDATE` over the
whole table almost exactly (24.8s modelled against 24.4s measured). Everything a
pass adds is the per-chunk fixed cost, and the only way to pay less of it is to
have fewer chunks.

So the curve is **flat above ten thousand, not above one thousand** — going from
1,000 to 10,000 nearly doubles throughput, and going on to 100,000 buys 3%.

**The default is still 1,000, deliberately.** A chunk is a lock footprint as
well as a unit of throughput: at `limit=100000` one chunk holds a hundred
thousand row locks for a quarter of a second, and a pass exists to *not* do that
to a live table. If you own the table and nothing else contends for it, raising
the limit is the single biggest thing you can do, and now there is a number
behind it. If the table is hot, the default is the conservative choice and the
cost of that choice is about 2× throughput.

`Buckets(on=..., step=Day, zone=...)` is the other shape, and it asks the table
nothing at all. The next range after "the day starting the 24th" is "the day
starting the 25th", whether or not a single row landed in either:

```python
fold_yesterday = ChunkedPass(
    "fold_treks",
    over=Table("treks"),
    units=Buckets(on=Key("recorded_at", "timestamptz", indexed=True),
                  step=Day, zone="Pacific/Auckland"),
    frontier=Sealed(after="2h"),
    work=Apply(fold_one_day, idempotent=Declared("the rollup upserts by day")),
)
```

Three things follow from a range being computed rather than found:

- **The key does not have to be unique.** A bucket boundary is a value the
  calendar produced, not a row the table happened to hold, so it cannot land
  between siblings. The index requirement stays, because the chunk's *predicate*
  is still a range scan.
- **The range is closed at the bottom and open at the top** — `>= start AND <
  end` — which is the opposite anchoring to a keyset chunk, and it is what stops
  a row exactly on a boundary being counted in both buckets.
- **The frontier is tested against a range's end.** A bucket cannot settle
  before the moment it stops accepting rows, so at noon on the 27th the 27th is
  still open and is left alone. Folding it in would settle a number that is
  still moving.

The zone is part of the declaration and **not** a runtime argument, for the same
reason it is in [calculated views](calculated-views.md): a materialised Auckland
day cannot be re-cut into a London day afterwards.

The calendar arithmetic is [`wreath.temporal`](dates-and-times.md)'s, so a day
that spans a daylight-saving change is 23 or 25 hours rather than 24. If you
ever compare two boundaries yourself, convert both to UTC first — two aware
datetimes sharing a `tzinfo` subtract on the wall clock, which is correct on
every day but the two a year that matter.

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
PASS                         STATE    PROGRESS               ROWS  ETA
--------------------------------------------------------------------------
purge_replays                slow     41.2% (estimated)     14000  22m
                             paced: duty cycle 0.25
```

The ledger row is the durable status, so this is honest at three in the morning:
a pass running for two hours is still there, and one that nothing is driving says
so instead of looking idle. `--json` emits the same thing for a machine.

### The percentage always says where it came from

`64%` reads as a measurement and people plan around it. `64% (estimated)` reads
as what it is. There are only three honest denominators and each lies in its own
way, so the kind travels with the number everywhere it goes:

| `progress=` | how | cost | lies when |
| --- | --- | --- | --- |
| `Estimated()` *(default)* | `pg_class.reltuples` | free | `ANALYZE` is stale |
| `Exact()` | `SELECT count(*)` once at launch | a full scan | never |
| `Keyspace()` | how far the cursor sits between the smallest key and the ceiling | free | the key is sparse or clumped |

`Estimated()` is the default because a full count in front of a long pass delays
the work you actually asked for in order to make a progress bar prettier. Reach
for `Exact()` when the number matters more than the minutes.

Two cases report **no denominator** rather than a wrong one, and a pass with no
denominator still walks — it just declines to quote a percentage. A table
`ANALYZE` has never seen answers `-1`, which is not a count; and a table that is
not there answers nothing at all, rather than raising and taking the shift down
with it.

### There is no ETA unless there can be one

Rate is measured over a trailing window of chunks rather than since launch, so
ten minutes of hard pacing shows up instead of being averaged away against a fast
first hour. When that window is empty there is **no ETA** — not infinity, not
zero, not "calculating…" forever. The field is absent and the row says which
input was missing:

```console
purge_replays                walking  ? (estimated)          1200  -
                             no ETA: the rate window is empty: no chunk has
                             finished recently
```

A fabricated ETA is worse than none, because somebody plans around it.
`Keyspace()` never reports one at all: its progress is measured in keyspace and
the rate is measured in rows, so dividing one by the other would not be a time.

### Slow, stalled, and blocked are three different problems

This is the part hand-rolled backfills leave out, and each state wants a
different person to do a different thing:

- **`slow`** — the cursor is advancing, below the rate you might expect. Usually
  the pass *chose* this, and it says so: `paced: duty cycle 0.25`. A paced pass
  that does not report being paced is indistinguishable from a broken one.
- **`stalled`** — a chunk is stuck. Go and look at `pg_stat_activity` for a lock
  wait or a pathological statement. The threshold is a multiple of the pass's own
  observed chunk time, so a pass whose chunks take two minutes is not accused of
  stalling after thirty seconds.
- **`blocked`** — nothing is driving it, and it will silently never finish. A
  dead-lettered chunk under `halt`, a scheduler that is not running, or an
  enqueue that failed. This is the state that has no name in a `screen` session,
  and it is the one that costs you three weeks.

### Health checks: page someone, do not drain the pod

```python
from wreath.health import health_router, passes_check, postgres_check

app.include_router(health_router(
    [postgres_check(db)],                 # decides traffic
    alerts=[passes_check(db)],            # decides who gets paged
))
```

`passes_check` goes on `alerts=`, served at `/health/alerts`, which the load
balancer does not read. A blocked backfill is a data problem and the application
is still serving correctly — failing readiness for it turns that into an outage
*and* removes the very workers that would have resumed the pass. The check is
built non-critical as well, so putting it in the wrong list still cannot drop
traffic.

## Which drive started this walk

The ledger row carries a `traceparent`, and every shift rebinds it — so a chunk that
dead-letters on day three of a backfill still names whatever started the walk, and
`wreath passes status` prints the trace id for a pass that has stopped:

```
normalize_grades             blocked  61.4% (estimated)      2,140,000  -
                             last chunk error: RuntimeError('deadlock detected')
                             trace: 4bf92f3577b34da6a3ce929d0e0e4736  (wreath doctor trace 4bf9...)
```

Two decisions shape what that trace *is*, and both are worth knowing before you rely on
one.

**Capture, never mint.** A pass driven only by `cron` has no originating request. Its
ledger row stores SQL `NULL` and its shifts run untraced, rather than being handed a
freshly invented trace id. Wreath propagates context; it does not generate spans, and it
carries the upstream sampling decision rather than re-deciding it — a minted traceparent
would have to pick a sampled flag, and neither choice survives scrutiny. `-01` forces
every backend in the path to retain a trace that may run for three days; `-00` produces
an id that is stored, printed by the CLI, and collected by nothing. To get a trace on a
pass, drive a shift from something that has one — an admin endpoint that enqueues the
first shift is the ordinary way, and every later shift inherits it.

**The trace belongs to the cycle, not to the pass.** The first drive that *has* a trace
names it (`COALESCE`, so a later drive does not re-attribute a walk already under way),
and a recurring pass re-captures when a new cycle begins. That is the retention bound: a
recurring pass runs for the life of the deployment, and carrying one drive's traceparent
across every cycle would produce a trace that never ends and that no backend assembles.
A single finite backfill is still one trace for as long as it runs — that is the reading
you asked for when you drove it from a traced request, and the alternative expressible in
one `traceparent` column is no trace at all.

The column arrives as version 2 of the `passes` schema component. A build newer than its
database asks the catalog once, walks untraced, and does not fail the shift.

## A query budget for a shift

A pass is where an N+1 costs the most. Per chunk it is invisible; multiplied by
the chunk count it is a six-hour outage — and it passes every test, because a
test table fits in one chunk.

```python
ChunkedPass("recode_species", ..., query_budget=500)
```

The budget is per **shift**, not per chunk. An N+1 inside a hundred-row chunk is
under any per-chunk ceiling a person would write; what does the damage is the
product of queries per chunk and chunks per shift, and the shift is the scope
where that product is visible.

Omitted, a shift is observed rather than bounded. See
[Finding the N+1 query](n-plus-one.md).

## When a chunk keeps failing

Retries inside the shift come first: a lock wait that clears in fifty
milliseconds does not deserve a trip through the job queue. After
`chunk_retries` attempts the chunk becomes a **hole** — a row in
`"wreath".pass_holes` carrying the range, the attempt count, and the statement
that reproduces it:

```console
$ wreath passes status myapp:app --holes
...
1 dead-lettered chunk(s):

  purge_replays  after 3 attempt(s), at 2026-07-27 12:00:00+00:00
    error: RuntimeError('deadlock detected')
    reproduce: SELECT * FROM replays WHERE (expires, key) > ('2026-07-27T10:00:00+00:00', 'k000') AND (expires, key) <= ('2026-07-27T11:00:00+00:00', 'k042')
```

That last line is what turns a hole into a task. Paste it into `psql`, in a
transaction, and see the actual error rather than a truncated `repr` from three
weeks ago.

What happens next is declared, because no default suits both callers:

- **`on_chunk_failure="halt"`** *(default)* — the pass stops at the hole and
  nothing after it runs. Correct for a conversion, where a backfill with a hole
  must never reach a terminal step; halting makes that structural rather than
  remembered. It is the default because nothing should be skipped by omission.
- **`on_chunk_failure="skip"`** — the cursor moves past, the pass carries on, and
  **the terminal gate is barred until the hole is cleared**. Correct for a
  recurring purge or rollup, where one malformed row must not stop the work
  forever.

The barring rule is the whole point:

> Skipping is allowed for throughput. It cannot buy the irreversible step.

Skipping is not silent, because the gate remembers. Blocking is not forever,
because the cursor moves. The only way to un-bar the gate is to clear the hole:

```console
$ wreath passes retry myapp:app
purge_replays: requeued 1 chunk(s)

A hole clears when its chunk succeeds, not when it is queued -- check
`wreath passes status` again once a shift has run.
```

### Walking one range out of order

`retry` is one caller of a smaller primitive. `pass.requeue(db, unit, after=...)`
appends a range to the ledger's pending queue, and the walk takes the oldest
pending unit before it takes the cursor's next range. **The cursor never moves
backwards** — rewinding it to collect one late row would redo months of correct
work:

```python
await roll_up_days.requeue(db, bucket_end, after=bucket_start)
```

Two callers arrive at this from opposite directions — a rollup folding in a late
correction, and an operator clearing a dead-lettered chunk — and get the same
mechanism.

## The terminal gate: verify before anything irreversible

Some passes exist to make something else safe afterwards — a migration that
narrows a column, a partition that gets dropped. The rule is one line, and every
arrow in it is a place where reversing the order loses data permanently:

> **materialise → verify → only then the irreversible step.**

```python
convert_grades = ChunkedPass(
    "convert_grades",
    over=Trek,
    units=Rows(key=Trek.id, limit=5_000),
    frontier=Ceiling.at_launch(),
    work=Rewrite({"grade_text": "grade::text"}, where="grade IS NOT NULL"),
    gate=Gate(
        verify=Constraint("trek_grade_text_present", "grade_text IS NOT NULL"),
        publishes="trek.grade_text",
    ),
)
```

### Counters are progress, never proof

`rows_done == denominator` is a statement about the pass's own bookkeeping, and
the failure it absorbs perfectly is a walk that skipped one range and
double-counted another. So a verification is always a question the *database*
answers, in one of three grades:

- **`NoRowsMatch("grade_text IS NULL")`** — ask the table the question the
  irreversible step depends on.
- **`Reconcile(source, against)`** — two independent counts that must agree.
  Cheap at coarse grain, and possible only *before* the source is removed, which
  is the only moment it will ever be possible.
- **`Constraint(name, check)`** — add it `NOT VALID` (instant, checks nothing),
  then `VALIDATE CONSTRAINT` (scans under `SHARE UPDATE EXCLUSIVE`, blocking
  neither reads nor writes, and naming the offending row if it fails).

`Constraint` is the one to reach for when it fits, because the verification and
the thing that will enforce the invariant afterwards are the *same predicate*.
That is available only because the same tool emits the DDL; a bolt-on backfill
library has to hand-write a `SELECT` and hope it matches the constraint somebody
adds later.

### The verification may not restate the walk

If the walk selected `WHERE grade_text IS NULL` and the check asks
`WHERE grade_text IS NULL`, a walk whose predicate was subtly wrong verifies its
own bug and reports success. So a gate that repeats the work's own `where` is
**refused where you declared it**. Derive the check from the invariant the
irreversible step needs, not from the walk.

### The gate publishes a fact; the irreversible step consumes it

Verification always writes a durable fact into the ledger. Running something
irreversible is separate and opt-in, because the two callers need different
things: a deferred migration's terminal step is *permission for a later
migration somebody else runs*, while a rollup owns the partition it is dropping.

```python
for fact in await published_facts(db):
    print(fact.name, fact.fact, fact.verified_at)
```

That is readable with a connection and a schema and no pass declaration, which
is exactly what a migration deciding "may I narrow this column?" has in hand.
Pass `then=` when the pass does own its terminal step; it runs once, behind a
phase compare-and-swap, after the fact is published.

### A migration will not narrow a column you are still converting

The fact is not only readable — it is *read*. When a pass guards a column,
`wreath migrations apply` refuses any artifact that drops it or changes its type
until the gate has published:

```python
from wreath.passes import column_fact

normalize_grades = ChunkedPass(
    "normalize_grades",
    over=Trek,
    units=Rows(key=Trek.id, limit=10_000),
    frontier=Ceiling.at_launch(monotone="ids come from an identity column"),
    work=Rewrite(...),
    gate=Gate(
        verify=Constraint("grade_next IS NOT NULL"),
        publishes=column_fact("app", "treks", "grade"),
    ),
)
```

`column_fact` exists so both halves of that contract spell the column the same
way. A free-form string would agree right up until someone wrote `treks.grade`
where the other side expected `app.treks.grade`, and the failure would be a
migration that sails through rather than one that refuses.

**Name the column a later migration will narrow, not the one you are filling.**
A retype drains `grade` into `grade_next`; the fact is about `grade`, because
`grade` is what the swap migration drops.

The refusal names the pass and what would clear it:

```
refusing to apply migration to schema 'app': it narrows 1 column(s) a chunked
pass is still converting, and narrowing a column before its backfill finishes
loses the rows behind the cursor:
  - drops app.treks.grade, which pass 'normalize_grades' has not finished
    (phase walking)
Let the pass finish -- `wreath passes status` shows where it is -- and apply
this migration afterwards.
```

A pass with an open hole cannot publish, because a hole bars its gate. The
refusal says so and points at `wreath passes retry`, since waiting will not
clear that one. `wreath migrations check` lists every guarded column before you
deploy, so the first you hear of this is not a failed release.

A pass whose gate publishes nothing guards nothing, and a column no pass guards
is never refused — the scan exists to catch the one dangerous case, not to make
every migration ask permission.

### A failed verification is not transient

If the check answers *no*, the walk's logic is wrong and running it again will
fail identically at the same row. The pass stops at `unverified` with the
failing check recorded, and `wreath passes retry` deliberately will not restart
it — that is for a chunk that was given up on, and burning a maintenance window
to fail at the same row helps nobody.

A check that *could not run* — a dropped connection, a lock timeout — is a
different thing entirely: nothing has been concluded, so the pass stays where it
is and the next shift tries again.

### Scope

`Gate(scope="pass")` verifies the whole table once the walk completes, which is
what a migration needs. `Gate(scope="unit")` verifies each range as the walk
passes it, which is what a recurring pass needs — one bad bucket must not freeze
the ladder behind it. A recurring pass has no completion for a whole-pass gate
to fire at, so declaring one is refused.

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
