from __future__ import annotations

import datetime
import os

import pytest
from tracking.wire import MEDIA_TYPE, BatchReceipt, Position, PositionBatch, milliseconds

from wreath.geospatial import Coordinate, Trajectory, distance
from wreath.protobuf import decode, encode

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the tracking ingest tests",
)

#: Well past the seed's last fix, so an ingest test's rows are the only ones in
#: the window it asks about.
FIRST = datetime.datetime(2026, 5, 1, 6, 0, tzinfo=datetime.UTC)

#: Sarara, an open plains zebra. Open so a ranger and a volunteer see the same
#: thing here and the ingest tests are about ingest.
COLLAR = 8
ANIMAL = 8

#: A short walk east from the conservancy centre, about 300 m a step.
WALK = (
    (-1.9705, 36.1042),
    (-1.9705, 36.1069),
    (-1.9702, 36.1096),
    (-1.9698, 36.1123),
    (-1.9691, 36.1150),
)


def position(step: int, lat: float, lon: float, *, minutes: int = 20) -> Position:
    return Position(
        collar_id=COLLAR,
        recorded_at_ms=milliseconds(FIRST + datetime.timedelta(minutes=minutes * step)),
        lat=lat,
        lon=lon,
        accuracy_m=11.5,
        battery_pct=64,
        satellites=7,
    )


def batch(*positions: Position, relay: str = "relay-kimana") -> bytes:
    return encode(PositionBatch(relay=relay, positions=list(positions)))


def walk_batch(steps: range) -> bytes:
    return batch(*(position(step, *WALK[step]) for step in steps))


@pytest.fixture
async def client():
    """The application on a schema seeded with animals and collars but no fixes.

    A batch landing into an empty table is the only way to assert what a batch
    landed; with the seed's 51,840 positions in the way, every count here would
    be a difference rather than a number.
    """
    from _tracking import build_schema, drop_schema
    from tracking.app import build

    from wreath.postgres import connect
    from wreath.testing import TestClient

    connection = await connect(_DSN)
    try:
        await build_schema(connection, fixes=False)
    finally:
        await connection.close()

    application = build(cross_worker=False)
    async with TestClient(application) as test_client:
        yield test_client.acting_as("relay-1", roles=["ranger"], type="Observer")

    connection = await connect(_DSN)
    try:
        await drop_schema(connection)
    finally:
        await connection.close()


async def post(client, body: bytes):
    return await client.post(
        "/ingest/positions", content=body, headers={"content-type": MEDIA_TYPE}
    )


@skip_without_database
async def test_a_protobuf_batch_lands_rows_and_answers_in_protobuf(client) -> None:
    response = await post(client, walk_batch(range(5)))
    assert response.status == 200, response.text
    assert response.header("content-type") == MEDIA_TYPE

    receipt = decode(BatchReceipt, response.body)
    assert receipt.accepted == 5
    assert receipt.rejected == 0
    assert receipt.watermark_ms == milliseconds(FIRST + datetime.timedelta(minutes=80))

    listed = await client.get(f"/animals/{ANIMAL}/track?since=2026-05-01&days=1")
    assert listed.status == 200, listed.text
    assert len(listed.json()["fixes"]) == 5


@skip_without_database
async def test_the_receipt_watermark_is_what_a_restarting_station_resumes_from(
    client,
) -> None:
    first = decode(BatchReceipt, (await post(client, walk_batch(range(3)))).body)
    second = decode(BatchReceipt, (await post(client, walk_batch(range(3, 5)))).body)
    assert second.watermark_ms > first.watermark_ms
    assert second.watermark_ms == milliseconds(FIRST + datetime.timedelta(minutes=80))


@skip_without_database
async def test_a_body_that_is_not_protobuf_at_all_is_a_400(client) -> None:
    response = await post(client, b'{"relay": "kimana"}')
    assert response.status == 400
    assert "malformed position batch" in response.text


@skip_without_database
async def test_a_truncated_batch_is_a_400(client) -> None:
    body = walk_batch(range(5))
    response = await post(client, body[: len(body) // 2])
    assert response.status == 400


@skip_without_database
async def test_an_empty_body_is_a_valid_empty_batch_and_not_an_error(client) -> None:
    response = await post(client, b"")
    assert response.status == 400
    assert "relay" in response.text


@skip_without_database
async def test_a_position_that_cannot_be_a_place_is_counted_not_fatal(client) -> None:
    good = position(0, *WALK[0])
    broken = Position(
        collar_id=COLLAR,
        recorded_at_ms=milliseconds(FIRST + datetime.timedelta(minutes=20)),
        lat=91.4,
        lon=36.1,
        accuracy_m=None,
        battery_pct=64,
        satellites=2,
    )
    receipt = decode(BatchReceipt, (await post(client, batch(good, broken))).body)
    assert receipt.accepted == 1
    assert receipt.rejected == 1


@skip_without_database
async def test_a_position_from_a_collar_nobody_fitted_is_rejected(client) -> None:
    stray = Position(
        collar_id=9_999,
        recorded_at_ms=milliseconds(FIRST),
        lat=-1.97,
        lon=36.10,
        accuracy_m=None,
        battery_pct=50,
        satellites=6,
    )
    receipt = decode(BatchReceipt, (await post(client, batch(position(0, *WALK[0]), stray))).body)
    assert receipt.accepted == 1
    assert receipt.rejected == 1


@skip_without_database
async def test_a_batch_past_the_limit_is_refused_rather_than_allocated(client) -> None:
    from tracking.ingest import MAX_POSITIONS

    huge = [position(0, *WALK[0]) for _ in range(MAX_POSITIONS + 1)]
    response = await post(client, batch(*huge))
    assert response.status == 400
    assert str(MAX_POSITIONS) in response.text


def test_the_batch_ceiling_is_a_days_spool_for_one_relay() -> None:
    from tracking.ingest import MAX_POSITIONS

    assert MAX_POSITIONS == 5_000


@skip_without_database
async def test_a_batch_where_everything_is_rejected_writes_nothing(client) -> None:
    broken = Position(
        collar_id=COLLAR,
        recorded_at_ms=milliseconds(FIRST),
        lat=91.4,
        lon=36.1,
        accuracy_m=None,
        battery_pct=64,
        satellites=2,
    )
    response = await post(client, batch(broken))
    assert response.status == 200
    receipt = decode(BatchReceipt, response.body)
    assert (receipt.accepted, receipt.rejected, receipt.watermark_ms) == (0, 1, 0)

    listed = await client.get(f"/animals/{ANIMAL}/track?since=2026-05-01&days=1")
    assert listed.json()["fixes"] == []


@skip_without_database
async def test_a_collar_that_reported_no_satellite_count_stores_zero(client) -> None:
    from tracking.config import SCHEMA

    from wreath.postgres import connect

    silent = Position(
        collar_id=COLLAR,
        recorded_at_ms=milliseconds(FIRST),
        lat=WALK[0][0],
        lon=WALK[0][1],
        accuracy_m=None,
        battery_pct=64,
        satellites=None,
    )
    await post(client, batch(silent, position(1, *WALK[1])))

    connection = await connect(_DSN)
    try:
        rows = await connection.fetch(
            f'SELECT satellites FROM "{SCHEMA}"."fixes" WHERE animal_id = $1 ORDER BY recorded_at',
            ANIMAL,
        )
    finally:
        await connection.close()
    assert [row[0] for row in rows] == [0, 7], "absent means 0; a reported 7 stays 7"


@skip_without_database
async def test_a_batch_that_arrives_out_of_order_repairs_from_its_oldest_position(
    client,
) -> None:
    await post(client, batch(position(0, *WALK[0]), position(4, *WALK[4])))

    # Steps 3, 1, 2 -- deliberately not sorted, and step 3 first so a repair
    # anchored on the batch's first element would skip 1 and 2.
    await post(
        client,
        batch(position(3, *WALK[3]), position(1, *WALK[1]), position(2, *WALK[2])),
    )

    truth = Trajectory(
        [
            (FIRST + datetime.timedelta(minutes=20 * step), Coordinate(lat=lat, lon=lon))
            for step, (lat, lon) in enumerate(WALK)
        ]
    ).distance
    legs = [leg for leg in await _legs(client) if leg is not None]
    assert len(legs) == 4
    assert sum(legs) == pytest.approx(truth, abs=0.05)


@skip_without_database
async def test_an_unsigned_station_cannot_relay(client) -> None:
    from tracking.app import build

    from wreath.testing import TestClient

    async with TestClient(build(cross_worker=False)) as anonymous:
        response = await post(anonymous, walk_batch(range(1)))
    assert response.status == 401


@skip_without_database
async def test_a_station_retrying_a_batch_lands_nothing_twice(client) -> None:
    await post(client, walk_batch(range(5)))
    again = decode(BatchReceipt, (await post(client, walk_batch(range(5)))).body)
    assert again.accepted == 5

    listed = await client.get(f"/animals/{ANIMAL}/track?since=2026-05-01&days=1")
    assert len(listed.json()["fixes"]) == 5


@skip_without_database
async def test_one_batch_carrying_the_same_position_twice_is_not_a_failure(
    client,
) -> None:
    duplicated = batch(position(0, *WALK[0]), position(0, *WALK[0]))
    response = await post(client, duplicated)
    assert response.status == 200, response.text
    assert decode(BatchReceipt, response.body).accepted == 1


@skip_without_database
async def test_the_stored_legs_sum_to_what_a_trajectory_measures(client) -> None:
    await post(client, walk_batch(range(5)))
    body = (await client.get(f"/animals/{ANIMAL}/track?since=2026-05-01&days=1")).json()

    expected = Trajectory(
        [
            (FIRST + datetime.timedelta(minutes=20 * step), Coordinate(lat=lat, lon=lon))
            for step, (lat, lon) in enumerate(WALK)
        ]
    )
    assert body["distance_m"] == pytest.approx(expected.distance, abs=0.05)

    legs = await _legs(client)
    assert legs[0] is None, "the first fix of an animal has no leg before it"
    assert sum(leg for leg in legs if leg is not None) == pytest.approx(expected.distance, abs=0.05)


@skip_without_database
async def test_a_late_buffer_dump_repairs_the_leg_it_landed_in_front_of(client) -> None:
    await post(client, batch(position(0, *WALK[0]), position(4, *WALK[4])))
    before = [leg for leg in await _legs(client) if leg is not None]
    straight = distance(
        Coordinate(lat=WALK[0][0], lon=WALK[0][1]),
        Coordinate(lat=WALK[4][0], lon=WALK[4][1]),
    )
    assert sum(before) == pytest.approx(straight, abs=0.05)

    await post(client, walk_batch(range(1, 4)))
    truth = Trajectory(
        [
            (FIRST + datetime.timedelta(minutes=20 * step), Coordinate(lat=lat, lon=lon))
            for step, (lat, lon) in enumerate(WALK)
        ]
    ).distance
    after = [leg for leg in await _legs(client) if leg is not None]
    assert len(after) == 4
    assert sum(after) == pytest.approx(truth, abs=0.05)
    assert sum(after) > sum(before), "the filled-in gap is longer than the shortcut"


async def _legs(client) -> list[float | None]:
    """Every stored `leg_m` for the animal, in `recorded_at` order.

    Read from the database rather than from the API, because `leg_m` is
    deliberately not on the wire: it is an implementation of the daily chart,
    and putting it in a response would make it a contract.
    """
    from tracking.config import SCHEMA

    from wreath.postgres import connect

    connection = await connect(_DSN)
    try:
        rows = await connection.fetch(
            f'SELECT leg_m FROM "{SCHEMA}"."fixes" WHERE animal_id = $1 ORDER BY recorded_at',
            ANIMAL,
        )
        return [row[0] for row in rows]
    finally:
        await connection.close()
