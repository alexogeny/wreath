"""Instants, durations, and zones — one meaning for time at every boundary.

Time is the type that has to mean the same thing in six places at once: the
column it is stored in, the parameter it arrives as, the JSON it leaves as, the
OpenAPI schema, the generated TypeScript, and the GraphQL scalar. Most
applications re-decide it in each of them, and the drift is invisible until a
client reads a naive timestamp as if it were UTC.

The usual answer is to reach for ``arrow`` or ``pendulum`` in every module.
Wreath's core carries no mandatory dependencies, so that answer is unavailable —
which turns out to be the better outcome, because it forces the decision into
one place that every surface already goes through::

    from wreath.temporal import Instant, now, relative

    started = Instant.parse(request.query["since"])   # aware, or it raises
    when = relative(started, locale=request.locale)   # "3 hours ago"

**An `Instant` is always zone-aware.** It subclasses :class:`datetime.datetime`,
so it stores, compares, and does arithmetic exactly like one — but it cannot be
constructed without an offset. Assuming UTC for a naive value is the single bug
this module exists to prevent, and it is refused loudly rather than guessed at.
Where a naive value genuinely needs a zone, say so: ``Instant.of(value,
assume="Australia/Sydney")``.

**The relative formatter takes a locale.** "3 hours ago" is the string every
codebase ends up hand-rolling at the edge, and it is also the one that is
locale-dependent. Keeping it here — reached through ``request.locale`` — means
translating it later is a parameter to one function rather than a hunt through
every template. English ships today; :data:`_LOCALES` is where the next
language goes, and the docstring there marks exactly where CLDR plural rules
slot in.

Everything is pure Python over stdlib ``datetime``/``zoneinfo``. There is no
native twin yet; see the note on :func:`format_iso` for what would have to be
measured before writing one.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "BUCKETS",
    "Bucket",
    "Day",
    "Hour",
    "Instant",
    "Minute",
    "Month",
    "Quarter",
    "TemporalError",
    "Week",
    "Year",
    "bucket",
    "format_duration",
    "format_iso",
    "from_wall_clock",
    "jsonable",
    "now",
    "parse",
    "parse_duration",
    "relative",
    "wall_clock",
    "zone",
]

UTC = datetime.UTC


class TemporalError(ValueError):
    """A value could not be understood as a time, a duration, or a zone.

    A ``ValueError`` subclass because that is what a caller parsing untrusted
    input already handles.
    """


def zone(name: str) -> ZoneInfo:
    """The named IANA time zone, or a :class:`TemporalError` naming it.

    ``ZoneInfoNotFoundError`` is accurate but arrives without the name in the
    common formatting, and a bad zone is nearly always a typo in configuration.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise TemporalError(f"unknown time zone {name!r}") from error


class Instant(datetime.datetime):
    """A moment in time that always knows its offset.

    A :class:`datetime.datetime` subclass, so it is stored by ``TimestampTz``,
    compared, and added to a ``timedelta`` exactly like one — arithmetic and
    ``astimezone`` return an ``Instant`` because CPython preserves the subclass.
    What it will not do is exist without a ``tzinfo``.
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

        Accepts everything ``datetime.fromisoformat`` does, which since 3.11
        includes the trailing ``Z`` every JSON client emits. A string without an
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
        """Adopt an existing ``datetime``.

        A naive value needs ``assume`` to say which zone it was written in.
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

        UTC renders as ``+00:00`` rather than ``Z`` so that two services never
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
        """This moment as a person would say it — ``"3 hours ago"``."""
        return relative(self, now=now, locale=locale)


def _tzinfo(value: str | datetime.tzinfo) -> datetime.tzinfo:
    return zone(value) if isinstance(value, str) else value


def now(in_zone: str | datetime.tzinfo = UTC) -> Instant:
    """The current moment, in UTC unless another zone is named."""
    return Instant.of(datetime.datetime.now(_tzinfo(in_zone)))


def _utc_now() -> Instant:
    """The current moment, reachable from functions that shadow ``now``."""
    return Instant.of(datetime.datetime.now(UTC))


def parse(text: str) -> Instant:
    """An ISO-8601 timestamp that carries an offset. See :meth:`Instant.parse`."""
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
    """``value`` as a plain naive ``datetime`` on ``tz``'s wall clock.

    Public because reading an instant on somebody's clock is the first step of
    every calendar calculation in the codebase, and each caller that reinvents
    it reinvents the trap below with it. :class:`Bucket` uses it to truncate,
    and :mod:`wreath.series` to step a comparison period back a month.

    Built component-wise rather than with ``replace(tzinfo=None)`` because
    ``replace`` preserves the subclass, and an :class:`Instant` refuses to exist
    without an offset -- correctly, since that is the bug it is here to prevent.

    ``fold`` is dropped, and nothing is lost by dropping it: both passes of an
    ambiguous hour read the same on a wall clock, which is exactly what
    ``value AT TIME ZONE zone`` returns for them too. Putting a local time back
    on the timeline is where the choice actually happens -- see
    :func:`from_wall_clock`.
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

    The inverse of :func:`wall_clock`, and public for the same reason: every
    calendar calculation here ends by putting a local time back on the
    timeline, and a caller that reinvents that reinvents the ``fold`` question
    with it.

    On the two days a year a zone changes offset, a local time can name two
    instants or none at all. This resolves **to the later of the two
    candidates**, because that is what ``timestamp AT TIME ZONE zone`` does.
    That is measured, not assumed: 864 samples across nine zones, both
    transition directions, and the ambiguous, skipped and ordinary cases, with
    no disagreement -- including a zone with a half-hour DST step
    (Australia/Lord_Howe), one whose tzdata entry uses negative DST
    (Europe/Dublin), and four whose transition is at local midnight
    (America/Santiago, Asia/Beirut, America/Havana, Africa/Cairo) so that a
    *day* boundary lands in the gap rather than an hour boundary.

    Matching it is not tidiness. A bucket boundary computed here and one
    generated by ``generate_series`` have to be the same instant: when they
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

    ``trunc`` is the PostgreSQL ``date_trunc`` unit that assigns a row to a
    bucket; ``step`` is the ``generate_series`` interval that walks from one
    bucket to the next; and :meth:`floor` and :meth:`end_of` are the Python
    answers to the same two questions, for code that has a moment in hand and
    no connection.

    Both SQL fragments are drawn from :data:`BUCKETS` rather than from a
    caller, so neither is ever user input -- :func:`bucket` is the only way to
    reach one by name, and it refuses anything not in the table.
    """

    #: The name a caller writes and a payload carries -- ``"day"``.
    name: str
    #: The ``date_trunc`` unit. Equal to :attr:`name` for every unit today, and
    #: kept separate because the two are not the same kind of thing.
    trunc: str
    #: The ``generate_series`` step, as an interval literal: ``"1 day"``.
    step: str
    #: Calendar months per step, for the units a ``timedelta`` cannot hold.
    #: Zero means :attr:`delta` is the width instead.
    months: int = 0
    #: Fixed width, for the units that have one. ``None`` for calendar units.
    delta: datetime.timedelta | None = None

    def floor(self, value: datetime.datetime, in_zone: str | datetime.tzinfo) -> Instant:
        """The instant this bucket starts, for the wall clock in ``in_zone``.

        The mirror of ``date_trunc(unit, value AT TIME ZONE zone)``: read the
        moment on the zone's wall clock, truncate there, and convert back. Doing
        it in that order is what makes a "day" the reader's calendar day rather
        than a fixed 24 hours -- see :meth:`end_of` for why that distinction has
        teeth.

        On the two days a year a zone changes offset, a local wall clock can be
        ambiguous or absent. Both cases resolve to the *later* of the candidate
        instants, via :func:`from_wall_clock`, because that is what
        ``AT TIME ZONE`` does -- **measured against a live PostgreSQL**, across
        nine zones and both transition directions, rather than reasoned from the
        documentation. An earlier revision resolved an ambiguous local time to
        the first of its two instants, which disagreed with ``date_trunc`` for
        every value in the second pass of a repeated hour.

        **Comparing the result across zones needs care**, and this is CPython's
        rule rather than this module's: by PEP 495 a datetime inside an
        ambiguous hour compares *unequal* to the same instant expressed in
        another zone, so that comparison stays transitive when one local time
        names two instants. Convert with ``astimezone`` before comparing, or
        compare two values in the same zone. The trap is that the naive form is
        correct on every day but one.

        **``floor(v) <= v < end_of(v)`` does not hold for every value**, at
        ``minute`` granularity inside a repeated hour, and that is a property of
        the calendar rather than a defect here: one local time names two
        instants, a bucket start can only be one of them, and values at the
        other one fall outside their own bucket. **PostgreSQL does the same
        thing with the same inputs** — verified, not assumed — so this is the
        shared answer rather than a divergence. At ``hour`` and coarser the
        window is wide enough to contain both passes and the invariant holds.

        Every caller in the tree passes an already-truncated boundary except the
        sealing watermark, and there the boundary is now the one
        ``generate_series`` will emit, which is the property sealing actually
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
        :meth:`floor` up to but not including this. Sealing (when a bucket
        becomes final) is the other caller: a bucket cannot settle before the
        moment it stops accepting rows, and that moment is this one.

        The step is added on the *local* wall clock and then converted back, so
        a day spanning a DST change is 23 or 25 hours rather than 24, and a
        month is a calendar month rather than an approximation. The conversion
        back resolves an ambiguous or skipped boundary the same way
        :meth:`floor` does -- see :func:`from_wall_clock` -- so a bucket's end
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
    """The named bucket, or a :class:`TemporalError` listing the real ones."""
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
    """An ISO-8601 duration such as ``PT3H`` or ``P1DT2H30M``.

    The stdlib has no parser for these and configuration files are full of
    them, which is how every codebase ends up with its own half-correct one.

    Years and months are rejected: they are not a fixed number of seconds, so
    a ``timedelta`` cannot hold one honestly.
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
    """An ISO-8601 duration, the inverse of :func:`parse_duration`.

    Zero renders as ``PT0S`` rather than the empty ``P``, which is what a
    reader — and most parsers — expect.

    Built from the timedelta's own integer ``days``/``seconds``/``microseconds``
    rather than from ``total_seconds()``. A float total loses precision on large
    values, and formatting one with ``%g`` rounds to six significant digits and
    switches to scientific notation for small ones — so ``timedelta.max`` came
    out a day too long and one microsecond came out ``PT1e-06S``, which
    :func:`parse_duration` rejects. A formatter whose own inverse cannot read its
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


# --- the relative formatter ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Locale:
    """One language's phrasing for a relative time.

    ``units`` maps a unit to ``(singular, plural)``. English needs only that,
    which is exactly why it is a poor guide: most languages need a *plural
    rule*, not a pair. When a second language lands, this grows a
    ``plural(n) -> category`` callable per locale implementing the CLDR
    categories (``zero``/``one``/``two``/``few``/``many``/``other``) and
    ``units`` becomes a mapping keyed by category. Nothing outside this module
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
    """``value`` phrased the way a person would say it, relative to ``now``.

    ``"just now"``, ``"3 minutes ago"``, ``"yesterday"``, ``"in 2 hours"``.
    Pass ``request.locale`` to honour the caller's ``Accept-Language``; an
    unknown locale renders English rather than failing.

    Both ``value`` and ``now`` must carry an offset. Comparing an aware moment
    to a naive one is a ``TypeError`` waiting to fire on whichever request first
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

    **On a native twin:** this and :meth:`Instant.parse` are the two operations
    on the response path, so they are where C would pay if anywhere. Before
    writing it, measure: encode a realistic response body containing timestamps
    with `wreath-decomp`, ablate the formatting, and compare against the A/A
    noise floor. `datetime.isoformat` is already C, so the cost being measured
    is the dispatch around it, and that may well be below the floor — which
    would mean a native twin is not justified. Do not write it on intuition.
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


#: The types :func:`jsonable` knows how to render. `datetime` precedes `date`
#: because it subclasses it.
_TEMPORAL = (datetime.datetime, datetime.date, datetime.time, datetime.timedelta)


def jsonable(value: Any) -> Any:
    """``value`` with every temporal object replaced by its ISO-8601 string.

    Used by the JSON encoder's fallback so a handler never writes
    ``.isoformat()`` by hand. Containers are rebuilt rather than mutated, and
    anything that is not temporal is returned untouched — so a genuinely
    unserializable object reaches the encoder again and raises the error it
    should, rather than being swallowed here.

    An object may also say how to become JSON by defining ``__jsonable__``,
    which is how a result type like ``wreath.series.SeriesResult`` can be
    returned from a handler directly. The hook is **opt-in and deliberately not
    a blanket dataclass rule**: "serialize any dataclass" would put every field
    of every model a handler happened to return on the wire, including the ones
    a sensitive-field guard exists to keep off it. A type has to say it knows
    how to become JSON. The result is walked again, so a hook may return
    temporal values without converting them itself.

    Cost, stated as inspection rather than measurement: this whole function runs
    only after the encoder has already raised ``TypeError``, so a payload that
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
