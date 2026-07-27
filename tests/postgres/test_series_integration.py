"""Live-PostgreSQL checks for the two temporal claims a fake cannot settle.

Skipped unless ``WREATH_TEST_POSTGRES_DSN`` points at a throwaway database. The
fake-driver suite in ``tests/series/`` proves the statement's shape and the
envelope's rules; these prove the things only a real ``date_trunc`` and a real
``generate_series`` can:

* **Python's bucket arithmetic agrees with PostgreSQL's.** ``Bucket.floor`` is
  documented as the mirror of ``date_trunc(unit, t AT TIME ZONE zone)``, and
  until this runs that is a claim reasoned from the documentation rather than a
  measured fact. Sealing will depend on the two agreeing, so a drift here is a
  bucket that settles at the wrong moment.
* **The spine steps a calendar day, not 86400 seconds.** This is the DST bug the
  whole ordering in the design exists to prevent, and it manifests twice a year,
  in one bucket, in a way nobody traces back to the chart.

Both are asserted against a zone with a large offset and a southern-hemisphere
transition (Auckland), because a bug that cancels out in UTC or in Europe shows
up there.
"""

from __future__ import annotations

import datetime
import os

import pytest

from wreath._series.settle import schema_sql, watermark
from wreath.postgres import Database
from wreath.temporal import Day, Hour, Instant, Month, Week, zone

pytestmark = pytest.mark.skipif(
    not os.environ.get("WREATH_TEST_POSTGRES_DSN"),
    reason="set WREATH_TEST_POSTGRES_DSN to run live calculated-view integration tests",
)

AUCKLAND = "Pacific/Auckland"


@pytest.fixture
async def database():
    dsn = os.environ["WREATH_TEST_POSTGRES_DSN"]
    db = Database("main", dsn, pools={"read": {"min_size": 1, "max_size": 2}})
    await db.start()
    try:
        yield db
    finally:
        await db.stop()


async def fetchval(database, sql, *args):
    connection = await database.acquire("read")
    try:
        return await connection.fetchval(sql, *args)
    finally:
        await database.release("read", connection)


#: Moments chosen to straddle both Auckland transitions in 2026 -- DST ends on
#: 5 April (the 25-hour day) and begins on 27 September (the 23-hour day) --
#: plus ordinary days either side, so a rule that only holds away from a
#: boundary fails here rather than in production.
MOMENTS = [
    datetime.datetime(2026, 4, 4, 12, tzinfo=datetime.UTC),
    datetime.datetime(2026, 4, 4, 13, tzinfo=datetime.UTC),   # 2026-04-05 02:00 NZDT
    datetime.datetime(2026, 4, 4, 14, tzinfo=datetime.UTC),   # after the clock went back
    datetime.datetime(2026, 4, 5, 6, tzinfo=datetime.UTC),
    datetime.datetime(2026, 9, 26, 13, tzinfo=datetime.UTC),
    datetime.datetime(2026, 9, 26, 14, tzinfo=datetime.UTC),  # into the gap
    datetime.datetime(2026, 9, 27, 6, tzinfo=datetime.UTC),
    datetime.datetime(2026, 6, 15, 9, tzinfo=datetime.UTC),
]


class TestFloorMatchesDateTrunc:
    """The claim ``Bucket.floor`` makes about itself, checked against the source."""

    @pytest.mark.parametrize("unit", [Hour, Day, Week, Month])
    @pytest.mark.parametrize("moment", MOMENTS)
    async def test_python_and_postgres_agree_on_the_boundary(
        self, database, unit, moment
    ):
        # `date_trunc` on the wall clock, converted back the same way the spine
        # converts -- so this compares the whole round trip, not just the
        # truncation.
        theirs = await fetchval(
            database,
            f"SELECT date_trunc('{unit.trunc}', $1::timestamptz AT TIME ZONE $2) "
            "AT TIME ZONE $2",
            moment,
            AUCKLAND,
        )
        ours = unit.floor(moment, zone(AUCKLAND))
        assert theirs == ours.astimezone(datetime.UTC), (
            f"{unit.name} boundary for {moment.isoformat()} disagrees"
        )

    async def test_end_of_matches_one_step_of_generate_series(self, database):
        moment = datetime.datetime(2026, 4, 5, 6, tzinfo=datetime.UTC)
        theirs = await fetchval(
            database,
            "SELECT (date_trunc('day', $1::timestamptz AT TIME ZONE $2) "
            "+ interval '1 day') AT TIME ZONE $2",
            moment,
            AUCKLAND,
        )
        assert theirs == Day.end_of(moment, zone(AUCKLAND)).astimezone(datetime.UTC)


class TestTheSpineStepsACalendarDay:
    async def test_a_dst_day_is_twenty_five_hours_of_real_time(self, database):
        """Auckland leaves daylight saving on 2026-04-05: that day runs 25 hours.

        Generated over naive local timestamps and converted back, consecutive
        buckets are 25 hours apart in real time while remaining one calendar day
        apart on the wall clock. Generated over ``timestamptz`` they would be
        exactly 24 hours apart, which is the bug.
        """
        rows = await _spine(database, "2026-04-04", "2026-04-07", "day")
        gaps = [(b - a).total_seconds() / 3600 for a, b in zip(rows, rows[1:], strict=False)]
        assert 25 in gaps, f"expected a 25-hour day among {gaps}"

    async def test_the_other_transition_is_twenty_three_hours(self, database):
        rows = await _spine(database, "2026-09-26", "2026-09-29", "day")
        gaps = [(b - a).total_seconds() / 3600 for a, b in zip(rows, rows[1:], strict=False)]
        assert 23 in gaps, f"expected a 23-hour day among {gaps}"

    async def test_every_bucket_is_local_midnight(self, database):
        rows = await _spine(database, "2026-04-04", "2026-04-07", "day")
        local = [item.astimezone(zone(AUCKLAND)) for item in rows]
        assert {(item.hour, item.minute) for item in local} == {(0, 0)}

    async def test_the_upper_bound_is_exclusive(self, database):
        """A range ending exactly on a boundary stops at the previous bucket."""
        inclusive = await _spine(database, "2026-06-01", "2026-06-04", "day")
        assert len(inclusive) == 3, "1st, 2nd and 3rd -- not the 4th"


async def _spine(database, start: str, end: str, unit: str) -> list:
    """The spine exactly as ``_series.compile`` renders it."""
    connection = await database.acquire("read")
    try:
        rows = await connection.fetch(
            f"SELECT generate_series("
            f"date_trunc('{unit}', $1::timestamptz AT TIME ZONE $3), "
            f"date_trunc('{unit}', ($2::timestamptz AT TIME ZONE $3) "
            f"- interval '1 microsecond'), "
            f"interval '1 {unit}') AT TIME ZONE $3 AS b",
            datetime.datetime.fromisoformat(f"{start}T00:00:00+13:00"),
            datetime.datetime.fromisoformat(f"{end}T00:00:00+12:00"),
            AUCKLAND,
        )
        return [row[0] for row in rows]
    finally:
        await database.release("read", connection)


class TestTheComparisonShift:
    """`compare(previous=...)` shifts the local bounds, and only a real server
    can confirm what `interval '1 month'` does to a naive local timestamp."""

    async def test_a_month_shift_is_calendar_arithmetic_not_thirty_days(
        self, database
    ):
        """"The same day last month" has to land on the same day number.

        Subtracting a fixed number of days walks backwards through the calendar;
        `interval '1 month'` on a naive local timestamp does not.
        """
        moment = datetime.datetime(2026, 3, 31, 12, tzinfo=datetime.UTC)
        shifted = await fetchval(
            database,
            "SELECT (($1::timestamptz AT TIME ZONE $2) - interval '1 month') "
            "AT TIME ZONE $2",
            moment,
            AUCKLAND,
        )
        local = shifted.astimezone(zone(AUCKLAND))
        assert (local.month, local.day) == (2, 28), "clamped to February's last day"

    async def test_the_shift_preserves_the_wall_clock_across_a_transition(
        self, database
    ):
        """Shifting a local bound and converting back keeps the wall time and
        moves the instant, which is what makes a comparison period comparable.

        The other order — shifting the instant — keeps the instant's spacing and
        moves the wall time by an hour, so every bucket after a transition is
        compared against the wrong one.
        """
        # 2026-04-20 is after Auckland's April transition; one month earlier is
        # before it, so the offset differs between the two.
        moment = datetime.datetime(2026, 4, 19, 12, tzinfo=datetime.UTC)
        shifted = await fetchval(
            database,
            "SELECT (($1::timestamptz AT TIME ZONE $2) - interval '1 month') "
            "AT TIME ZONE $2",
            moment,
            AUCKLAND,
        )
        here = moment.astimezone(zone(AUCKLAND))
        there = shifted.astimezone(zone(AUCKLAND))
        assert (there.hour, there.minute) == (here.hour, here.minute)
        assert there.utcoffset() != here.utcoffset(), "the offset really did change"

    async def test_the_two_arms_may_be_different_lengths(self, database):
        """March against February is 31 buckets against 28.

        The envelope gives each period its own bucket run precisely because
        this is true; a shared run would have to invent three buckets.
        """
        current = await _spine(database, "2026-03-01", "2026-04-01", "day")
        previous = await _shifted_spine(database, "2026-03-01", "2026-04-01", "day")
        assert len(current) == 31
        assert len(previous) == 28


class TestTheMarkerBucket:
    async def test_a_marker_lands_in_the_bucket_that_contains_it(self, database):
        """The bucket travels with the event, computed by the same `date_trunc`
        in the same zone, so a marker cannot sit a column away from the bar it
        describes."""
        # 13:00 UTC on 4 April 2026 is 02:00 on the 5th in Auckland -- inside
        # the ambiguous hour, and on the far side of local midnight from UTC.
        moment = datetime.datetime(2026, 4, 4, 13, tzinfo=datetime.UTC)
        bucket = await fetchval(
            database,
            "SELECT date_trunc('day', $1::timestamptz AT TIME ZONE $2) AT TIME ZONE $2",
            moment,
            AUCKLAND,
        )
        local = bucket.astimezone(zone(AUCKLAND))
        assert (local.month, local.day, local.hour) == (4, 5, 0)
        assert bucket <= moment, "a marker never precedes its own bucket"


async def _shifted_spine(database, start: str, end: str, unit: str) -> list:
    """The comparison arm exactly as ``_series.compile`` renders it."""
    connection = await database.acquire("read")
    try:
        rows = await connection.fetch(
            f"SELECT generate_series("
            f"date_trunc('{unit}', (($1::timestamptz AT TIME ZONE $3) "
            f"- interval '1 month')), "
            f"date_trunc('{unit}', ((($2::timestamptz AT TIME ZONE $3) "
            f"- interval '1 microsecond') - interval '1 month')), "
            f"interval '1 {unit}') AT TIME ZONE $3 AS b",
            datetime.datetime.fromisoformat(f"{start}T00:00:00+13:00"),
            datetime.datetime.fromisoformat(f"{end}T00:00:00+13:00"),
            AUCKLAND,
        )
        return [row[0] for row in rows]
    finally:
        await database.release("read", connection)


# -- stage 7: sealing ---------------------------------------------------------


class TestSealedBucketBoundaries:
    """What only a real server can settle about a settled bucket.

    The arithmetic in `_series.settle` decides *which* buckets are sealed; these
    check that the boundary it picks is the boundary PostgreSQL would pick, on
    the two days a year the answer is interesting.
    """

    async def test_the_watermark_lands_on_a_boundary_date_trunc_agrees_with(
        self, database
    ):
        """A settled bucket start and a freshly computed one must be one instant.

        If they disagree even once, a settled row files itself under a bucket
        the spine will never generate, and the value silently disappears from
        every later read.
        """
        for moment in (
            datetime.datetime(2026, 4, 5, 1, 30, tzinfo=datetime.UTC),
            datetime.datetime(2026, 9, 27, 1, 30, tzinfo=datetime.UTC),
        ):
            theirs = await fetchval(
                database,
                "SELECT date_trunc('day', $1::timestamptz AT TIME ZONE $2) "
                "AT TIME ZONE $2",
                moment,
                AUCKLAND,
            )
            ours = watermark(
                Instant.of(moment), bucket=Day, zone_name=AUCKLAND, after=0
            )
            assert ours == theirs, f"python and postgres disagree at {moment}"

    async def test_the_lateness_allowance_is_elapsed_time(self, database):
        """Two hours after a 23-hour day still means two hours.

        The allowance is subtracted as a fixed offset and only the *bucket*
        boundary is calendar arithmetic. Checking against the server keeps that
        split honest across the spring-forward day.
        """
        moment = datetime.datetime(2026, 9, 27, 14, tzinfo=datetime.UTC)
        theirs = await fetchval(
            database,
            "SELECT date_trunc('day', ($1::timestamptz - interval '2 hours') "
            "AT TIME ZONE $2) AT TIME ZONE $2",
            moment,
            AUCKLAND,
        )
        ours = watermark(
            Instant.of(moment), bucket=Day, zone_name=AUCKLAND, after=7200
        )
        assert ours == theirs

    async def test_the_gap_step_lands_on_the_next_bucket_across_a_short_day(
        self, database
    ):
        """Stepping past the last settled bucket is `end_of`, not plus 24 hours.

        On a 23-hour day, adding a nominal day would start the gap an hour into
        a bucket that is already stored — recomputing part of a settled value
        and leaving a real gap unfilled.
        """
        start = await fetchval(
            database,
            "SELECT date_trunc('day', $1::timestamptz AT TIME ZONE $2) AT TIME ZONE $2",
            datetime.datetime(2026, 9, 27, 6, tzinfo=datetime.UTC),
            AUCKLAND,
        )
        theirs = await fetchval(
            database,
            "SELECT (($1::timestamptz AT TIME ZONE $2) + interval '1 day') "
            "AT TIME ZONE $2",
            start,
            AUCKLAND,
        )
        assert Day.end_of(Instant.of(start), AUCKLAND) == theirs

    async def test_the_settled_tables_apply_cleanly(self, database):
        """The DDL a migration would carry, against a real server."""
        connection = await database.acquire("write")
        try:
            await connection.execute(schema_sql(schema="wreath_series_test"))
            await connection.execute(schema_sql(schema="wreath_series_test"))
        finally:
            await database.release("write", connection)
