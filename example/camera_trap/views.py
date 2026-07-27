"""The two charts a reserve manager actually asks for.

A camera-trap network produces one question more often than any other: *how
much moved past this station, and when*. That is a time series. The second
question is *what moved past it*, which has no time axis at all — it is a
ranking. Wreath spells those as two different declarations on purpose, and the
difference is not cosmetic: a series guarantees every bucket in the range
exists, and a ranking guarantees nothing is quietly folded away.

**Why the timezone is a per-request argument and not a constant.** "How much
moved last night" is asked per reserve, and *night* is a local idea. Olkiramatian
is `Africa/Nairobi`; Nullarbor is `Australia/Adelaide`, which is half an hour off
the hour and observes daylight saving. Bucketing both in UTC would put the same
question's answer in different places for the two of them, and a reader comparing
reserves would be comparing different wall clocks without being told. So the
handler passes `zone=reserve.timezone` and the framework cuts the days in the
reader's own calendar.

**What is deliberately not here: `seal()`.** The design for this stage is the
late-SD-card story — a card collected on the 20th carries images from the 1st, so
a bucket that looked settled acquires rows weeks later, and sealing plus
corrections is how that is supposed to be reported rather than discovered in a
spreadsheet. That story cannot be told against a real PostgreSQL today:
`wreath._series.settle.insert_settled` binds the measures as a Python `dict` into
a `jsonb` parameter, and the driver's parameter-type inference has no `dict`
case, so the first bucket that seals raises
``TypeError: unsupported PostgreSQL value type: dict`` on the write. The example
does not paper over that with a smaller claim — the declarations below are honest
about computing every read, and the sealing story lands when the write path does.
"""

from __future__ import annotations

from wreath.queries import Param
from wreath.series import Aggregate, Series, avg, count
from wreath.temporal import Day

from .models import Sighting

#: Sightings per day at one station, with the mean identification confidence
#: beside it.
#:
#: Two measures rather than two views because they are read together and share a
#: scan: a chart showing activity without confidence invites the reader to treat
#: a spike of low-confidence night-time triggers as a spike of animals.
#:
#: The per-measure fill is the reason this is a `Series` and not a `GROUP BY`.
#: A day with no sightings is `sightings=0` and `mean_confidence=None`, because
#: zero animals is a fact and the average confidence of nothing is not a number.
#: A hand-written aggregate returns no row at all for that day, and every caller
#: then reinvents the same interpolation slightly differently.
station_activity = (
    Series(Sighting, at=Sighting.captured_at, bucket=Day)
    .where(Sighting.station_id == Param("station"))
    .measure(
        sightings=count(),
        mean_confidence=avg(Sighting.confidence, unit="%"),
    )
)

#: Which species a station saw, most-seen first. No time axis: this is the
#: ranking behind a bar chart, and its whole job is to be complete.
#:
#: `Aggregate` rather than `Series.by()` on purpose. `by()` folds everything past
#: its ceiling into an `other` remainder, which is right for a part-to-whole
#: chart because it preserves the total. Here the bars *are* the answer, and a
#: silently folded tail would mean a species a ranger is looking for had been
#: merged into a bucket labelled "other" — so a result past the ceiling refuses
#: rather than drawing something quietly wrong.
station_species = (
    Aggregate(Sighting)
    .where(Sighting.station_id == Param("station"))
    .measure(sightings=count())
    .by(Sighting.species_id)
)
