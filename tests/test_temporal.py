from __future__ import annotations

import datetime
import json
import random
from zoneinfo import ZoneInfo

import pytest

from wreath._json import dumps
from wreath.temporal import (
    Day,
    Hour,
    Instant,
    Minute,
    Month,
    Quarter,
    TemporalError,
    Week,
    Year,
    format_duration,
    format_iso,
    now,
    parse,
    parse_duration,
    relative,
    zone,
)

SYDNEY = "Australia/Sydney"


def test_an_instant_is_a_datetime() -> None:
    moment = Instant.parse("2026-07-26T09:30:00+00:00")
    assert isinstance(moment, datetime.datetime)
    assert moment.year == 2026 and moment.hour == 9


def test_a_naive_string_is_refused_rather_than_assumed_to_be_utc() -> None:
    with pytest.raises(TemporalError, match="offset"):
        Instant.parse("2026-07-26T09:30:00")


def test_a_naive_datetime_is_refused() -> None:
    with pytest.raises(TemporalError, match="offset"):
        Instant.of(datetime.datetime(2026, 7, 26, 9, 30))


def test_a_naive_datetime_can_be_placed_in_a_zone_explicitly() -> None:
    moment = Instant.of(datetime.datetime(2026, 7, 26, 9, 30), assume=SYDNEY)
    assert moment.utcoffset() is not None
    assert moment.tzinfo is not None


def test_an_aware_datetime_passes_straight_through() -> None:
    original = datetime.datetime(2026, 7, 26, 9, 30, tzinfo=datetime.UTC)
    assert Instant.of(original) == original


def test_now_is_aware_and_defaults_to_utc() -> None:
    moment = now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == datetime.timedelta(0)


def test_now_can_be_taken_in_a_named_zone() -> None:
    assert now(SYDNEY).tzinfo == ZoneInfo(SYDNEY)


@pytest.mark.parametrize(
    "text",
    [
        "2026-07-26T09:30:00+00:00",
        "2026-07-26T09:30:00Z",
        "2026-07-26T09:30:00.250Z",
        "2026-07-26T19:30:00+10:00",
    ],
)
def test_the_iso_forms_a_client_actually_sends_all_parse(text: str) -> None:
    assert parse(text).tzinfo is not None


def test_iso_output_round_trips() -> None:
    moment = parse("2026-07-26T09:30:00+00:00")
    assert Instant.parse(moment.iso()) == moment


def test_iso_output_normalises_utc_to_an_offset() -> None:
    assert parse("2026-07-26T09:30:00Z").iso() == "2026-07-26T09:30:00+00:00"


def test_rubbish_is_refused_with_the_text_in_the_message() -> None:
    with pytest.raises(TemporalError, match="not-a-time"):
        parse("not-a-time")


def test_a_non_string_is_refused() -> None:
    with pytest.raises(TemporalError):
        parse(1753520000)  # type: ignore[arg-type]


def test_converting_between_zones_keeps_the_same_instant() -> None:
    utc = parse("2026-07-26T09:30:00+00:00")
    sydney = utc.to(SYDNEY)
    assert sydney == utc  # the same moment
    assert sydney.hour != utc.hour  # a different wall clock


def test_an_unknown_zone_says_so_by_name() -> None:
    with pytest.raises(TemporalError, match="Mars/Olympus"):
        zone("Mars/Olympus")


def test_a_known_zone_resolves() -> None:
    assert zone(SYDNEY) == ZoneInfo(SYDNEY)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("PT30S", 30),
        ("PT5M", 300),
        ("PT3H", 10800),
        ("P1D", 86400),
        ("P1DT2H30M", 95400),
        ("-PT1H", -3600),
    ],
)
def test_iso_durations_parse(text: str, seconds: int) -> None:
    assert parse_duration(text) == datetime.timedelta(seconds=seconds)


def test_a_duration_round_trips() -> None:
    assert parse_duration(format_duration(datetime.timedelta(hours=2, minutes=5))) == (
        datetime.timedelta(hours=2, minutes=5)
    )


def test_a_zero_duration_is_not_the_empty_string() -> None:
    assert format_duration(datetime.timedelta(0)) == "PT0S"


def test_a_malformed_duration_is_refused() -> None:
    with pytest.raises(TemporalError, match="P1Y"):
        parse_duration("P1Y")  # years are not a fixed number of seconds


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (datetime.timedelta(seconds=5), "just now"),
        (datetime.timedelta(minutes=1), "1 minute ago"),
        (datetime.timedelta(minutes=3), "3 minutes ago"),
        (datetime.timedelta(hours=1), "1 hour ago"),
        (datetime.timedelta(hours=3), "3 hours ago"),
        (datetime.timedelta(days=1), "yesterday"),
        (datetime.timedelta(days=3), "3 days ago"),
        (datetime.timedelta(days=400), "1 year ago"),
    ],
)
def test_the_past_reads_the_way_a_person_would_say_it(
    delta: datetime.timedelta, expected: str
) -> None:
    reference = parse("2026-07-26T09:30:00+00:00")
    assert relative(reference - delta, now=reference) == expected


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (datetime.timedelta(minutes=3), "in 3 minutes"),
        (datetime.timedelta(hours=1), "in 1 hour"),
        (datetime.timedelta(days=1), "tomorrow"),
    ],
)
def test_the_future_reads_the_same_way(delta: datetime.timedelta, expected: str) -> None:
    reference = parse("2026-07-26T09:30:00+00:00")
    assert relative(reference + delta, now=reference) == expected


def test_the_singular_is_not_1_minutes() -> None:
    reference = parse("2026-07-26T09:30:00+00:00")
    assert "1 minutes" not in relative(reference - datetime.timedelta(minutes=1), now=reference)


def test_relative_is_available_on_the_instant_itself() -> None:
    reference = parse("2026-07-26T09:30:00+00:00")
    moment = reference - datetime.timedelta(hours=2)
    assert moment.relative(now=reference) == "2 hours ago"


def test_a_naive_reference_is_refused() -> None:
    with pytest.raises(TemporalError, match="offset"):
        relative(now(), now=datetime.datetime(2026, 7, 26, 9, 30))


def test_the_formatter_takes_a_locale() -> None:
    reference = parse("2026-07-26T09:30:00+00:00")
    earlier = reference - datetime.timedelta(hours=3)
    assert relative(earlier, now=reference, locale="en") == "3 hours ago"


def test_an_unknown_locale_falls_back_rather_than_failing() -> None:
    reference = parse("2026-07-26T09:30:00+00:00")
    earlier = reference - datetime.timedelta(hours=3)
    assert relative(earlier, now=reference, locale="qq-ZZ") == "3 hours ago"


def test_a_regional_locale_uses_its_base_language() -> None:
    reference = parse("2026-07-26T09:30:00+00:00")
    earlier = reference - datetime.timedelta(hours=3)
    assert relative(earlier, now=reference, locale="en-AU") == "3 hours ago"


@pytest.mark.parametrize(
    "delta",
    [
        datetime.timedelta(microseconds=1),
        datetime.timedelta(milliseconds=1),
        datetime.timedelta(seconds=0.5),
        datetime.timedelta(seconds=59, microseconds=999999),
        datetime.timedelta(days=1, microseconds=1),
        datetime.timedelta(days=-2, seconds=86398, microseconds=999999),
        datetime.timedelta(microseconds=-1),
        datetime.timedelta.max,
        datetime.timedelta.min,
    ],
)
def test_a_duration_round_trips_exactly(delta: datetime.timedelta) -> None:
    assert parse_duration(format_duration(delta)) == delta


def test_a_sub_second_duration_is_not_scientific_notation() -> None:
    assert format_duration(datetime.timedelta(microseconds=1)) == "PT0.000001S"


def test_format_duration_never_emits_years_or_months() -> None:
    for days in (0, 1, 31, 365, 4000, 999999999):
        head = format_duration(datetime.timedelta(days=days)).split("T")[0]
        assert "Y" not in head and "M" not in head


# `format_duration` was broken for 19254 of 20012 durations and survived because
# its only round-trip test used `timedelta(hours=2, minutes=5)` -- whole minutes,
# the one shape `%g` renders cleanly. One well-chosen example passing forever is
# the failure mode, so every remaining pair is swept over a domain built out of
# the edges rather than out of the typical case.
# The fast samples below run in the default suite. The full sweeps carry
# `@pytest.mark.fuzz` and run under `pytest -m ''`, because the default marks
# have to stay at ~3.5s.

#: Zones that actually change offset, including the two that do it by 30 and 45
#: minutes -- a whole-hour assumption is the bug these are here to catch.
_SWEEP_ZONES = [
    "Pacific/Auckland",
    "Australia/Sydney",
    "America/New_York",
    "Europe/London",
    "Pacific/Chatham",
    "Asia/Kolkata",
    "Asia/Kathmandu",
    "Australia/Eucla",
    "Asia/Tokyo",
    "UTC",
]

#: Every microsecond shape that has ever broken a formatter: none, one, the
#: powers of ten between, and the largest.
_SWEEP_MICROS = [0, 1, 10, 1000, 123456, 999990, 999999]


def _sweep_instants() -> list[Instant]:
    """Aware moments across DST transitions, fractional offsets, and the
    calendar extremes -- both folds where a local time is ambiguous."""
    out: list[Instant] = []
    for name in _SWEEP_ZONES:
        tz = ZoneInfo(name)
        for month, day, hour in [(1, 1, 0), (4, 6, 2), (6, 15, 13), (10, 5, 2), (12, 31, 23)]:
            for micro in _SWEEP_MICROS:
                for fold in (0, 1):
                    out.append(Instant(2025, month, day, hour, 30, 7, micro, tz, fold=fold))
    for tz in (datetime.UTC, ZoneInfo("Pacific/Auckland"), ZoneInfo("Asia/Kathmandu")):
        for year in (1, 2, 1970, 2000, 9998, 9999):
            for micro in (0, 1, 999999):
                out.append(Instant(year, 6, 15, 12, 0, 0, micro, tz))
    return out


@pytest.mark.parametrize(
    ("year", "month", "day", "hour", "micro", "zone_name", "fold"),
    [
        (2025, 4, 6, 2, 0, "Pacific/Auckland", 1),  # the ambiguous hour, second pass
        (2025, 4, 6, 2, 999999, "Pacific/Auckland", 0),  # the ambiguous hour, first pass
        (2025, 9, 28, 2, 1, "Pacific/Auckland", 0),  # the hour that does not exist
        (2025, 6, 15, 13, 123456, "Asia/Kathmandu", 0),  # +05:45
        (2025, 6, 15, 13, 1, "Pacific/Chatham", 0),  # +12:45
        (1, 1, 1, 12, 0, "UTC", 0),  # datetime.min's year
        (9999, 12, 31, 23, 999999, "UTC", 0),  # datetime.max's year
    ],
)
def test_an_instant_round_trips_through_iso(
    year: int, month: int, day: int, hour: int, micro: int, zone_name: str, fold: int
) -> None:
    moment = Instant(year, month, day, hour, 30, 7, micro, ZoneInfo(zone_name), fold=fold)
    assert Instant.parse(format_iso(moment)).timestamp() == moment.timestamp()


def test_builtin_datetime_iso_formatter_matches_the_stdlib_definition() -> None:
    zones = (
        None,
        datetime.UTC,
        datetime.timezone(datetime.timedelta(hours=5, minutes=30)),
        datetime.timezone(-datetime.timedelta(hours=3, seconds=1, microseconds=2)),
    )
    values = [
        datetime.datetime(year, 2, 3, 4, 5, 6, microsecond, tzinfo=tz)
        for year in (1, 99, 999, 2026, 9999)
        for microsecond in (0, 1, 12_000, 999_999)
        for tz in zones
    ]
    values.extend(
        datetime.datetime(
            2026,
            4,
            5,
            2,
            30,
            tzinfo=ZoneInfo(SYDNEY),
            fold=fold,
        )
        for fold in (0, 1)
    )
    values.extend(
        Instant.parse(text)
        for text in (
            "2026-08-23T00:00:00+00:00",
            "2026-08-23T00:00:00.123456+10:00",
        )
    )

    assert len(values) == 84
    assert [format_iso(value) for value in values] == [value.isoformat() for value in values]


def test_datetime_subclasses_keep_their_isoformat_override() -> None:
    class CustomDateTime(datetime.datetime):
        def isoformat(self, *args, **kwargs) -> str:
            return "custom-iso"

    assert format_iso(CustomDateTime(2026, 8, 23)) == "custom-iso"


@pytest.mark.fuzz
def test_every_instant_in_the_sweep_round_trips_through_iso() -> None:
    values = _sweep_instants()
    assert len(values) == 754
    bad = [v for v in values if Instant.parse(format_iso(v)).timestamp() != v.timestamp()]
    assert bad == []


@pytest.mark.fuzz
def test_every_instant_in_the_sweep_survives_the_json_path() -> None:
    values = _sweep_instants()
    bad = []
    for value in values:
        text = json.loads(dumps({"at": value}))["at"]
        if Instant.parse(text).timestamp() != value.timestamp():
            bad.append(value)
    assert bad == []


@pytest.mark.fuzz
def test_the_duration_sweep_is_exact_across_the_whole_range() -> None:
    values = [
        datetime.timedelta(0),
        datetime.timedelta.max,
        datetime.timedelta.min,
        datetime.timedelta.resolution,
        -datetime.timedelta.resolution,
    ]
    values += [datetime.timedelta(microseconds=micro) for micro in range(0, 1000000, 37)]
    rng = random.Random(20260727)
    for _ in range(4000):
        values.append(
            datetime.timedelta(
                days=rng.randint(-999999999, 999999999) // rng.choice([1, 1000, 10**6]),
                seconds=rng.randint(0, 86399),
                microseconds=rng.randint(0, 999999),
            )
        )
    bad = [v for v in values if parse_duration(format_duration(v)) != v]
    assert bad == []


def test_a_bucket_brackets_the_moment_it_was_asked_about() -> None:
    tz = ZoneInfo(SYDNEY)
    moment = Instant(2025, 6, 15, 13, 47, 3, 250000, tz)
    for width in (Minute, Hour, Day, Week, Month, Quarter, Year):
        low, high = width.floor(moment, tz), width.end_of(moment, tz)
        assert low.timestamp() <= moment.timestamp() < high.timestamp(), width.name


def test_a_bucket_does_not_bracket_the_first_pass_of_an_ambiguous_hour() -> None:
    tz = ZoneInfo("Pacific/Auckland")
    first_pass = Instant(2025, 4, 6, 2, 30, 0, 0, tz, fold=0)
    second_pass = Instant(2025, 4, 6, 2, 30, 0, 0, tz, fold=1)

    # The second pass is bracketed at every width, because `floor` lands on it.
    for width in (Minute, Hour, Day):
        low, high = width.floor(second_pass, tz), width.end_of(second_pass, tz)
        assert low.timestamp() <= second_pass.timestamp() < high.timestamp(), width.name

    # The first pass is not: its bucket starts an hour after it.
    minute_start = Minute.floor(first_pass, tz)
    assert minute_start.timestamp() > first_pass.timestamp()
    assert minute_start.timestamp() - first_pass.timestamp() == 3600.0
