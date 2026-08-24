"""A length has a type, and a schedule knows its zone.

`wreath.temporal` already refused a naive `datetime`, because a moment without
an offset has no single meaning. A *length* had no type at all: a job lease, a
quota period, a notification digest, a store window and a rate-limit span were
five bare floats, so "how long" was documented five times and spelled five ways.
A *recurrence* was worse than untyped -- it was a five-field cron string read in
UTC, so "03:00 depot-local" was an hour wrong for half the year, in whichever
half the reader did not test in.

These tests pin the three properties that make the pair worth having:

* `Duration.of` takes a length however it was written, and `total_seconds()` is
  what every call site it replaces already wanted.
* A `Recurrence` fires on its own zone's wall clock, not on UTC's.
* The two DST days answer the way a person expects rather than the way the
  arithmetic falls out -- a local time that does not exist does not fire, and a
  local time that happens twice fires once.

The DST cases are the reason this file exists. They are also the cases a test
suite normally discovers in production, so they are written against real zones
and real 2026 transition dates rather than against a fabricated fixed offset.
"""

from __future__ import annotations

import datetime

import pytest

from wreath.temporal import (
    Duration,
    Instant,
    Recurrence,
    RecurrenceError,
    TemporalError,
    days,
    hours,
    milliseconds,
    minutes,
    seconds,
    weeks,
)

# Australia/Sydney, 2026. DST ends on the first Sunday in April (03:00 -> 02:00,
# so local 02:30 happens twice) and begins on the first Sunday in October
# (02:00 -> 03:00, so local 02:30 never happens at all).
FALL_BACK = datetime.date(2026, 4, 5)
SPRING_FORWARD = datetime.date(2026, 10, 4)
SYDNEY = "Australia/Sydney"


# --- Duration ------------------------------------------------------------------------


def test_unit_helpers_build_the_span_they_name() -> None:
    assert seconds(90).total_seconds() == 90
    assert minutes(2).total_seconds() == 120
    assert hours(1).total_seconds() == 3600
    assert days(1).total_seconds() == 86400
    assert weeks(1).total_seconds() == 604800
    assert milliseconds(250).total_seconds() == 0.25


def test_helpers_return_the_declared_type() -> None:
    # `notifications` documented `hours(1)` before anything defined it, which is
    # the clearest evidence the vocabulary was missing rather than merely thin.
    assert isinstance(hours(1), Duration)
    assert isinstance(hours(1), datetime.timedelta)


@pytest.mark.parametrize(
    "written",
    [30, 30.0, datetime.timedelta(seconds=30), Duration(seconds=30), "PT30S"],
)
def test_of_accepts_every_spelling_a_call_site_already_used(written: object) -> None:
    assert Duration.of(written).total_seconds() == 30


def test_of_returns_the_same_object_when_it_already_is_one() -> None:
    value = hours(3)
    assert Duration.of(value) is value


def test_a_bool_is_refused_rather_than_read_as_one_second() -> None:
    # True is an int in Python, so `Duration.of(True)` would otherwise be one
    # second -- which is never what a caller passing a flag by mistake meant.
    with pytest.raises(TemporalError, match="bool"):
        Duration.of(True)


def test_of_refuses_a_type_it_cannot_read() -> None:
    with pytest.raises(TemporalError, match="ISO-8601"):
        Duration.of(object())


def test_iso_round_trips_through_of() -> None:
    for value in (seconds(1), minutes(90), hours(3), days(2), milliseconds(1500)):
        assert Duration.of(value.iso()).total_seconds() == value.total_seconds()


def test_seconds_component_is_not_shadowed_by_the_helper() -> None:
    # `timedelta.seconds` is the seconds *component*, 0..86399, and code that
    # reads it means that. Shadowing it with the total would break every such
    # reader silently, so the total stays `total_seconds()`.
    value = hours(25)
    assert value.seconds == 3600
    assert value.total_seconds() == 90000


def test_arithmetic_degrades_to_timedelta_and_of_takes_it_back() -> None:
    # CPython preserves the subclass for datetime but not for timedelta. That is
    # documented rather than worked around: Duration is a declaration type.
    total = hours(1) + minutes(30)
    assert not isinstance(total, Duration)
    assert Duration.of(total).total_seconds() == 5400


def test_a_duration_still_moves_an_instant() -> None:
    start = Instant.parse("2026-08-03T00:00:00+00:00")
    assert (start + hours(3)).hour == 3


# --- Recurrence: the zone is the point -----------------------------------------------


def test_cron_defaults_to_utc_so_an_existing_expression_is_unchanged() -> None:
    recurrence = Recurrence.cron("0 3 * * *")
    assert recurrence.matches_at(Instant.parse("2026-08-03T03:00:00+00:00"))
    assert not recurrence.matches_at(Instant.parse("2026-08-03T04:00:00+00:00"))


def test_a_zoned_recurrence_fires_on_its_own_wall_clock() -> None:
    recurrence = Recurrence.cron("0 3 * * *", tz=SYDNEY)
    # 03:00 in Sydney is 17:00 UTC the previous day in August (AEST, +10).
    assert recurrence.matches_at(Instant.parse("2026-08-02T17:00:00+00:00"))
    # ... and 03:00 UTC is emphatically not the depot's 03:00.
    assert not recurrence.matches_at(Instant.parse("2026-08-03T03:00:00+00:00"))


def test_the_offset_change_does_not_move_the_local_firing_time() -> None:
    # The whole argument for carrying a zone: one declaration stays correct on
    # both sides of a transition, where a UTC expression is an hour out on one.
    recurrence = Recurrence.cron("0 3 * * *", tz=SYDNEY)
    winter = recurrence.next_after(Instant.parse("2026-08-01T00:00:00+00:00"))
    summer = recurrence.next_after(Instant.parse("2026-12-01T00:00:00+00:00"))
    assert (winter.to(SYDNEY).hour, winter.to(SYDNEY).minute) == (3, 0)
    assert (summer.to(SYDNEY).hour, summer.to(SYDNEY).minute) == (3, 0)
    # Same local time, different UTC offsets -- which is the fact a UTC cron
    # expression cannot represent.
    assert winter.utcoffset() != summer.utcoffset()


def test_next_after_keeps_only_the_identical_latest_moment() -> None:
    recurrence = Recurrence.cron("0 3 * * *", tz=SYDNEY)
    moment = Instant.parse("2026-08-01T00:00:00+00:00")
    first = recurrence.next_after(moment)

    assert recurrence.next_after(moment) is first

    equal_moment = moment.replace()
    second = recurrence.next_after(equal_moment)
    assert second == first
    assert second is not first


# --- Recurrence: the two DST days ----------------------------------------------------


def test_a_local_time_that_does_not_exist_does_not_fire() -> None:
    recurrence = Recurrence.cron("30 2 * * *", tz=SYDNEY)
    before = Instant.parse("2026-10-03T00:00:00+10:00")
    first = recurrence.next_after(before)
    assert first.to(SYDNEY).date() == datetime.date(2026, 10, 3)
    # 2026-10-04 02:30 is inside the gap, so the next occurrence is the day after.
    assert recurrence.next_after(first).to(SYDNEY).date() == datetime.date(2026, 10, 5)


def test_next_after_and_matches_at_agree_across_the_gap() -> None:
    # The invariant that keeps the two answers one answer: every instant
    # `next_after` proposes must be one `matches_at` accepts. This is what the
    # normalise-through-UTC step in `next_after` exists to preserve -- an
    # `astimezone` onto the zone a value already carries is a no-op, so without
    # it a nonexistent 02:30 reads back as 02:30 and passes its own check.
    recurrence = Recurrence.cron("30 2 * * *", tz=SYDNEY)
    moment = Instant.parse("2026-10-01T00:00:00+00:00")
    for _ in range(8):
        moment = recurrence.next_after(moment)
        assert recurrence.matches_at(moment), moment
        assert moment.to(SYDNEY).date() != SPRING_FORWARD


def test_a_local_time_that_happens_twice_fires_once() -> None:
    recurrence = Recurrence.cron("30 2 * * *", tz=SYDNEY)
    first_pass = Instant.parse("2026-04-04T15:30:00+00:00")  # 02:30 AEDT (+11)
    second_pass = Instant.parse("2026-04-04T16:30:00+00:00")  # 02:30 AEST (+10)

    assert first_pass.to(SYDNEY).date() == FALL_BACK
    assert second_pass.to(SYDNEY).date() == FALL_BACK
    # Two distinct instants, both genuinely reading 02:30 on the wall clock.
    assert first_pass != second_pass
    assert recurrence.matches_at(first_pass)
    assert recurrence.matches_at(second_pass)
    # One local minute, so a scheduler keyed on `bucket_key` enqueues once.
    assert recurrence.bucket_key(first_pass) == recurrence.bucket_key(second_pass)


def test_bucket_key_is_local_and_distinguishes_ordinary_minutes() -> None:
    recurrence = Recurrence.cron("* * * * *", tz=SYDNEY)
    one = Instant.parse("2026-08-03T00:00:00+00:00")
    assert recurrence.bucket_key(one) == "202608031000"
    assert recurrence.bucket_key(one) != recurrence.bucket_key(one + minutes(1))


# --- Recurrence: cron parsing --------------------------------------------------------


def test_cron_accepts_seven_as_sunday() -> None:
    # A form people copy straight out of a crontab; refusing it was a startup error.
    assert Recurrence.cron("0 0 * * 7").weekday == Recurrence.cron("0 0 * * 0").weekday


def test_cron_refuses_a_wrong_field_count() -> None:
    with pytest.raises(RecurrenceError, match="5 fields"):
        Recurrence.cron("0 3 * *")


def test_cron_refuses_an_out_of_range_field() -> None:
    with pytest.raises(RecurrenceError, match=r"out of range"):
        Recurrence.cron("0 99 * * *")


def test_cron_refuses_a_field_that_is_not_a_number() -> None:
    with pytest.raises(RecurrenceError, match="not a number"):
        Recurrence.cron("0 x * * *")


def test_vixie_day_semantics_are_preserved() -> None:
    # Both day fields restricted: either matching fires. This is the rule every
    # crontab implements and the one a naive rewrite gets wrong.
    recurrence = Recurrence.cron("0 0 1 * MON".replace("MON", "1"))
    first_of_month = Instant.parse("2026-07-01T00:00:00+00:00")  # a Wednesday
    a_monday = Instant.parse("2026-07-06T00:00:00+00:00")
    assert recurrence.matches_at(first_of_month)
    assert recurrence.matches_at(a_monday)


def test_an_unsatisfiable_recurrence_refuses_instead_of_looping() -> None:
    recurrence = Recurrence.cron("0 0 31 2 *")  # February 31st
    with pytest.raises(RecurrenceError, match="never happens"):
        recurrence.next_after(Instant.parse("2026-01-01T00:00:00+00:00"))


# --- Recurrence: the calendar spelling -----------------------------------------------


def test_weekdays_at_three_is_the_shape_a_calendar_ui_emits() -> None:
    recurrence = Recurrence.calendar(
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=3", tz="Europe/London"
    )
    saturday = Instant.parse("2026-08-08T00:00:00+00:00")
    following = recurrence.next_after(saturday)
    assert following.to("Europe/London").weekday() == 0  # Monday
    assert following.to("Europe/London").hour == 3


def test_a_leading_rrule_prefix_is_accepted() -> None:
    # It is how the line arrives from a calendar client.
    assert Recurrence.calendar("RRULE:FREQ=DAILY;BYHOUR=3").hour == frozenset({3})


def test_calendar_and_cron_compile_to_the_same_fields() -> None:
    # The reason the calendar form lives here rather than in a translator beside it.
    from_rrule = Recurrence.calendar("FREQ=DAILY;BYHOUR=3;BYMINUTE=30")
    from_cron = Recurrence.cron("30 3 * * *")
    assert (from_rrule.minute, from_rrule.hour) == (from_cron.minute, from_cron.hour)


def test_hourly_interval_that_divides_the_day_is_supported() -> None:
    recurrence = Recurrence.calendar("FREQ=HOURLY;INTERVAL=6")
    assert recurrence.hour == frozenset({0, 6, 12, 18})
    assert recurrence.minute == frozenset({0})


def test_an_interval_that_does_not_divide_its_unit_is_refused() -> None:
    # FREQ=HOURLY;INTERVAL=7 drifts by an hour a day. There is no set of hours
    # that means it, so approximating would put the job an hour out silently.
    with pytest.raises(RecurrenceError, match="drifts"):
        Recurrence.calendar("FREQ=HOURLY;INTERVAL=7")


@pytest.mark.parametrize("part", ["COUNT=5", "UNTIL=20261231T000000Z"])
def test_a_bounded_recurrence_is_refused(part: str) -> None:
    # A recurrence that stops needs somewhere to record that it has, and this
    # type has nowhere.
    with pytest.raises(RecurrenceError, match="bounds a recurrence"):
        Recurrence.calendar(f"FREQ=DAILY;{part}")


@pytest.mark.parametrize("part", ["BYSETPOS=-1", "BYWEEKNO=3", "BYYEARDAY=100"])
def test_selectors_are_refused_by_name(part: str) -> None:
    with pytest.raises(RecurrenceError, match="selects from a generated set"):
        Recurrence.calendar(f"FREQ=MONTHLY;BYMONTHDAY=1;{part}")


def test_an_ordinal_byday_is_refused_rather_than_read_as_a_weekday() -> None:
    # "2MO" is the second Monday of the period, not "Monday". Reading it as the
    # latter fires on four days a month instead of one.
    with pytest.raises(RecurrenceError, match="ordinal"):
        Recurrence.calendar("FREQ=MONTHLY;BYDAY=2MO")


def test_weekly_without_byday_is_refused() -> None:
    with pytest.raises(RecurrenceError, match="needs BYDAY"):
        Recurrence.calendar("FREQ=WEEKLY")


def test_monthly_without_a_day_field_is_refused() -> None:
    with pytest.raises(RecurrenceError, match="needs BYMONTHDAY or BYDAY"):
        Recurrence.calendar("FREQ=MONTHLY")


def test_an_unsupported_freq_names_the_ones_that_work() -> None:
    with pytest.raises(RecurrenceError, match="MINUTELY, HOURLY, DAILY"):
        Recurrence.calendar("FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1")


def test_an_unknown_part_is_refused_rather_than_ignored() -> None:
    with pytest.raises(RecurrenceError, match="not supported"):
        Recurrence.calendar("FREQ=DAILY;RSCALE=CHINESE")


def test_recurrence_error_is_a_temporal_error() -> None:
    # A caller already catching this module's error does not learn a second name.
    assert issubclass(RecurrenceError, TemporalError)


# --- the refusals `wreath mutant` found nothing watching ------------------------------
#
# Every test below was written because `wreath mutant` removed the control it
# covers and no test objected. They are the difference between "the parser
# accepts what it should" (which the tests above prove) and "the parser refuses
# what it must", which nothing was checking.


@pytest.mark.parametrize("given", [0 - 0, None, ["0 3 * * *"], b"0 3 * * *"])
def test_cron_refuses_something_that_is_not_a_string(given: object) -> None:
    with pytest.raises(RecurrenceError, match="expected a cron expression"):
        Recurrence.cron(given)  # type: ignore[arg-type]


@pytest.mark.parametrize("given", [0, None, ["FREQ=DAILY"], b"FREQ=DAILY"])
def test_the_calendar_form_refuses_something_that_is_not_a_string(given: object) -> None:
    with pytest.raises(RecurrenceError, match="expected a calendar recurrence string"):
        Recurrence.calendar(given)  # type: ignore[arg-type]


def test_a_part_without_a_value_is_refused() -> None:
    with pytest.raises(RecurrenceError, match="not NAME=VALUE"):
        Recurrence.calendar("FREQ=DAILY;BYHOUR")


@pytest.mark.parametrize("interval", ["0", "-1"])
def test_an_interval_below_one_is_refused(interval: str) -> None:
    with pytest.raises(RecurrenceError, match="INTERVAL must be >= 1"):
        Recurrence.calendar(f"FREQ=HOURLY;INTERVAL={interval}")


def test_a_week_start_other_than_monday_is_refused() -> None:
    # Refused rather than ignored: WKST moves which week a BYDAY falls in, so
    # accepting and disregarding it fires on the wrong days without saying so.
    with pytest.raises(RecurrenceError, match="WKST is only supported as MO"):
        Recurrence.calendar("FREQ=WEEKLY;BYDAY=MO;WKST=SU")


def test_the_default_week_start_is_accepted_explicitly_or_implicitly() -> None:
    assert Recurrence.calendar("FREQ=WEEKLY;BYDAY=MO;WKST=MO").weekday == frozenset({1})
    assert Recurrence.calendar("FREQ=WEEKLY;BYDAY=MO").weekday == frozenset({1})


def test_a_recurrence_with_no_freq_is_refused() -> None:
    with pytest.raises(RecurrenceError, match="needs a FREQ"):
        Recurrence.calendar("BYHOUR=3")


def test_an_empty_byday_is_refused() -> None:
    with pytest.raises(RecurrenceError, match="BYDAY is empty"):
        Recurrence.calendar("FREQ=WEEKLY;BYDAY=")


@pytest.mark.parametrize(
    ("part", "message"),
    [
        ("BYMINUTE=60", "BYMINUTE out of range"),
        ("BYHOUR=24", "BYHOUR out of range"),
        ("BYMONTHDAY=32", "BYMONTHDAY out of range"),
        ("BYMONTH=13", "BYMONTH out of range"),
        ("BYMINUTE=x", "BYMINUTE is not a number"),
        ("BYMINUTE=", "BYMINUTE is empty"),
    ],
)
def test_a_by_field_outside_its_range_is_refused(part: str, message: str) -> None:
    with pytest.raises(RecurrenceError, match=message):
        Recurrence.calendar(f"FREQ=DAILY;{part}")


# --- the field defaults each FREQ implies --------------------------------------------
#
# These pin what a FREQ means when a BY field is *absent*, which is where the
# whole vocabulary either lines up with cron or quietly does not.

EVERY_MINUTE = frozenset(range(60))
EVERY_HOUR = frozenset(range(24))
EVERY_DAY = frozenset(range(1, 32))
EVERY_MONTH = frozenset(range(1, 13))
EVERY_WEEKDAY = frozenset(range(7))


def test_minutely_means_every_minute_of_every_hour() -> None:
    recurrence = Recurrence.calendar("FREQ=MINUTELY")
    assert recurrence.minute == EVERY_MINUTE
    assert recurrence.hour == EVERY_HOUR
    assert recurrence.day == EVERY_DAY
    assert recurrence.month == EVERY_MONTH
    assert recurrence.weekday == EVERY_WEEKDAY


def test_minutely_with_an_interval_steps_the_minute_field() -> None:
    assert Recurrence.calendar("FREQ=MINUTELY;INTERVAL=15").minute == frozenset(
        {0, 15, 30, 45}
    )


def test_hourly_pins_the_minute_to_zero_rather_than_every_minute() -> None:
    # The difference between "once an hour" and "sixty times an hour", which is
    # the kind of mistake a scheduler makes exactly once.
    recurrence = Recurrence.calendar("FREQ=HOURLY")
    assert recurrence.minute == frozenset({0})
    assert recurrence.hour == EVERY_HOUR


def test_hourly_honours_an_explicit_byminute() -> None:
    assert Recurrence.calendar("FREQ=HOURLY;BYMINUTE=30").minute == frozenset({30})


def test_daily_pins_both_clock_fields_to_midnight() -> None:
    recurrence = Recurrence.calendar("FREQ=DAILY")
    assert (recurrence.minute, recurrence.hour) == (frozenset({0}), frozenset({0}))
    assert recurrence.day == EVERY_DAY
    assert recurrence.weekday == EVERY_WEEKDAY


def test_weekly_constrains_the_weekday_and_leaves_the_month_day_open() -> None:
    recurrence = Recurrence.calendar("FREQ=WEEKLY;BYDAY=MO,FR")
    assert recurrence.weekday == frozenset({1, 5})
    assert recurrence.day == EVERY_DAY


def test_monthly_by_month_day_leaves_the_weekday_open() -> None:
    recurrence = Recurrence.calendar("FREQ=MONTHLY;BYMONTHDAY=1,15")
    assert recurrence.day == frozenset({1, 15})
    assert recurrence.weekday == EVERY_WEEKDAY


def test_monthly_accepts_byday_instead_of_bymonthday() -> None:
    recurrence = Recurrence.calendar("FREQ=MONTHLY;BYDAY=MO")
    assert recurrence.weekday == frozenset({1})
    assert recurrence.day == EVERY_DAY


def test_bymonth_narrows_the_month_field() -> None:
    assert Recurrence.calendar("FREQ=DAILY;BYMONTH=1,7").month == frozenset({1, 7})


@pytest.mark.parametrize("freq", ["DAILY", "WEEKLY", "MONTHLY"])
def test_a_calendar_interval_on_a_date_frequency_is_refused(freq: str) -> None:
    # "every second Tuesday" counts periods from a start date, which a field set
    # cannot hold. Accepting it would fire every Tuesday instead.
    extra = {"DAILY": "", "WEEKLY": ";BYDAY=TU", "MONTHLY": ";BYMONTHDAY=1"}[freq]
    with pytest.raises(RecurrenceError, match="only INTERVAL=1 is supported"):
        Recurrence.calendar(f"FREQ={freq};INTERVAL=2{extra}")


def test_an_explicit_interval_of_one_is_accepted_on_a_date_frequency() -> None:
    assert Recurrence.calendar("FREQ=DAILY;INTERVAL=1").hour == frozenset({0})


def test_an_empty_interval_reads_as_one() -> None:
    # `parts.get("INTERVAL", "1") or "1"` -- the second `"1"` is what stops
    # `INTERVAL=` reaching `int("")` and raising a bare ValueError out of a
    # function whose whole contract is that it raises `RecurrenceError`.
    assert Recurrence.calendar("FREQ=DAILY;INTERVAL=").hour == frozenset({0})


def test_an_explicit_byminute_beats_the_interval_step() -> None:
    # `minute or _calendar_step(...)`: with the left operand dropped the step
    # always wins, and an explicitly requested minute is silently discarded.
    recurrence = Recurrence.calendar("FREQ=MINUTELY;BYMINUTE=5;INTERVAL=15")
    assert recurrence.minute == frozenset({5})


def test_an_explicit_byhour_beats_the_interval_step() -> None:
    recurrence = Recurrence.calendar("FREQ=HOURLY;BYHOUR=3;INTERVAL=6")
    assert recurrence.hour == frozenset({3})
    assert recurrence.minute == frozenset({0})
