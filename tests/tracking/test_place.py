"""Proximity, nearest, and a path — against 51,840 real rows.

The half of this file that matters most is the `EXPLAIN`. "Which fixes are
within 5 km of the waterhole" is answerable two ways, and one of them is a
sequential scan over the whole table. Both return the same rows, so no
correctness test can tell them apart -- which is exactly why the plan has to be
asserted rather than assumed. The same argument wreath already makes for vector
search, where `where()` turns away a bare distance comparison.

The plan is read from the SQL the ORM actually compiles for
`tracking.place.box_query`, not from a hand-written reconstruction of it. A
reconstruction would assert that somebody's SQL uses the index; the claim is
that *this example's* query does.
"""

from __future__ import annotations

import datetime
import os

import pytest
from tracking.config import SCHEMA
from tracking.place import box_query, nearest, nearest_landmark, track, within
from tracking.seed import CENTRE_LAT, CENTRE_LON, EPOCH, LANDMARKS

from wreath.geospatial import Coordinate, distance

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the tracking place tests",
)

#: Ndovu Waterhole, which three of the seeded animals range around.
NDOVU = Coordinate(lat=CENTRE_LAT + LANDMARKS[0][3], lon=CENTRE_LON + LANDMARKS[0][4])

#: Sarara, a plains zebra whose home range is Ndovu.
SARARA = 8


@pytest.fixture
async def session():
    """A read session on a freshly built and seeded schema.

    No HTTP: these are questions about queries, and putting a router between the
    test and the plan would only make a failure harder to attribute.
    """
    from _tracking import build_schema, drop_schema
    from tracking.models import MODELS

    from wreath.orm.registry import Registry
    from wreath.orm.session import Session
    from wreath.postgres import Database, connect

    connection = await connect(_DSN)
    try:
        await build_schema(connection)
    finally:
        await connection.close()

    database = Database("main", _DSN, pools={"read": {"min_size": 1, "max_size": 2}})
    await database.start()
    try:
        yield Session(Registry(database, list(MODELS), validate_schema="off"), "read")
    finally:
        await database.stop()
        connection = await connect(_DSN)
        try:
            await drop_schema(connection)
        finally:
            await connection.close()


# -- the plan -----------------------------------------------------------------


@skip_without_database
async def test_the_proximity_query_reaches_the_index_as_a_condition() -> None:
    """**The assertion the rest of this file cannot make.**

    The discriminator is `Index Cond`, not the scan node's name. A plan that
    read the whole table and threw rows away would still say "Scan", and a
    bounding box that reached the index only as a `Filter` would be reading the
    whole index -- which is the sequential scan wearing a different plan.
    """
    from _tracking import build_schema, drop_schema

    from wreath.orm.compiler import compile_select
    from wreath.orm.registry import Registry
    from wreath.postgres import Database, connect

    connection = await connect(_DSN)
    try:
        await build_schema(connection)
    finally:
        await connection.close()

    database = Database("main", _DSN, pools={"read": {"min_size": 1, "max_size": 2}})
    await database.start()
    try:
        from tracking.models import MODELS

        registry = Registry(database, list(MODELS), validate_schema="off")
        # The very query `within` runs, compiled by the ORM rather than written
        # out here. `compile_select` is `wreath.orm`'s internal entry point and
        # is used deliberately: reading the plan of a reconstruction would prove
        # something about the reconstruction.
        compiled = compile_select(registry, box_query(NDOVU, 1_000.0, limit=20_001))
        connection = await database.acquire("read")
        try:
            rows = await connection.fetch(
                "EXPLAIN (FORMAT TEXT) " + compiled.sql, *compiled.bind_values
            )
        finally:
            await database.release("read", connection)
    finally:
        await database.stop()
        connection = await connect(_DSN)
        try:
            await drop_schema(connection)
        finally:
            await connection.close()

    plan = "\n".join(str(row[0]) for row in rows)
    assert "Index Cond" in plan, plan
    assert "latitude" in plan and "longitude" in plan, plan
    assert "Seq Scan" not in plan, plan


@skip_without_database
async def test_a_search_too_broad_to_answer_is_refused_rather_than_truncated(
    session,
) -> None:
    """**A bound on a proximity search cannot be a `LIMIT`.**

    The rectangle is unordered -- the distance is computed in Python after the
    rows come back, so there is no `ORDER BY distance` to write. Truncating it
    therefore returns an *arbitrary* subset of the box, and the nearest members
    of an arbitrary subset are not the nearest members of the circle. The answer
    would be wrong and would look exactly like a right one, which is why the
    bound refuses instead.

    This is not a hypothetical: `nearest` was written with a `LIMIT` sized to
    the number of fixes it wanted, and it returned confidently wrong answers
    around a waterhole where the box held hundreds of rows.
    """
    from tracking.place import SearchTooBroad

    with pytest.raises(SearchTooBroad, match="narrow the radius"):
        await within(session, NDOVU, 5_000.0, limit=100)


# -- within -------------------------------------------------------------------


@skip_without_database
async def test_within_returns_only_what_is_actually_inside_the_circle(session) -> None:
    """The rectangle narrows and the exact test decides, and both are needed.

    A bounding box is a *superset* of a circle -- its corners stick out by up to
    41% of the radius -- so a query that stopped at the box would return fixes
    that are not within the radius it was asked about. Asserting every returned
    fix is genuinely inside is what pins the second half.
    """
    found = await within(session, NDOVU, 3_000.0)
    assert found, "the seed must put fixes near Ndovu or this proves nothing"
    for row in found:
        assert distance(NDOVU, Coordinate(lat=row.latitude, lon=row.longitude)) <= 3_000.0


@skip_without_database
async def test_the_rectangle_really_does_return_more_than_the_circle(session) -> None:
    """The exact test is not a formality: it drops about a fifth of the box.

    A rectangle's corners stick out past the circle it contains by up to 41% of
    the radius, so a `within` that skipped the second pass would be fast,
    plausible, and quietly answering a different question. This asserts the two
    counts differ, which is what stops the filter from being deleted as dead
    code by somebody who checked that the rows all looked nearby.

    Written after exactly that happened here: the first version of `within`
    computed every distance, sorted by it, and returned the whole rectangle.
    """
    boxed = await session.fetch(box_query(NDOVU, 3_000.0, limit=20_001))
    circled = await within(session, NDOVU, 3_000.0)
    assert len(boxed) > len(circled)
    # A circle is pi/4 of its bounding square, so the survivors should be around
    # 78% of the box. Bounded loosely on both sides, because the seed's animals
    # are not uniformly spread and an exact ratio would be asserting the walk.
    assert 0.6 < len(circled) / len(boxed) < 0.95


@skip_without_database
async def test_within_finds_the_corner_cases_the_box_would_have_missed(session) -> None:
    """A wider radius contains everything a narrower one found.

    Monotonicity is a cheap property and it catches the class of bug where the
    box arithmetic is subtly asymmetric -- a sign flipped on one edge returns a
    plausible set of rows that is missing a quarter of the circle, and no single
    query looks wrong.
    """
    near = {(row.collar_id, row.recorded_at) for row in await within(session, NDOVU, 2_000.0)}
    far = {(row.collar_id, row.recorded_at) for row in await within(session, NDOVU, 6_000.0)}
    assert near, "the seed must put fixes near Ndovu"
    assert near < far


@skip_without_database
async def test_within_orders_by_distance_and_breaks_ties_on_the_key(session) -> None:
    """The documentation pastes this output, so it cannot depend on the planner.

    Row order from PostgreSQL is not defined without an `ORDER BY`, and the
    exact test runs in Python afterwards -- so the sort has to happen here or a
    reseed, a vacuum or a different plan silently reorders the example's docs.
    """
    found = await within(session, NDOVU, 4_000.0)
    metres = [
        distance(NDOVU, Coordinate(lat=row.latitude, lon=row.longitude)) for row in found
    ]
    assert metres == sorted(metres)
    again = await within(session, NDOVU, 4_000.0)
    assert [(row.collar_id, row.recorded_at) for row in again] == [
        (row.collar_id, row.recorded_at) for row in found
    ]


# -- nearest ------------------------------------------------------------------


@skip_without_database
async def test_nearest_returns_the_closest_fixes_in_order(session) -> None:
    """A doubling search, and the answer it lands on is the true answer.

    Checked against a much wider `within`, which is the definition: the five
    nearest fixes to a point are the first five of everything inside a circle
    big enough to contain them.
    """
    closest = await nearest(session, NDOVU, count=5)
    assert len(closest) == 5
    everything = await within(session, NDOVU, 4_000.0)
    assert [(row.collar_id, row.recorded_at) for row in closest] == [
        (row.collar_id, row.recorded_at) for row in everything[:5]
    ]


@skip_without_database
async def test_nearest_widens_when_the_first_circle_is_not_enough(session) -> None:
    """The doubling actually happens, which the waterhole case cannot show.

    Around Ndovu the very first 500 m circle already holds hundreds of fixes, so
    a `nearest` that never widened would return the right answer there and be
    completely broken everywhere else. The airstrip is the opposite: a handful
    of fixes at 500 m, so getting five requires the search to grow.
    """
    airstrip = Coordinate(
        lat=CENTRE_LAT + LANDMARKS[5][3], lon=CENTRE_LON + LANDMARKS[5][4]
    )
    close = await within(session, airstrip, 500.0)
    assert len(close) < 5, "the airstrip must be quiet, or this proves nothing"

    found = await nearest(session, airstrip, count=5)
    assert len(found) == 5
    assert set(close).issubset(set(found)), "widening kept what the first circle had"


def test_the_widening_sequence_is_computed_before_any_query_runs() -> None:
    """Termination is a property of the data, not of what the database returns.

    The version this replaced was a `while` loop with the ceiling and the
    have-enough test in one condition, and every way of getting that slightly
    wrong is a *hang* on a request path rather than a wrong answer. Two mutants
    of it came back undecided for exactly that reason: the harness cannot tell a
    non-terminating handler from a slow one.
    """
    from tracking.place import widening

    assert widening(500.0, 50_000.0) == (500.0, 1_000.0, 2_000.0, 4_000.0, 8_000.0,
                                         16_000.0, 32_000.0, 50_000.0)
    # The ceiling is always tried, even when doubling overshoots it.
    assert widening(500.0, 600.0) == (500.0, 600.0)
    # And a start past the ceiling is one query, not zero and not an error.
    assert widening(9_000.0, 1_000.0) == (1_000.0,)


def test_a_widening_with_no_room_to_widen_is_refused() -> None:
    """A negative or zero radius is a caller's mistake, named where it is made."""
    from tracking.place import widening

    from wreath.geospatial import GeospatialError

    with pytest.raises(GeospatialError, match="positive"):
        widening(0.0, 1_000.0)
    with pytest.raises(GeospatialError, match="positive"):
        widening(500.0, -1.0)


@skip_without_database
async def test_nearest_gives_up_rather_than_walking_out_to_a_hemisphere(
    session,
) -> None:
    """A point in the ocean has no fixes near it, and that is a true answer.

    Returning fewer than asked for is honest; raising would make an empty
    conservancy an error, and doubling without a ceiling would turn a mistyped
    longitude into a scan of the whole table.
    """
    ocean = Coordinate(lat=-30.0, lon=70.0)
    assert await nearest(session, ocean, count=3, max_m=20_000.0) == []


# -- a path -------------------------------------------------------------------


@skip_without_database
async def test_a_track_is_the_sum_of_its_legs_and_not_a_straight_line(session) -> None:
    """An animal that comes back to the waterhole still walked all day.

    A `Trajectory` sums the legs, so its distance must exceed the displacement
    from first fix to last -- by a lot, for a correlated random walk over a day.
    A straight-line implementation would pass every other test in this file.
    """
    start = EPOCH + datetime.timedelta(days=3)
    path = await track(session, SARARA, since=start, until=start + datetime.timedelta(days=1))
    assert len(path) == 72, "72 fixes at a twenty-minute duty cycle"
    displacement = distance(path.fixes[0][1], path.fixes[-1][1])
    assert path.distance > displacement * 2


@skip_without_database
async def test_a_track_is_ordered_by_when_it_happened_not_by_when_it_arrived(
    session,
) -> None:
    """The collar that lost the sky is why this is not the same ordering.

    Naserian's buffer covers days 12 to 15 and arrives on day 16, so its rows
    were written long after rows that come *after* them in time. A trajectory
    built in arrival order would double back through three days of history and
    report several times the true distance.
    """
    start = EPOCH + datetime.timedelta(days=12)
    path = await track(session, 1, since=start, until=start + datetime.timedelta(days=4))
    moments = [moment for moment, _ in path.fixes]
    assert moments == sorted(moments)

    rows = await session.raw(
        f'SELECT recorded_at, received_at FROM "{SCHEMA}"."fixes" '
        "WHERE animal_id = 1 AND recorded_at >= $1 AND recorded_at < $2 "
        "ORDER BY recorded_at",
        start,
        start + datetime.timedelta(days=4),
    ).fetch()
    assert rows, "the seeded silence must be in this window"
    # Every fix in the silence carries the same arrival: the moment the collar
    # saw the sky again and drained its buffer in one upload.
    arrivals = {row[1] for row in rows}
    assert len(arrivals) == 1, "a buffer dump arrives all at once, by definition"
    latest = max(row[0] for row in rows)
    assert arrivals.pop() > latest, "the dump is later than the last fix in it"
    earliest = min(row[0] for row in rows)
    assert latest - earliest >= datetime.timedelta(days=3), (
        "the silence must actually span days, or this proves nothing about late data"
    )


@skip_without_database
async def test_a_window_with_no_fixes_is_an_empty_path_rather_than_an_error(
    session,
) -> None:
    """`speed` is None rather than 0.0 when the path spans no time.

    A division that cannot be performed has no answer, and zero would read as
    "stationary" -- which is a different claim and the one a dashboard would
    draw as a dead animal.
    """
    future = EPOCH + datetime.timedelta(days=400)
    path = await track(session, SARARA, since=future, until=future + datetime.timedelta(days=1))
    assert len(path) == 0
    assert path.distance == 0.0
    assert path.speed is None


# -- landmarks ----------------------------------------------------------------


@skip_without_database
async def test_the_nearest_landmark_is_found_in_python_over_twelve_rows(session) -> None:
    """Six landmarks is not a query, and the answer is still the right one."""
    from tracking.models import Landmark

    landmarks = await session.fetch(Landmark.select().order_by(Landmark.id))
    assert len(landmarks) == len(LANDMARKS)

    found = nearest_landmark(NDOVU, landmarks)
    assert found is not None
    mark, metres = found
    assert mark.name == "Ndovu Waterhole"
    assert metres == pytest.approx(0.0, abs=1.0)


def test_no_landmarks_is_a_state_rather_than_an_error() -> None:
    """A conservancy with nothing surveyed yet is what the seeder passes through."""
    assert nearest_landmark(NDOVU, []) is None
