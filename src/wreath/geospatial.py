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
from math import ceil, cos, floor, isfinite, radians
from typing import Any

from ._geodesy import EARTH_RADIUS_M, latitude_span, longitude_span
from ._native import _core as _core_module

# The `hasattr` asks one question: was this built before `geospatial.c` landed.
#
# The distance is the only thing selected. The radius and the two span
# conversions above have no C arm -- see `wreath._geodesy` for why.
_haversine = _core_module.geo_haversine

__all__ = [
    "EARTH_RADIUS_M",
    "BoundingBox",
    "Coordinate",
    "GeospatialError",
    "Grid",
    "Polygon",
    "Trajectory",
    "bounding_boxes",
    "distance",
    "grid",
]

#: How much a cell's width may vary across an extent before `grid` refuses.
#: One longitude step cannot stay square over a tall extent, because the
#: metres in a degree of longitude shrink towards the poles. Ten per cent is
#: the point past which a legend claiming "10 km cells" stops being true
#: enough to draw.
MAX_CELL_DISTORTION = 0.10


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

    def __jsonable__(self) -> dict[str, float]:
        """The canonical REST shape: a named object, never a bare pair.

        Declared here rather than inferred by the encoder because
        `wreath.temporal.jsonable` keeps the hook **opt-in** -- a blanket
        "serialize any object with fields" rule would put every field of every
        model a handler happened to return on the wire, including the ones a
        sensitive-field guard exists to keep off it.

        An object rather than `[lat, lon]` for the same reason `__init__`
        refuses a positional pair: GeoJSON writes `[lon, lat]` and mapping UIs
        write "lat, lon", so an array on the wire silently picks one and every
        consumer that guessed the other is wrong without an error. This is the
        one canonical spelling -- the OpenAPI `format`, the typegen alias and
        `wreath.crud` all render it, and none of them writes its own.
        """
        return {"lat": self.lat, "lon": self.lon}


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


def _metres(value: Any) -> float:
    """A positive, finite distance in metres, or the refusal that says why.

    One spelling for every caller that takes a radius or a cell size, here and
    in `wreath.orm.expressions` -- tier 2's `ST_DWithin` has to refuse a
    negative radius exactly as tier 1's bounding box does, and a second copy of
    this check is how the two tiers would come to disagree about what a radius
    is.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeospatialError("metres must be a number")
    span = float(value)
    if not isfinite(span) or span <= 0.0:
        raise GeospatialError(f"metres must be a positive finite number, got {value!r}")
    return span


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
    span = _metres(metres)

    d_lat = latitude_span(span)
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
    d_lon = longitude_span(widest, span)
    if d_lon < 0.0:
        return (BoundingBox(max(-90.0, lat_min), min(90.0, lat_max), -180.0, 180.0),)

    lon_min = centre.lon - d_lon
    lon_max = centre.lon + d_lon
    # There was a `lon_max - lon_min >= 360.0` full-longitude branch here, and
    # it could not fire. `longitude_span` returns `degrees(asin(ratio))` with
    # `ratio < 1`, so a non-negative `d_lon` is strictly under 90 degrees and
    # the span is strictly under 180; a negative one is the sentinel the branch
    # above already answered. A mutation pass reported the branch as removable
    # with no test objecting, and a sweep of 10 080 latitude/radius pairs never
    # reached it -- the third such second-spelling deleted from this function.
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


class Polygon:
    """A closed ring of `Coordinate`s — a region with a shape.

    This is what tier 1 structurally cannot answer and the reason PostGIS
    exists in wreath at all: a `BoundingBox` is a rectangle in degrees, and
    "which subjects crossed this catchment" is not a rectangle. Pass one to
    `covered_by()` on a `Geography` column.

    ```python
    from wreath.geospatial import Coordinate, Polygon

    catchment = Polygon([
        Coordinate(lat=-34.0, lon=150.0),
        Coordinate(lat=-34.0, lon=152.0),
        Coordinate(lat=-32.0, lon=151.0),
    ])
    Beacon.select().where(Beacon.at.covered_by(catchment))
    ```

    **Built from `Coordinate`s and nothing else.** Not `(lon, lat)` pairs, and
    not a WKT string: both are the transposition this module exists to refuse,
    wearing a different hat. A ring of bare pairs is the same ambiguity as one
    bare pair repeated, and WKT writes longitude first — so a caller who typed
    `POLYGON((-34 150, ...))` from a map UI has written a valid document
    describing the wrong hemisphere, and no exception would ever tell them.

    The ring is **closed for you**: the first vertex is repeated as the last if
    the caller did not, because a WKT ring must close and forgetting to is a
    database error rather than a mistake anyone can see. It never edits the
    shape otherwise — no winding correction, no simplification, no
    self-intersection check. PostGIS owns those questions and answers them
    against the real spheroid; a second opinion here would be a different one.
    """

    __slots__ = ("vertices",)

    #: The ring, closed: `vertices[0] == vertices[-1]`, with at least four
    #: entries because at least three of them are distinct.
    vertices: tuple[Coordinate, ...]

    def __init__(self, vertices: Iterable[Coordinate]) -> None:
        if isinstance(vertices, (str, bytes)):
            raise TypeError(
                "Polygon takes Coordinates, not WKT text. WKT writes longitude "
                "first and every mapping UI writes 'lat, lon', so a hand-written "
                "POLYGON(...) silently describes a different place when the pair "
                "is the wrong way round -- build the ring from "
                "Coordinate(lat=..., lon=...) and wreath writes the WKT"
            )
        ring = list(vertices)
        for index, vertex in enumerate(ring):
            if not isinstance(vertex, Coordinate):
                raise TypeError(
                    f"vertex {index} must be a Coordinate, got "
                    f"{type(vertex).__name__}. {_POSITIONAL_REFUSAL}"
                )
        if ring and ring[0] == ring[-1]:
            # A caller who closed the ring by hand must not have that vertex
            # counted twice towards the minimum below; a triangle written as
            # four points is still a triangle.
            ring = ring[:-1]
        if len(set(ring)) < 3:
            raise ValueError(
                f"a polygon needs at least three distinct vertices, got "
                f"{len(set(ring))}: two describe a line and one a point, and "
                f"neither encloses anything to be inside of"
            )
        object.__setattr__(self, "vertices", (*ring, ring[0]))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            f"Polygon is immutable; build a new one rather than setting {name!r}"
        )

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Polygon is immutable")

    @property
    def wkt(self) -> str:
        """The EWKT this region travels to the database as.

        `SRID=4326;POLYGON((lon lat, ...))` — **longitude first**, which is what
        WKT, GeoJSON and PostGIS all want and the opposite of what people say.
        This is the one place that order is transcribed for a region, exactly as
        `orm.types` holds the one place it is transcribed for a point.

        The SRID is written rather than defaulted: `ST_GeogFromText` assumes
        4326 for a bare `POLYGON(...)`, and an assumption that is right is still
        an assumption when the next projection lands.
        """
        ring = ", ".join(f"{item.lon!r} {item.lat!r}" for item in self.vertices)
        return f"SRID=4326;POLYGON(({ring}))"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Polygon):
            return NotImplemented
        return self.vertices == other.vertices

    def __hash__(self) -> int:
        return hash((Polygon, self.vertices))

    def __repr__(self) -> str:
        return f"<Polygon {len(self.vertices) - 1} vertices>"


class Grid:
    """A lattice of approximately-square cells tiling an extent.

    This is the spatial analogue of a bucket width, and it exists for the same
    reason: a heatmap needs somewhere to put the cells that had nothing in
    them. `rows` and `columns` are known before any query runs, so the number
    of cells a declaration will produce is a declaration-time fact -- which is
    what lets a ceiling be enforced where it can be read rather than after the
    database has already done the work.

    **The cells are approximately square, not exactly.** Latitude steps are
    constant; longitude steps are computed once, at the extent's middle
    latitude, because the metres in a degree of longitude shrink towards the
    poles. Across a modest extent that is a fraction of a per cent. Across a
    tall one it is the difference between a 10 km cell and a 5 km cell with one
    legend covering both, so `grid` refuses past
    `MAX_CELL_DISTORTION` and `distortion` reports where a given
    lattice actually sits.

    Cells are indexed from the extent's south-west corner, `(0, 0)`, and the
    lattice always reaches past the extent's far edge rather than dropping a
    partial cell -- narrowing the region the reader asked for is the one thing
    a dense axis must not do.
    """

    __slots__ = ("columns", "distortion", "extent", "lat_step", "lon_step", "metres", "rows")

    extent: BoundingBox
    metres: float
    lat_step: float
    lon_step: float
    rows: int
    columns: int
    distortion: float

    def __init__(
        self,
        extent: BoundingBox,
        metres: float,
        lat_step: float,
        lon_step: float,
        rows: int,
        columns: int,
        distortion: float,
    ) -> None:
        for name, value in (
            ("extent", extent),
            ("metres", metres),
            ("lat_step", lat_step),
            ("lon_step", lon_step),
            ("rows", rows),
            ("columns", columns),
            ("distortion", distortion),
        ):
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Grid is immutable")

    @property
    def count(self) -> int:
        """How many cells this lattice has. Known without running anything."""
        return self.rows * self.columns

    def _check(self, row: int, column: int) -> None:
        if isinstance(row, bool) or not isinstance(row, int) or not 0 <= row < self.rows:
            raise GeospatialError(
                f"row must be in 0..{self.rows - 1}, got {row!r}. A negative index "
                f"does not address the far edge here -- a cell that is not in the "
                f"lattice is a mistake, not a wrap"
            )
        if (
            isinstance(column, bool)
            or not isinstance(column, int)
            or not 0 <= column < self.columns
        ):
            raise GeospatialError(
                f"column must be in 0..{self.columns - 1}, got {column!r}"
            )

    def cell(self, row: int, column: int) -> BoundingBox:
        """The bounds of one cell, clamped to the valid coordinate range."""
        self._check(row, column)
        lat_min = self.extent.lat_min + row * self.lat_step
        lon_min = self.extent.lon_min + column * self.lon_step
        return BoundingBox(
            max(-90.0, lat_min),
            min(90.0, lat_min + self.lat_step),
            max(-180.0, lon_min),
            min(180.0, lon_min + self.lon_step),
        )

    def centre(self, row: int, column: int) -> Coordinate:
        """The middle of one cell — what a renderer pins a marker to."""
        bounds = self.cell(row, column)
        return Coordinate(
            lat=(bounds.lat_min + bounds.lat_max) / 2.0,
            lon=(bounds.lon_min + bounds.lon_max) / 2.0,
        )

    def index_of(self, point: Coordinate) -> tuple[int, int] | None:
        """`(row, column)` for `point`, or `None` when it is off the extent.

        `None` rather than a raise: asking where a point falls is a question
        with a legitimate negative answer, and the caller filtering a mixed set
        should not have to guard every call.
        """
        if not isinstance(point, Coordinate):
            raise TypeError("index_of() takes a Coordinate")
        if not self.extent.contains(point):
            return None
        row = int(floor((point.lat - self.extent.lat_min) / self.lat_step))
        column = int(floor((point.lon - self.extent.lon_min) / self.lon_step))
        # A point exactly on the extent's far edge lands one past the last
        # cell. It is inside the extent, so it belongs to the edge cell.
        return (min(row, self.rows - 1), min(column, self.columns - 1))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Grid):
            return NotImplemented
        return self.extent == other.extent and self.metres == other.metres

    def __hash__(self) -> int:
        return hash((self.extent, self.metres))

    def __repr__(self) -> str:
        return (
            f"<Grid {self.rows}x{self.columns} of {self.metres:g}m "
            f"distortion={self.distortion:.1%}>"
        )


def grid(extent: BoundingBox, *, metres: float) -> Grid:
    """Tile `extent` with approximately-square cells `metres` on a side.

    Refuses rather than guesses in three cases, each of which produces a chart
    that is wrong rather than absent:

    * an **inverted** extent, which describes no region at all;
    * an extent crossing the **antimeridian**, where a lattice generated over a
      wrapped longitude range runs backwards -- `bounding_boxes` already
      returns two boxes for that case, so the caller has the tool;
    * an extent too **tall** for one longitude step to stay square across.
    """
    if not isinstance(extent, BoundingBox):
        raise TypeError("grid() takes a BoundingBox extent")
    span = _metres(metres)
    if extent.lat_min > extent.lat_max:
        raise GeospatialError(
            f"extent has lat_min {extent.lat_min!r} above lat_max {extent.lat_max!r}, "
            f"which describes no region"
        )
    if extent.lon_min > extent.lon_max:
        raise GeospatialError(
            f"extent crosses the antimeridian (lon_min {extent.lon_min!r} is east of "
            f"lon_max {extent.lon_max!r}); a lattice cannot be generated over a "
            f"wrapped longitude range. Use bounding_boxes() to split it and grid "
            f"each half"
        )

    lat_step = latitude_span(span)
    middle = (extent.lat_min + extent.lat_max) / 2.0
    lon_step = longitude_span(middle, span)
    if lon_step < 0.0:
        raise GeospatialError(
            f"a {span:g} m cell at latitude {middle:g} reaches a pole, where no "
            f"finite longitude step tiles the extent"
        )

    # Cell width in metres scales with cos(latitude), so it is widest at
    # whichever edge is nearest the equator and narrowest at the far one. An
    # extent straddling the equator reaches the maximum inside itself.
    straddles = extent.lat_min <= 0.0 <= extent.lat_max
    nearest = 0.0 if straddles else min(abs(extent.lat_min), abs(extent.lat_max))
    farthest = max(abs(extent.lat_min), abs(extent.lat_max))
    # `reference` is strictly positive here: `longitude_span` returns its
    # -1.0 sentinel for every latitude whose cosine is not, and that is
    # refused above. A guard against a non-positive divisor was written here
    # and a mutation pass showed it could never fire.
    reference = cos(radians(middle))
    distortion = (cos(radians(nearest)) - cos(radians(farthest))) / reference
    if distortion > MAX_CELL_DISTORTION:
        raise GeospatialError(
            f"this extent spans {extent.lat_min:g}..{extent.lat_max:g} degrees of "
            f"latitude, over which one longitude step would distort cell width by "
            f"{distortion:.0%} -- the cells at one edge would be that much wider "
            f"than at the other while the legend claimed {span:g} m. Split the "
            f"extent into bands, or accept degrees rather than metres"
        )

    rows = max(1, int(ceil((extent.lat_max - extent.lat_min) / lat_step)))
    columns = max(1, int(ceil((extent.lon_max - extent.lon_min) / lon_step)))
    return Grid(extent, span, lat_step, lon_step, rows, columns, distortion)


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

    def between(self, start: Any, end: Any) -> Trajectory:
        """The path travelled during `[start, end)`, anchored to where it began.

        **The anchor is the whole point, and it is what makes a windowed
        distance add up.** The fixes inside the window describe where the
        subject *was*, but the distance it covered during the window includes
        the leg it was already on when the window opened. Taking the last fix
        at or before `start` as an anchor means the windows of a path partition
        its distance exactly: sum the days and you get the trip.

        Dropping the anchor instead -- the obvious reading of "the fixes in
        this window" -- silently loses every leg that crosses a boundary, so a
        fleet billing on daily distance under-counts by one leg per vehicle per
        day and the error is invisible because each day looks plausible.

        Half-open, like every other window in wreath: a fix exactly on `end`
        belongs to the next one.
        """
        if getattr(start, "tzinfo", None) is None or getattr(end, "tzinfo", None) is None:
            raise GeospatialError(
                "between() takes aware timestamps; a naive bound cannot be "
                "compared to a fix without assuming a zone"
            )
        if end < start:
            raise GeospatialError(f"between() end {end!r} is before start {start!r}")
        inside = [fix for fix in self._fixes if start <= fix[0] < end]
        # `[-1:]` on an empty list is already the empty list, so there is no
        # branch to write here. A mutation pass removed the `if anchor else []`
        # arm and nothing objected, correctly: two spellings of one rule is how
        # they drift apart later.
        anchor = [fix for fix in self._fixes if fix[0] < start][-1:]
        return Trajectory(anchor + inside)

    def __repr__(self) -> str:
        return f"<Trajectory {len(self._fixes)} fixes, {self.distance:.1f} m>"
