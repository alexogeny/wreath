"""Time that means the same thing at every boundary.

Old code reaches for `arrow` or `pendulum` in nearly every module, and time
ends up meaning something slightly different at each surface: the ORM hands
back a `datetime`, something wraps it, the serializer calls `.isoformat()`, and
"3 hours ago" gets built by hand in a template. Five conventions, and the drift
between them is invisible until a client reads a naive timestamp as UTC.

Wreath cannot take `arrow` as a dependency -- the core is dependency-free by
rule -- so `wreath.temporal` is both the replacement and the chance to settle
the question once. These tests pin the two properties that make it worth
having: an `Instant` is *always* zone-aware, and the relative formatter takes a
locale so i18n later is a parameter rather than a search for every call site.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pytest

from wreath.temporal import (
    Instant,
    TemporalError,
    format_duration,
    now,
    parse,
    parse_duration,
    relative,
    zone,
)

SYDNEY = "Australia/Sydney"


# --- an instant is always aware -------------------------------------------------


def test_an_instant_is_a_datetime() -> None:
    """So the ORM, comparisons, and arithmetic all keep working unchanged."""
    moment = Instant.parse("2026-07-26T09:30:00+00:00")
    assert isinstance(moment, datetime.datetime)
    assert moment.year == 2026 and moment.hour == 9


def test_a_naive_string_is_refused_rather_than_assumed_to_be_utc() -> None:
    """Assuming UTC is the bug this type exists to prevent."""
    with pytest.raises(TemporalError, match="offset"):
        Instant.parse("2026-07-26T09:30:00")


def test_a_naive_datetime_is_refused() -> None:
    with pytest.raises(TemporalError, match="offset"):
        Instant.of(datetime.datetime(2026, 7, 26, 9, 30))


def test_a_naive_datetime_can_be_placed_in_a_zone_explicitly() -> None:
    """Explicit is fine; implicit is what is refused."""
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


# --- parsing and formatting -----------------------------------------------------


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
    """`Z` is the one stdlib used to refuse and every JSON client emits."""
    assert parse(text).tzinfo is not None


def test_iso_output_round_trips() -> None:
    moment = parse("2026-07-26T09:30:00+00:00")
    assert Instant.parse(moment.iso()) == moment


def test_iso_output_normalises_utc_to_an_offset() -> None:
    """One spelling on the wire, so two services never disagree about `Z`."""
    assert parse("2026-07-26T09:30:00Z").iso() == "2026-07-26T09:30:00+00:00"


def test_rubbish_is_refused_with_the_text_in_the_message() -> None:
    with pytest.raises(TemporalError, match="not-a-time"):
        parse("not-a-time")


def test_a_non_string_is_refused() -> None:
    with pytest.raises(TemporalError):
        parse(1753520000)          # type: ignore[arg-type]


# --- zones ----------------------------------------------------------------------


def test_converting_between_zones_keeps_the_same_instant() -> None:
    utc = parse("2026-07-26T09:30:00+00:00")
    sydney = utc.to(SYDNEY)
    assert sydney == utc                       # the same moment
    assert sydney.hour != utc.hour             # a different wall clock


def test_an_unknown_zone_says_so_by_name() -> None:
    with pytest.raises(TemporalError, match="Mars/Olympus"):
        zone("Mars/Olympus")


def test_a_known_zone_resolves() -> None:
    assert zone(SYDNEY) == ZoneInfo(SYDNEY)


# --- durations ------------------------------------------------------------------


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
    """stdlib has no ISO-8601 duration parser, and config files are full of them."""
    assert parse_duration(text) == datetime.timedelta(seconds=seconds)


def test_a_duration_round_trips() -> None:
    assert parse_duration(format_duration(datetime.timedelta(hours=2, minutes=5))) == (
        datetime.timedelta(hours=2, minutes=5)
    )


def test_a_zero_duration_is_not_the_empty_string() -> None:
    assert format_duration(datetime.timedelta(0)) == "PT0S"


def test_a_malformed_duration_is_refused() -> None:
    with pytest.raises(TemporalError, match="P1Y"):
        parse_duration("P1Y")      # years are not a fixed number of seconds


# --- the relative formatter ------------------------------------------------------


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
def test_the_future_reads_the_same_way(
    delta: datetime.timedelta, expected: str
) -> None:
    reference = parse("2026-07-26T09:30:00+00:00")
    assert relative(reference + delta, now=reference) == expected


def test_the_singular_is_not_1_minutes() -> None:
    """The detail every hand-rolled formatter gets wrong on the first pass."""
    reference = parse("2026-07-26T09:30:00+00:00")
    assert "1 minutes" not in relative(
        reference - datetime.timedelta(minutes=1), now=reference
    )


def test_relative_is_available_on_the_instant_itself() -> None:
    reference = parse("2026-07-26T09:30:00+00:00")
    moment = reference - datetime.timedelta(hours=2)
    assert moment.relative(now=reference) == "2 hours ago"


def test_a_naive_reference_is_refused() -> None:
    """Comparing an aware instant to a naive `now` is a TypeError waiting to fire."""
    with pytest.raises(TemporalError, match="offset"):
        relative(now(), now=datetime.datetime(2026, 7, 26, 9, 30))


# --- the locale seam --------------------------------------------------------------


def test_the_formatter_takes_a_locale() -> None:
    """i18n later must be a parameter, not a hunt for every call site."""
    reference = parse("2026-07-26T09:30:00+00:00")
    earlier = reference - datetime.timedelta(hours=3)
    assert relative(earlier, now=reference, locale="en") == "3 hours ago"


def test_an_unknown_locale_falls_back_rather_than_failing() -> None:
    """A missing translation must render English, never 500 a page."""
    reference = parse("2026-07-26T09:30:00+00:00")
    earlier = reference - datetime.timedelta(hours=3)
    assert relative(earlier, now=reference, locale="qq-ZZ") == "3 hours ago"


def test_a_regional_locale_uses_its_base_language() -> None:
    reference = parse("2026-07-26T09:30:00+00:00")
    earlier = reference - datetime.timedelta(hours=3)
    assert relative(earlier, now=reference, locale="en-AU") == "3 hours ago"
