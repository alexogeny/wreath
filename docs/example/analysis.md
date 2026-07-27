# Charts a reserve manager asks for

A camera-trap network gets asked two questions more than any other. *How much
moved past this station, and when* — that is a time series. *What moved past it*
— that has no time axis; it is a ranking. Wreath spells them as two different
declarations, and the difference is not cosmetic.

The declarations live in `example/camera_trap/views.py`, beside the models rather
than inside a handler, because a chart is a property of the domain and not of the
route that happens to render it.

## The time series

```python
station_activity = (
    Series(Sighting, at=Sighting.captured_at, bucket=Day)
    .where(Sighting.station_id == Param("station"))
    .measure(
        sightings=count(),
        mean_confidence=avg(Sighting.confidence, unit="%"),
    )
)
```

Two measures rather than two views, because they are read together and share a
scan. A chart showing activity without confidence invites the reader to treat a
spike of low-confidence night-time triggers as a spike of animals.

Ask it for one station's month:

```bash
curl -s "$BASE/reserves/olkiramatian/stations/1/activity?since=2026-01-05&days=30" \
  -b cookies.txt | jq '{zone, bucket, days: (.buckets | length)}'
```

```json
{
  "zone": "Africa/Nairobi",
  "bucket": "day",
  "days": 30
}
```

```chart
source: example/data/station-activity.json
data: days
x: day
y: sightings
title: Sightings per local day, station 1 (Olkiramatian)
```

### Why every day is in the axis

Thirty buckets for thirty days, whether or not anything walked past. That is the
property a hand-written `GROUP BY` does not have: it returns no row at all for a
quiet Tuesday, and a renderer then draws a line straight from Monday to
Wednesday and invents activity that did not happen. Every caller reinvents the
same interpolation slightly differently, and the charts stop agreeing.

### Why the fill is per measure

An empty day is `sightings: 0` and `mean_confidence: null`.

Zero animals is a fact worth plotting. The average confidence of no
identifications is not a number, and filling it with `0` would draw a confidence
collapse on every quiet night. `count()` fills with zero, `avg()` fills with
null, and neither has to be argued about at the call site.

`tests/example/test_analysis_views.py` asserts both halves — the empty days *and*
a busy day carrying a real average — because an assertion about zeros passes
trivially if everything is empty.

### Why the timezone is a per-request argument

`zone=reserve.timezone`, not a constant.

"How much moved last night" is asked per reserve, and *night* is a local idea.
Olkiramatian is `Africa/Nairobi`; Nullarbor is `Australia/Adelaide`, half an hour
off the hour and observing daylight saving. Bucketing both in UTC would cut
Nullarbor's days at 09:30 local, put a dusk trigger in the wrong column, and be
visibly wrong to anyone who lives there and invisible to everyone else.

The bucket boundaries come back as instants, so the first bucket of a Nairobi
day is `2026-01-04T21:00:00+00:00` — midnight in Nairobi, written in UTC. A
renderer formats them in the zone the envelope names.

## The ranking

```python
station_species = (
    Aggregate(Sighting)
    .where(Sighting.station_id == Param("station"))
    .measure(sightings=count())
    .by(Sighting.species_id)
)
```

```chart
source: example/data/station-species.json
data: species
x: species
y: sightings
title: Species recorded at station 1, all time
sort: desc
limit: 12
```

`Aggregate` rather than `Series.by()`, and that is a deliberate choice rather
than a shortcut. `by()` folds everything past its ceiling into an `other`
remainder, which is right for a part-to-whole chart because it preserves the
total. Here the bars *are* the answer: a ranger looking for a species needs it
to be absent or present, not silently summed into a remainder. So a result past
the ceiling refuses rather than drawing something quietly wrong.

The response carries species *ids*, not names. The vocabulary is its own endpoint
with its own cache; repeating forty names inside every chart response would be
the same rows travelling twice, and a client that has read `/species` once holds
the labels already.

## The card that comes back late

A card collected on the 20th carries images from the 1st, so a bucket that looked
finished acquires rows weeks later. Sealing plus corrections is how that is
*reported* rather than discovered in a spreadsheet three weeks on.

Past its lateness allowance a day stops being a question and becomes an answer:
the value is stored, and the next read comes from storage rather than from the
rows it was computed from. `camera_trap.views` declares that as

```python
CARD_COLLECTION_LATENESS = "14d"
```

— roughly how often somebody walks out to a camera. It is a claim about *this*
network's field logistics, not a framework default, which is why the example
states it rather than inheriting one.

When the card finally arrives, the settled number is **not** rewritten. The
difference is recorded beside it and folded in on read, and the envelope names
the bucket that carries one — so late data looks like late data arriving rather
than like a number that changed on its own while nobody was watching.

**Sealing takes the zone into the declaration, and that is the design showing
through rather than an inconsistency.** `station_activity` is a constant that
takes its zone per request, because a day is only a day once you say whose.
`sealed_activity(timezone)` is a *function*, because a materialised Nairobi day
cannot be re-cut into an Adelaide day afterwards: the zone a bucket was settled
in is part of what that bucket is. A deployment spanning timezones therefore
declares one sealed view per zone. That costs nothing — the stored zone is part
of the view's identity, so the two sets of rows cannot collide — but it has to be
a choice the application makes rather than a default it inherits.

`sealed_activity` measures only `sightings`. Sealing an *average* would fail on
the write for the reason below, and a settled row is stored rather than rendered,
so there is no edge to round at. Sealing the count and recomputing the confidence
is the honest split until the encoder learns `Decimal`.

`test_a_card_pulled_late_records_a_correction` drives exactly that against a
seeded database: seal a day, insert a sighting for it a year later, reconcile,
and read the corrected total back.

## One defect worked around

`avg()` over an integer column returns a `decimal.Decimal`, which the JSON
encoder cannot serialise, so a `Series` carrying an average cannot be returned
from a handler untouched. `camera_trap.wire.chart_json` rounds it, and explains
why the
conversion is forced and why it belongs in an application's wire layer rather
than in a framework-wide rule — a percentage wants rounding, a money column
would want the `Decimal` kept.

That one is worth reading for how it was found: a 2,000-row fixture passed and a
4,000-row fixture did not, because with fewer rows every bucket in the sampled
window was empty and every average was `null`. A fixture too small to fill a
bucket serialises cleanly and the defect stays invisible.

## Where to go next

- [The read API](read-api.md) — the routes these two hang off, and the paging and
  authorization rules they inherit.
- [Ingest](ingest.md) — where the late data comes from.
- [Calculated views](../guides/calculated-views.md) — the full `Series` and
  `Aggregate` surface — sealing's full vocabulary, rollup tiers, and the
  `on_late="reopen"` alternative this page does not use.
