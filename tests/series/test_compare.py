"""`compare(previous=...)`: the period before, in the same statement.

Two statements are how the periods end up misaligned by a bucket, so the whole
point of these tests is that there is exactly one — and that the shift happens
where it has to happen, on the local wall clock rather than on the instant. The
DST case is the one that separates a correct implementation from one that works
for eleven months of the year.
"""

from __future__ import annotations

import datetime

import pytest

from wreath.series import Range, Series, SeriesError, avg, count
from wreath.temporal import Day, Month, Week, Year, zone

from .conftest import Trek, utc

#: New Zealand leaves daylight saving at 3am on the first Sunday in April, so
#: 5 April 2026 is a twenty-five hour day on that wall clock.
AUCKLAND = "Pacific/Auckland"


def local(year, month, day, hour=0, name=AUCKLAND):
    return datetime.datetime(year, month, day, hour, tzinfo=zone(name))


def series(bucket=Day, **kwargs):
    return Series(Trek, at=Trek.started_at, bucket=bucket, **kwargs)


async def run(view, session, database, rows, **kwargs):
    database.connection.responses.clear()
    database.connection.script("spine", rows)
    kwargs.setdefault("range", Range(utc(2026, 1, 1), utc(2026, 2, 1)))
    kwargs.setdefault("zone", "UTC")
    return await view.run(session, **kwargs)


def sql_of(database):
    return database.connection.calls[-1][0]


class TestOneStatement:
    async def test_the_comparison_is_not_a_second_query(self, session, database):
        """Two statements are how the periods end up misaligned by a bucket.

        Anyone can run the query twice; the alignment is the whole feature, and
        it only holds if one statement computes both spines from one pair of
        bounds by one rule.
        """
        view = series(bucket=Month).measure(n=count()).compare(previous=Year)
        await run(view, session, database, [])
        assert len(database.connection.calls) == 1

    async def test_the_spine_gains_an_arm_rather_than_a_second_spine(
        self, session, database
    ):
        view = series(bucket=Month).measure(n=count()).compare(previous=Year)
        await run(view, session, database, [])
        sql = sql_of(database)
        spine = sql[sql.index('"spine"') :]
        assert spine.count("generate_series") == 2, "one arm per period"
        assert "UNION ALL" in spine
        assert "'current'::text" in spine and "'previous'::text" in spine

    async def test_the_join_carries_the_discriminator(self, session, database):
        """Without it a bucket present in both arms picks up both aggregates."""
        view = series(bucket=Month).measure(n=count()).compare(previous=Year)
        await run(view, session, database, [])
        sql = sql_of(database)
        assert '"a"."period" = "s"."period"' in sql

    async def test_both_periods_reach_the_aggregate(self, session, database):
        """One scan over the union of the windows, not one scan per period."""
        view = series(bucket=Month).measure(n=count()).compare(previous=Year)
        await run(view, session, database, [])
        agg = sql_of(database)
        agg = agg[agg.index('"agg"') : agg.index('"spine"')]
        assert " OR " in agg, "the window covers both periods"
        assert "CASE WHEN" in agg and "'previous'" in agg, "each row is tagged"


class TestTheShiftHappensOnTheWallClock:
    async def test_the_comparison_spine_shifts_local_bounds_before_truncating(
        self, session, database
    ):
        """`- interval '1 month'` lands on the naive local value, not the instant.

        Applied to a `timestamptz` the interval is exact arithmetic and "the
        same day last month" drifts; applied to a naive local timestamp it is
        calendar arithmetic and lands on the same day number.
        """
        view = series(bucket=Day).measure(n=count()).compare(previous=Month)
        await run(view, session, database, [])
        sql = sql_of(database)
        arm = sql[sql.index("UNION ALL") :]
        # The shift sits inside the parentheses that close the `AT TIME ZONE`,
        # so it is applied to the local reading rather than to the bound value.
        assert "AT TIME ZONE" in arm.split("- interval '1 month'")[0]
        assert "date_trunc('day', ((" in arm

    async def test_the_filter_is_shifted_by_the_same_rule_as_the_spine(
        self, session, database
    ):
        """A window and a spine that disagree by a bucket is the whole bug.

        They agree here because one function renders both from the same two
        values — there is no second author to drift from.
        """
        view = series(bucket=Day).measure(n=count()).compare(previous=Month)
        await run(view, session, database, [])
        sql = sql_of(database)
        agg = sql[sql.index('"agg"') : sql.index('"spine"')]
        assert agg.count("- interval '1 month'") == 2, "both shifted window bounds"
        assert "AT TIME ZONE" in agg.split("- interval '1 month'")[0]


class TestOverlapIsRefused:
    async def test_a_shift_shorter_than_the_range_refuses(self, session, database):
        """Rows in the overlap would belong to both periods.

        Counting them twice inflates the comparison and counting them once drops
        them from a side; there is no third option, so this refuses rather than
        picking one silently.
        """
        view = series().measure(n=count()).compare(previous=Week)
        with pytest.raises(SeriesError, match="overlaps the range"):
            await run(view, session, database, [], range=Range(utc(2026, 1, 1), utc(2026, 2, 1)))

    async def test_a_shift_equal_to_the_range_is_allowed(self, session, database):
        """The previous period ending exactly where this one starts is the
        ordinary week-over-week case, and half-open means they do not touch."""
        view = series().measure(n=count()).compare(previous=Week)
        await run(view, session, database, [], range=Range(utc(2026, 1, 1), utc(2026, 1, 8)))
        assert "UNION ALL" in sql_of(database)

    async def test_the_refusal_names_the_period_and_the_way_out(self, session, database):
        view = series().measure(n=count()).compare(previous=Week)
        with pytest.raises(SeriesError) as caught:
            await run(view, session, database, [], range=Range(utc(2026, 1, 1), utc(2026, 3, 1)))
        message = str(caught.value)
        assert "week" in message
        assert "longer period" in message and "narrow the range" in message

    async def test_a_twenty_five_hour_day_does_not_trip_the_guard(
        self, session, database
    ):
        """The day New Zealand leaves daylight saving is 25 hours long.

        Shifting the *instant* back by 24 hours lands an hour inside the range
        and would refuse a perfectly ordinary day-over-day comparison. Shifting
        the *wall clock* back by one day lands exactly on the start, which is
        what half-open means by "does not overlap". The trap is that the naive
        form is right on every day but this one.
        """
        view = series().measure(n=count()).compare(previous=Day)
        await run(
            view, session, database, [],
            range=Range(local(2026, 4, 5), local(2026, 4, 6)),
            zone=AUCKLAND,
        )
        assert "UNION ALL" in sql_of(database)

    async def test_a_month_end_shift_clamps_rather_than_overflowing(
        self, session, database
    ):
        """31 March minus one month is 28 February, not an invalid date.

        `interval '1 month'` clamps, so the guard has to as well or a range
        ending on the 31st raises `ValueError` out of the declaration instead of
        answering the question.
        """
        view = series(bucket=Day).measure(n=count()).compare(previous=Month)
        await run(
            view, session, database, [],
            range=Range(utc(2026, 3, 30), utc(2026, 3, 31)),
        )
        assert "UNION ALL" in sql_of(database)


class TestSurvivors:
    async def test_they_are_ranked_over_the_primary_period_only(
        self, session, database
    ):
        """"The top seven paddocks this month, and what those seven did last
        month" keeps a legend that means one thing. Ranking across both periods
        would let a series that has since gone to zero hold a slot."""
        view = (
            series(bucket=Day)
            .measure(n=count())
            .by(Trek.paddock_id, top=3)
            .compare(previous=Month)
        )
        await run(view, session, database, [], range=Range(utc(2026, 1, 1), utc(2026, 1, 8)))
        sql = sql_of(database)
        survivors = sql[sql.index('"survivors"') : sql.index('"agg"')]
        assert "interval" not in survivors, "the primary window only, unshifted"


class TestTheEnvelope:
    async def test_each_period_keeps_its_own_bucket_run(self, session, database):
        """February against March is 28 buckets against 31.

        Padding the shorter one to match would invent data, and lining them up
        by index is a decision for whoever draws the chart.
        """
        view = series().measure(n=count()).compare(previous=Month)
        rows = [
            (utc(2026, 3, 1), "current", 5),
            (utc(2026, 3, 2), "current", 7),
            (utc(2026, 3, 3), "current", 1),
            (utc(2026, 2, 1), "previous", 4),
            (utc(2026, 2, 2), "previous", 9),
        ]
        result = await run(
            view, session, database, rows,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 4)),
        )
        assert len(result.buckets) == 3
        assert result.comparison is not None
        assert len(result.comparison.buckets) == 2
        assert result.comparison.previous == "month"
        assert result.series[0].values == (5, 7, 1)
        assert result.comparison.series[0].values == (4, 9)

    async def test_a_period_that_matched_nothing_is_present_and_empty(
        self, session, database
    ):
        """An absent comparison and an empty one are different answers, and a
        caller should not have to tell them apart by a missing key."""
        view = series().measure(n=count()).compare(previous=Month)
        rows = [(utc(2026, 3, 1), "current", 5)]
        result = await run(
            view, session, database, rows,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
        )
        assert result.comparison is not None
        assert result.comparison.buckets == ()
        assert result.comparison.series == ()

    async def test_fill_applies_to_the_comparison_too(self, session, database):
        """A quiet day last month is still a zero for a count and still a gap
        for an average — the rule does not change because the period did."""
        view = series().measure(n=count(), pace=avg(Trek.distance_km)).compare(
            previous=Month
        )
        rows = [
            (utc(2026, 3, 1), "current", 2, 4.0),
            (utc(2026, 2, 1), "previous", 3, 6.0),
            (utc(2026, 2, 2), "previous", None, None),
        ]
        result = await run(
            view, session, database, rows,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
        )
        previous = {item.measure: item for item in result.comparison.series}
        assert previous["n"].values == (3, 0), "a count fills with zero"
        assert previous["pace"].values == (6.0, None), "an average stays undefined"

    async def test_grouped_series_split_by_period(self, session, database):
        view = (
            series()
            .measure(n=count())
            .by(Trek.paddock_id, top=2)
            .compare(previous=Month)
        )
        rows = [
            (utc(2026, 3, 1), "current", 1, False, 5),
            (utc(2026, 3, 1), "current", 2, False, 3),
            (utc(2026, 2, 1), "previous", 1, False, 8),
            (utc(2026, 2, 1), "previous", 2, False, 2),
        ]
        result = await run(
            view, session, database, rows,
            range=Range(utc(2026, 3, 1), utc(2026, 3, 2)),
        )
        assert {item.key for item in result.series} == {1, 2}
        assert {item.key for item in result.comparison.series} == {1, 2}
        assert {item.values for item in result.comparison.series} == {(8,), (2,)}

    async def test_a_view_that_does_not_compare_has_no_comparison(
        self, session, database
    ):
        """A caller who never compares should never have to check for it."""
        view = series().measure(n=count())
        result = await run(view, session, database, [(utc(2026, 1, 1), 1)])
        assert result.comparison is None


class TestDeclaration:
    def test_it_takes_a_bucket_not_a_duration(self):
        """The useful comparisons are calendar ones, and no fixed number of
        hours expresses "the same days last month"."""
        with pytest.raises(SeriesError, match="takes a bucket"):
            series().measure(n=count()).compare(previous="1 month")

    def test_declaring_it_twice_refuses(self):
        view = series().measure(n=count()).compare(previous=Month)
        with pytest.raises(SeriesError, match="already declared"):
            view.compare(previous=Year)

    def test_it_survives_further_declaration(self):
        """`compare()` before `by()` and after must mean the same thing."""
        first = series().measure(n=count()).compare(previous=Month).by(Trek.paddock_id)
        second = series().measure(n=count()).by(Trek.paddock_id).compare(previous=Month)
        assert first._compare is second._compare is Month
