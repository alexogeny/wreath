"""The collar that lost the sky, and the day that changed after it ended.

One test carries this file. A day is sealed; a buffered position for that day
arrives afterwards; and the settled number **does not move**. The difference is
recorded beside it, folded in on read, and named in the envelope -- so late data
reads as late data arriving rather than as a figure somebody quoted on Wednesday
that says something else on Friday.

The sharpest assertion here is the one on the stored row: not that the read
changed, but that the *settled value in the table* did not. A view that
rewrote the settled value would satisfy every other assertion in this file while
destroying the only property sealing exists to provide.
"""

from __future__ import annotations

import datetime
import json
import os

import pytest
from tracking.config import CONSERVANCY_ZONE, SCHEMA
from tracking.seed import EPOCH
from tracking.views import BUFFER_LATENESS, daily_distance
from tracking.wire import Position, PositionBatch, milliseconds

from wreath.protobuf import encode
from wreath.series import Range
from wreath.temporal import zone as tz

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the tracking late-data tests",
)

#: Nkoteiya, a blue wildebeest whose collar goes quiet on seeded days 25 and 26.
#: Chosen so the *seed itself* contains a real buffer dump, and so the day this
#: test adds to is one the story is about.
ANIMAL = 11
COLLAR = 11

#: A day well inside the seeded window and well behind the sealing horizon.
DAY = 20

#: Far past the end of the seed, so every day in it is sealed at 36 hours of
#: lateness.
NOW = datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC)


def local_day(offset: int) -> Range:
    """The half-open instant range of one local day, in the conservancy's zone.

    A day is only a day once you say whose. Africa/Nairobi is +03, so the local
    day starting on seeded day `offset` begins three hours before the UTC one --
    and a bucket cut in UTC would put three hours of that day's fixes in the
    day before it.
    """
    here = tz(CONSERVANCY_ZONE)
    midnight = (EPOCH + datetime.timedelta(days=offset)).astimezone(here).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return Range(midnight, midnight + datetime.timedelta(days=1))


@pytest.fixture
async def world():
    """A seeded schema, wreath's settled tables, a database and a session."""
    from _tracking import build_schema, drop_schema
    from tracking.models import MODELS

    from wreath.orm.registry import Registry
    from wreath.orm.session import Session
    from wreath.postgres import Database, connect

    connection = await connect(_DSN)
    try:
        # `build_schema` applies the settled-bucket tables too -- they are
        # wreath's own furniture, deliberately absent from the example's
        # migration artifact, and claimed by nothing that runs at startup.
        await build_schema(connection)
    finally:
        await connection.close()

    database = Database("main", _DSN, pools={"write": {"min_size": 1, "max_size": 2}})
    await database.start()
    session = Session(Registry(database, list(MODELS), validate_schema="off"), "write")
    try:
        yield database, session
    finally:
        # Ahead of `stop()`: the session's lease is only returned by `close()`,
        # and a connection still out when the pool stops costs the whole 10s
        # `shutdown_timeout` before it is closed underneath the session anyway.
        await session.close()
        await database.stop()
        connection = await connect(_DSN)
        try:
            await drop_schema(connection)
        finally:
            await connection.close()


async def stored_bucket(database, view, params: dict, bucket) -> dict | None:
    """The settled row exactly as it sits in `wreath.series_buckets`.

    Read from the table rather than from a result envelope, because the claim
    is about the *stored* value: a read that folds a correction in is supposed
    to differ, and only the row can say whether anything was rewritten.
    """
    view_id, params_id = view._identity(CONSERVANCY_ZONE, params)
    connection = await database.acquire("write")
    try:
        rows = await connection.fetch(
            'SELECT measures FROM "wreath"."series_buckets" '
            "WHERE view = $1 AND params = $2 AND bucket = $3",
            view_id,
            params_id,
            bucket,
        )
    finally:
        await database.release("write", connection)
    if not rows:
        return None
    measures = rows[0][0]
    return json.loads(measures) if isinstance(measures, str) else measures


# -- the seed's own late data -------------------------------------------------


@skip_without_database
async def test_the_seed_really_contains_a_buffered_dump(world) -> None:
    """The story is in the data, not only in the prose.

    Nkoteiya's collar is quiet for two days and then uploads everything at once,
    so `received_at` is identical across two days of `recorded_at`. Without this
    the late-data chapter would be a paragraph about a case the example does not
    have.
    """
    _database, session = world
    rows = await session.raw(
        f'SELECT recorded_at, received_at FROM "{SCHEMA}"."fixes" '
        "WHERE animal_id = $1 ORDER BY recorded_at",
        ANIMAL,
    ).fetch()
    lags = [row[1] - row[0] for row in rows]
    assert max(lags) > datetime.timedelta(days=1), (
        "no fix arrived more than a day after it was taken; the silence is missing"
    )
    assert min(lags) < datetime.timedelta(hours=1), (
        "every fix arrived late; the contrast with the ordinary case is gone"
    )


# -- the sealing horizon ------------------------------------------------------


@skip_without_database
async def test_a_day_behind_the_horizon_is_settled_and_a_recent_one_is_not(
    world,
) -> None:
    """36 hours after a day closes, its distance stops being recomputed.

    Asserted from both sides: a day a year old is settled, and the day the
    clock is currently in is not. A view that settled everything would make the
    correction story untestable, and one that settled nothing would make the
    seal decorative.
    """
    from _tracking import clear_settled

    database, session = world
    view = daily_distance(CONSERVANCY_ZONE)
    window = local_day(DAY)
    await clear_settled(database, view, {"animal": ANIMAL}, CONSERVANCY_ZONE)

    old = await view.run(session, range=window, now=NOW, animal=ANIMAL)
    assert old.state.settled, "a day a year old must be sealed at 36 hours of lateness"

    # `now` inside the day itself: nothing in it can be settled yet.
    fresh = await view.run(
        session, range=window, now=window.start + datetime.timedelta(hours=6), animal=ANIMAL
    )
    assert not fresh.state.settled


@skip_without_database
async def test_a_late_position_corrects_a_sealed_day_without_rewriting_it(
    world,
) -> None:
    """**The whole of stage five, and the assertion that carries it.**

    A day is settled. A collar's buffer then delivers a position that belongs to
    it. Three things must be true afterwards and the third is the one that
    matters:

    1. The read reflects the new data -- the correction is folded in.
    2. The envelope *names* the day as carrying a correction, so late data is
       distinguishable from a number that changed on its own.
    3. The settled row in `wreath.series_buckets` is **byte-for-byte what it
       was**. If a settled number can change under you, it was never settled,
       and the weekly report that quoted it can no longer be reconciled against
       the system that produced it.
    """
    from _tracking import clear_settled

    database, session = world
    view = daily_distance(CONSERVANCY_ZONE)
    window = local_day(DAY)
    await clear_settled(database, view, {"animal": ANIMAL}, CONSERVANCY_ZONE)

    first = await view.run(session, range=window, now=NOW, animal=ANIMAL)
    measures = {item.measure: item.values[0] for item in first.series}
    # Reading does not store, so the settling job is an explicit step. It has
    # to happen *before* the buffer arrives, which is the situation the whole
    # test is about: a day that was settled, and then moved.
    await view.settle(session, range=window, now=NOW, animal=ANIMAL)
    settled_before = await stored_bucket(database, view, {"animal": ANIMAL}, window.start)
    assert settled_before is not None, "the day must have settled or this proves nothing"
    assert not first.state.corrections

    # The collar's buffer arrives, carrying a position ten seconds after one
    # that is already there -- so it lands *inside* the sealed day, which is the
    # case that has no honest answer other than a correction.
    late = window.start + datetime.timedelta(hours=9, seconds=10)
    await _relay(session, late)

    corrected = await view.reconcile(session, range=window, now=NOW, animal=ANIMAL)
    assert corrected, "reconcile did not notice the buffered position"

    after = await view.run(session, range=window, now=NOW, animal=ANIMAL)
    folded = {item.measure: item.values[0] for item in after.series}
    assert folded["fixes"] == measures["fixes"] + 1, "the correction did not fold in"
    assert after.state.corrections == (window.start,), (
        "the envelope must name the day that carries a correction, or late data "
        "is indistinguishable from a number that changed on its own"
    )

    settled_after = await stored_bucket(database, view, {"animal": ANIMAL}, window.start)
    assert settled_after == settled_before, (
        "the settled value was rewritten; a value that can change was never settled"
    )


@skip_without_database
async def test_the_distance_a_correction_carries_is_the_distance_that_changed(
    world,
) -> None:
    """A correction is a delta, and the delta has to be the right number.

    Adding a position inside a day does not merely add a leg: it *splits* one,
    so the day's total moves by the difference between two legs and the one they
    replaced. `repair_legs` is what makes that arithmetic come out, and this is
    where its answer meets the chart.
    """
    from _tracking import clear_settled

    database, session = world
    view = daily_distance(CONSERVANCY_ZONE)
    window = local_day(DAY)
    await clear_settled(database, view, {"animal": ANIMAL}, CONSERVANCY_ZONE)

    before = await view.run(session, range=window, now=NOW, animal=ANIMAL)
    was = {item.measure: item.values[0] for item in before.series}["distance_m"]
    # Settle before the buffer arrives, or the reconcile below has nothing to
    # compare against and stores the corrected number as if it were the first.
    await view.settle(session, range=window, now=NOW, animal=ANIMAL)

    late = window.start + datetime.timedelta(hours=9, seconds=10)
    await _relay(session, late)
    await view.reconcile(session, range=window, now=NOW, animal=ANIMAL)

    after = await view.run(session, range=window, now=NOW, animal=ANIMAL)
    now_is = {item.measure: item.values[0] for item in after.series}["distance_m"]

    # The detour is real: a position a few hundred metres off the animal's line
    # lengthens the day. What is asserted is that it lengthened by an amount the
    # raw fixes agree with, computed independently from the source rows.
    total = await session.raw(
        f'SELECT sum(leg_m) FROM "{SCHEMA}"."fixes" '
        "WHERE animal_id = $1 AND recorded_at >= $2 AND recorded_at < $3",
        ANIMAL,
        window.start,
        window.end,
    ).fetch()
    assert now_is == pytest.approx(float(total[0][0]), abs=0.5)
    assert now_is != pytest.approx(was, abs=0.5)


@skip_without_database
async def test_a_day_with_no_fixes_is_present_with_zero_rather_than_missing(
    world,
) -> None:
    """Every bucket in the range exists, which is why this is a `Series`.

    A hand-written `GROUP BY` returns no row for a silent day, and then every
    caller reinvents the same interpolation slightly differently. Here a day the
    collar said nothing is `fixes = 0`, which is a fact.
    """
    database, session = world
    view = daily_distance(CONSERVANCY_ZONE)
    # A window entirely after the seed ends.
    empty = local_day(200)
    result = await view.run(session, range=empty, now=NOW, animal=ANIMAL)
    assert len(result.buckets) == 1
    assert {item.measure: item.values[0] for item in result.series}["fixes"] == 0


def test_the_lateness_allowance_is_a_field_claim_and_not_a_default() -> None:
    """36 hours, declared here, because it is a statement about these collars.

    A programme on open grassland with hourly uplinks would seal in two hours
    and see corrections almost never. Pinning the constant stops it from drifting
    into a number nobody can justify, and pinning `on_late` stops somebody
    reaching for `reopen` -- which overwrites the settled value and clears the
    correction that would have shown anything was wrong.
    """
    assert BUFFER_LATENESS == "36h"
    view = daily_distance(CONSERVANCY_ZONE)
    assert view._seal.on_late == "correct"


async def _relay(session, moment: datetime.datetime) -> None:
    """One buffered position, through the real ingest path.

    Through `accept` rather than an `INSERT`, so this exercises the leg repair
    that a late arrival needs -- which is the half of the story a hand-written
    insert would skip while still producing a correction.
    """
    from tracking.ingest import accept

    from wreath.protobuf import decode

    row = await session.raw(
        f'SELECT latitude, longitude FROM "{SCHEMA}"."fixes" '
        "WHERE animal_id = $1 AND recorded_at < $2 ORDER BY recorded_at DESC LIMIT 1",
        ANIMAL,
        moment,
    ).fetch()
    assert row, "there must be a preceding fix for the detour to be measured from"
    latitude, longitude = row[0]

    batch = decode(
        PositionBatch,
        encode(
            PositionBatch(
                relay="relay-kimana",
                positions=[
                    Position(
                        collar_id=COLLAR,
                        recorded_at_ms=milliseconds(moment),
                        # A few hundred metres off the line the animal was on,
                        # which is what a buffered position between two fixes
                        # actually looks like.
                        lat=latitude + 0.0031,
                        lon=longitude + 0.0024,
                        accuracy_m=22.0,
                        battery_pct=58,
                        satellites=4,
                    )
                ],
            )
        ),
    )
    receipt = await accept(session, batch, now=NOW)
    assert receipt.accepted == 1, "the buffered position did not land"
