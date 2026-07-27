"""Tiers: the same declaration, materialised at more than one grain.

Stage 7 settled a bucket once it could no longer change. A tier is that idea
applied twice: **a tier is this view at a coarser grain.** Nothing new is stored
and no second table appears -- a daily tier and an hourly tier are two view keys
in `series_buckets`, filed under the same `(view, params, bucket)` primary
key that a sealed ungrouped view already uses, because `view_key` is derived
from the declaration's *content* and the grain is part of that content.

That is why §7.3's claim that rollup and settlement "are the same thing from two
ends" is not a metaphor here. Settling computes a bucket that will not change
again; rolling up computes a *coarser* bucket that will not change again. Same
watermark, same table, same insert, same refusal to overwrite. The only thing
that differs is which grain you asked for, and that already lives in the key.

Two consequences follow, and both are load-bearing:

* **A coarser tier cannot exist without a seal.** A tier stores a value on the
  understanding that it is final, and only `seal`
  says when a value is final. `retain()` past `raw` therefore requires
  `seal()` rather than inventing a second watermark.
* **Rollup is computed from the source rows, not from the finer tier.** At this
  stage nothing is ever removed, so raw is always present and recomputing from
  it is both correct and free of the average-of-averages trap. Tier-from-tier
  rollup becomes necessary only when retention starts actually removing rows,
  and that is the moment `rollup_safe` earns its
  keep rather than merely being recorded.

What retention means today
--------------------------

`retain(raw="3 days", day="1 year")` does **not** delete anything, and this
stage adds no way to. It is a promise about what will stay warm, and the read
path keeps that promise honestly: a range older than raw's window is served from
the coarsest tier that still covers it, *even though raw happens to still be
there*. A query written today therefore keeps returning the same answer on the
day a later stage starts enforcing the window, instead of quietly changing shape
when the rows it was secretly relying on disappear.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from ..temporal import BUCKETS, Bucket
from ..temporal import zone as _zone_of

#: A day, in seconds. The boundary past which a materialised grain can serve
#: only the zone it was computed in -- see `serves_zone`.
_DAY = 86400.0

#: Mean lengths for the calendar units, used only to *order* grains by
#: coarseness. Never used as an interval: the arithmetic that has to be right
#: lives in `Bucket.floor` and `Bucket.end_of`, which use the calendar.
_MEAN_MONTH = 2629800.0


def width(grain: Bucket) -> float:
    """Roughly how long `grain` is, for ordering grains from fine to coarse.

    Approximate on purpose. Nothing computes a boundary from this -- it answers
    "is a month coarser than a week?", which does not need a calendar.
    """
    if grain.months:
        return grain.months * _MEAN_MONTH
    assert grain.delta is not None
    return grain.delta.total_seconds()


@dataclass(frozen=True, slots=True)
class Tier:
    """One rung of the ladder: a grain, and how long it is guaranteed warm.

    Args:
        grain: the bucket width this tier stores, or `None` for the raw rows
            themselves. Raw is a tier because retention is a ladder and the
            source rows are its bottom rung.
        keep: seconds this tier is guaranteed queryable, or `None` for
            "indefinitely". Never a deletion instruction -- see the module
            docstring.
    """

    grain: Bucket | None
    keep: float | None

    @property
    def name(self) -> str:
        return "raw" if self.grain is None else self.grain.name

    @property
    def is_raw(self) -> bool:
        return self.grain is None

    def covers(self, instant: Any, *, now: Any) -> bool:
        """Whether this tier still promises to answer for `instant`."""
        if self.keep is None:
            return True
        return instant >= now - datetime.timedelta(seconds=self.keep)

    def __repr__(self) -> str:
        window = "forever" if self.keep is None else f"{self.keep:g}s"
        return f"<Tier {self.name} kept {window}>"


@dataclass(frozen=True, slots=True)
class Ladder:
    """The declared tiers, finest first, with `raw` always at the bottom."""

    tiers: tuple[Tier, ...]

    @property
    def raw(self) -> Tier:
        return self.tiers[0]

    @property
    def materialised(self) -> tuple[Tier, ...]:
        """Every tier that stores rows -- everything above `raw`."""
        return self.tiers[1:]

    @property
    def raw_bounded(self) -> bool:
        """Whether the source rows have a declared expiry.

        The one fact two separate refusals turn on. An unbounded raw window
        makes rolling up from source sound (§7.5) *and* makes reopening a sealed
        bucket sound (§7.2), for the same reason in both cases: the rows being
        recomputed from are always there. Named once so the two checks cannot
        drift into disagreeing about what "bounded" means.
        """
        return self.raw.keep is not None

    def named(self, name: str) -> Tier | None:
        for tier in self.tiers:
            if tier.name == name:
                return tier
        return None

    def __iter__(self) -> Any:
        return iter(self.tiers)

    def __len__(self) -> int:
        return len(self.tiers)


def build(spec: dict[str, Any], *, refuse: Any) -> Ladder:
    """Turn `retain(raw=…, hour=…, day=…)` into an ordered ladder.

    `refuse` is the exception class to raise, passed in so this module does
    not import the public one and create a cycle.
    """
    if not spec:
        raise refuse(
            "retain() needs at least one tier. Write retain(raw=...) to say how "
            "long the source rows stay warm, and add coarser grains above it"
        )
    if "raw" not in spec:
        raise refuse(
            "retain() must name 'raw': it is the bottom rung of the ladder, and "
            "without it there is no statement about how long the source rows "
            f"answer for. Got {', '.join(sorted(spec))}"
        )

    tiers: list[Tier] = []
    for name, value in spec.items():
        if name == "raw":
            grain = None
        else:
            if name not in BUCKETS:
                raise refuse(
                    f"retain() got unknown tier {name!r}; tiers are 'raw' or a "
                    f"bucket name: {', '.join(sorted(BUCKETS))}"
                )
            grain = BUCKETS[name]
        tiers.append(Tier(grain=grain, keep=_window(value, name, refuse)))

    tiers.sort(key=lambda item: -1.0 if item.grain is None else width(item.grain))

    # A coarser tier kept for less time than a finer one leaves a hole: the
    # range is too old for the fine tier and already past the coarse one, so
    # nothing answers. That is a declaration mistake rather than a runtime
    # surprise, so it is refused where it was written.
    seen: float | None = 0.0
    for tier in tiers:
        if seen is None and tier.keep is not None:
            raise refuse(
                f"retain() keeps {tier.name!r} for a bounded time but a finer "
                "tier forever. Retention has to grow as the grain coarsens, or "
                "a range falls past the coarse tier while the fine one is still "
                "answering -- which is a ladder with a rung missing"
            )
        if seen is not None and tier.keep is not None and tier.keep < seen:
            raise refuse(
                f"retain() keeps {tier.name!r} for less time than a finer tier. "
                "Retention has to grow as the grain coarsens, or an old range "
                "has no tier left that covers it"
            )
        seen = tier.keep
    return Ladder(tuple(tiers))


#: Retention windows are written the way people say them -- `"3 days"`,
#: `"14 days"`, `"1 year"`. Deliberately a *different* parser from
#: `seal(after=)`, and the difference is the point.
#:
#: A seal allowance is elapsed time and has to be exact, so
#: `temporal.parse_duration` refuses months and years for the good reason that
#: they are not a fixed number of seconds. A retention window is a promise about
#: roughly how long something stays warm, and it is only ever compared against
#: `now - keep` to decide which tier answers. "About a year" is the honest
#: meaning of `day="1 year"`, so mean lengths are the right precision here
#: rather than a compromise -- and keeping this local leaves `parse_duration`
#: as strict as it should be.
_UNITS: dict[str, float] = {
    "ms": 0.001,
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
    "w": 604800.0, "week": 604800.0, "weeks": 604800.0,
    "month": _MEAN_MONTH, "months": _MEAN_MONTH,
    "y": 31557600.0, "year": 31557600.0, "years": 31557600.0,
}


def _window(value: Any, name: str, refuse: Any) -> float | None:
    """Seconds, or `None` for forever.

    `None` is spelled `None` rather than `0` or `"forever"` because it
    is the *absence* of an expiry, and the design writes it that way.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise refuse(
            f"retain({name}=…) takes a duration like '3 days' or a number of "
            f"seconds, or None for forever; got {value!r}"
        )
    if isinstance(value, str):
        import re

        match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]+)\s*", value)
        if match is None or match.group(2).lower() not in _UNITS:
            raise refuse(
                f"retain({name}={value!r}) is not a duration. Write it as a "
                "number and a unit -- '3 days', '14 days', '1 year', '2h' -- or "
                "as a number of seconds, or None to keep it indefinitely"
            )
        seconds = float(match.group(1)) * _UNITS[match.group(2).lower()]
    else:
        seconds = float(value)
    if seconds <= 0:
        raise refuse(
            f"retain({name}=…) must be a positive duration, or None for forever. "
            f"Got {value!r}, and zero is not a way to spell 'delete immediately' "
            "-- nothing here deletes anything"
        )
    return seconds


# -- which tier answers, and for which zones ----------------------------------


def serves_zone(
    grain: Bucket | None, stored_zone: str, read_zone: str, *, at: Any
) -> bool:
    """Whether a tier cut in `stored_zone` can answer for `read_zone`.

    §7.4's rule, and the non-obvious half of tiering: **a materialised tier is
    zone-specific.** Daily buckets cut in `Pacific/Auckland` cannot answer a
    question about London days, because the boundaries do not line up and no
    amount of re-aggregation recovers them.

    The rule is that the offset *difference* between the two zones has to be a
    whole multiple of the grain. Hourly rows therefore serve any whole-hour
    zone but not `Asia/Kolkata` (+5:30), `Asia/Kathmandu` (+5:45), or
    `Pacific/Chatham` (+12:45). Daily and coarser rows serve only the zone
    they were computed in -- an offset difference is never a whole number of
    days.

    Raw always serves, because raw is not cut into anything yet.

    **The sampling caveat, stated rather than hidden.** Offsets move, so this
    asks at one instant. It is checked at each end of the requested range by
    the caller, which catches a zone whose offset is permanently fractional --
    the case that matters, since those offsets do not change. A zone that
    switched between fractional and whole offsets *inside* a range would slip
    through, and no zone in the IANA database does that today.
    """
    if grain is None or read_zone == stored_zone:
        return True
    seconds = width(grain)
    if seconds >= _DAY:
        return False
    read_offset = _offset(read_zone, at)
    stored_offset = _offset(stored_zone, at)
    return (read_offset - stored_offset) % seconds == 0


def _offset(zone_name: str, at: Any) -> float:
    delta = at.astimezone(_zone_of(zone_name)).utcoffset()
    return 0.0 if delta is None else delta.total_seconds()


@dataclass(frozen=True, slots=True)
class Segment:
    """One contiguous piece of a requested range, and what will answer it."""

    start: Any
    end: Any
    tier: Tier

    @property
    def grain(self) -> str:
        return self.tier.name

    def __repr__(self) -> str:
        return f"<Segment {self.start:%Y-%m-%d}..{self.end:%Y-%m-%d} from {self.tier.name}>"


def plan(
    *,
    ladder: Ladder,
    requested: Bucket,
    start: Any,
    end: Any,
    now: Any,
    stored_zone: str,
    read_zone: str,
    allow_coarsening: bool,
    refuse: Any,
) -> tuple[Segment, ...]:
    """Split `[start, end)` into contiguous pieces, one authoritative tier each.

    §7.4's three steps: split by tier availability, pick **exactly one** tier
    per piece so a boundary can neither double-count nor gap, and hand the
    pieces back for the caller to stitch onto one spine.

    The split points are the retention edges, `now - keep`, because that is
    the only place availability changes. Each piece is then resolved from its
    *oldest* instant, which is the binding one -- a tier that covers the start
    of a half-open piece covers all of it.

    Preference order is coarsest-first among the tiers that can answer, because
    a stored coarse row is the cheapest correct answer. Raw is the fallback and
    always correct, but it is chosen only when no materialised tier promises to
    cover the piece -- keeping the promise even while nothing yet enforces it,
    so the query does not change shape on the day it starts being enforced.
    """
    edges = {start, end}
    for tier in ladder:
        if tier.keep is None:
            continue
        edge = now - datetime.timedelta(seconds=tier.keep)
        if start < edge < end:
            edges.add(edge)
    bounds = sorted(edges)

    pieces: list[Segment] = []
    for left, right in zip(bounds, bounds[1:], strict=False):
        if left >= right:
            continue
        tier = _authoritative(
            ladder=ladder,
            requested=requested,
            oldest=left,
            now=now,
            stored_zone=stored_zone,
            read_zone=read_zone,
            allow_coarsening=allow_coarsening,
            span=(start, end),
            refuse=refuse,
        )
        if pieces and pieces[-1].tier == tier:
            pieces[-1] = Segment(pieces[-1].start, right, tier)
        else:
            pieces.append(Segment(left, right, tier))
    return tuple(pieces)


def _authoritative(
    *,
    ladder: Ladder,
    requested: Bucket,
    oldest: Any,
    now: Any,
    stored_zone: str,
    read_zone: str,
    allow_coarsening: bool,
    span: tuple[Any, Any],
    refuse: Any,
) -> Tier:
    """Exactly one tier for one piece, or a refusal naming what is available."""
    wanted = width(requested)
    zone_ok = [
        tier
        for tier in ladder
        if all(
            serves_zone(tier.grain, stored_zone, read_zone, at=moment)
            for moment in span
        )
    ]
    covering = [tier for tier in zone_ok if tier.covers(oldest, now=now)]

    # Raw first whenever it still promises to cover the piece, and the reason is
    # correctness before cost. Raw is the only source that knows about the
    # watermark: it settles what is sealed and recomputes what is still open, so
    # it is the only one that can answer for buckets too recent to have been
    # materialised. A materialised tier asked for the last three days would
    # return the rows it happens to hold and silently omit today.
    #
    # It is also cheap, because raw's window is short by construction -- if it
    # were long there would be no reason to declare a coarser tier at all.
    if ladder.raw in covering:
        return ladder.raw

    stored = sorted(
        ((width(tier.grain), tier) for tier in covering if tier.grain is not None),
        key=lambda item: item[0],
    )
    exact = [tier for grain, tier in stored if grain == wanted]
    if exact:
        return exact[0]

    coarser = [tier for grain, tier in stored if grain > wanted]
    if coarser:
        if allow_coarsening:
            return coarser[0]
        best = coarser[0].name
        raise refuse(
            f"this range reaches past every tier that stores {requested.name} "
            f"buckets; the coarsest grain still covering it is {best!r}. "
            "Returning coarser data labelled as what you asked for would be a "
            "lie a chart cannot show, so pass allow_coarsening=True to accept "
            f"{best!r} for the part that needs it -- the envelope reports the "
            "grain used per segment -- or shorten the range"
        )

    # Nothing covers it. If the tiers exist but the zone rules them out, say so:
    # it is a different mistake with a different fix.
    if any(tier not in zone_ok for tier in ladder):
        raise refuse(
            f"no tier can answer for zone {read_zone!r}: this view materialises "
            f"in {stored_zone!r}, and a stored grain can only be re-cut into "
            "another zone when the offset between them is a whole number of "
            "grains. Daily rows serve only the zone they were computed in. If "
            "your readers span timezones, materialise at 'hour' or finer and "
            "let day bucketing happen at read time"
        )
    raise refuse(
        "this range is older than every declared tier's retention. Extend a "
        "tier's window in retain(), or ask for a shorter range"
    )
