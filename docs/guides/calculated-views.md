# Calculated views

Somewhere in most applications there is a handler that fetches a few thousand
rows in order to count them. It loops over them in Python, buckets them by day,
fills in the days nothing happened, and hands the result to a chart. Nobody
writes that because they want to — they write it because the query layer could
not say what the chart needed, so the rows came into the process and the
arithmetic happened in a loop.

That loop is where the interesting bugs live, and they are all quiet ones. It
buckets by UTC, so the days come out shifted for every reader who is not on it.
It skips the empty days, so the line joins straight across a gap that was really
a zero. It fills the empty days with zero for *every* measure, so an average
collapses to the floor on the quietest day of the week and reads as a crisis. And
it recomputes the whole history on every page load, because there is nowhere to
put the answer.

A calculated view says the same thing once, as a value, and lets PostgreSQL do
the work.

## The shape of it

```python
from wreath.series import Range, Series, count, sum_
from wreath.temporal import Day, zone
from wreath.queries import Param

activity = (
    Series(Trek, at=Trek.started_at, bucket=Day, stored_in=zone("Pacific/Auckland"))
        .where(Trek.herd_id == Param("herd"))
        .measure(started=count(), distance=sum_(Trek.distance_km, unit="km"))
        .by(Trek.paddock_id, top=7)
)
```

Then, per request:

```python
@app.get("/herds/{herd_id}/activity")
@cached(ttl=300, invalidate_on=activity.sources)
async def herd_activity(
    request,
    herd_id: int,
    session: Session,
    zone: str = "UTC",
):
    result = await activity.run(
        session,
        herd=herd_id,
        range=Range(start, end),
        zone=zone,
    )
    return result.as_dict()
```

The zone is an ordinary bound parameter — `?zone=Pacific/Auckland` — because
wreath does not guess it. There is no reliable timezone in an HTTP request:
`Accept-Language` carries a *language*, and an offset gathered from the browser
is not a zone (it cannot tell Auckland from Adelaide in February). So the caller
names it, or you take it from the resource — a venue row's own `timezone` column
is usually the better answer, since "the herd's day" rarely means "the reader's
day".

`as_dict()` is what goes on the wire. The result itself is a declared type
rather than a dictionary — `result.series[0].values` is a tuple you can read in
the handler — and `as_dict()` renders it with the field names the generated
client expects. Returning the result object directly does not work: the JSON
encoder knows temporal values but not dataclasses, so it raises rather than
guessing at a shape.

The declaration is a value, built once at import time and reused. Every builder
method returns a new one, so nothing needs a defensive copy — the same property
`Select` has, for the same reason.

## What you get that a loop does not

**Every bucket in the range exists.** The range generates a spine and the
aggregate is joined onto it, so a Tuesday when nothing happened arrives as a zero
rather than as an absence the caller has to notice. That is what makes the line
honest.

**Fill is per measure.** A count of nothing really is zero, and so is a sum. An
average of nothing is *undefined* — it stays `None`, and the renderer draws a gap
rather than a plunge to the floor. Minimum and maximum behave the same way. If
you want an average to read as zero on a quiet day you can say so, with
`.fill(pace=0)`, and it sits at the call site where a reviewer can see that the
flat line was a decision.

**Every series has a stable key.** The key comes from the grouping value, never
from its position in the result. A reader who learned that the north paddock is
the blue line is still looking at the north paddock after somebody narrows the
date range — which is not true of a chart coloured by rank.

**Two measures are two series.** `.measure(started=count(), distance=sum_(...))`
returns two separately named series, each carrying its own `unit` and `kind`.
They are never merged into one plottable line, because two quantities with
different units on one pair of axes is a dual-axis chart: the alignment of the
two scales is arbitrary, so the chart invents a correlation that is not in the
data. A renderer that wants them together still can, by facetting or by indexing
both to a common base — and because they arrive separately labelled, it *can*.

## Timezones, which is the hard part

Bucketing is a timezone problem before it is an aggregation problem. "Daily"
means daily *where the reader is*, and a day is a calendar day on somebody's wall
clock rather than 86400 seconds.

So the bucket is assigned on the local wall clock, the run of buckets is
generated there too, and both are converted back afterwards. That ordering is the
whole trick: stepping a day over local timestamps advances by a calendar day,
which is what a reader means, while stepping over absolute time advances by
exactly 24 hours — so the day a clock changes comes out an hour wrong and every
boundary after it is wrong too. It is a bug that appears twice a year, in one
bucket, and is almost never traced back to the chart.

Python's half of this and PostgreSQL's are checked against each other rather
than assumed to match: `Bucket.floor` and `date_trunc` are run over 9,828
comparisons — every bucket unit, nine zones, densely across both transitions of
each — and agree on all of them, including how each resolves the hour a
fall-back repeats. That check matters because a bucket boundary computed in
Python has to be an instant `generate_series` will emit. When those drift, a
settled row files itself under a bucket no read will ever ask for.

The **range** is a runtime argument, because it changes per request. The
**`stored_in`** zone is part of the declaration, and is the default when a caller
does not name one. Today every run buckets fresh, so any zone costs the same; the
distinction earns its keep once buckets are settled, because an Auckland day
cannot be re-cut into a London day after the fact.

Ranges are half-open throughout — `start <= t < end` — stated once and used for
the filter and the spine bounds alike. The off-by-one in a chart comes from
writing the boundary twice with two slightly different intentions; there is only
one here.

## Bounded results, and what happens at the edge

An unbounded `by` returns a million series and kills the browser rather than the
query, so the failure lands on the reader instead of on the thing that caused it.
Both shapes have a ceiling, and they behave differently on purpose:

- **A series folds.** `.by(column, top=7)` keeps the top seven and folds the rest
  into a single remainder carrying the reserved key `None` and `other=True`.
  Folding is meaningful because it preserves the total, which is what a
  part-to-whole chart is for. The survivors are ranked over the *whole* range, so
  a series does not appear and vanish as the reader pans, and ties break on the
  key so two runs of one query agree.
- **An aggregate refuses.** `Aggregate(...).by(column, limit=50)` raises past its
  ceiling rather than truncating, because a bar chart's bars are the answer and
  quietly dropping some of them draws a chart that is wrong rather than absent.

A raised ceiling is part of the declaration, not a query parameter — it lives
where it is reviewed, rather than somewhere a client can set it to a million.

One detail worth knowing: a grouping column that is nullable produces a
null-keyed series of its own, and the folded remainder also carries a null key.
`other` is what tells them apart, so a legend can label one "unassigned" and the
other "other" without merging them.

## Against the period before

"Up or down on last month?" is the second thing anyone asks of a chart, and it is
the easiest thing to get quietly wrong by hand. `compare()` answers it:

```python
from wreath.temporal import Day, Month

activity = (
    Series(Trek, at=Trek.started_at, bucket=Day)
        .measure(started=count())
        .compare(previous=Month)
)

result = await activity.run(session, range=Range(start, end), zone="Pacific/Auckland")
result.buckets              # this period
result.comparison.buckets   # the one before it
result.comparison.previous  # "month" — what a legend should call it
```

Three things about it are deliberate.

**It is one statement.** Two statements are how the periods end up misaligned by
a bucket: one clips its range differently, or truncates in a different zone, and
the comparison line sits a day out. Running the query twice is something anyone
can do; the alignment is the whole feature, and it only holds if one statement
computes both spines from one pair of bounds by one rule.

**The shift happens on the wall clock, not on the instant.** `previous=Month` is
a bucket rather than a duration because the useful comparisons are calendar ones
— a month is 28 to 31 days depending on when you ask, and no fixed number of
hours expresses "the same days last month". The bounds are read on the zone's
clock, stepped back a calendar unit, and converted back, so a clock change moves
the instant rather than the wall time. Subtracting a `timedelta` instead is
correct on every day but the two a year when it is not.

**The two periods can be different lengths, and the payload says so.** March
against February is 31 buckets against 28. Each period keeps its own bucket run
rather than being padded to match, because padding invents data and lining them
up by index is a decision for whoever draws the chart.

`compare()` refuses a period shorter than the range — comparing March against "a
week ago" would put rows in both periods, and there is no honest way to draw
that: counting them twice inflates the comparison, counting them once drops them
from a side. When the view is also grouped, the survivors are ranked over the
primary period alone, so "the top seven paddocks this month, and what those seven
did last month" keeps a legend that means one thing.

## Marking what happened

A chart that shows a step change is only half an answer; the other half is what
happened that day. `events()` puts markers on the same spine:

```python
activity = (
    Series(Trek, at=Trek.started_at, bucket=Day)
        .measure(started=count())
        .events(
            Deploy,
            at=Deploy.happened_at,
            label=Deploy.version,
            where=Deploy.environment == "production",
            limit=25,
        )
)

for marker in result.events:
    marker.at       # when it actually happened — its true x-position
    marker.bucket   # which column it annotates
    marker.label    # what to write on it
```

Both times are carried because neither can be derived from the other on the
client, and the bucket is computed by the same `date_trunc` in the same zone as
the series it sits over — so a marker cannot land a column away from the bar it
describes. That alignment is why this belongs on the declaration rather than in a
second handler: two hand-written queries drift, and an annotation layer that is
subtly misaligned is worse than none, because the chart looks explained.

Markers have their own ceiling and it **refuses** rather than truncating. A
hundred markers is not an annotation layer, it is noise; and drawing the first
twenty-five of two hundred annotates the chart with a subset that nothing in the
chart explains.

This is a second statement on the same session rather than a tagged union of
buckets and markers. The union would force two different row shapes into one row
type, half the columns null in every row, with a discriminator the client has to
switch on — a worse envelope and worse generated types, bought with a round trip
the driver describes itself as pipelining anyway. Whether it really costs one
round trip or two is a question for a real server; the alignment does not depend
on the answer.

With `compare()`, markers cover the primary period only. An annotation layer
answers "what happened during *this*".

## Without a time axis

`Aggregate` is the same core with no spine — the bar chart, the KPI, and the
scatter all fall out of it:

```python
from wreath.series import Aggregate, avg, count

busiest = (
    Aggregate(Trek)
        .measure(treks=count(), distance=avg(Trek.distance_km, unit="km"))
        .by(Trek.paddock_id, limit=20)
)

result = await busiest.run(session)
```

With no `by`, it is one row: a KPI. With two measures and a `by`, it is a
scatter — two quantities per entity. That is a declared ceiling rather than a
separate type, which is why there is no third class here.

## With a place axis instead

`Cells` is the same core again, bucketed by *where* rather than *when* — a
heatmap as a declaration, with the same obligation a line chart has.

```python
from wreath.geospatial import BoundingBox
from wreath.series import Cells, avg, count

reserve = BoundingBox(lat_min=-30.0, lat_max=-29.0, lon_min=150.0, lon_max=151.0)

heat = (
    Cells(Sighting)
        .where(Sighting.species == Param("species"))
        .measure(seen=count(), mean_weight=avg(Sighting.weight_kg, unit="kg"))
        .over(Sighting.lat, Sighting.lon, metres=10_000, extent=reserve)
)

result = await heat.run(session, species="llama")
for cell in result.cells:
    cell.row, cell.column     # index from the extent's south-west corner
    cell.bounds               # the ground it covers, a BoundingBox
    cell.centre               # a Coordinate, for pinning a marker
    cell.values["seen"]
```

**Every cell in the extent is present, and fill is per measure** — the same two
rules as the time axis, taken from the same function rather than restated. A
cell nothing fell into reads `seen: 0` and `mean_weight: None`, because a count
of nothing is zero and an average of nothing is undefined. A heatmap with
missing cells lies about a gap in exactly the way a line chart with missing days
does, and one that fills every measure with zero puts a hole in the map on the
quietest ground.

The lattice comes from [`grid`](geospatial.md), so the number of cells is known
before the query runs. `over` refuses past a declared ceiling — every cell is a
row on the wire whether or not anything is in it, which is the point of a dense
axis and also its cost:

```python
wide = (
    Cells(Sighting)
        .measure(seen=count())
        .over(
            Sighting.lat,
            Sighting.lon,
            metres=10_000,
            extent=reserve,
            limit=50_000,
        )
)
```

The refusal happens at declaration time, where a reviewer reads it, rather than
after the database has already scanned. `grid`'s own refusals — an extent
crossing the antimeridian, or one too tall for a single longitude step to tile
squarely — surface here too.

## Where it stops

A calculated view takes **one source model, declared measures, and a bounded
result**. Before reaching for it, four questions:

1. Can you name the source model? One, not "it depends".
2. Can you name each measure and its unit?
3. Can you state the largest the result can get?
4. Would a chart consume it without reshaping?

If any answer is no, what you have is a query rather than a chart, and
`session.raw()` is the honest tool — it exists, it is documented, and a
hand-written recursive CTE over a genuinely irregular topology is the right
answer to a genuinely irregular topology. What this module refuses to become is a
declaration that can express everything, which is SQL with worse syntax.

## The client gets the same names you declared

`wreath typegen` finds the views your routes use and emits a type for each, so a
component destructures a series instead of indexing `number[][]`:

```ts
import type { ActivityResult } from "./api";

function ActivityChart({ data }: { data: ActivityResult }) {
  for (const line of data.series) {
    if (line.measure === "started") {
      line.values;        // readonly number[]  — a count fills with zero
    } else {
      line.values;        // readonly (number | null)[]  — an average may be absent
    }
  }
}
```

This is the return on measures being named. The name you wrote in `.measure()`
is the field name in the generated union, and the per-measure fill rule decides
whether the compiler makes you handle a gap: a count that fills with zero is
`number[]`, while an average that stays undefined on a quiet day is
`(number | null)[]` and a component that would have drawn a plunge to the floor
has to say what it draws instead.

Discovery is by use. A declaration a routed handler actually references is
emitted, named after the variable it was written as — `activity` becomes
`ActivityResult`. A handler that reaches its declaration indirectly, through an
argument or a lookup, is not seen; that shows up as a type not being generated,
which is easier to notice than a wrong one.

A view can feed a docs chart too. Write the result to a file and point a
` ```chart ` block at it:

```
​```chart
source: ../data/activity.json
measure: started
title: Treks per day
​```
```

The chart is then the declaration's own numbers rather than a second copy
maintained by hand. `measure:` picks one of several, `series:` picks one line of
a grouped view, and naming one that is not there is an error in the build output
rather than a chart quietly drawn from something else.

## Converting a column underneath a view

A [deferred data migration](../reference/roadmap.md) rewrites a column's values
while the application keeps serving. A calculated view is unusually exposed to
that, because summing, averaging, grouping and ordering a column mid-conversion
are all meaningless in ways that do not raise: a `GROUP BY` splits one logical
category into two and the chart shows it forking, with nothing to catch it.

The check belongs to the migration side, which is the only half that knows a
conversion is running — this module has no notion of a converting column and is
not going to grow one. What a declaration does provide is `declared_columns`,
tagging each column it names with what it does to it (`time`, `aggregate`,
`group`), and `predicates` for the filters, where the *operator* is what decides
whether a rewrite is safe. Both are readable at import time, because a
declaration is a value.

That is the whole contract, and it is deliberately small: a scan can read a
declaration instead of parsing the handler that runs it.

## Sealing: when a bucket stops being able to change

Yesterday's total is not going to move. Recomputing it on every page load is not
a cheap safety net, it is the same arithmetic over the same rows reaching the
same number, once per reader, forever. `.seal()` says how long you are willing
to wait for a straggler, and after that the bucket is computed once and read:

```python
activity = (
    Series(Trek, at=Trek.started_at, bucket=Day, stored_in=zone("Pacific/Auckland"))
        .measure(started=count(), distance=sum_(Trek.distance_km, unit="km"))
        .seal(after="2h")
)
```

A bucket `[start, end)` is **sealed** once `after` has elapsed since it *closed*
— so with `Day` buckets in Auckland and two hours of allowance, Tuesday seals at
2am on Wednesday, Auckland time. Before that it is **open** and every run
recomputes it, because it can still move.

A settled bucket is **not a cache**, and the difference is not pedantry. A cache
may be evicted, must be recomputable, and manages staleness with a TTL. A
settled value is final: there is no TTL column, nothing evicts it, and nothing
recomputes it on a hunch. Reading a fully settled range does not touch the
source table at all.

The zone stops being a per-request argument once a view seals. A materialised
Auckland day cannot be re-cut into a London day afterwards, so settled buckets
are filed under the zone they were computed in — reading the same view in
another zone settles separately rather than lying about it. Declare it with
`stored_in=`.

Two tables hold this, and like every other table Wreath owns, **a migration
applies them and nothing applies them for you**:
`wreath._series.settle.schema_sql()` emits the DDL.

### The write that arrives late

A trek recorded on Monday for Friday's work lands behind the watermark, in a
bucket that is already settled. Three things could happen and only one of them
is defensible.

**Refusing the write** is wrong, decisively. `Trek` is a business table, and a
chart's watermark must never be able to fail a business write — the same rule
that stops a broken cache subscriber failing a committed one.

**Silently rewriting the settled value** is the other failure, and it is the one
sealing exists to prevent. If a settled number can change under you, it was
never settled.

So the settled value stays immutable and the difference is recorded beside it,
folded in when the series is read. The envelope says which buckets carry one:

```python
result = await activity.run(session, range=Range(start, end))
result.state.sealed_through   # the first bucket that is still open
result.state.corrections      # buckets whose settled value has a late adjustment
```

Late data arriving therefore looks like late data arriving, rather than like a
discrepancy somebody finds in a spreadsheet three weeks later.

**Nothing notices a late write on the write path**, and that is deliberate. The
ORM's write events are model-grained by design — they publish which models a
session touched, not which rows — so they cannot say which bucket a late trek
belongs to or what it contributes, and making them row-grained to serve a chart
would put per-row bookkeeping on every write in the application. Instead you run
`reconcile()`, from a scheduled job or after an import:

```python
corrected = await activity.reconcile(session, range=Range(start, end))
```

It recomputes the sealed part, compares it to what was settled, and records the
differences. Until something calls it, the gap is *visible* rather than assumed
away: `sealed_through` says exactly how far the settled data goes.

`on_late="reopen"` is available and replaces the settled value outright instead
of recording a delta. It is never the default, because it is only sound while
the source rows are still there to recompute from.

Once you declare `retain()`, that soundness becomes checkable, and a declaration
that can never satisfy it is refused where you wrote it:

```python
(
    Series(Trek, at=Trek.started_at, bucket=Day, stored_in=zone("Pacific/Auckland"))
        .measure(started=count())
        .seal(after="2h", on_late="reopen")
        .retain(raw="1 day")            # SeriesError
)
```

A day bucket sealing two hours after it closes is not fully recomputable until
**26** hours after it closes, because the oldest row in it is a whole day older
than its end. One day of raw is not enough, and neither is three hours — even
though three hours comfortably exceeds the two-hour seal. The requirement is the
lateness allowance **plus the bucket**, and the refusal names both numbers.

Ways out, in the order you should consider them: leave `on_late="correct"`, the
default, which records a delta beside a value it never destroys; keep the source
rows indefinitely with `retain(raw=None, ...)`; or lengthen raw's window past the
figure the refusal names.

**What that check cannot cover.** It refuses a declaration under which reopen
could never work. It cannot refuse a `reconcile()` that runs a month after a
bucket sealed, by which point the rows may have aged out under a window that was
ample at sealing time. And the hazard is not unique to `reopen`: `reconcile()`
recomputes from source rows in *both* modes, so `correct` running past the window
records a delta computed from rows that are no longer all there. The reason only
`reopen` is refused is what happens next — a correction leaves the settled value
standing and shows up in `result.state.corrections`, so it is observable and
recoverable, where a reopen overwrites the value and clears the correction that
would have shown anything was wrong. **Run `reconcile()` on a schedule shorter
than raw's retention window.**

### What cannot be sealed yet

**A grouped view.** The top-N survivors are ranked over the whole range, so
which series survive — and what falls into the remainder — depends on the range
being asked for. A bucket settled for one range would be wrong for the next, so
`.seal()` with `.by()` is refused rather than storing something only valid for
the range that happened to materialise it.

**A comparison.** The previous period is a second range and settling it is a
separate question; declare the seal on the view you read directly.

## Long ranges: the same view at more than one grain

A year of daily buckets is 365 rows to draw and rather more to aggregate. Once
a bucket is sealed you can keep a coarser copy of it too:

```python
activity = (
    Series(Trek, at=Trek.started_at, bucket=Day, stored_in=zone("Pacific/Auckland"))
        .measure(started=count(), distance=sum_(Trek.distance_km))
        .seal(after="2 hours")
        .retain(raw="3 days", day="1 year", month=None)
)
```

Read that as: keep the source rows answering for three days, keep daily buckets
a year, keep monthly buckets indefinitely. `None` means forever.

**A tier is this view at a coarser grain, and that is all it is.** There is no
second table and no second kind of identity — a settled bucket is filed under a
key derived from the declaration's content, and the grain is part of that
content, so asking for the same declaration at `Month` names the monthly tier.
The daily tier of a daily view is *literally* the rows sealing already writes.

Reading is transparent. A range spanning several tiers comes back as one dense
run of buckets and one series per measure, exactly as an untiered read does:

```python
result = await activity.run(session, range=Range(start, end), zone="Pacific/Auckland")
for segment in result.segments:
    print(segment.grain)      # "month", then "day", then "raw"
```

`segments` is reporting, not something to handle. It says which grain answered
where, so a chart drawn from two grains can say so.

### What retention does and does not mean

`retain()` **deletes nothing**, and this release adds no way to. It is a promise
about how long a grain stays warm, and the read path keeps that promise: a range
older than raw's window is answered from the coarsest tier that still covers it,
*even though raw happens to still be there*. That is deliberate — it means the
query keeps returning the same answer on the day a later release starts
enforcing the window, rather than quietly changing shape when rows it was
relying on disappear.

### Two refusals worth knowing about

**Not every measure survives being rolled up.** A count and a sum add. A minimum
and a maximum take the extreme of the extremes. An **average does not**: a daily
mean built by averaging twenty-four hourly means weights a quiet 3am hour exactly
as heavily as a busy noon, and produces a number that looks entirely reasonable.
So an average against a bounded raw window with a coarser tier above it is
refused where you wrote it:

```python no-check="continues the declaration above; not a statement on its own"
.measure(mean=avg(Trek.distance_km)).seal(after="2h").retain(raw="3 days", month=None)
# SeriesError: measure 'mean' is an average and cannot be rolled up ...
```

Two ways out. Keep the source rows for the whole window the chart asks about
(`retain(raw=None, ...)`), so every coarse bucket is always recomputed from
them. Or take the coarse tier off the ladder. Storing the average decomposed as
a sum and a count would make it additive, and that is not implemented.

**A materialised grain is timezone-specific**, and this is the part that
surprises people. Daily buckets cut in `Pacific/Auckland` cannot answer a
question about London days: the boundaries do not line up, and no amount of
re-aggregation recovers them. A tier can serve a reading zone only when the
offset between the two is a whole number of that grain — so hourly rows serve
any whole-hour zone but not `Asia/Kolkata` (+5:30), `Asia/Kathmandu` (+5:45) or
`Pacific/Chatham` (+12:45), and daily rows serve only the zone they were cut in.
A read in an incompatible zone falls back to raw, or refuses. It never quietly
returns the wrong day's numbers.

**The practical advice: if your readers span timezones, materialise at `hour`
grain or finer and let day bucketing happen at read time.** You lose some
compression and keep correctness. A single-zone business materialises daily and
keeps both.

Declaring `retain()` also constrains your seal: a bounded raw window and
`seal(on_late="reopen")` are refused together unless raw outlives the lateness
allowance **plus one bucket** — see [the write that arrives
late](#the-write-that-arrives-late).

### When a range outruns the grain you asked for

If part of a range is only covered by a tier coarser than the bucket you asked
for, the read refuses and names what is available:

```
SeriesError: this range reaches past every tier that stores day buckets; the
coarsest grain still covering it is 'month'. ... pass allow_coarsening=True ...
```

Returning monthly numbers labelled as days is a lie that survives review, so it
is not the default. `allow_coarsening=True` accepts it, and `segments` reports
where it was taken up.

### Building the coarser grains

`rollup()` materialises every tier above `raw` over the sealed part of a range:

```python
written = await activity.rollup(session, range=Range(start, end))
```

It is an ordinary method, so scheduling stays in `wreath.jobs` where it already
lives — `jobs.schedule` with a dedup key makes a re-run a no-op, and the insert
refuses to overwrite anyway.

Two things it does in order. It **reconciles first**, so a late write that
landed behind the watermark is folded in before anything coarser is built from
it — carving a stale number into a coarser grain puts it somewhere harder to
notice and no longer traceable to the row that caused it. Then it computes each
coarse grain **from the source rows**, not from the finer tier, which is both
correct by construction and immune to the average-of-averages trap.

## What is not built yet

The declaration surface names two more methods that a later stage fills in:
`.archive()` and `.drop()`. They exist today and they **refuse by name** rather
than quietly doing nothing, because a declaration that read as though archival
were configured while nothing enforced it would be worse than one that failed.
Nothing in this module deletes anything — sealing decides a bucket is final and
retention decides which grain answers, neither makes anything go away — and
`.drop()` will stay opt-in when it arrives, because a declaration written to
make a chart fast must not be able to remove a business record as a side effect.

Reference: [`wreath.series`](../reference/series.md), and
[`wreath.temporal`](../reference/temporal.md) for the bucket vocabulary.
