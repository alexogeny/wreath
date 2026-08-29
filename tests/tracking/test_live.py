from __future__ import annotations

import asyncio
import json
import os

import pytest
from tracking.live import RETRY_MS, ROOM, LiveMap, Subscriber
from tracking.policies import precision_grid

from wreath.auth import Identity
from wreath.rooms import RoomRegistry

_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")

skip_without_database = pytest.mark.skipif(
    _DSN is None,
    reason="set WREATH_TEST_POSTGRES_DSN for the tracking live-map HTTP test",
)

#: One sensitive animal's position, as the ingest path publishes it: exact,
#: because degradation happens at each reader's edge and not before.
NASHIPAE = {
    "animal_id": 3,
    "animal": "Nashipae",
    "protection": "sensitive",
    "collar_id": 3,
    "recorded_at": "2026-03-14T09:20:00+00:00",
    "battery_pct": 74,
    "lat": -1.9293,
    "lon": 36.0712,
}

#: An open animal, for the row that proves the rule is about the animal.
SARARA = {**NASHIPAE, "animal_id": 8, "animal": "Sarara", "protection": "open", "collar_id": 8}

#: A restricted one.
NASERIAN = {
    **NASHIPAE,
    "animal_id": 1,
    "animal": "Naserian",
    "protection": "restricted",
    "collar_id": 1,
}


def who(role: str | None) -> Identity | None:
    if role is None:
        return None
    return Identity(
        id=f"{role}-1",
        type="Observer",
        roles=frozenset({role}),
        permissions=frozenset(),
        claims={},
    )


async def drain(subscriber: Subscriber) -> list[dict]:
    """Every position frame this subscriber holds, after its stream is ended.

    `close()` first, so the generator terminates rather than parking on the
    keep-alive timeout: an SSE stream is deliberately endless, and a test that
    waited for it to finish on its own would wait forever.
    """
    subscriber.close()
    frames = []
    async for event in subscriber.events():
        if event.event == "position":
            frames.append(json.loads(event.data))
    return frames


async def test_two_readers_on_one_room_are_shown_different_maps() -> None:
    live = LiveMap(RoomRegistry())
    ranger = await live.subscribe(precision_grid(who("ranger")))
    volunteer = await live.subscribe(precision_grid(who("volunteer")))

    delivered = await live.publish([NASHIPAE])
    assert delivered == 2, "one broadcast, two members of one room"

    (theirs,) = await drain(ranger)
    (ours,) = await drain(volunteer)

    assert theirs["position"] == {"lat": NASHIPAE["lat"], "lon": NASHIPAE["lon"]}
    assert theirs["precision_m"] == 0.0

    assert ours["precision_m"] == 10_000.0
    assert ours["position"] != theirs["position"]

    # And the two frames are otherwise the same event: same animal, same
    # instant, same battery. A live map that told one reader about a different
    # animal would be a different bug wearing this one's clothes.
    assert theirs["animal"] == ours["animal"] == "Nashipae"
    assert theirs["recorded_at"] == ours["recorded_at"]
    assert theirs["battery_pct"] == ours["battery_pct"] == 74


async def test_a_partner_sits_between_them_on_the_same_broadcast() -> None:
    live = LiveMap(RoomRegistry())
    readers = {
        principal: await live.subscribe(precision_grid(who(principal)))
        for principal in ("ranger", "partner", "volunteer", None)
    }
    await live.publish([NASHIPAE])

    seen = {principal: (await drain(reader))[0] for principal, reader in readers.items()}
    assert seen["ranger"]["precision_m"] == 0.0
    assert seen["partner"]["precision_m"] == 1_000.0
    assert seen["volunteer"]["precision_m"] == 10_000.0
    assert "position" not in seen[None], "the public is told nothing, not told null"
    assert "precision_m" not in seen[None]

    # Four different answers, not three and a duplicate.
    plotted = {
        json.dumps(frame["position"], sort_keys=True)
        for frame in seen.values()
        if "position" in frame
    }
    assert len(plotted) == 3


async def test_an_absent_position_is_a_missing_key_and_the_event_still_arrives() -> None:
    live = LiveMap(RoomRegistry())
    volunteer = await live.subscribe(precision_grid(who("volunteer")))
    await live.publish([NASERIAN])
    (frame,) = await drain(volunteer)
    assert frame["animal"] == "Naserian"
    assert frame["battery_pct"] == 74
    assert "position" not in frame
    assert "precision_m" not in frame


async def test_an_open_animal_is_exact_for_everyone_on_the_stream() -> None:
    live = LiveMap(RoomRegistry())
    public = await live.subscribe(precision_grid(None))
    await live.publish([SARARA])
    (frame,) = await drain(public)
    assert frame["position"] == {"lat": SARARA["lat"], "lon": SARARA["lon"]}
    assert frame["precision_m"] == 0.0


async def test_one_broadcast_carries_a_whole_batch() -> None:
    live = LiveMap(RoomRegistry())
    reader = await live.subscribe(precision_grid(who("ranger")))
    assert await live.publish([NASHIPAE, SARARA, NASERIAN]) == 1
    assert len(await drain(reader)) == 3


async def test_an_empty_batch_is_not_broadcast_at_all() -> None:
    live = LiveMap(RoomRegistry())
    reader = await live.subscribe(precision_grid(who("ranger")))
    assert await live.publish([]) == 0
    assert await drain(reader) == []


async def test_a_reader_who_falls_behind_is_counted_rather_than_dropped() -> None:
    live = LiveMap(RoomRegistry())
    reader = Subscriber(precision_grid(who("ranger")), buffer=2)
    await live._rooms.join(ROOM, reader)

    for _ in range(6):
        await live.publish([NASHIPAE])

    assert live._rooms.members(ROOM) == 1, "a slow reader must stay in the room"
    assert reader.dropped == 4, "six broadcasts into a buffer of two"

    kept = await drain(reader)
    # `close` evicts one more to fit its sentinel into a full queue. A shutdown
    # that could be blocked by a full queue is one that hangs on exactly the
    # reader who was already in trouble, so the eviction is the design and this
    # is the count that proves it happened.
    assert reader.dropped == 5
    assert len(kept) == 1


async def test_unsubscribing_is_safe_twice_so_it_belongs_in_a_finally() -> None:
    live = LiveMap(RoomRegistry())
    reader = await live.subscribe(precision_grid(who("ranger")))
    await live.unsubscribe(reader)
    await live.unsubscribe(reader)
    assert live.readers() == 0
    assert await live.publish([NASHIPAE]) == 0


async def test_a_shutdown_ends_every_open_stream() -> None:
    live = LiveMap(RoomRegistry())
    readers = [await live.subscribe(precision_grid(who("ranger"))) for _ in range(3)]
    live.close_all()
    for reader in readers:
        # **The loop finishing is the assertion.** An unterminated stream parks
        # on the keep-alive timeout and this comprehension never returns, so a
        # regression here is a hung test rather than a failing one -- which is
        # worth naming, because a hang reads as flakiness.
        frames = [event async for event in reader.events()]
        assert len(frames) == 1, "only the opening hint, then the stream ended"
        assert frames[0].retry == RETRY_MS


async def test_an_idle_stream_sends_its_own_keep_alive(monkeypatch) -> None:
    import tracking.live as live_module

    monkeypatch.setattr(live_module, "KEEPALIVE_SECONDS", 0.01)
    reader = Subscriber(precision_grid(who("ranger")))
    stream = reader.events()
    assert (await anext(stream)).retry == RETRY_MS
    keep_alive = await anext(stream)
    assert keep_alive.comment == "keep-alive"
    assert keep_alive.data is None, "a keep-alive must not dispatch as an event"
    await stream.aclose()


def test_the_stream_timings_are_the_ones_the_documentation_quotes() -> None:
    from tracking.live import BUFFER, KEEPALIVE_SECONDS

    assert KEEPALIVE_SECONDS == 20.0
    assert RETRY_MS == 3_000
    assert BUFFER == 64


async def test_a_stream_opens_with_a_reconnection_hint() -> None:
    reader = Subscriber(precision_grid(who("ranger")))
    reader.close()
    first = await anext(reader.events())
    assert first.retry == RETRY_MS
    assert first.data is None


async def test_the_room_carries_the_exact_coordinate() -> None:
    seen: list[str] = []

    class Recorder:
        async def send(self, payload):
            seen.append(payload)

    rooms = RoomRegistry()
    live = LiveMap(rooms)
    await rooms.join(ROOM, Recorder())
    await live.publish([NASHIPAE])

    (payload,) = seen
    (carried,) = json.loads(payload)["positions"]
    assert carried["lat"] == NASHIPAE["lat"]
    assert carried["lon"] == NASHIPAE["lon"]


@skip_without_database
async def test_the_sse_endpoint_frames_what_the_room_delivered() -> None:
    from _tracking import build_schema, drop_schema
    from tracking.app import build

    from wreath.postgres import connect
    from wreath.testing import TestClient

    connection = await connect(_DSN)
    try:
        await build_schema(connection, seed_rows=False)
    finally:
        await connection.close()

    application = build(cross_worker=False)
    try:
        async with TestClient(application) as client:
            ranger = client.acting_as("ranger-1", roles=["ranger"], type="Observer")
            stream = asyncio.ensure_future(ranger.get("/live/positions"))
            # Let the handler run far enough to join the room. One turn is not
            # enough: the response has to be started before the generator is
            # first pulled.
            for _ in range(20):
                await asyncio.sleep(0)
                if application.state.live.readers():
                    break
            assert application.state.live.readers() == 1, "the handler never joined"

            await application.state.live.publish([NASHIPAE])
            application.state.live.close_all()
            response = await asyncio.wait_for(stream, timeout=5)

        assert response.status == 200
        assert response.header("content-type") == "text/event-stream"
        body = response.text
        assert "event: position" in body
        assert '"animal":"Nashipae"' in body
        assert f'"lat":{NASHIPAE["lat"]}' in body
        # And the handler's `finally` ran: the room is empty again.
        assert application.state.live.readers() == 0
    finally:
        connection = await connect(_DSN)
        try:
            await drop_schema(connection)
        finally:
            await connection.close()
