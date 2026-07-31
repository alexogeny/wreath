"""`Instant` across the protobuf wire, as a `google.protobuf.Timestamp`.

The mapping is the well-known type: `int64 seconds = 1; int32 nanos = 2`,
counted from the Unix epoch in UTC. Three properties in that specification are
easy to get wrong and are pinned here: `nanos` is **always non-negative** even
before the epoch, the wire carries an instant rather than a zone, and Python's
microsecond resolution is coarser than the field allows.
"""

from __future__ import annotations

import datetime

import pytest

from wreath.protobuf import decode, encode, field, message
from wreath.temporal import (
    Instant,
    TemporalError,
    Timestamp,
    from_timestamp,
    to_timestamp,
    zone,
)


@message
class Event:
    at: Timestamp = field(1)
    label: str = field(2)


def test_the_epoch_is_all_zeroes() -> None:
    epoch = Instant(1970, 1, 1, tzinfo=datetime.UTC)
    assert to_timestamp(epoch) == Timestamp(seconds=0, nanos=0)


def test_microseconds_become_nanos() -> None:
    value = Instant(1970, 1, 1, 0, 0, 1, 500_000, tzinfo=datetime.UTC)
    assert to_timestamp(value) == Timestamp(seconds=1, nanos=500_000_000)


def test_nanos_stay_non_negative_before_the_epoch() -> None:
    """The specification requires 0 <= nanos <= 999,999,999 for every instant,
    so a moment half a second before the epoch is seconds=-1, nanos=500000000
    -- not seconds=0, nanos=-500000000. Both encode the same moment and only
    one of them is legal, and a peer reading the illegal form gets a time a
    second out."""
    value = Instant(1969, 12, 31, 23, 59, 59, 500_000, tzinfo=datetime.UTC)
    stamp = to_timestamp(value)
    assert stamp == Timestamp(seconds=-1, nanos=500_000_000)
    assert 0 <= stamp.nanos <= 999_999_999
    assert from_timestamp(stamp) == value


@pytest.mark.parametrize(
    "value",
    [
        Instant(1970, 1, 1, tzinfo=datetime.UTC),
        Instant(2026, 7, 31, 12, 34, 56, 789_012, tzinfo=datetime.UTC),
        Instant(1969, 12, 31, 23, 59, 59, 1, tzinfo=datetime.UTC),
        Instant(1900, 1, 1, tzinfo=datetime.UTC),
        Instant(2262, 4, 11, tzinfo=datetime.UTC),
    ],
)
def test_round_trip_preserves_the_moment(value: Instant) -> None:
    assert from_timestamp(to_timestamp(value)) == value


def test_the_result_is_always_aware() -> None:
    """The whole point of `Instant` is that it cannot exist without an offset,
    and decoding must not be the hole in that."""
    back = from_timestamp(Timestamp(seconds=0, nanos=0))
    assert isinstance(back, Instant)
    assert back.tzinfo is not None
    assert back.utcoffset() == datetime.timedelta(0)


def test_a_naive_datetime_is_refused_rather_than_assumed_utc() -> None:
    with pytest.raises(TemporalError):
        to_timestamp(datetime.datetime(2026, 7, 31, 12, 0, 0))


class _Undecided(datetime.tzinfo):
    """A tzinfo that declines to say what its offset is. `datetime` treats such
    a value as naive, and `tzinfo is not None` does not catch it."""

    def utcoffset(self, dt: datetime.datetime | None) -> datetime.timedelta | None:
        return None

    def dst(self, dt: datetime.datetime | None) -> datetime.timedelta | None:
        return None

    def tzname(self, dt: datetime.datetime | None) -> str | None:
        return None


def test_a_tzinfo_with_no_offset_is_refused_too() -> None:
    """The case a `tzinfo is None` check misses. A `tzinfo` whose `utcoffset()`
    returns None is naive in effect -- there is no moment to encode -- but the
    attribute is present, so the obvious guard waves it through and exports a
    time computed from an offset that does not exist.

    Found by a surviving mutant: dropping the `utcoffset()` clause from the
    guard changed no test's outcome, which meant nothing covered this."""
    value = datetime.datetime(2026, 7, 31, 12, 0, 0, tzinfo=_Undecided())
    assert value.tzinfo is not None
    with pytest.raises(TemporalError):
        to_timestamp(value)


def test_the_wire_carries_a_moment_not_a_zone() -> None:
    """A `Timestamp` has no zone field, so a round trip preserves the instant
    and returns it in UTC. That is a property of the format, not a defect --
    but it means the reader's zone has to be carried separately if it matters."""
    sydney = Instant(2026, 7, 31, 22, 0, tzinfo=zone("Australia/Sydney"))
    back = from_timestamp(to_timestamp(sydney))
    assert back == sydney                      # the same moment
    assert back.utcoffset() == datetime.timedelta(0)   # expressed in UTC
    assert back.hour != sydney.hour


def test_sub_microsecond_nanos_are_refused_rather_than_silently_truncated() -> None:
    """Python's resolution is microseconds and the field's is nanoseconds, so
    500 ns cannot be represented. Truncating would make a round trip through
    wreath quietly lossy for a peer that had the precision."""
    with pytest.raises(TemporalError) as excinfo:
        from_timestamp(Timestamp(seconds=0, nanos=500))
    assert "nanosecond" in str(excinfo.value)


def test_nanos_outside_the_legal_range_are_refused() -> None:
    for bad in (-1, 1_000_000_000):
        with pytest.raises(TemporalError):
            from_timestamp(Timestamp(seconds=0, nanos=bad))


def test_a_timestamp_field_round_trips_inside_a_message() -> None:
    at = Instant(2026, 7, 31, 12, 34, 56, 789_012, tzinfo=datetime.UTC)
    event = Event(at=to_timestamp(at), label="woken")
    back = decode(Event, encode(event))
    assert from_timestamp(back.at) == at
    assert back.label == "woken"


def test_the_wire_bytes_match_the_specification() -> None:
    """Hand-computed from the wire format rather than captured from this
    implementation, so a bug fails as a spec mismatch instead of agreeing with
    itself.

    Field 1 (seconds), wire type 0, value 1  -> tag `08`, varint `01`.
    Field 2 (nanos),   wire type 0, value 5e8 -> tag `10`, varint `80 ca b5 ee 01`
        (500000000 = 0b11101110110101100101000000000; seven bits at a time,
        least-significant group first, continuation bit set on all but the last:
        0000000 -> 0x80, 1001010 -> 0xca, 0110101 -> 0xb5, 1101110 -> 0xee, 1 -> 0x01).
    Eight bytes in total, so nesting it in Event field 1 (wire type 2) gives
    tag `0a`, length `08`.
    """
    stamp = Timestamp(seconds=1, nanos=500_000_000)
    assert encode(stamp) == bytes.fromhex("0801" "1080cab5ee01")

    event = Event(at=stamp, label="")
    assert encode(event) == bytes.fromhex("0a08") + encode(stamp)
