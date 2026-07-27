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
    "Instant",
    "TemporalError",
    "format_duration",
    "format_iso",
    "jsonable",
    "now",
    "parse",
    "parse_duration",
    "relative",
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
    """
    if not isinstance(value, datetime.timedelta):
        raise TemporalError(f"expected a timedelta, got {type(value).__name__}")
    seconds = value.total_seconds()
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    out = [sign, "P"]
    if days:
        out.append(f"{_trim(days)}D")
    if hours or minutes or secs or not days:
        out.append("T")
        if hours:
            out.append(f"{_trim(hours)}H")
        if minutes:
            out.append(f"{_trim(minutes)}M")
        if secs or (not hours and not minutes):
            out.append(f"{_trim(secs)}S")
    return "".join(out)


def _trim(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


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
    """
    if isinstance(value, _TEMPORAL):
        return format_iso(value)
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value
