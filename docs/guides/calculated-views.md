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
    return await activity.run(
        session,
        herd=herd_id,
        range=Range(start, end),
        zone=request.zone,
    )
```

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
