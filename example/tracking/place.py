"""Where things are, how far apart, and how much of that a reader is told.

Three groups of things live here, and they are together because they are all
the same subject seen from different distances.

**Queries** -- :func:`within`, :func:`nearest`, :func:`track`. Wreath ships
:class:`~wreath.geospatial.Coordinate`, :func:`~wreath.geospatial.distance`,
:func:`~wreath.geospatial.bounding_boxes` and
:class:`~wreath.geospatial.Trajectory`; it does not ship an ORM column type or a
``within()`` query helper, so the join between the geospatial primitives and the
ORM is written here. That is about thirty lines, and writing them in the open is
more useful than a helper would be, because the *shape* is the lesson: a
rectangle narrows, and then the exact test decides.

**Degradation** -- :class:`Precision` and :func:`degrade`. Turning an exact
position into a coarse one is arithmetic, and it is here rather than in
``policies`` because *who gets which resolution* and *what a resolution is* are
different questions with different reasons to change.

## The one honest limit

Everything below runs on stock PostgreSQL with no extension, which is the point
of it. It answers "which fixes are within 5 km of this waterhole" and "how far
did this animal travel", because those are rectangles and arithmetic.

It cannot answer "which animals crossed the northern boundary", "how much time
did this animal spend inside the conservancy", or "infer the migration corridor
from these tracks". Those are polygon containment and a spatial join, and they
need PostGIS.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, cos, floor, log2, radians
from typing import Any

from wreath.geospatial import (
    Coordinate,
    GeospatialError,
    Trajectory,
    bounding_boxes,
    distance,
)
from wreath.orm import and_, or_

from .models import Fix, Landmark

#: Metres in one degree of latitude, everywhere. A sphere of mean radius
#: 6 371 008.8 m -- the same sphere `wreath.geospatial.distance` measures on, so
#: a degraded coordinate and a distance to it agree about what a metre is.
METRES_PER_DEGREE = 111_195.0

#: Below this cosine a longitude cell is wider than the world and a grid stops
#: being a grid. `cos(89.9 degrees)`; see `degrade`.
_POLAR_COSINE = 1.7e-3


@dataclass(frozen=True, slots=True)
class Precision:
    """How much of a position a reader is given.

    ``metres`` is the width of the grid cell a coordinate is snapped to, and
    zero means no snapping at all. A ``Precision`` is *not* an accuracy
    estimate: it is a deliberate coarsening, and the difference matters on the
    wire, which is why `tracking.wire.fix_json` refuses to publish the collar's
    own ``accuracy_m`` beside a coarsened position.
    """

    name: str
    metres: float


#: What the collar said. A ranger walking towards a snared animal needs the
#: fix, not a neighbourhood.
EXACT = Precision("exact", 0.0)

#: A kilometre. Enough to say which drainage an animal is using and to plot a
#: season's movement at landscape scale; not enough to find it.
COARSE = Precision("coarse", 1_000.0)

#: Ten kilometres. Enough to say "the herd is in the north of the conservancy",
#: which is what a public-facing volunteer dashboard is actually for.
APPROXIMATE = Precision("approximate", 10_000.0)

#: Every grade, coarsest first. `tracking.policies` walks this in order and
#: takes the first one Cedar permits, so adding a grade is one line here and one
#: policy statement there rather than an edit to a decision tree.
GRADES = (EXACT, COARSE, APPROXIMATE)


def degrade(point: Coordinate, precision: Precision) -> Coordinate:
    """`point`, snapped to the centre of a `precision.metres` grid cell.

    **A grid, and deliberately not noise.** The obvious way to blur a position
    is to add a random offset, and it is wrong for a reason that only shows up
    later: a reader who asks twice gets two samples, and a reader who asks a
    thousand times gets the mean, which is the truth. Any unbiased jitter is
    defeated by averaging, and a *biased* one is a lie about where the animal
    was. Snapping is neither. The same fix always produces the same coarse
    answer, so a thousand requests are one request, and repetition buys nothing.
    `tests/tracking/test_precision.py` asserts exactly that, because it is the
    property the whole scheme rests on.

    The grid is fixed to the equator and the prime meridian rather than to the
    animal, so it does not move with what it is hiding. An animal crossing a
    cell boundary does change answer -- from one cell centre to the next -- and
    that is the honest behaviour: it moved a kilometre.

    The answer is never further from the truth than the cell's half-diagonal,
    about 0.7 of `metres`.

    Longitude cells are widened by `1/cos(latitude)` so a cell is roughly square
    on the ground rather than a sliver at high latitude. Within about a tenth of
    a degree of a pole the widening exceeds the whole world, and then there is
    no grid to snap to: the longitude collapses to zero, which is the same
    answer `bounding_boxes` gives there and for the same reason. Every meridian
    passes through a pole, so longitude has stopped carrying information.

    Args:
        point: The exact coordinate, as the collar reported it.
        precision: The grade to coarsen to. `EXACT` returns `point` unchanged.

    Returns:
        A `Coordinate` that is the centre of the cell `point` falls in.
    """
    if precision.metres <= 0.0:
        return point
    cell = precision.metres / METRES_PER_DEGREE
    # Clamped rather than allowed to run past the pole: a cell centred at 89.97
    # with a 10 km cell snaps to 90.02, which is not a place, and `Coordinate`
    # would refuse it -- correctly, and unhelpfully, at the far end of a
    # serializer.
    lat = min(90.0, max(-90.0, _snap(point.lat, cell)))
    scale = cos(radians(lat))
    if scale <= _POLAR_COSINE or cell / scale >= 360.0:
        return Coordinate(lat=lat, lon=0.0)
    lon = _snap(point.lon, cell / scale)
    # Wrapped, not clamped. A cell centre just past the antimeridian is a real
    # place a few hundred metres the other side of the line, and clamping would
    # pile every one of them onto 180.0 exactly.
    return Coordinate(lat=lat, lon=((lon + 180.0) % 360.0) - 180.0)


def _snap(value: float, cell: float) -> float:
    """The centre of the `cell`-wide interval containing `value`.

    `floor` rather than `round`, and the half-cell offset afterwards: rounding
    to the nearest multiple would put cell *boundaries* on the round numbers,
    so a fix at exactly 36.8 degrees would sit on a seam and its answer would
    depend on the last bit of a float.
    """
    return (floor(value / cell) + 0.5) * cell


def box_query(centre: Coordinate, metres: float, *, limit: int) -> Any:
    """The rectangle half of a proximity search, as a `Select`.

    Named and returned rather than built inside :func:`within` so a test can
    compile *this* query and read the plan PostgreSQL chose for it. Asserting on
    a hand-written reconstruction would be asserting that somebody's SQL uses
    the index, which is not the claim -- the claim is that the query this
    example runs does.

    `limit` has no default. Every caller has a bound in mind and they are not
    the same bound -- `within` asks for one more than it will tolerate so it can
    tell a full page from an overflowing one -- and a default here would make
    the difference invisible at the two call sites where it matters.
    """
    boxes = bounding_boxes(centre, metres)
    return (
        Fix.select()
        .where(
            or_(
                *(
                    and_(
                        Fix.latitude >= box.lat_min,
                        Fix.latitude <= box.lat_max,
                        Fix.longitude >= box.lon_min,
                        Fix.longitude <= box.lon_max,
                    )
                    for box in boxes
                )
            )
        )
        .limit(limit)
    )


class SearchTooBroad(ValueError):
    """A proximity search matched more rectangle rows than its bound allows.

    **A bound on a proximity search cannot be a `LIMIT`.** The rectangle is
    unordered -- there is no `ORDER BY distance` to write, because the distance
    is computed in Python after the rows come back -- so truncating it returns
    an *arbitrary* subset of the box and then reports the nearest members of
    that subset as though they were the nearest members of the circle. The
    answer is wrong and looks exactly like a right one.

    So the bound refuses instead. A `ValueError`, so a handler can turn it into
    a 4xx: "you asked a question whose answer is too big" is the caller's to
    fix, by narrowing the radius.
    """


async def within(
    session: Any,
    centre: Coordinate,
    metres: float,
    *,
    limit: int = 20_000,
) -> list[Fix]:
    """Every fix within `metres` of `centre`, nearest first.

    **A proximity search has two halves and using only one of them is the
    mistake.** `bounding_boxes` gives the degree-aligned rectangles that contain
    the circle, and a rectangle is what a btree index can answer -- that is what
    stops this query reading the whole table. The rectangles are a *superset* of
    the circle, so the exact great-circle test still has to run over what comes
    back, and it runs here in Python.

    Writing only the exact test gives a correct answer and a sequential scan.
    Wreath already refuses that shape for vector search, and
    `tests/tracking/test_place.py` holds this one to it with `EXPLAIN` against
    the SQL the ORM actually emits.

    `bounding_boxes` returns **two** rectangles for a circle crossing the
    antimeridian, which is why this is an `or_` of boxes rather than one
    `BETWEEN`. This conservancy is nowhere near the date line and the branch
    never fires here -- and that is exactly the shape of code that is wrong in
    production the first week somebody deploys it in Fiji, so it is written
    correctly rather than conveniently.

    Args:
        session: An ORM read session.
        centre: The point to search around.
        metres: Radius. Great-circle, on a sphere, so about 0.5% out against an
            ellipsoidal calculation -- fine for "near the waterhole", not fine
            for an invoice. See `wreath.geospatial.distance`.
        limit: How many rectangle rows this search may consider. Exceeding it
            **raises** rather than truncating; see `SearchTooBroad`.

    Returns:
        Fixes inside the circle, ordered by distance from `centre`.

    Raises:
        SearchTooBroad: the rectangle holds more than `limit` fixes.
    """
    # `limit + 1`, so a full page is distinguishable from an overflowing one
    # without a second `count(*)` over the same box.
    rows = await session.fetch(box_query(centre, metres, limit=limit + 1))
    if len(rows) > limit:
        raise SearchTooBroad(
            f"more than {limit} fixes lie in the rectangle around "
            f"({centre.lat}, {centre.lon}) at {metres:.0f} m; narrow the radius, "
            f"because a truncated rectangle would answer with an arbitrary "
            f"subset of it"
        )
    # The exact test, and the reason it is a separate pass: a rectangle's
    # corners stick out past the circle it contains by up to 41% of the radius,
    # so about a fifth of what the index returned is not actually within the
    # radius that was asked for. Dropping this filter leaves a query that is
    # fast, plausible, and quietly answers a different question.
    inside: list[tuple[float, Fix]] = []
    for row in rows:
        metres_away = distance(centre, Coordinate(lat=row.latitude, lon=row.longitude))
        if metres_away <= metres:
            inside.append((metres_away, row))
    # The primary key breaks the tie, so two fixes the same distance away come
    # back in the same order on every run. A seeded example whose output depends
    # on the planner's row order is not reproducible, and the docs paste this.
    return [
        row
        for _, row in sorted(
            inside, key=lambda pair: (pair[0], pair[1].collar_id, pair[1].recorded_at)
        )
    ]


async def nearest(
    session: Any,
    centre: Coordinate,
    *,
    count: int = 1,
    start_m: float = 500.0,
    max_m: float = 50_000.0,
) -> list[Fix]:
    """The `count` fixes nearest `centre`, by widening a circle until enough fall in.

    **There is no nearest-neighbour index here, and this is what that costs.**
    pgvector answers a KNN query by walking an index in distance order; a btree
    on two independent columns cannot, because "nearest" is not a range. So the
    honest tier-1 answer is a bounded search: ask a small circle, and if it did
    not contain enough, double it.

    Doubling means the number of queries is logarithmic in how wrong the first
    guess was -- five widenings cover a hundredfold radius -- and `max_m` stops
    a query for a point in the ocean from walking out to a hemisphere. A caller
    that reaches `max_m` gets what there was, which may be nothing; returning
    fewer than `count` is a true answer and raising would not be.

    Args:
        session: An ORM read session.
        centre: The point to search around.
        count: How many fixes are wanted.
        start_m: The first radius. Set it near the answer you expect.
        max_m: Stop widening here.

    Returns:
        Up to `count` fixes, nearest first.
    """
    found: list[Fix] = []
    for radius in widening(start_m, max_m):
        # `within`'s own bound, deliberately not a tighter one. A limit sized to
        # `count` would truncate the rectangle to an arbitrary subset and then
        # report its nearest members as the nearest overall -- which is the
        # failure `SearchTooBroad` exists to make impossible, and it would be
        # invisible here because every returned fix really is nearby.
        found = await within(session, centre, radius)
        if len(found) >= count:
            break
    return found[:count]


def widening(start_m: float, max_m: float) -> tuple[float, ...]:
    """The radii a search will try, computed before it tries any of them.

    **The version this replaced was an unbounded loop on a request path**: it
    doubled until it had enough rows or reached the ceiling, with both
    conditions in one `if`. That is correct code and a bad shape, because every
    way of getting the condition slightly wrong is a *hang* rather than a wrong
    answer -- a handler that never returns, holding a connection, on input a
    caller chose. Mutation testing found it by being unable to decide: two
    mutants came back as timeouts, because a harness cannot tell a
    non-terminating handler from a slow one.

    So the count is arithmetic and the loop is a `range`. Not a `while` either:
    a `while` whose condition a single edit can make permanently true is the
    same hazard one layer down, and this one would build a list until the
    process died. `range(steps)` cannot be made to run forever by changing a
    comparison, which is the property worth having in code that runs per
    request.

    Args:
        start_m: The first radius, which should be near the answer you expect.
        max_m: The last. Always tried, even when doubling overshoots it.

    Returns:
        Increasing radii, `start_m` first and `max_m` last.
    """
    if start_m <= 0.0 or max_m <= 0.0:
        raise GeospatialError(f"radii must be positive, got {start_m} and {max_m}")
    first = min(start_m, max_m)
    # How many doublings fit strictly below the ceiling. `ceil(log2(ratio))` is
    # the count of steps to reach it; the ceiling itself is appended after, so a
    # ratio that is an exact power of two does not try `max_m` twice.
    steps = max(0, ceil(log2(max_m / first)))
    return (*(first * 2.0**step for step in range(steps)), max_m)


async def track(
    session: Any,
    animal_id: int,
    *,
    since: Any,
    until: Any,
) -> Trajectory:
    """One animal's path through a window, as a `Trajectory`.

    Half-open -- ``since <= recorded_at < until`` -- so two adjacent windows
    neither drop a fix on the boundary nor count it twice. Closing the upper end
    is the mistake that makes a month's distance disagree with the sum of its
    days.

    Ordered by `recorded_at`, which is not the order the rows arrived in: a
    collar that spent three days under canopy uploads its buffer afterwards, so
    the fixes in the middle of this track were inserted last. A `Trajectory`
    built in insertion order would measure a path that doubles back through
    three days of history and report several times the true distance.

    Returns:
        A `Trajectory`, whose `.distance` is the sum of the legs -- never the
        straight line from first fix to last, because an animal that returns to
        the waterhole it started at still walked all day.
    """
    rows = await session.fetch(
        Fix.select()
        .where(
            Fix.animal_id == animal_id,
            Fix.recorded_at >= since,
            Fix.recorded_at < until,
        )
        .order_by(Fix.recorded_at)
    )
    return Trajectory(
        [(row.recorded_at, Coordinate(lat=row.latitude, lon=row.longitude)) for row in rows]
    )


def nearest_landmark(point: Coordinate, landmarks: list[Landmark]) -> tuple[Landmark, float] | None:
    """The landmark closest to `point`, and how far away it is.

    In Python over the whole table, because the table is twelve rows. A
    bounding-box query here would cost a plan, a round trip and a paragraph
    explaining an index on twelve rows, to save comparing twelve numbers.

    Returns `None` for an empty list rather than raising: a conservancy with no
    landmarks recorded yet is a state the seeder passes through, not an error.
    """
    if not landmarks:
        return None
    return min(
        (
            (mark, distance(point, Coordinate(lat=mark.latitude, lon=mark.longitude)))
            for mark in landmarks
        ),
        key=lambda pair: (pair[1], pair[0].id),
    )
