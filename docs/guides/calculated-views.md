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
async def herd_activity(request, herd_id: int, session: Session):
    result = await activity.run(
        session,
        herd=herd_id,
        range=Range(start, end),
        zone=request.zone,
    )
    return result.as_dict()
```

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

result = await activity.run(session, range=Range(start, end), zone=request.zone)
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

## What is not built yet

The declaration surface names four methods that a later stage fills in:
`.seal()`, `.retain()`, `.archive()` and `.drop()`. They exist today and they
**refuse by name** rather than quietly doing nothing, because a declaration that
read as though retention were configured while nothing enforced it would be worse
than one that failed. Nothing in this module deletes anything, and `.drop()` will
stay opt-in when it arrives — a declaration written to make a chart fast must not
be able to remove a business record as a side effect.

Reference: [`wreath.series`](../reference/series.md), and
[`wreath.temporal`](../reference/temporal.md) for the bucket vocabulary.
