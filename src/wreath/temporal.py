"""Instants, durations, and zones — one meaning for time at every boundary.

Time is the type that has to mean the same thing in six places at once: the
column it is stored in, the parameter it arrives as, the JSON it leaves as, the
OpenAPI schema, the generated TypeScript, and the GraphQL scalar. Most
applications re-decide it in each of them, and the drift is invisible until a
client reads a naive timestamp as if it were UTC.

The usual answer is to reach for `arrow` or `pendulum` in every module.
Wreath's core carries no mandatory dependencies, so that answer is unavailable —
which turns out to be the better outcome, because it forces the decision into
one place that every surface already goes through:

```python
from wreath.temporal import Instant, now, relative

started = Instant.parse(request.query["since"])   # aware, or it raises
when = relative(started, locale=request.locale)   # "3 hours ago"
```
**An `Instant` is always zone-aware.** It subclasses `datetime.datetime`,
so it stores, compares, and does arithmetic exactly like one — but it cannot be
constructed without an offset. Assuming UTC for a naive value is the single bug
this module exists to prevent, and it is refused loudly rather than guessed at.
Where a naive value genuinely needs a zone, say so: ``Instant.of(value,
assume="Australia/Sydney")``.

**The relative formatter takes a locale.** "3 hours ago" is the string every
codebase ends up hand-rolling at the edge, and it is also the one that is
locale-dependent. Keeping it here — reached through `request.locale` — means
translating it later is a parameter to one function rather than a hunt through
every template. English ships today; `_LOCALES` is where the next
language goes, and the docstring there marks exactly where CLDR plural rules
slot in.

Everything is Python over stdlib `datetime`/`zoneinfo`. None of it is in C;
see the note on `format_iso` for what would have to be measured first.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .protobuf import field as _protobuf_field
from .protobuf import message as _message

__all__ = [
    "BUCKETS",
    "Bucket",
    "Day",
    "Duration",
    "Hour",
    "Instant",
    "Minute",
    "Month",
    "Quarter",
    "Recurrence",
    "RecurrenceError",
    "TemporalError",
    "Timestamp",
    "Week",
    "Year",
    "bucket",
    "days",
    "format_duration",
    "format_iso",
    "from_timestamp",
    "from_wall_clock",
    "hours",
    "jsonable",
    "milliseconds",
    "minutes",
    "now",
    "parse",
    "parse_duration",
    "relative",
    "seconds",
    "to_timestamp",
    "wall_clock",
    "weeks",
    "zone",
]

UTC = datetime.UTC


class TemporalError(ValueError):
    """A value could not be understood as a time, a duration, or a zone.

    A `ValueError` subclass because that is what a caller parsing untrusted
    input already handles.
    """


def zone(name: str) -> ZoneInfo:
    """The named IANA time zone, or a `TemporalError` naming it.

    `ZoneInfoNotFoundError` is accurate but arrives without the name in the
    common formatting, and a bad zone is nearly always a typo in configuration.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise TemporalError(f"unknown time zone {name!r}") from error


class Instant(datetime.datetime):
    """A moment in time that always knows its offset.

    A `datetime.datetime` subclass, so it is stored by `TimestampTz`,
    compared, and added to a `timedelta` exactly like one — arithmetic and
    `astimezone` return an `Instant` because CPython preserves the subclass.
    What it will not do is exist without a `tzinfo`.
    """

    __slots__ = ()

    def __new__(cls, year: Any, month: Any = None, day: Any = None, hour: int = 0,
                minute: int = 0, second: int = 0, microsecond: int = 0,
                tzinfo: datetime.tzinfo | None = None, *, fold: int = 0) -> Instant:
        if isinstance(year, (bytes, bytearray)):
            # The pickle/copy path hands the packed state through; `month` is
            # the tzinfo there, and it is already whatever it was. The stub only
            # describes the seven-argument form, so the two-argument unpickling
            # constructor -- which CPython does support -- has to be waived.
            return super().__new__(cls, year, month)  # ty: ignore[missing-argument]
        if tzinfo is None:
            raise TemporalError(
                "an Instant must carry a UTC offset; pass tzinfo=, or use "
                "Instant.of(value, assume='Area/City') to place a naive value"
            )
        return super().__new__(
            cls, year, month, day, hour, minute, second, microsecond, tzinfo, fold=fold
        )

    # -- construction --------------------------------------------------------
    #
    # There is deliberately no `Instant.now()`: `datetime.now` is a classmethod
    # with a different signature, and overriding it incompatibly is the kind of
    # thing that reads fine and then surprises a caller who passed a positional
    # `tz`. The module-level `now()` is the one way to ask.

    @classmethod
    def parse(cls, text: str) -> Instant:
        """An ISO-8601 timestamp that carries an offset.

        Accepts everything `datetime.fromisoformat` does, which since 3.11
        includes the trailing `Z` every JSON client emits. A string without an
        offset raises: there is no correct default, and UTC is merely the most
        popular wrong one.
        """
        if not isinstance(text, str):
            raise TemporalError(f"expected an ISO-8601 string, got {type(text).__name__}")
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError as error:
            raise TemporalError(f"{text!r} is not an ISO-8601 timestamp") from error
        return cls.of(parsed)

    @classmethod
    def of(
        cls,
        value: datetime.datetime,
        *,
        assume: str | datetime.tzinfo | None = None,
    ) -> Instant:
        """Adopt an existing `datetime`.

        A naive value needs `assume` to say which zone it was written in.
        Without it this raises rather than picking one — the caller knows and
        this module does not.
        """
        if not isinstance(value, datetime.datetime):
            raise TemporalError(f"expected a datetime, got {type(value).__name__}")
        if value.tzinfo is None:
            if assume is None:
                raise TemporalError(
                    "this datetime carries no UTC offset; pass "
                    "assume='Area/City' to say which zone it was written in"
                )
            value = value.replace(tzinfo=_tzinfo(assume))
        if type(value) is cls:
            return value
        return cls(
            value.year, value.month, value.day, value.hour, value.minute,
            value.second, value.microsecond, value.tzinfo, fold=value.fold,
        )

    # -- reading -------------------------------------------------------------

    def iso(self) -> str:
        """The ISO-8601 form, with an explicit offset.

        UTC renders as `+00:00` rather than `Z` so that two services never
        produce different bytes for the same moment.
        """
        return self.isoformat()

    def to(self, in_zone: str | datetime.tzinfo) -> Instant:
        """The same moment, read on another zone's wall clock."""
        # Round-tripped through `of` rather than returning `astimezone`
        # directly: CPython does preserve the subclass, but `of` returns the
        # same object when it already is one, so this costs nothing and does
        # not make the return type depend on an implementation detail.
        return Instant.of(self.astimezone(_tzinfo(in_zone)))

    def relative(
        self, *, now: datetime.datetime | None = None, locale: str = "en"
    ) -> str:
        """This moment as a person would say it — `"3 hours ago"`."""
        return relative(self, now=now, locale=locale)


def _tzinfo(value: str | datetime.tzinfo) -> datetime.tzinfo:
    return zone(value) if isinstance(value, str) else value


def now(in_zone: str | datetime.tzinfo = UTC) -> Instant:
    """The current moment, in UTC unless another zone is named."""
    return Instant.of(datetime.datetime.now(_tzinfo(in_zone)))


def _utc_now() -> Instant:
    """The current moment, reachable from functions that shadow `now`."""
    return Instant.of(datetime.datetime.now(UTC))


def parse(text: str) -> Instant:
    """An ISO-8601 timestamp that carries an offset. See `Instant.parse`."""
    return Instant.parse(text)


# --- buckets ---------------------------------------------------------------------
#
# Bucketing is a timezone problem before it is an aggregation problem: "daily"
# means daily *where the reader is*. A bucket therefore has to say the same
# thing in three places at once -- the SQL that assigns a row to a bucket, the
# SQL that generates the run of buckets a range covers, and the Python that
# reasons about a boundary without asking the database. Keeping those three in
# one object is what stops them drifting apart.


def wall_clock(value: datetime.datetime, tz: datetime.tzinfo) -> datetime.datetime:
    """`value` as a plain naive `datetime` on `tz`'s wall clock.

    Public because reading an instant on somebody's clock is the first step of
    every calendar calculation in the codebase, and each caller that reinvents
    it reinvents the trap below with it. `Bucket` uses it to truncate,
    and `wreath.series` to step a comparison period back a month.

    Built component-wise rather than with `replace(tzinfo=None)` because
    `replace` preserves the subclass, and an `Instant` refuses to exist
    without an offset -- correctly, since that is the bug it is here to prevent.

    `fold` is dropped, and nothing is lost by dropping it: both passes of an
    ambiguous hour read the same on a wall clock, which is exactly what
    `value AT TIME ZONE zone` returns for them too. Putting a local time back
    on the timeline is where the choice actually happens -- see
    `from_wall_clock`.
    """
    local = Instant.of(value).astimezone(tz)
    return datetime.datetime(
        local.year, local.month, local.day,
        local.hour, local.minute, local.second, local.microsecond,
    )


def from_wall_clock(
    local: datetime.datetime, tz: datetime.tzinfo
) -> datetime.datetime:
    """A naive local wall clock put back on the timeline, as PostgreSQL does it.

    The inverse of `wall_clock`, and public for the same reason: every
    calendar calculation here ends by putting a local time back on the
    timeline, and a caller that reinvents that reinvents the `fold` question
    with it.

    On the two days a year a zone changes offset, a local time can name two
    instants or none at all. This resolves **to the later of the two
    candidates**, because that is what `timestamp AT TIME ZONE zone` does.
    That is measured, not assumed: 864 samples across nine zones, both
    transition directions, and the ambiguous, skipped and ordinary cases, with
    no disagreement -- including a zone with a half-hour DST step
    (Australia/Lord_Howe), one whose tzdata entry uses negative DST
    (Europe/Dublin), and four whose transition is at local midnight
    (America/Santiago, Asia/Beirut, America/Havana, Africa/Cairo) so that a
    *day* boundary lands in the gap rather than an hour boundary.

    Matching it is not tidiness. A bucket boundary computed here and one
    generated by `generate_series` have to be the same instant: when they
    differ, a settled row files itself under a bucket the spine never emits and
    the value silently disappears from every later read.
    """
    first = local.replace(tzinfo=tz, fold=0)
    second = local.replace(tzinfo=tz, fold=1)
    # PEP 495 makes `first` and `second` compare *equal* despite naming
    # different instants, so the comparison has to happen in UTC.
    if second.astimezone(datetime.UTC) > first.astimezone(datetime.UTC):
        return second
    return first


@dataclass(frozen=True, slots=True)
class Bucket:
    """One interval width, in the three vocabularies that have to agree.

    `trunc` is the PostgreSQL `date_trunc` unit that assigns a row to a
    bucket; `step` is the `generate_series` interval that walks from one
    bucket to the next; and `floor` and `end_of` are the Python
    answers to the same two questions, for code that has a moment in hand and
    no connection.

    Both SQL fragments are drawn from `BUCKETS` rather than from a
    caller, so neither is ever user input -- `bucket` is the only way to
    reach one by name, and it refuses anything not in the table.
    """

    #: The name a caller writes and a payload carries -- `"day"`.
    name: str
    #: The `date_trunc` unit. Equal to `name` for every unit today, and
    #: kept separate because the two are not the same kind of thing.
    trunc: str
    #: The `generate_series` step, as an interval literal: `"1 day"`.
    step: str
    #: Calendar months per step, for the units a `timedelta` cannot hold.
    #: Zero means `delta` is the width instead.
    months: int = 0
    #: Fixed width, for the units that have one. `None` for calendar units.
    delta: datetime.timedelta | None = None

    def floor(self, value: datetime.datetime, in_zone: str | datetime.tzinfo) -> Instant:
        """The instant this bucket starts, for the wall clock in `in_zone`.

        The mirror of `date_trunc(unit, value AT TIME ZONE zone)`: read the
        moment on the zone's wall clock, truncate there, and convert back. Doing
        it in that order is what makes a "day" the reader's calendar day rather
        than a fixed 24 hours -- see `end_of` for why that distinction has
        teeth.

        On the two days a year a zone changes offset, a local wall clock can be
        ambiguous or absent. Both cases resolve to the *later* of the candidate
        instants, via `from_wall_clock`, because that is what
        `AT TIME ZONE` does -- **measured against a live PostgreSQL**, across
        nine zones and both transition directions, rather than reasoned from the
        documentation. An earlier revision resolved an ambiguous local time to
        the first of its two instants, which disagreed with `date_trunc` for
        every value in the second pass of a repeated hour.

        **Comparing the result across zones needs care**, and this is CPython's
        rule rather than this module's: by PEP 495 a datetime inside an
        ambiguous hour compares *unequal* to the same instant expressed in
        another zone, so that comparison stays transitive when one local time
        names two instants. Convert with `astimezone` before comparing, or
        compare two values in the same zone. The trap is that the naive form is
        correct on every day but one.

        **`floor(v) <= v < end_of(v)` does not hold for every value**, at
        `minute` granularity inside a repeated hour, and that is a property of
        the calendar rather than a defect here: one local time names two
        instants, a bucket start can only be one of them, and values at the
        other one fall outside their own bucket. **PostgreSQL does the same
        thing with the same inputs** — verified, not assumed — so this is the
        shared answer rather than a divergence. At `hour` and coarser the
        window is wide enough to contain both passes and the invariant holds.

        Every caller in the tree passes an already-truncated boundary except the
        sealing watermark, and there the boundary is now the one
        `generate_series` will emit, which is the property sealing actually
        needs.
        """
        tz = _tzinfo(in_zone)
        return Instant.of(
            from_wall_clock(self._truncate(wall_clock(value, tz)), tz)
        )

    def end_of(
        self, value: datetime.datetime, in_zone: str | datetime.tzinfo
    ) -> Instant:
        """The instant the *next* bucket starts -- this one's exclusive end.

        Ranges here are half-open throughout, so a bucket runs from
        `floor` up to but not including this. Sealing (when a bucket
        becomes final) is the other caller: a bucket cannot settle before the
        moment it stops accepting rows, and that moment is this one.

        The step is added on the *local* wall clock and then converted back, so
        a day spanning a DST change is 23 or 25 hours rather than 24, and a
        month is a calendar month rather than an approximation. The conversion
        back resolves an ambiguous or skipped boundary the same way
        `floor` does -- see `from_wall_clock` -- so a bucket's end
        and the next bucket's start are one instant.
        """
        tz = _tzinfo(in_zone)
        local = self._truncate(wall_clock(value, tz))
        return Instant.of(from_wall_clock(self._advance(local), tz))

    def _truncate(self, local: datetime.datetime) -> datetime.datetime:
        """Truncate a naive local wall clock to this bucket's start."""
        if self.name == "minute":
            return local.replace(second=0, microsecond=0)
        if self.name == "hour":
            return local.replace(minute=0, second=0, microsecond=0)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.name == "day":
            return midnight
        if self.name == "week":
            # PostgreSQL's `date_trunc('week', ...)` is ISO-8601: weeks start on
            # Monday, and `weekday()` is 0 there too.
            return midnight - datetime.timedelta(days=midnight.weekday())
        if self.name == "month":
            return midnight.replace(day=1)
        if self.name == "quarter":
            return midnight.replace(month=1 + 3 * ((midnight.month - 1) // 3), day=1)
        return midnight.replace(month=1, day=1)

    def _advance(self, local: datetime.datetime) -> datetime.datetime:
        """One step forward from a truncated naive local wall clock."""
        if not self.months:
            # `delta` is set for every non-calendar unit in the table below.
            return local + self.delta  # ty: ignore[unsupported-operator]
        total = (local.year * 12 + local.month - 1) + self.months
        return local.replace(year=total // 12, month=total % 12 + 1)

    def __repr__(self) -> str:
        return f"<Bucket {self.name}>"


Minute = Bucket("minute", "minute", "1 minute", delta=datetime.timedelta(minutes=1))
Hour = Bucket("hour", "hour", "1 hour", delta=datetime.timedelta(hours=1))
Day = Bucket("day", "day", "1 day", delta=datetime.timedelta(days=1))
Week = Bucket("week", "week", "1 week", delta=datetime.timedelta(weeks=1))
Month = Bucket("month", "month", "1 month", months=1)
Quarter = Bucket("quarter", "quarter", "3 months", months=3)
Year = Bucket("year", "year", "1 year", months=12)

#: Every bucket, by name. The SQL fragments a query interpolates come from
#: here and nowhere else, so a bucket named by a caller is looked up rather
#: than trusted.
BUCKETS: dict[str, Bucket] = {
    item.name: item for item in (Minute, Hour, Day, Week, Month, Quarter, Year)
}


def bucket(name: str | Bucket) -> Bucket:
    """The named bucket, or a `TemporalError` listing the real ones."""
    if isinstance(name, Bucket):
        return name
    found = BUCKETS.get(name) if isinstance(name, str) else None
    if found is None:
        raise TemporalError(
            f"unknown bucket {name!r}; one of {', '.join(sorted(BUCKETS))}"
        )
    return found


# --- durations -------------------------------------------------------------------

# ISO-8601 durations, restricted to the units that are a fixed number of
# seconds. Years and months are deliberately absent: "P1M" is 28 to 31 days
# depending on when you ask, so a `timedelta` cannot represent it and silently
# picking 30 would be a bug in a scheduler somewhere down the line.
_DURATION = re.compile(
    r"^(?P<sign>[+-])?P"
    r"(?:(?P<weeks>\d+(?:\.\d+)?)W)?"
    r"(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
    r")?$"
)


def parse_duration(text: str) -> datetime.timedelta:
    """An ISO-8601 duration such as `PT3H` or `P1DT2H30M`.

    The stdlib has no parser for these and configuration files are full of
    them, which is how every codebase ends up with its own half-correct one.

    Years and months are rejected: they are not a fixed number of seconds, so
    a `timedelta` cannot hold one honestly.
    """
    if not isinstance(text, str):
        raise TemporalError(f"expected an ISO-8601 duration, got {type(text).__name__}")
    match = _DURATION.match(text)
    # A bare "P" matches the pattern (every component is optional) but is not a
    # duration, so at least one component has to be present.
    if match is None or not any(
        match.group(name) for name in ("weeks", "days", "hours", "minutes", "seconds")
    ):
        raise TemporalError(
            f"{text!r} is not an ISO-8601 duration in whole units of time "
            "(years and months are not a fixed length)"
        )
    parts = {
        name: float(match.group(name) or 0)
        for name in ("weeks", "days", "hours", "minutes", "seconds")
    }
    total = datetime.timedelta(**parts)  # type: ignore[arg-type]
    return -total if match.group("sign") == "-" else total


def format_duration(value: datetime.timedelta) -> str:
    """An ISO-8601 duration, the inverse of `parse_duration`.

    Zero renders as `PT0S` rather than the empty `P`, which is what a
    reader — and most parsers — expect.

    Built from the timedelta's own integer `days`/`seconds`/`microseconds`
    rather than from `total_seconds()`. A float total loses precision on large
    values, and formatting one with `%g` rounds to six significant digits and
    switches to scientific notation for small ones — so `timedelta.max` came
    out a day too long and one microsecond came out `PT1e-06S`, which
    `parse_duration` rejects. A formatter whose own inverse cannot read its
    output is worse than no formatter, so the integer components are the only
    honest source.
    """
    if not isinstance(value, datetime.timedelta):
        raise TemporalError(f"expected a timedelta, got {type(value).__name__}")
    sign = ""
    if value < datetime.timedelta(0):
        sign, value = "-", -value
    days = value.days
    hours, rest = divmod(value.seconds, 3600)
    minutes, secs = divmod(rest, 60)
    micro = value.microseconds
    out = [sign, "P"]
    if days:
        out.append(f"{days}D")
    if hours or minutes or secs or micro or not days:
        out.append("T")
        if hours:
            out.append(f"{hours}H")
        if minutes:
            out.append(f"{minutes}M")
        if secs or micro or (not hours and not minutes):
            out.append(f"{_seconds_text(secs, micro)}S")
    return "".join(out)


def _seconds_text(secs: int, micro: int) -> str:
    """Whole seconds, or a fixed-point fraction — never scientific notation."""
    if not micro:
        return str(secs)
    return f"{secs}.{micro:06d}".rstrip("0")


# --- durations as a declared type ----------------------------------------------------


class Duration(datetime.timedelta):
    """A span of time, declared once and read wherever a length is configured.

    This module already refuses a naive `datetime`, because a moment without an
    offset has no single meaning. A *length* had no type at all: every span in
    the framework was a bare `float` of seconds — a job lease, a quota period, a
    notification digest window, a store window, a rate-limit span — so five
    subsystems each documented "seconds" and a caller learned "how long" five
    times. `Duration` is the one spelling, and `of` is what every one of those
    parameters now accepts.

    A `datetime.timedelta` subclass, so it compares, sorts, and adds to an
    `Instant` exactly like one, and `total_seconds()` is inherited — which is
    what a call site that genuinely needs a float asks for.

    **Arithmetic returns a plain `timedelta`, not a `Duration`.** CPython
    preserves the subclass for `datetime` but not for `timedelta`
    (`timedelta.__add__` constructs the base type directly). That is not worked
    around: `Duration` is a type for the *declaration* boundary, and a value
    that has been through arithmetic has left it. `Duration.of` takes it back
    when a boundary needs one.

    Note `.seconds` is `timedelta`'s own field — the seconds *component*, 0 to
    86399 — and deliberately not shadowed. `total_seconds()` is the whole span.
    """

    __slots__ = ()

    @classmethod
    def of(cls, value: Any) -> Duration:
        """Adopt a length however it was written.

        Accepts a `Duration`, a `timedelta`, a number of seconds, or an
        ISO-8601 duration string such as `"PT3H"`. A bool is refused: `True`
        is an `int` in Python and one second is never what it meant.

        Numbers are seconds because that is what every parameter this replaces
        already meant, so an existing call site keeps working unchanged.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, datetime.timedelta):
            return cls(seconds=value.total_seconds())
        if isinstance(value, bool):
            raise TemporalError("a bool is not a duration; say seconds(1) if you meant one")
        if isinstance(value, (int, float)):
            return cls(seconds=value)
        if isinstance(value, str):
            return cls(seconds=parse_duration(value).total_seconds())
        raise TemporalError(
            f"expected a Duration, timedelta, seconds, or an ISO-8601 duration "
            f"string, got {type(value).__name__}"
        )

    def iso(self) -> str:
        """The ISO-8601 form — `PT3H`. The spelling `of` reads back."""
        return format_duration(self)

    def __repr__(self) -> str:
        return f"Duration({self.iso()})"


def milliseconds(value: float) -> Duration:
    """`value` milliseconds."""
    return Duration(milliseconds=value)


def seconds(value: float) -> Duration:
    """`value` seconds."""
    return Duration(seconds=value)


def minutes(value: float) -> Duration:
    """`value` minutes."""
    return Duration(minutes=value)


def hours(value: float) -> Duration:
    """`value` hours."""
    return Duration(hours=value)


def days(value: float) -> Duration:
    """`value` days.

    A fixed 24 hours, which is what a `timedelta` can hold honestly. The two
    days a year that are 23 or 25 hours long are a *calendar* question, and the
    answer to those is `Bucket` and `Recurrence`, both of which read a zone.
    """
    return Duration(days=value)


def weeks(value: float) -> Duration:
    """`value` weeks, as a fixed seven `days`."""
    return Duration(weeks=value)


# --- recurrence ----------------------------------------------------------------------


class RecurrenceError(TemporalError):
    """A recurrence could not be understood, or names something not supported.

    A subclass of `TemporalError`, so a caller that already catches the one this
    module raises does not have to learn a second name.
    """


#: The bound on how far `next_after` will search before giving up. Four years,
#: so a February 29th recurrence is always reachable and a genuinely
#: unsatisfiable one (April 31st) refuses in bounded time rather than spinning.
_SEARCH_DAYS = 1462

_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))

#: Calendar weekday names, in cron's day-of-week numbering (Sunday = 0).
_CALENDAR_DAYS = {"SU": 0, "MO": 1, "TU": 2, "WE": 3, "TH": 4, "FR": 5, "SA": 6}


@dataclass(frozen=True, slots=True)
class Recurrence:
    """A repeating schedule that knows the zone its wall clock is read on.

    `wreath.series` goes out of its way to bucket on the local wall clock,
    because stepping naive timestamps advances a calendar day while stepping
    `timestamptz` advances exactly 24 hours — the DST bug, twice a year, in one
    bucket. Scheduling had the same problem and no answer: a cron expression is
    read in UTC, so *"rebalance at 03:00 depot-local"* is an hour wrong for half
    the year, in whichever half the reader does not test in.

    A `Recurrence` carries its zone, so that is one declaration rather than a
    conversion the caller has to remember to redo when the offset changes:

    ```python
    Recurrence.cron("0 3 * * *", tz="Australia/Sydney")
    Recurrence.rrule("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=3", tz="Europe/London")
    ```

    **Both spellings compile to the same five sets**, which is why the calendar
    form is here
    rather than in a translator beside it: the calendar UIs people actually
    mount emit it, and every application that has one grows a lossy translator to
    cron that is wrong across a DST boundary. The supported subset is named in
    `calendar`, and everything outside it is refused by name rather than
    approximated.

    **The two DST days, stated rather than discovered.** A local time that does
    not exist (spring forward) never occurs as a wall-clock reading, so it does
    not match and does not fire — the same answer cron gives. A local time that
    occurs twice (fall back) matches on both passes, but `bucket_key` names the
    *local* minute, so a scheduler keyed on it fires once. Both are pinned by
    tests rather than left to the reader.
    """

    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    weekday: frozenset[int]
    tz: datetime.tzinfo
    text: str

    # -- construction --------------------------------------------------------

    @classmethod
    def cron(cls, expression: str, *, tz: str | datetime.tzinfo = UTC) -> Recurrence:
        """A five-field cron expression, read on `tz`'s wall clock.

        `tz` defaults to UTC, so an existing expression means exactly what it
        meant before this type existed.
        """
        if not isinstance(expression, str):
            raise RecurrenceError(
                f"expected a cron expression, got {type(expression).__name__}"
            )
        fields = expression.split()
        if len(fields) != 5:
            raise RecurrenceError(
                f"cron expression must have 5 fields, got {len(fields)}: {expression!r}"
            )
        minute, hour, day, month, weekday = (
            _parse_cron_field(field, low, high, wrap=index == 4)
            for index, (field, (low, high)) in enumerate(
                zip(fields, _FIELD_BOUNDS, strict=True)
            )
        )
        return cls(
            minute=minute, hour=hour, day=day, month=month, weekday=weekday,
            tz=_tzinfo(tz), text=expression,
        )

    @classmethod
    def calendar(cls, text: str, *, tz: str | datetime.tzinfo = UTC) -> Recurrence:
        """The calendar spelling — `FREQ=WEEKLY;BYDAY=MO,TU` — on `tz`'s wall clock.

        This is the syntax a calendar client emits and RFC 5545 defines, named
        once here so a caller knows what they can paste in. A leading `RRULE:`
        is accepted for the same reason.

        Supported: `FREQ` of `MINUTELY`, `HOURLY`, `DAILY`, `WEEKLY` or
        `MONTHLY`; `INTERVAL` where it divides its parent unit evenly;
        `BYMINUTE`, `BYHOUR`, `BYDAY` (without an ordinal prefix), `BYMONTHDAY`
        and `BYMONTH`.

        Everything else is **refused by name**: `COUNT` and `UNTIL` because a
        recurrence that stops is a different contract from one that repeats and
        a scheduler holding this has nowhere to record exhaustion; `BYSETPOS`,
        `BYWEEKNO`, `BYYEARDAY` and an ordinal `BYDAY` (`2MO`) because they
        select from a generated set rather than constrain a field, which these
        five sets cannot express. Approximating any of them would put a job on
        the wrong day quietly, which is the failure this type exists to remove.
        """
        if not isinstance(text, str):
            raise RecurrenceError(
                f"expected a calendar recurrence string, got {type(text).__name__}"
            )
        body = text.strip()
        if body.upper().startswith("RRULE:"):
            body = body[6:]
        parts: dict[str, str] = {}
        for chunk in body.split(";"):
            if not chunk:
                continue
            name, sep, value = chunk.partition("=")
            if not sep:
                raise RecurrenceError(f"recurrence part is not NAME=VALUE: {chunk!r}")
            parts[name.strip().upper()] = value.strip()
        minute, hour, day, month, weekday = _calendar_fields(parts)
        return cls(
            minute=minute, hour=hour, day=day, month=month, weekday=weekday,
            tz=_tzinfo(tz), text=text,
        )

    # -- reading -------------------------------------------------------------

    def matches_at(self, moment: datetime.datetime) -> bool:
        """Whether `moment`, read on this recurrence's zone, is a firing time.

        The one a scheduler asks. `matches` is the same question in already-split
        wall-clock components, for a caller that has them.
        """
        local = wall_clock(moment, self.tz)
        return self.matches(
            minute=local.minute,
            hour=local.hour,
            day=local.day,
            month=local.month,
            weekday=local.weekday(),
        )

    def matches(
        self, *, minute: int, hour: int, day: int, month: int, weekday: int
    ) -> bool:
        """Whether a wall-clock reading is a firing time. `weekday` is Monday=0.

        Vixie-cron semantics for the day fields: when both day-of-month and
        day-of-week are restricted, either matching is sufficient; otherwise
        both must match. Every crontab implements that rule and a rewrite that
        does not gets one job a month wrong.

        This takes components rather than an instant, so it cannot apply the
        zone itself — which is exactly why `matches_at` exists and is the one to
        reach for. A caller holding a UTC reading and calling this directly gets
        UTC semantics, and that is the bug this type was built to remove.
        """
        if minute not in self.minute or hour not in self.hour or month not in self.month:
            return False
        return self._day_matches(day, weekday)

    def bucket_key(self, moment: datetime.datetime) -> str:
        """The local minute `moment` falls in, as a stable string.

        This is what a scheduler deduplicates on, and it is *local* rather than
        UTC on purpose. The hour that repeats on a fall-back day is two distinct
        instants but one wall-clock minute, so a schedule declared at that time
        fires once rather than twice — which is what "03:30 every day" means to
        the person who wrote it.
        """
        return wall_clock(moment, self.tz).strftime("%Y%m%d%H%M")

    def next_after(self, moment: datetime.datetime) -> Instant:
        """The first firing time strictly after `moment`.

        Searches forward at most four years and then raises, so an unsatisfiable
        recurrence (`0 0 31 2 *` — February 31st) reports itself instead of
        looping. A local time that does not exist on the search day is skipped,
        which is what keeps this and `matches_at` answering the same question.
        """
        start = wall_clock(moment, self.tz).replace(second=0, microsecond=0)
        hours = sorted(self.hour)
        minutes_ = sorted(self.minute)
        for offset in range(_SEARCH_DAYS):
            date = (start + datetime.timedelta(days=offset)).date()
            if date.month not in self.month:
                continue
            if not self._day_matches(date.day, date.weekday()):
                continue
            for hour in hours:
                for minute in minutes_:
                    local = datetime.datetime(
                        date.year, date.month, date.day, hour, minute
                    )
                    if local <= start:
                        continue
                    candidate = from_wall_clock(local, self.tz)
                    # A local time inside a spring-forward gap never occurs as a
                    # wall-clock reading, so `matches_at` would not fire for it.
                    # Reading the candidate back is what keeps the two agreeing.
                    #
                    # **Through UTC, and that is load-bearing.** `from_wall_clock`
                    # returns a datetime already carrying `tz`, and `astimezone`
                    # onto the zone a value already has is a no-op that preserves
                    # the displayed fields rather than renormalising them -- so a
                    # nonexistent 02:30 reads back as 02:30 and the check passes
                    # while the instant it names is really 03:30. Normalising to
                    # UTC first forces the conversion that catches it.
                    if wall_clock(candidate.astimezone(datetime.UTC), self.tz) != local:
                        continue
                    return Instant.of(candidate)
        raise RecurrenceError(
            f"{self.text!r} has no occurrence within {_SEARCH_DAYS} days of "
            f"{moment.isoformat()}; it may name a date that never happens"
        )

    def _day_matches(self, day: int, python_weekday: int) -> bool:
        cron_dow = (python_weekday + 1) % 7  # Python Mon=0 -> cron Sun=0
        dom_restricted = len(self.day) != 31
        dow_restricted = len(self.weekday) != 7
        dom_ok = day in self.day
        dow_ok = cron_dow in self.weekday
        if dom_restricted and dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def __str__(self) -> str:
        return f"{self.text} [{self.tz}]"


def _parse_cron_field(
    field: str, low: int, high: int, *, wrap: bool = False
) -> frozenset[int]:
    """Parse one cron field into the set of values it matches.

    `wrap` is the day-of-week field, where every crontab accepts **7** as a
    second spelling of Sunday (`0`). Refusing it made `0 0 * * 7` -- a form
    people copy straight out of a crontab -- a startup error.
    """
    if wrap:
        high = 7
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        body = part
        try:
            if "/" in part:
                body, _, step_text = part.partition("/")
                step = int(step_text)
                if step < 1:
                    raise RecurrenceError(f"cron step must be >= 1: {part!r}")
            if body == "*":
                start, end = low, high
            elif "-" in body:
                start_text, _, end_text = body.partition("-")
                start, end = int(start_text), int(end_text)
            else:
                start = end = int(body)
        except ValueError as error:
            raise RecurrenceError(f"cron field is not a number: {part!r}") from error
        if start < low or end > high or start > end:
            raise RecurrenceError(f"cron field out of range [{low},{high}]: {part!r}")
        values.update(range(start, end + 1, step))
    if wrap:
        values = {0 if value == 7 else value for value in values}
    return frozenset(values)


#: Calendar recurrence parts this maps onto the five fields. Anything outside it is refused,
#: with `_CALENDAR_SELECTORS` named separately because those fail for a *reason*
#: rather than merely being unimplemented.
_CALENDAR_KNOWN = frozenset(
    {"FREQ", "INTERVAL", "BYMINUTE", "BYHOUR", "BYDAY", "BYMONTHDAY", "BYMONTH", "WKST"}
)
_CALENDAR_SELECTORS = frozenset({"BYSETPOS", "BYWEEKNO", "BYYEARDAY", "BYEASTER"})


def _calendar_numbers(value: str, low: int, high: int, name: str) -> frozenset[int]:
    # The empty check comes *first* and is not a formality. `"".split(",")` is
    # `[""]`, not `[]`, so a trailing `BYHOUR=` would otherwise reach `int("")`
    # and be reported as "not a number" -- true, but it sends the reader looking
    # for a typo in a value that is not there. `wreath mutant` found the
    # unreachable branch this replaces.
    if not value.strip():
        raise RecurrenceError(f"{name} is empty")
    out: set[int] = set()
    for item in value.split(","):
        try:
            number = int(item)
        except ValueError as error:
            raise RecurrenceError(f"{name} is not a number: {item!r}") from error
        if not low <= number <= high:
            raise RecurrenceError(f"{name} out of range [{low},{high}]: {number}")
        out.add(number)
    return frozenset(out)


def _calendar_step(unit: str, span: int, interval: int) -> frozenset[int]:
    """Every `interval`th value of a unit, refusing an interval that does not fit.

    `FREQ=HOURLY;INTERVAL=6` is four times a day forever, because 6 divides 24.
    `INTERVAL=7` is not: it would drift by an hour every day, and there is no
    set of hours that means it. Refusing says so; approximating would put the
    job an hour out and never mention it.
    """
    if interval < 1:
        raise RecurrenceError(f"INTERVAL must be >= 1, got {interval}")
    if span % interval:
        raise RecurrenceError(
            f"INTERVAL={interval} does not divide {span} {unit}s evenly, so it "
            f"names a schedule that drifts rather than one that repeats; express it "
            f"as an explicit BY{unit.upper()} list, or drive it from a durable job"
        )
    return frozenset(range(0, span, interval))


def _calendar_fields(
    parts: dict[str, str],
) -> tuple[frozenset[int], frozenset[int], frozenset[int], frozenset[int], frozenset[int]]:
    """The five cron sets a calendar recurrence names, or a refusal naming what stopped it."""
    for name in ("COUNT", "UNTIL"):
        if name in parts:
            raise RecurrenceError(
                f"{name} bounds a recurrence, and a Recurrence repeats without "
                f"end — a schedule that stops needs somewhere to record that it has, "
                f"which this type does not have. Drive a bounded series from a job."
            )
    for name in sorted(parts.keys() & _CALENDAR_SELECTORS):
        raise RecurrenceError(
            f"{name} selects from a generated set rather than constraining a "
            f"field, which is not expressible here; it is refused rather than "
            f"approximated onto the wrong day"
        )
    unknown = sorted(parts.keys() - _CALENDAR_KNOWN)
    if unknown:
        raise RecurrenceError(f"recurrence part not supported: {', '.join(unknown)}")
    if parts.get("WKST", "MO").upper() != "MO":
        raise RecurrenceError("WKST is only supported as MO")

    freq = parts.get("FREQ", "").upper()
    if not freq:
        raise RecurrenceError("a calendar recurrence needs a FREQ")
    interval = int(parts.get("INTERVAL", "1") or "1")

    minute = (
        _calendar_numbers(parts["BYMINUTE"], 0, 59, "BYMINUTE")
        if "BYMINUTE" in parts
        else None
    )
    hour = _calendar_numbers(parts["BYHOUR"], 0, 23, "BYHOUR") if "BYHOUR" in parts else None
    monthday = (
        _calendar_numbers(parts["BYMONTHDAY"], 1, 31, "BYMONTHDAY")
        if "BYMONTHDAY" in parts
        else None
    )
    month = _calendar_numbers(parts["BYMONTH"], 1, 12, "BYMONTH") if "BYMONTH" in parts else None

    weekday: frozenset[int] | None = None
    if "BYDAY" in parts:
        # Before the per-token walk, for the reason `_calendar_numbers` states:
        # `"".split(",")` yields `[""]`, so `BYDAY=` would otherwise be reported
        # as an ordinal-weekday problem, which it is not.
        if not parts["BYDAY"].strip():
            raise RecurrenceError("BYDAY is empty")
        found: set[int] = set()
        for item in parts["BYDAY"].split(","):
            token = item.strip().upper()
            if token not in _CALENDAR_DAYS:
                raise RecurrenceError(
                    f"BYDAY {item!r} is not a plain weekday; an ordinal such as "
                    f"'2MO' selects the nth occurrence in a period, which is not "
                    f"expressible as a day-of-week set"
                )
            found.add(_CALENDAR_DAYS[token])
        weekday = frozenset(found)

    every_hour = frozenset(range(24))
    every_day = frozenset(range(1, 32))
    every_month = frozenset(range(1, 13))
    every_weekday = frozenset(range(7))

    if freq == "MINUTELY":
        minute = minute or _calendar_step("minute", 60, interval)
    elif freq == "HOURLY":
        minute = minute or frozenset({0})
        hour = hour or _calendar_step("hour", 24, interval)
    elif freq in ("DAILY", "WEEKLY", "MONTHLY"):
        if interval != 1:
            raise RecurrenceError(
                f"INTERVAL={interval} with FREQ={freq} names every {interval}th "
                f"period counted from a start date, which a field set cannot hold; "
                f"only INTERVAL=1 is supported for {freq}"
            )
        minute = minute or frozenset({0})
        hour = hour or frozenset({0})
        if freq == "WEEKLY" and weekday is None:
            raise RecurrenceError("FREQ=WEEKLY needs BYDAY to say which days")
        if freq == "MONTHLY" and monthday is None and weekday is None:
            raise RecurrenceError(
                "FREQ=MONTHLY needs BYMONTHDAY or BYDAY to say which days"
            )
    else:
        raise RecurrenceError(
            f"FREQ={freq} is not supported; use MINUTELY, HOURLY, DAILY, "
            f"WEEKLY or MONTHLY"
        )

    # `minute` has no `every_minute` fallback because every branch above assigns
    # it -- MINUTELY from the interval, the rest from `or frozenset({0})` -- so a
    # fallback here is unreachable. `wreath mutant` proved that by dropping it
    # and finding no test could tell. The other four keep theirs: `hour` really
    # is None for MINUTELY, and the three date fields for most frequencies.
    return (
        minute,
        hour if hour is not None else every_hour,
        monthday if monthday is not None else every_day,
        month if month is not None else every_month,
        weekday if weekday is not None else every_weekday,
    )


# --- the relative formatter ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Locale:
    """One language's phrasing for a relative time.

    `units` maps a unit to `(singular, plural)`. English needs only that,
    which is exactly why it is a poor guide: most languages need a *plural
    rule*, not a pair. When a second language lands, this grows a
    `plural(n) -> category` callable per locale implementing the CLDR
    categories (`zero`/`one`/`two`/`few`/`many`/`other`) and
    `units` becomes a mapping keyed by category. Nothing outside this module
    changes when that happens, which is the whole point of the seam.
    """

    just_now: str
    past: str            # formatted with {value}
    future: str
    yesterday: str
    tomorrow: str
    units: dict[str, tuple[str, str]]

    def count(self, amount: int, unit: str) -> str:
        singular, plural = self.units[unit]
        return f"{amount} {singular if amount == 1 else plural}"


_LOCALES: dict[str, _Locale] = {
    "en": _Locale(
        just_now="just now",
        past="{value} ago",
        future="in {value}",
        yesterday="yesterday",
        tomorrow="tomorrow",
        units={
            "second": ("second", "seconds"),
            "minute": ("minute", "minutes"),
            "hour": ("hour", "hours"),
            "day": ("day", "days"),
            "month": ("month", "months"),
            "year": ("year", "years"),
        },
    ),
}

#: Rendered when the requested locale has no table. English rather than an
#: error: a missing translation should read oddly, never fail a page.
_FALLBACK_LOCALE = "en"


def _locale_for(locale: str) -> _Locale:
    table = _LOCALES.get(locale)
    if table is not None:
        return table
    # "fr-CA" should reach "fr" before falling back to English, so a regional
    # tag never costs a translation that exists.
    base = locale.partition("-")[0]
    return _LOCALES.get(base, _LOCALES[_FALLBACK_LOCALE])


def relative(
    value: datetime.datetime,
    *,
    now: datetime.datetime | None = None,
    locale: str = "en",
) -> str:
    """`value` phrased the way a person would say it, relative to `now`.

    `"just now"`, `"3 minutes ago"`, `"yesterday"`, `"in 2 hours"`.
    Pass `request.locale` to honour the caller's `Accept-Language`; an
    unknown locale renders English rather than failing.

    Both `value` and `now` must carry an offset. Comparing an aware moment
    to a naive one is a `TypeError` waiting to fire on whichever request first
    supplies the other kind, so it is refused here with an explanation.
    """
    moment = Instant.of(value)
    reference = _utc_now() if now is None else Instant.of(now)
    table = _locale_for(locale)

    seconds = (reference - moment).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)

    phrase = _phrase(seconds, table, future=future)
    if phrase is not None:
        return phrase
    counted = _counted(seconds, table)
    return (table.future if future else table.past).format(value=counted)


def _phrase(seconds: float, table: _Locale, *, future: bool) -> str | None:
    """The phrasings that are words rather than counts."""
    if seconds < 45:
        return table.just_now
    # A day either side reads far better as yesterday/tomorrow than as "1 day".
    if 79200 <= seconds < 129600:          # 22h to 36h
        return table.tomorrow if future else table.yesterday
    return None


def _counted(seconds: float, table: _Locale) -> str:
    if seconds < 90:
        return table.count(1, "minute")
    minutes = round(seconds / 60)
    if minutes < 45:
        return table.count(minutes, "minute")
    hours = round(seconds / 3600)
    if hours < 22:
        return table.count(hours, "hour")
    days = round(seconds / 86400)
    if days < 26:
        return table.count(days, "day")
    months = round(seconds / 2629800)      # a mean Gregorian month
    if months < 11:
        return table.count(months, "month")
    return table.count(round(seconds / 31557600), "year")


# --- rendering for the wire ------------------------------------------------------------


def format_iso(value: Any) -> str:
    """The ISO-8601 form of a date, time, datetime, or duration.

    One function so every surface renders a temporal value identically — the
    JSON encoder, a template, and a log line cannot disagree.

    **Before moving this to C:** this and `Instant.parse` are the two
    operations on the response path, so they are where C would pay if anywhere.
    Measure first — encode a realistic response body containing timestamps with
    `wreath-decomp`, ablate the formatting, and compare against the A/A noise
    floor. `datetime.isoformat` is already C, so the cost being measured is the
    dispatch around it, and that may well be below the floor. Do not write it on
    intuition.
    """
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return format_duration(value)
    raise TemporalError(f"{type(value).__name__} is not a temporal value")


#: The types `jsonable` knows how to render. `datetime` precedes `date`
#: because it subclasses it.
_TEMPORAL = (datetime.datetime, datetime.date, datetime.time, datetime.timedelta)


def jsonable(value: Any) -> Any:
    """`value` with every temporal object replaced by its ISO-8601 string.

    Used by the JSON encoder's fallback so a handler never writes
    `.isoformat()` by hand. Containers are rebuilt rather than mutated, and
    anything that is not temporal is returned untouched — so a genuinely
    unserializable object reaches the encoder again and raises the error it
    should, rather than being swallowed here.

    An object may also say how to become JSON by defining `__jsonable__`,
    which is how a result type like `wreath.series.SeriesResult` can be
    returned from a handler directly. The hook is **opt-in and deliberately not
    a blanket dataclass rule**: "serialize any dataclass" would put every field
    of every model a handler happened to return on the wire, including the ones
    a sensitive-field guard exists to keep off it. A type has to say it knows
    how to become JSON. The result is walked again, so a hook may return
    temporal values without converting them itself.

    Cost, stated as inspection rather than measurement: this whole function runs
    only after the encoder has already raised `TypeError`, so a payload that
    encodes normally never reaches it and pays nothing. Within the walk the hook
    is one attribute lookup per non-temporal, non-container leaf.
    """
    if isinstance(value, _TEMPORAL):
        return format_iso(value)
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    hook = getattr(type(value), "__jsonable__", None)
    if hook is not None:
        return jsonable(hook(value))
    return value


# -- protobuf: the well-known Timestamp -------------------------------------
#
# `google.protobuf.Timestamp` is the interchange shape for an instant, and it is
# what a peer in another language expects on the wire. Declaring it here rather
# than in `wreath.protobuf` keeps the direction of dependency the same as every
# other surface time crosses: the codec knows nothing about time, and temporal
# says how time is spelled for it -- exactly as it already does for JSON, the
# OpenAPI format, the ORM column and the GraphQL scalar.


@_message
class Timestamp:
    """`google.protobuf.Timestamp`: seconds and nanos from the Unix epoch, UTC.

    The two field numbers are the wire contract with every peer and are fixed
    by the specification. `nanos` is **always** in `0..999_999_999`, including
    before the epoch -- see `to_timestamp`.
    """

    seconds: int = _protobuf_field(1)
    nanos: int = _protobuf_field(2, kind="int32")


#: The specification's bound on `Timestamp.nanos`, restated because a value
#: outside it is a peer's bug that would otherwise become a wrong time here.
_NANOS_PER_SECOND = 1_000_000_000
_NANOS_PER_MICROSECOND = 1_000

#: The Unix epoch as an `Instant`, not a bare `datetime`. Arithmetic preserves
#: the subclass, so `from_timestamp` returns an `Instant` -- which is the whole
#: contract of this module and must not have a hole at the decode boundary.
#: `to_timestamp` subtracts from it rather than calling `datetime.timestamp()`
#: so the pre-epoch sign convention falls out of `timedelta`'s own
#: normalisation instead of a correction applied afterwards.
_EPOCH = Instant(1970, 1, 1, tzinfo=datetime.UTC)


def to_timestamp(value: datetime.datetime) -> Timestamp:
    """`value` as a `google.protobuf.Timestamp`.

    A naive value is refused rather than assumed UTC, for the same reason
    `Instant` refuses one: guessing is the bug this module exists to prevent.

    **`nanos` is normalised non-negative.** For an instant before the epoch the
    specification requires `seconds` to carry the sign and `nanos` to stay in
    `0..999_999_999` -- so half a second before the epoch is
    `seconds=-1, nanos=500_000_000`, never `seconds=0, nanos=-500_000_000`.
    Python's `timestamp()` returns a signed float, and the obvious `int()` of it
    truncates toward zero, which produces exactly the illegal second form and
    puts a peer's reading one second out.

    Raises:
        TemporalError: `value` has no offset.
    """
    # `utcoffset()` alone, not `tzinfo is None or utcoffset() is None`. The
    # first clause is subsumed -- `datetime.utcoffset()` already returns None
    # when `tzinfo` is -- and it is *narrower*: a tzinfo whose `utcoffset()`
    # returns None is naive in effect while `tzinfo is not None`, which the
    # short-circuit would have waved through had the second clause ever been
    # dropped. One spelling of one check, so the two cannot drift apart.
    if value.utcoffset() is None:
        raise TemporalError(
            "a naive datetime has no moment to encode; attach a tzinfo whose "
            "utcoffset() is not None, or use Instant.of(value, "
            "assume='Area/City') to place it"
        )
    delta = value - _EPOCH
    # `timedelta` already normalises to non-negative microseconds with the sign
    # carried by `days`, which is the same convention Timestamp requires -- so
    # deriving from it gets the pre-epoch case right by construction rather than
    # by a correction applied afterwards.
    seconds = delta.days * 86400 + delta.seconds
    return Timestamp(seconds=seconds, nanos=delta.microseconds * _NANOS_PER_MICROSECOND)


def from_timestamp(value: Timestamp) -> Instant:
    """A `google.protobuf.Timestamp` as an aware `Instant` in UTC.

    The wire carries a moment and no zone, so the result is in UTC. Where the
    reader's zone matters it has to travel in its own field; `.to(zone)` places
    the result once it arrives.

    Sub-microsecond `nanos` are **refused rather than truncated**. Python's
    resolution is microseconds, and silently dropping the remainder would make a
    round trip through wreath lossy for a peer that had the precision -- the
    kind of loss that is invisible until two systems disagree about ordering.

    Raises:
        TemporalError: `nanos` is outside `0..999_999_999`, or carries
            precision finer than a microsecond.
    """
    if not 0 <= value.nanos < _NANOS_PER_SECOND:
        raise TemporalError(
            f"Timestamp.nanos must be in 0..999999999, got {value.nanos}; a "
            "negative remainder means the sender did not normalise a pre-epoch "
            "instant onto the seconds field"
        )
    if value.nanos % _NANOS_PER_MICROSECOND:
        remainder = value.nanos % _NANOS_PER_MICROSECOND
        raise TemporalError(
            f"Timestamp.nanos={value.nanos} carries {remainder} nanosecond(s) "
            "below microsecond resolution, which a Python datetime cannot "
            "hold; truncating would lose the remainder silently"
        )
    return _EPOCH + datetime.timedelta(
        seconds=value.seconds, microseconds=value.nanos // _NANOS_PER_MICROSECOND
    )
