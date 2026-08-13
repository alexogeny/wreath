"""The bucket vocabulary: truncation, stepping, and the two days a year it matters.

Every interesting case here is a zone whose offset changes. Bucketing is only
hard because "a day" is a calendar day on somebody's wall clock rather than
86400 seconds, and the whole point of putting this in ``temporal`` is that the
Python side and the SQL side cannot then disagree about which.
"""

from __future__ import annotations

import datetime

import pytest

from wreath.temporal import (
    BUCKETS,
    Bucket,
    Day,
    Hour,
    Instant,
    Minute,
    Month,
    Quarter,
    TemporalError,
    Week,
    Year,
    bucket,
    spine,
    spine_length,
    spine_lengths,
    zone,
)

AUCKLAND = zone("Pacific/Auckland")
LONDON = zone("Europe/London")
UTC = datetime.UTC


def at(year, month, day, hour=0, minute=0, tz=UTC):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=tz)


class TestFloor:
    def test_a_day_is_local_midnight_not_utc_midnight(self):
        # 2026-03-01 10:00 UTC is 2026-03-01 23:00 in Auckland, so the
        # Auckland day began 13 hours before the UTC one.
        moment = at(2026, 3, 1, 10)
        assert Day.floor(moment, AUCKLAND) == Instant.of(at(2026, 2, 28, 11)), (
            "the day containing this moment starts at Auckland midnight"
        )

    def test_each_unit_truncates_to_its_own_boundary(self):
        moment = at(2026, 5, 14, 15, 47)
        assert Minute.floor(moment, UTC) == Instant.of(at(2026, 5, 14, 15, 47))
        assert Hour.floor(moment, UTC) == Instant.of(at(2026, 5, 14, 15))
        assert Day.floor(moment, UTC) == Instant.of(at(2026, 5, 14))
        # 2026-05-14 is a Thursday; PostgreSQL's week starts on Monday.
        assert Week.floor(moment, UTC) == Instant.of(at(2026, 5, 11))
        assert Month.floor(moment, UTC) == Instant.of(at(2026, 5, 1))
        assert Quarter.floor(moment, UTC) == Instant.of(at(2026, 4, 1))
        assert Year.floor(moment, UTC) == Instant.of(at(2026, 1, 1))

    @pytest.mark.parametrize(
        ("month", "expected"),
        [(1, 1), (2, 1), (3, 1), (4, 4), (6, 4), (7, 7), (9, 7), (10, 10), (12, 10)],
    )
    def test_every_month_lands_in_the_right_quarter(self, month, expected):
        assert Quarter.floor(at(2026, month, 15), UTC).month == expected

    def test_a_monday_is_its_own_week(self):
        monday = at(2026, 5, 11)
        assert Week.floor(monday, UTC) == Instant.of(monday)

    def test_flooring_is_idempotent(self):
        once = Day.floor(at(2026, 3, 1, 10), AUCKLAND)
        assert Day.floor(once, AUCKLAND) == once


def elapsed(start, end):
    """True elapsed time between two instants.

    Both operands are converted to UTC first, because subtracting two aware
    datetimes that share a ``tzinfo`` object gives the *naive* difference --
    CPython ignores a common tzinfo and subtracts the wall clocks. That
    shortcut is only valid for a fixed-offset zone, so measuring a DST day
    with a bare ``end - start`` reports 24 hours for a day that was 23 or 25.
    """
    return end.astimezone(UTC) - start.astimezone(UTC)


class TestEndOf:
    def test_a_day_that_gains_an_hour_is_twenty_five_hours_long(self):
        # Auckland leaves daylight saving on 2026-04-05: at 03:00 NZDT the
        # clock goes back to 02:00 NZST, so that local day runs 25 hours.
        moment = at(2026, 4, 5, 6)
        assert elapsed(
            Day.floor(moment, AUCKLAND), Day.end_of(moment, AUCKLAND)
        ) == datetime.timedelta(hours=25)

    def test_a_day_that_loses_an_hour_is_twenty_three_hours_long(self):
        # ... and enters it on 2026-09-27, when 02:00 becomes 03:00.
        moment = at(2026, 9, 27, 6)
        assert elapsed(
            Day.floor(moment, AUCKLAND), Day.end_of(moment, AUCKLAND)
        ) == datetime.timedelta(hours=23)

    def test_an_ordinary_day_is_twenty_four_hours(self):
        moment = at(2026, 6, 10, 6)
        assert elapsed(
            Day.floor(moment, AUCKLAND), Day.end_of(moment, AUCKLAND)
        ) == datetime.timedelta(days=1)

    def test_a_dst_day_still_advances_the_calendar_by_exactly_one_day(self):
        """The wall clock moves one day even though the elapsed time does not.

        This is the property `generate_series` over naive local timestamps has
        and `generate_series` over `timestamptz` does not, which is why the
        spine is generated in local time and converted back afterwards.
        """
        moment = at(2026, 4, 5, 6)
        start = Day.floor(moment, AUCKLAND)
        end = Day.end_of(moment, AUCKLAND)
        assert (end.day - start.day, end.hour, end.minute) == (1, 0, 0)

    def test_a_month_is_a_calendar_month_not_thirty_days(self):
        assert Month.end_of(at(2026, 2, 14), UTC) == Instant.of(at(2026, 3, 1))
        assert Month.end_of(at(2026, 1, 31), UTC) == Instant.of(at(2026, 2, 1))

    def test_december_rolls_the_year(self):
        assert Month.end_of(at(2026, 12, 9), UTC) == Instant.of(at(2027, 1, 1))
        assert Quarter.end_of(at(2026, 11, 9), UTC) == Instant.of(at(2027, 1, 1))
        assert Year.end_of(at(2026, 6, 9), UTC) == Instant.of(at(2027, 1, 1))


class TestSpine:
    def test_it_is_half_open_and_materializes_instants(self):
        assert spine(at(2026, 3, 1, 12), at(2026, 3, 4), bucket=Day, in_zone=UTC) == (
            Instant.of(at(2026, 3, 1)),
            Instant.of(at(2026, 3, 2)),
            Instant.of(at(2026, 3, 3)),
        )

    def test_it_walks_the_local_clock_across_both_dst_directions(self):
        buckets = spine(at(2026, 4, 3), at(2026, 4, 8), bucket=Day, in_zone=AUCKLAND)
        gaps = [
            elapsed(left, right)
            for left, right in zip(buckets, buckets[1:], strict=False)
        ]
        assert datetime.timedelta(hours=25) in gaps

    def test_an_empty_range_is_an_empty_spine(self):
        moment = at(2026, 3, 1)
        assert spine(moment, moment, bucket=Day, in_zone=UTC) == ()

    @pytest.mark.parametrize("unit", [Minute, Hour, Day, Week, Month, Quarter, Year])
    @pytest.mark.parametrize("timezone", [UTC, AUCKLAND, LONDON])
    def test_length_agrees_without_materializing_the_spine(self, unit, timezone):
        start = at(2025, 9, 20, 7, 17)
        end = at(2027, 4, 20, 18, 41)
        assert spine_length(start, end, bucket=unit, in_zone=timezone) == len(
            spine(start, end, bucket=unit, in_zone=timezone)
        )

    def test_several_lengths_share_the_range_without_changing_answers(self):
        start = at(2025, 9, 20, 7, 17)
        end = at(2027, 4, 20, 18, 41)
        units = (Hour, Day, Week, Month, Quarter, Year)
        assert spine_lengths(
            start, end, buckets=units, in_zone=AUCKLAND
        ) == tuple(
            spine_length(start, end, bucket=unit, in_zone=AUCKLAND)
            for unit in units
        )

    def test_the_end_of_one_bucket_is_the_start_of_the_next(self):
        """Half-open, so these have to be the same instant and not merely close."""
        for unit in (Minute, Hour, Day, Week, Month, Quarter, Year):
            moment = at(2026, 4, 5, 6)
            assert unit.end_of(moment, AUCKLAND) == unit.floor(
                unit.end_of(moment, AUCKLAND), AUCKLAND
            ), unit.name


class TestAmbiguousAndMissingLocalTimes:
    def test_an_ambiguous_local_hour_resolves_to_the_later_of_its_two_instants(self):
        """London repeats 01:00-02:00 on 2026-10-25; floor takes the later.

        Not a preference -- it is what `timestamp AT TIME ZONE zone` does, so
        `floor` and `date_trunc` name the same instant. Measured against a live
        server in `tests/postgres/test_series_integration.py`; the Python check
        here is the fast mirror of it.
        """
        # 00:30 UTC on that date is 01:30 BST, the *first* pass through 01:30 --
        # but its bucket start is the *second* 01:00, an hour later.
        floored = Hour.floor(at(2026, 10, 25, 0, 30), LONDON)
        assert floored.utcoffset() == datetime.timedelta(0), "the post-transition offset"
        assert floored.astimezone(UTC) == Instant.of(at(2026, 10, 25, 1))

    def test_an_ambiguous_result_must_be_converted_before_it_is_compared(self):
        """PEP 495: a datetime inside a fold compares unequal across zones.

        Not a defect in :meth:`Bucket.floor` -- it is how CPython keeps
        comparison transitive when a local time names two instants. It is worth
        pinning because the failure is silent and asymmetric: the same
        expression is true in June and false on one day in October, so a test
        written the naive way passes all year and then does not.
        """
        ambiguous = Hour.floor(at(2026, 10, 25, 0, 30), LONDON)
        same_instant = Instant.of(at(2026, 10, 25, 1))
        assert ambiguous != same_instant
        assert ambiguous.astimezone(UTC) == same_instant

        unambiguous = Hour.floor(at(2026, 6, 25, 0, 30), LONDON)
        assert unambiguous == Instant.of(at(2026, 6, 25, 0)), "ordinary days compare fine"

    def test_a_local_midnight_that_a_spring_forward_skipped_still_answers(self):
        """Some zones jump *at* midnight, so local midnight does not exist.

        Cuba moves 00:00 to 01:00 on 2026-03-08. There is no correct instant for
        that local midnight, and the useful behaviour is a defined one rather
        than a raise -- a chart should not fail to draw one day a year. The
        answer is the pre-transition offset, which is what zoneinfo gives.
        """
        havana = zone("America/Havana")
        floored = Day.floor(at(2026, 3, 8, 12), havana)
        assert floored.tzinfo is havana
        # Whatever instant it maps to, the day it belongs to is still the 8th.
        assert floored.astimezone(havana).day == 8


class TestVocabulary:
    def test_the_sql_fragments_come_from_the_table_not_from_a_caller(self):
        for name, item in BUCKETS.items():
            assert item.name == name
            assert item.trunc == name
            assert " " in item.step  # "1 day", "3 months"

    def test_a_bucket_is_looked_up_by_name_never_trusted(self):
        assert bucket("day") is Day
        assert bucket(Day) is Day
        with pytest.raises(TemporalError, match="unknown bucket"):
            bucket("fortnight")
        with pytest.raises(TemporalError, match="unknown bucket"):
            bucket("day'; DROP TABLE treks; --")

    def test_calendar_units_carry_months_and_fixed_units_carry_a_delta(self):
        assert (Month.months, Quarter.months, Year.months) == (1, 3, 12)
        for unit in (Minute, Hour, Day, Week):
            assert unit.months == 0
            assert unit.delta is not None

    def test_a_bucket_is_immutable(self):
        with pytest.raises(AttributeError):
            Day.step = "1 year"

    def test_it_reads_as_its_name(self):
        assert repr(Day) == "<Bucket day>"
        assert isinstance(Day, Bucket)
