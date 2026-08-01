"""The live map, and the one test the whole plan turns on.

`test_two_readers_on_one_room_are_shown_different_maps` is that test. Everything
else in this example composes two things that were designed together; this
composes the realtime fan-out with the authorization ladder, and nothing but an
integration test can show that they meet. Unit tests for `RoomRegistry` prove a
payload reaches a member, and unit tests for `degrade` prove a coordinate
coarsens. Neither can prove that *one* broadcast produces *two* different maps,
which is the claim the example makes on its front page.

Most of this file needs no database and no HTTP: a `RoomRegistry` with no bus is
the single-process fan-out wreath documents as the right default for one worker,
and it is the whole mechanism. The one test that does need a database is the one
asserting the SSE endpoint frames what the room delivered, because that is the
part a fake would let drift.
"""

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


# -- the composition, with no database and no HTTP ----------------------------


async def test_two_readers_on_one_room_are_shown_different_maps() -> None:
    """**The single most valuable test here.**

    One `RoomRegistry`, one room, one broadcast, two subscribers -- and the
    ranger is given the collar's own coordinate while the volunteer beside them
    is given a 10 km cell centre. Neither has a different subscription or a
    different feed.

    The three assertions are separate on purpose. That the values *differ* would
    pass if the volunteer were shown noise; that the volunteer's answer carries
    `precision_m` is what makes it honest on the wire; and that the ranger's is
    the exact seeded number is what stops a bug that coarsened *both* from
    looking like success.
    """
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
    """Four readers, one broadcast, four resolutions -- the whole ladder at once.

    The REST tests assert this grid one principal at a time. Asserting it again
    here is not duplication: it is the difference between "the policy says so"
    and "the stream does so", and those have been the same thing only since the
    subscriber started reading the grid instead of the role.
    """
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
    """A reader with no grade is still told the animal was heard from.

    `"position": null` would say the collar failed, which is false and which a
    welfare dashboard would draw as an alarm. The absent key says "not for you",
    and the battery reading beside it is the half of the event that reader is
    entitled to -- so withholding the location must not withhold the event.
    """
    live = LiveMap(RoomRegistry())
    volunteer = await live.subscribe(precision_grid(who("volunteer")))
    await live.publish([NASERIAN])
    (frame,) = await drain(volunteer)
    assert frame["animal"] == "Naserian"
    assert frame["battery_pct"] == 74
    assert "position" not in frame
    assert "precision_m" not in frame


async def test_an_open_animal_is_exact_for_everyone_on_the_stream() -> None:
    """The stream reads the same grid the REST routes do, tier by tier.

    Without this a `Subscriber` that coarsened everything for a volunteer would
    pass the headline test above and make the public map -- the reason the
    collars are funded -- useless.
    """
    live = LiveMap(RoomRegistry())
    public = await live.subscribe(precision_grid(None))
    await live.publish([SARARA])
    (frame,) = await drain(public)
    assert frame["position"] == {"lat": SARARA["lat"], "lon": SARARA["lon"]}
    assert frame["precision_m"] == 0.0


async def test_one_broadcast_carries_a_whole_batch() -> None:
    """A station draining a spool costs the bus one message, not two hundred.

    The split matters: the fan-out is proportional to *batches*, and the frames
    a browser receives are proportional to positions. Making the broadcast
    per-position would multiply `NOTIFY` traffic by batch size for no gain.
    """
    live = LiveMap(RoomRegistry())
    reader = await live.subscribe(precision_grid(who("ranger")))
    assert await live.publish([NASHIPAE, SARARA, NASERIAN]) == 1
    assert len(await drain(reader)) == 3


async def test_an_empty_batch_is_not_broadcast_at_all() -> None:
    """A batch whose every position was rejected must not wake every worker."""
    live = LiveMap(RoomRegistry())
    reader = await live.subscribe(precision_grid(who("ranger")))
    assert await live.publish([]) == 0
    assert await drain(reader) == []


async def test_a_reader_who_falls_behind_is_counted_rather_than_dropped() -> None:
    """Overflow is a counter, not an exception, and that is load-bearing.

    `RoomRegistry` treats an exception from `send` as a dead peer and removes
    the member from the room. So a `QueueFull` escaping `Subscriber.send` would
    silently unsubscribe every reader who fell one frame behind, and their
    stream would stay open and empty forever -- a live map that looks like a
    quiet afternoon. Counting instead keeps the degradation visible, which is
    what `wreath.messaging` does with its own dropped work.
    """
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
    """The handler's `finally` runs on disconnect, on shutdown, and on discard."""
    live = LiveMap(RoomRegistry())
    reader = await live.subscribe(precision_grid(who("ranger")))
    await live.unsubscribe(reader)
    await live.unsubscribe(reader)
    assert live.readers() == 0
    assert await live.publish([NASHIPAE]) == 0


async def test_a_shutdown_ends_every_open_stream() -> None:
    """An SSE response finishes when its generator does, and not before.

    A generator parked on a queue nobody will fill again is a connection that
    outlives the process trying to stop, which is why `build` registers
    `close_all` on shutdown rather than leaving it to a signal handler.
    """
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
    """Nothing in `SSEResponse` is on a timer, so the application yields one.

    An intermediary drops a connection it has seen nothing on, and the first
    symptom is a map that stops updating for one user behind one proxy — which
    is the hardest kind of bug to be told about. The comment is what stops it.

    The interval is monkeypatched to make the test fast; the *value* is asserted
    separately below, because a patched constant proves the mechanism and says
    nothing about the number that ships.
    """
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
    """Twenty seconds, three of them, and sixty-four frames of slack.

    Asserted as values because each is a claim about somebody else's software:
    20 s is under the 30 s idle timeout the common proxies default to, 3 s is
    how eagerly a browser is told to come back, and 64 is how far behind a
    reader may fall before a live map starts preferring the newest position to a
    complete history. A constant that drifted would leave every behavioural test
    in this file green.
    """
    from tracking.live import BUFFER, KEEPALIVE_SECONDS

    assert KEEPALIVE_SECONDS == 20.0
    assert RETRY_MS == 3_000
    assert BUFFER == 64


async def test_a_stream_opens_with_a_reconnection_hint() -> None:
    """`EventSource` reconnects on its own; the first frame says how eagerly.

    It is also a comment rather than data, so a client that dispatches on
    `event` type never sees it as a position.
    """
    reader = Subscriber(precision_grid(who("ranger")))
    reader.close()
    first = await anext(reader.events())
    assert first.retry == RETRY_MS
    assert first.data is None


async def test_the_room_carries_the_exact_coordinate() -> None:
    """Degradation happens at the reader's edge, and this pins where.

    Coarsening before the broadcast would need one room per grade, would put
    the policy decision on the writer's side where nobody knows who is reading,
    and would make the fan-out cost proportional to the number of grades. The
    bus is inside the trust boundary; the response is the boundary.
    """
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


# -- and the same thing over HTTP ---------------------------------------------


@skip_without_database
async def test_the_sse_endpoint_frames_what_the_room_delivered() -> None:
    """The transport half: `text/event-stream`, and a `position` event in it.

    Driven through the real application rather than a `Subscriber` by hand, so
    this covers the handler's own work -- computing the grid from
    `request.identity`, joining the room, and leaving it in a `finally`.

    The request is launched as a task and the streams are ended underneath it,
    because `TestClient` buffers a whole response and an endless one never
    arrives. That is the pattern `tests/test_mcp_notifications.py` uses for the
    same reason.
    """
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
