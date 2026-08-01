"""Protobuf in, rows out — and what happens when the bytes are wrong.

Four properties, in descending order of how much they would cost to get wrong:

1. **A malformed body is a 4xx.** A satellite relay retries. If a truncated
   upload answers 500, it will retry forever at the rate its spool refills, and
   the endpoint is down for everyone.
2. **One bad position does not fail thirty-nine good ones.** A collar with a
   corrupt almanac is a fact about that collar, and losing an entire station's
   upload to it is a much worse outcome than dropping the reading.
3. **A retry lands nothing.** The primary key is `(collar_id, recorded_at)` and
   the insert is `ON CONFLICT DO NOTHING`, so idempotency is a property of the
   schema rather than of a flag somebody remembered to check.
4. **The stored leg agrees with `Trajectory`.** `Fix.leg_m` is a derivation kept
   in a column, and a derivation with two implementations has two answers. There
   is one `distance` function and this holds the column to it -- including after
   a late buffer dump lands in the middle of history, which is the case the
   sealed daily view then has to report.
"""

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


# -- the batch lands ----------------------------------------------------------


@skip_without_database
async def test_a_protobuf_batch_lands_rows_and_answers_in_protobuf(client) -> None:
    """The whole of stage one, in one test.

    Bytes go up as `application/x-protobuf`, rows appear, and the reply is
    protobuf too -- a station on a metered link should not be made to parse JSON
    to learn how many of its positions were kept.
    """
    response = await post(client, walk_batch(range(5)))
    assert response.status == 200, response.text
    assert response.header("content-type") == MEDIA_TYPE

    receipt = decode(BatchReceipt, response.body)
    assert receipt.accepted == 5
    assert receipt.rejected == 0
    assert receipt.watermark_ms == milliseconds(FIRST + datetime.timedelta(minutes=80))

    listed = await client.get(
        f"/animals/{ANIMAL}/track?since=2026-05-01&days=1"
    )
    assert listed.status == 200, listed.text
    assert len(listed.json()["fixes"]) == 5


@skip_without_database
async def test_the_receipt_watermark_is_what_a_restarting_station_resumes_from(
    client,
) -> None:
    """The station does not have to remember; the receipt tells it.

    A relay that restarts sends everything newer than the watermark it was last
    given. If that number were the *arrival* time rather than the newest
    `recorded_at`, a collar's buffered positions would be skipped on the resume
    -- which is silent, and exactly the data the late-data chapter is about.
    """
    first = decode(BatchReceipt, (await post(client, walk_batch(range(3)))).body)
    second = decode(BatchReceipt, (await post(client, walk_batch(range(3, 5)))).body)
    assert second.watermark_ms > first.watermark_ms
    assert second.watermark_ms == milliseconds(FIRST + datetime.timedelta(minutes=80))


# -- and the refusals ---------------------------------------------------------


@skip_without_database
async def test_a_body_that_is_not_protobuf_at_all_is_a_400(client) -> None:
    """**The most important status code in this file.**

    A 500 tells a retrying satellite relay to try again, and it will, forever.
    `ProtobufDecodeError` covers every malformed-input failure, so one `except`
    in the handler is the whole fix.
    """
    response = await post(client, b"{\"relay\": \"kimana\"}")
    assert response.status == 400
    assert "malformed position batch" in response.text


@skip_without_database
async def test_a_truncated_batch_is_a_400(client) -> None:
    """The failure mode of an upload that lost its link halfway.

    Sliced short rather than corrupted at random, because that is the shape a
    dropped connection actually produces: a valid prefix and a length prefix
    claiming more bytes than remain.
    """
    body = walk_batch(range(5))
    response = await post(client, body[: len(body) // 2])
    assert response.status == 400


@skip_without_database
async def test_an_empty_body_is_a_valid_empty_batch_and_not_an_error(client) -> None:
    """Zero bytes is a well-formed protobuf message with every field defaulted.

    Which makes it a batch with no relay name, and *that* is what the refusal
    names -- not "malformed". Getting this wrong the other way would mean a
    station with nothing to send could never say so.
    """
    response = await post(client, b"")
    assert response.status == 400
    assert "relay" in response.text


@skip_without_database
async def test_a_position_that_cannot_be_a_place_is_counted_not_fatal(client) -> None:
    """One collar's firmware fault must not lose the station's whole upload.

    `Coordinate` refuses a latitude past the pole, and here that refusal is
    caught into a number in the receipt rather than a 4xx -- because the batch
    parsed perfectly and thirty-nine other collars were fine. The count is what
    makes the failing collar findable.
    """
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
    """A collar id this application has no row for cannot become a fix.

    Without the check it would be a foreign-key violation, which fails the whole
    statement -- so one stray id in a two-hundred-position batch would lose the
    other hundred and ninety-nine.
    """
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
    """An unbounded batch is one collar's failure mode becoming a dead worker."""
    from tracking.ingest import MAX_POSITIONS

    huge = [position(0, *WALK[0]) for _ in range(MAX_POSITIONS + 1)]
    response = await post(client, batch(*huge))
    assert response.status == 400
    assert str(MAX_POSITIONS) in response.text


def test_the_batch_ceiling_is_a_days_spool_for_one_relay() -> None:
    """5,000: forty collars at a twenty-minute duty cycle for about a day and a half.

    Asserted as a value, not only through the refusal above. A ceiling raised
    past reach still *has* a refusal — it is simply never reached — and the
    request that finds out is one that allocates until the worker dies. Pinning
    the number is what makes widening it a decision rather than a diff.
    """
    from tracking.ingest import MAX_POSITIONS

    assert MAX_POSITIONS == 5_000


@skip_without_database
async def test_a_batch_where_everything_is_rejected_writes_nothing(client) -> None:
    """Zero accepted is a 200 with a receipt, not an error and not a broadcast.

    The station's upload was well-formed; its collar was not. A 4xx here would
    make it retry a batch that will never be accepted, and a broadcast would put
    positions on every open live map that are in no table.
    """
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
    """`| None` on the wire, a plain column in the table, and the default is zero.

    proto3's explicit presence is what lets a collar say "I did not measure
    this" rather than "I measured zero", and the storage layer has to choose one
    number for that. Zero is the honest one -- a fix with no satellite count is
    not a fix with a good satellite count -- and this pins it, because the
    fallback is a one-character edit away from writing the wrong number for
    every collar that omits the field.
    """
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
            f'SELECT satellites FROM "{SCHEMA}"."fixes" WHERE animal_id = $1 '
            "ORDER BY recorded_at",
            ANIMAL,
        )
    finally:
        await connection.close()
    assert [row[0] for row in rows] == [0, 7], "absent means 0; a reported 7 stays 7"


@skip_without_database
async def test_a_batch_that_arrives_out_of_order_repairs_from_its_oldest_position(
    client,
) -> None:
    """A spool is not guaranteed sorted, and the repair point is the *minimum*.

    A station draining a buffer may send the newest position first. If the
    repair started from whichever position happened to come first in the batch,
    everything older than it would keep a leg measured across a gap that is no
    longer there — and the day's distance would be too long, for exactly the
    rows the late-data chapter is about.
    """
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
    """Ingest is the one route here that is not public.

    Everything else in this application is `@identify()` -- read differently for
    a known caller, readable by anyone. Writing is not.
    """
    from tracking.app import build

    from wreath.testing import TestClient

    async with TestClient(build(cross_worker=False)) as anonymous:
        response = await post(anonymous, walk_batch(range(1)))
    assert response.status == 401


# -- idempotency --------------------------------------------------------------


@skip_without_database
async def test_a_station_retrying_a_batch_lands_nothing_twice(client) -> None:
    """The upload timed out *after* the write. The station cannot know that.

    So it sends the batch again, and the primary key absorbs it. The receipt
    still reports the positions as accepted, which is the honest answer to the
    question the station is asking -- "is this data safely with you" -- and the
    row count is what proves nothing was duplicated.
    """
    await post(client, walk_batch(range(5)))
    again = decode(BatchReceipt, (await post(client, walk_batch(range(5)))).body)
    assert again.accepted == 5

    listed = await client.get(f"/animals/{ANIMAL}/track?since=2026-05-01&days=1")
    assert len(listed.json()["fixes"]) == 5


@skip_without_database
async def test_one_batch_carrying_the_same_position_twice_is_not_a_failure(
    client,
) -> None:
    """PostgreSQL refuses `ON CONFLICT DO NOTHING` for a collision *inside* one
    command -- "cannot affect row a second time" -- so a station that spooled a
    position twice would fail the whole request rather than landing it once.
    Deduplicating before the statement is what stops the conflict clause from
    being defeated by the very case it exists for.
    """
    duplicated = batch(position(0, *WALK[0]), position(0, *WALK[0]))
    response = await post(client, duplicated)
    assert response.status == 200, response.text
    assert decode(BatchReceipt, response.body).accepted == 1


# -- the stored derivation ----------------------------------------------------


@skip_without_database
async def test_the_stored_legs_sum_to_what_a_trajectory_measures(client) -> None:
    """One `distance` function, so the column and the primitive cannot disagree.

    If `repair_legs` wrote haversine in SQL there would be two implementations,
    and they would diverge by a rounding rule nobody would notice for a year.
    The track endpoint's `distance_m` comes from `Trajectory`; the daily chart's
    comes from `sum(leg_m)`; this asserts they are the same number.
    """
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
    assert sum(leg for leg in legs if leg is not None) == pytest.approx(
        expected.distance, abs=0.05
    )


@skip_without_database
async def test_a_late_buffer_dump_repairs_the_leg_it_landed_in_front_of(client) -> None:
    """**The subtlety the whole late-data chapter rests on.**

    Steps 0 and 4 land first, so step 4's leg is measured across the whole walk
    -- about a kilometre in a straight line. Then the collar's buffer arrives
    with steps 1, 2 and 3, which belong *between* them. If step 4's leg were
    left alone, the animal's total distance would count that stretch twice.

    So the repair walks forward from the earliest position that landed, and the
    assertion is that the total afterwards is the true path length rather than
    the true path plus a phantom shortcut.
    """
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
            f'SELECT leg_m FROM "{SCHEMA}"."fixes" WHERE animal_id = $1 '
            "ORDER BY recorded_at",
            ANIMAL,
        )
        return [row[0] for row in rows]
    finally:
        await connection.close()
