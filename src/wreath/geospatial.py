"""Coordinates, distances, and bounded proximity queries.

`wreath.temporal` refuses a naive datetime rather than assuming UTC, because
the assumption is wrong twice a year and silent every other day. This module
makes the same refusal for place: **a `Coordinate` is built with keywords and
never from a bare pair.**

```python
from wreath.geospatial import Coordinate, distance

depot = Coordinate(lat=-27.4698, lon=153.0251)
site = Coordinate(lat=-33.8688, lon=151.2093)
metres = distance(depot, site)
```

`Coordinate(-27.4698, 153.0251)` raises. GeoJSON writes `[lon, lat]`, every
mapping UI writes "lat, lon", and a library that accepts a positional pair has
picked one silently -- which is the single most common defect in geospatial
code and the hardest to see in review, because both orders look plausible.
Wreath owns both sides of its wire and its storage, so the ambiguity exists in
exactly one place and is refused there.

## What this is not

Tier 1 -- everything here -- needs **no PostgreSQL extension**. It answers
"how far apart", "which rows are within N metres", and "which are nearest",
which is what fleet tracking, delivery, field service and store-locator work
actually ask.

It does not answer "which rows fall inside this polygon", and it has no
projections beyond WGS84. Those need PostGIS, which wreath supports as an
opt-in client half in the same shape it supports pgvector -- see the guide.

## The accuracy this promises

Distances are great-circle on a sphere of mean radius 6 371 008.8 m. That is
within about **0.5%** of an ellipsoidal (Vincenty/Karney) distance, worst near
the poles and best near 45 degrees. Good enough to route a van and to sort a
list of nearby sites; **not** good enough to bill by the kilometre without
saying which model produced the number.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from math import isfinite
from typing import Any

from ._pure import geospatial as _reference

try:  # pragma: no cover - the native twin is present in a normal build
    from ._native import _core as _core_module
except ImportError:  # pragma: no cover - pure-Python fallback
    _core_module = None  # type: ignore[assignment]

import os

_FORCE_PURE = os.environ.get("WREATH_PURE") == "1"

if not _FORCE_PURE and _core_module is not None and hasattr(_core_module, "geo_haversine"):
    _haversine = _core_module.geo_haversine
else:  # pragma: no cover - exercised under WREATH_PURE=1
    _haversine = _reference.haversine

#: Mean Earth radius in metres (IUGG), the sphere every distance here assumes.
EARTH_RADIUS_M = _reference.EARTH_RADIUS_M

__all__ = [
    "EARTH_RADIUS_M",
    "BoundingBox",
    "Coordinate",
    "GeospatialError",
    "Trajectory",
    "bounding_boxes",
    "distance",
]


class GeospatialError(ValueError):
    """A coordinate or a bound that cannot mean what it says.

    A `ValueError` so the binding layer renders it as a 422 the way
    `TemporalError` already does, rather than as a 500.
    """


_POSITIONAL_REFUSAL = (
    "Coordinate takes keywords: Coordinate(lat=..., lon=...). A bare pair is "
    "refused because it has no self-evident order -- GeoJSON writes [lon, lat] "
    "and mapping UIs write 'lat, lon', so a positional pair silently picks one. "
    "Name them and the ambiguity disappears."
)


def _check(name: str, value: Any, limit: float) -> float:
    if isinstance(value, bool):
        raise GeospatialError(
            f"{name} must be a number, not a bool; True is an int in Python and "
            f"is never a deliberate coordinate"
        )
    if not isinstance(value, (int, float)):
        raise GeospatialError(
            f"{name} must be a number, got {type(value).__name__}; wreath does "
            f"not parse coordinates from text, because the format is ambiguous "
            f"in the same way the ordering is"
        )
    number = float(value)
    # No separate `isfinite` guard: a NaN fails every comparison and an
    # infinity fails the bound, so the range check below already refuses both
    # and says something true about them. A mutation pass reported the
    # `isfinite` branch as removable with no test objecting, which was correct
    # -- it was a second spelling of a check that had already happened.
    if not -limit <= number <= limit:
        raise GeospatialError(
            f"{name} must be between {-limit} and {limit} degrees, got {number!r}"
        )
    return number


class Coordinate:
    """One WGS84 position in degrees. Immutable, hashable, keyword-only.

    The bounds are inclusive: a pole (`lat=90`) and the antimeridian
    (`lon=180`) are real places and real coordinates. It is only *beyond* them
    that the value cannot mean anything.
    """

    __slots__ = ("lat", "lon")

    lat: float
    lon: float

    def __init__(self, *args: Any, lat: Any = None, lon: Any = None) -> None:
        if args:
            raise TypeError(_POSITIONAL_REFUSAL)
        if lat is None or lon is None:
            raise TypeError(_POSITIONAL_REFUSAL)
        object.__setattr__(self, "lat", _check("lat", lat, 90.0))
        object.__setattr__(self, "lon", _check("lon", lon, 180.0))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            f"Coordinate is immutable; build a new one rather than setting {name!r}"
        )

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Coordinate is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Coordinate):
            return NotImplemented
        return self.lat == other.lat and self.lon == other.lon

    def __hash__(self) -> int:
        return hash((Coordinate, self.lat, self.lon))

    def __repr__(self) -> str:
        return f"Coordinate(lat={self.lat!r}, lon={self.lon!r})"


def distance(a: Coordinate, b: Coordinate) -> float:
    """Great-circle metres between two coordinates. Symmetric.

    See the module docstring for the accuracy this promises; it is a sphere,
    not an ellipsoid, and the difference matters if you are invoicing.
    """
    if not isinstance(a, Coordinate) or not isinstance(b, Coordinate):
        raise TypeError("distance() takes two Coordinate values")
    return _haversine(a.lat, a.lon, b.lat, b.lon)


class BoundingBox:
    """A degree-aligned rectangle, in the order PostgreSQL's `box` wants.

    Longitudes are always within [-180, 180]; a circle that crosses the
    antimeridian yields *two* of these rather than one box with a wrapped
    edge, because no comparison operator understands a wrapped edge.
    """

    __slots__ = ("lat_max", "lat_min", "lon_max", "lon_min")

    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def __init__(self, lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> None:
        object.__setattr__(self, "lat_min", lat_min)
        object.__setattr__(self, "lat_max", lat_max)
        object.__setattr__(self, "lon_min", lon_min)
        object.__setattr__(self, "lon_max", lon_max)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("BoundingBox is immutable")

    def contains(self, point: Coordinate) -> bool:
        """Whether `point` falls in this box. The cheap half of `within`."""
        return (
            self.lat_min <= point.lat <= self.lat_max
            and self.lon_min <= point.lon <= self.lon_max
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BoundingBox):
            return NotImplemented
        return (
            self.lat_min == other.lat_min
            and self.lat_max == other.lat_max
            and self.lon_min == other.lon_min
            and self.lon_max == other.lon_max
        )

    def __hash__(self) -> int:
        return hash((self.lat_min, self.lat_max, self.lon_min, self.lon_max))

    def __repr__(self) -> str:
        return (
            f"BoundingBox(lat_min={self.lat_min!r}, lat_max={self.lat_max!r}, "
            f"lon_min={self.lon_min!r}, lon_max={self.lon_max!r})"
        )


def bounding_boxes(centre: Coordinate, metres: float) -> tuple[BoundingBox, ...]:
    """The degree-aligned boxes that contain every point within `metres`.

    This is the half of a proximity search an index can answer. The exact
    great-circle filter still runs over what the boxes return, because a box
    is a superset of the circle -- but the box is what stops the query reading
    the whole table.

    Returns **two** boxes when the circle crosses the antimeridian, and one
    spanning every longitude when it reaches a pole. Both are the correct
    answer rather than a refusal: a fleet operating across the date line and a
    research station near a pole are ordinary, and a library that raised on
    them would simply be unusable there.
    """
    if not isinstance(centre, Coordinate):
        raise TypeError("bounding_boxes() takes a Coordinate centre")
    if isinstance(metres, bool) or not isinstance(metres, (int, float)):
        raise GeospatialError("metres must be a number")
    span = float(metres)
    if not isfinite(span) or span <= 0.0:
        raise GeospatialError(f"metres must be a positive finite number, got {metres!r}")

    d_lat = _reference.latitude_span(span)
    lat_min = centre.lat - d_lat
    lat_max = centre.lat + d_lat

    # A circle reaching a pole has no finite longitude bound -- every meridian
    # passes through it -- and `longitude_span` says so with its -1.0 sentinel.
    #
    # There was an explicit `lat_max >= 90 or lat_min <= -90` fast path here.
    # A mutation pass removed it and nothing objected, correctly: when the band
    # crosses a pole, `cos(radians(widest))` is <= 0 or the ratio reaches 1, so
    # the sentinel fires and yields the identical clamped full-longitude box.
    # Two spellings of one branch is how they drift apart later.
    widest = max(abs(lat_min), abs(lat_max))
    d_lon = _reference.longitude_span(widest, span)
    if d_lon < 0.0:
        return (BoundingBox(max(-90.0, lat_min), min(90.0, lat_max), -180.0, 180.0),)

    lon_min = centre.lon - d_lon
    lon_max = centre.lon + d_lon
    if lon_max - lon_min >= 360.0:
        return (BoundingBox(lat_min, lat_max, -180.0, 180.0),)
    if lon_min < -180.0:
        return (
            BoundingBox(lat_min, lat_max, lon_min + 360.0, 180.0),
            BoundingBox(lat_min, lat_max, -180.0, lon_max),
        )
    if lon_max > 180.0:
        return (
            BoundingBox(lat_min, lat_max, lon_min, 180.0),
            BoundingBox(lat_min, lat_max, -180.0, lon_max - 360.0),
        )
    return (BoundingBox(lat_min, lat_max, lon_min, lon_max),)


class Trajectory:
    """An ordered path: `(Instant, Coordinate)` pairs and what they imply.

    This is where `wreath.temporal` and this module compose, and it is the
    thing every fleet, delivery and tracking application writes by hand. It is
    only expressible cleanly because both halves are declared types rather
    than loose floats and naive datetimes -- a naive timestamp here would make
    `duration` wrong across a DST boundary and `speed` wrong with it.

    Deliberately small: derived measures over an ordered sequence. Route
    matching, map matching and corridor inference are not here and need a real
    spatial engine.
    """

    __slots__ = ("_fixes",)

    _fixes: tuple[tuple[Any, Coordinate], ...]

    def __init__(self, fixes: Iterable[tuple[Any, Coordinate]]) -> None:
        collected = list(fixes)
        for index, fix in enumerate(collected):
            if not isinstance(fix, tuple) or len(fix) != 2:
                raise GeospatialError(
                    f"fix {index} must be a (timestamp, Coordinate) pair"
                )
            when, where = fix
            if not isinstance(where, Coordinate):
                raise GeospatialError(
                    f"fix {index} must carry a Coordinate, got "
                    f"{type(where).__name__}"
                )
            if getattr(when, "tzinfo", None) is None:
                raise GeospatialError(
                    f"fix {index} has a timestamp with no timezone; a trajectory "
                    f"derives durations and speeds from these, and a naive "
                    f"timestamp makes both wrong across a DST boundary. Use "
                    f"wreath.temporal.Instant."
                )
        collected.sort(key=lambda fix: fix[0])
        object.__setattr__(self, "_fixes", tuple(collected))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Trajectory is immutable")

    @property
    def fixes(self) -> Sequence[tuple[Any, Coordinate]]:
        """The fixes, oldest first."""
        return self._fixes

    def __len__(self) -> int:
        return len(self._fixes)

    @property
    def distance(self) -> float:
        """Metres travelled along the path, summed leg by leg.

        The sum of the legs, never the straight line from first to last: an
        animal that returns to where it started travelled a long way, and a
        van that did a loop is owed its fuel.
        """
        total = 0.0
        previous: Coordinate | None = None
        for _, where in self._fixes:
            if previous is not None:
                total += distance(previous, where)
            previous = where
        return total

    @property
    def duration(self) -> float:
        """Seconds between the first and last fix, or 0.0 for fewer than two."""
        if len(self._fixes) < 2:
            return 0.0
        return (self._fixes[-1][0] - self._fixes[0][0]).total_seconds()

    @property
    def speed(self) -> float | None:
        """Mean metres per second over the whole path.

        `None` rather than zero when the path spans no time: a division that
        cannot be performed has no answer, and returning 0.0 would read as
        "stationary", which is a different claim.
        """
        seconds = self.duration
        if seconds <= 0.0:
            return None
        return self.distance / seconds

    def __repr__(self) -> str:
        return f"<Trajectory {len(self._fixes)} fixes, {self.distance:.1f} m>"
