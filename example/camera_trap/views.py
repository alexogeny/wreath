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

**The late-SD-card story, and why sealing takes the zone into the declaration.**
A card collected on the 20th carries images from the 1st, so a bucket that looked
finished acquires rows weeks later. `sealed_activity` below is how that is
*reported* rather than discovered in a spreadsheet: past the lateness allowance a
day stops being recomputed and becomes a stored answer, and a card arriving after
that records a correction beside the settled value instead of rewriting it.

`sealed_activity` is a function where `station_activity` is a constant, and that
is the design showing through rather than an inconsistency. An open view takes
its zone per request, because a day is only a day once you say whose. A sealed
one cannot: a materialised Nairobi day cannot be re-cut into an Adelaide day
afterwards, so the zone a bucket was settled in is part of what that bucket *is*.
The zone therefore moves into the declaration, and a deployment spanning
timezones declares one sealed view per zone. That costs nothing — the stored zone
is part of the view's identity, so the two sets of rows cannot collide — but it
has to be a choice the application makes rather than a default it inherits.
"""

from __future__ import annotations

from wreath.queries import Param
from wreath.series import Aggregate, Series, avg, count
from wreath.temporal import Day, zone

from .models import Sighting

#: How long after a day ends its count can still change.
#:
#: Fourteen days because that is roughly how often somebody walks out to a
#: camera. It is a claim about *this* network's field logistics, not a framework
#: default, which is why it is declared here and not inherited: a reserve that
#: services its cameras weekly would seal sooner and see corrections less often.
CARD_COLLECTION_LATENESS = "14d"

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


def sealed_activity(timezone: str) -> Series:
    """`station_activity`, sealed in one reserve's calendar.

    A function rather than a constant because a sealed view stores its zone --
    see the module docstring. Callers pass `reserve.timezone`, and the two
    reserves' settled rows never meet: the zone is part of the view's identity,
    so a Nairobi day and an Adelaide day are different buckets of different
    views rather than two spellings of one.

    Only `sightings` is measured. `avg` over an integer column is `numeric`,
    which the driver decodes to `Decimal`, which `json.dumps` cannot serialise --
    so a settled *average* fails on the write. `camera_trap.wire.chart_json`
    works around that for the open view by rounding at the edge, but a settled
    row is stored rather than rendered and there is no edge to round at. Sealing
    the count and recomputing the confidence is the honest split until the
    encoder learns `Decimal`.

    Args:
        timezone: an IANA zone name, from the reserve that owns the station.

    Returns:
        A declaration whose sealed buckets are stored on first read and
        thereafter answered from storage.
    """
    return (
        Series(
            Sighting,
            at=Sighting.captured_at,
            bucket=Day,
            stored_in=zone(timezone),
        )
        .where(Sighting.station_id == Param("station"))
        .measure(sightings=count())
        .seal(after=CARD_COLLECTION_LATENESS)
    )
