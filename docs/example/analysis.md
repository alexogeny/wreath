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

## What this stage does not do yet

The design for this stage is the late-SD-card story: a card collected on the 20th
carries images from the 1st, so a bucket that looked finished acquires rows weeks
later. Sealing plus corrections is how that is meant to be *reported* rather than
discovered in a spreadsheet three weeks on.

**That story is blocked on a driver defect, and the example says so rather than
telling a smaller story as though it were the whole one.**
`wreath._series.settle.insert_settled` binds a bucket's measures as a Python
`dict` into a `jsonb` parameter. The driver infers a parameter's type from the
value it is given and has no `dict` case, so the first bucket that seals raises
`TypeError: unsupported PostgreSQL value type: dict` on the write. No sealed
series has ever stored a bucket against a real PostgreSQL.

`test_sealing_is_blocked_on_a_driver_defect` pins that. When the driver learns to
bind a mapping, that test fails — and the failure is the reminder to come back
and write this section properly.

A second, smaller one is worked around rather than blocked: `avg()` over an
integer column returns a `decimal.Decimal`, which the JSON encoder cannot
serialise, so a `Series` carrying an average cannot be returned from a handler
untouched. `camera_trap.wire.chart_json` rounds it, and explains why the
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
  `Aggregate` surface, including the sealing this page cannot yet demonstrate.
