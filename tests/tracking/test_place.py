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


@pytest.fixture(scope="module")
def seeded_schema():
    """Build and seed this worker's schema once for the whole file.

    **Every test below only reads**, which is the precondition for sharing and
    the reason this is safe here rather than generally. A test that wrote would
    see its neighbours' rows, and the failure would be order-dependent -- so if
    one is added, it builds its own schema instead of joining this.

    Rebuilding per test cost far more than the 1.2s it looks like. Measured
    alone, `DROP SCHEMA` + create + seeding 51,840 rows is 1,183ms; measured in
    the suite it was **9.18 seconds a test**, sixteen times over, because eight
    xdist workers were doing it at once against one PostgreSQL. The seed is the
    load, and there is no reason to apply it sixteen times to a schema nobody
    modifies.

    Deliberately synchronous, driving its own loop with `asyncio.run`. The tests
    are function-scoped and async, so each gets its own event loop, and a pool
    opened on a module-scoped loop would be handed to tests running on a
    different one. Only the DDL is shared; `Database.start()` costs 4ms and
    stays per test, where its loop affinity is correct by construction.
    """
    import asyncio

    from _tracking import build_schema, drop_schema

    from wreath.postgres import connect

    async def _apply(step):
        connection = await connect(_DSN)
        try:
            await step(connection)
        finally:
            await connection.close()

    asyncio.run(_apply(build_schema))
    try:
        yield
    finally:
        asyncio.run(_apply(drop_schema))


@pytest.fixture
async def session(seeded_schema):
    """A read session on the schema `seeded_schema` built.

    No HTTP: these are questions about queries, and putting a router between the
    test and the plan would only make a failure harder to attribute.
    """
    from tracking.models import MODELS

    from wreath.orm.registry import Registry
    from wreath.orm.session import Session
    from wreath.postgres import Database

    database = Database("main", _DSN, pools={"read": {"min_size": 1, "max_size": 2}})
    await database.start()
    session = Session(Registry(database, list(MODELS), validate_schema="off"), "read")
    try:
        yield session
    finally:
        # `close()` returns the leased connection. Without it the pool has a
        # borrowed connection nobody will hand back, so `stop()` waits out the
        # whole 10s `shutdown_timeout` and then closes it underneath the session
        # anyway -- 10.01s of teardown against calls of 0.01-0.22s.
        await session.close()
        await database.stop()


@skip_without_database
async def test_the_proximity_query_reaches_the_index_as_a_condition(seeded_schema) -> None:
    from wreath.orm.compiler import compile_select
    from wreath.orm.registry import Registry
    from wreath.postgres import Database

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

    plan = "\n".join(str(row[0]) for row in rows)
    assert "Index Cond" in plan, plan
    assert "latitude" in plan and "longitude" in plan, plan
    assert "Seq Scan" not in plan, plan


@skip_without_database
async def test_a_search_too_broad_to_answer_is_refused_rather_than_truncated(
    session,
) -> None:
    from tracking.place import SearchTooBroad

    with pytest.raises(SearchTooBroad, match="narrow the radius"):
        await within(session, NDOVU, 5_000.0, limit=100)


@skip_without_database
async def test_within_returns_only_what_is_actually_inside_the_circle(session) -> None:
    found = await within(session, NDOVU, 3_000.0)
    assert found, "the seed must put fixes near Ndovu or this proves nothing"
    for row in found:
        assert distance(NDOVU, Coordinate(lat=row.latitude, lon=row.longitude)) <= 3_000.0


@skip_without_database
async def test_the_rectangle_really_does_return_more_than_the_circle(session) -> None:
    boxed = await session.fetch(box_query(NDOVU, 3_000.0, limit=20_001))
    circled = await within(session, NDOVU, 3_000.0)
    assert len(boxed) > len(circled)
    # A circle is pi/4 of its bounding square, so the survivors should be around
    # 78% of the box. Bounded loosely on both sides, because the seed's animals
    # are not uniformly spread and an exact ratio would be asserting the walk.
    assert 0.6 < len(circled) / len(boxed) < 0.95


@skip_without_database
async def test_within_finds_the_corner_cases_the_box_would_have_missed(session) -> None:
    near = {(row.collar_id, row.recorded_at) for row in await within(session, NDOVU, 2_000.0)}
    far = {(row.collar_id, row.recorded_at) for row in await within(session, NDOVU, 6_000.0)}
    assert near, "the seed must put fixes near Ndovu"
    assert near < far


@skip_without_database
async def test_within_orders_by_distance_and_breaks_ties_on_the_key(session) -> None:
    found = await within(session, NDOVU, 4_000.0)
    metres = [distance(NDOVU, Coordinate(lat=row.latitude, lon=row.longitude)) for row in found]
    assert metres == sorted(metres)
    again = await within(session, NDOVU, 4_000.0)
    assert [(row.collar_id, row.recorded_at) for row in again] == [
        (row.collar_id, row.recorded_at) for row in found
    ]


@skip_without_database
async def test_nearest_returns_the_closest_fixes_in_order(session) -> None:
    closest = await nearest(session, NDOVU, count=5)
    assert len(closest) == 5
    everything = await within(session, NDOVU, 4_000.0)
    assert [(row.collar_id, row.recorded_at) for row in closest] == [
        (row.collar_id, row.recorded_at) for row in everything[:5]
    ]


@skip_without_database
async def test_nearest_widens_when_the_first_circle_is_not_enough(session) -> None:
    airstrip = Coordinate(lat=CENTRE_LAT + LANDMARKS[5][3], lon=CENTRE_LON + LANDMARKS[5][4])
    close = await within(session, airstrip, 500.0)
    assert len(close) < 5, "the airstrip must be quiet, or this proves nothing"

    found = await nearest(session, airstrip, count=5)
    assert len(found) == 5
    assert set(close).issubset(set(found)), "widening kept what the first circle had"


def test_the_widening_sequence_is_computed_before_any_query_runs() -> None:
    from tracking.place import widening

    assert widening(500.0, 50_000.0) == (
        500.0,
        1_000.0,
        2_000.0,
        4_000.0,
        8_000.0,
        16_000.0,
        32_000.0,
        50_000.0,
    )
    # The ceiling is always tried, even when doubling overshoots it.
    assert widening(500.0, 600.0) == (500.0, 600.0)
    # And a start past the ceiling is one query, not zero and not an error.
    assert widening(9_000.0, 1_000.0) == (1_000.0,)


def test_a_widening_with_no_room_to_widen_is_refused() -> None:
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
    ocean = Coordinate(lat=-30.0, lon=70.0)
    assert await nearest(session, ocean, count=3, max_m=20_000.0) == []


@skip_without_database
async def test_a_track_is_the_sum_of_its_legs_and_not_a_straight_line(session) -> None:
    start = EPOCH + datetime.timedelta(days=3)
    path = await track(session, SARARA, since=start, until=start + datetime.timedelta(days=1))
    assert len(path) == 72, "72 fixes at a twenty-minute duty cycle"
    displacement = distance(path.fixes[0][1], path.fixes[-1][1])
    assert path.distance > displacement * 2


@skip_without_database
async def test_a_track_is_ordered_by_when_it_happened_not_by_when_it_arrived(
    session,
) -> None:
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
    future = EPOCH + datetime.timedelta(days=400)
    path = await track(session, SARARA, since=future, until=future + datetime.timedelta(days=1))
    assert len(path) == 0
    assert path.distance == 0.0
    assert path.speed is None


@skip_without_database
async def test_the_nearest_landmark_is_found_in_python_over_twelve_rows(session) -> None:
    from tracking.models import Landmark

    landmarks = await session.fetch(Landmark.select().order_by(Landmark.id))
    assert len(landmarks) == len(LANDMARKS)

    found = nearest_landmark(NDOVU, landmarks)
    assert found is not None
    mark, metres = found
    assert mark.name == "Ndovu Waterhole"
    assert metres == pytest.approx(0.0, abs=1.0)


def test_no_landmarks_is_a_state_rather_than_an_error() -> None:
    assert nearest_landmark(NDOVU, []) is None
