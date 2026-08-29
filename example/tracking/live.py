"""The live map: one broadcast, many readers, each at their own resolution.

A researcher watching a map wants a position to appear the moment it lands, and
they are only *listening* -- no client ever sends anything up this channel. That
is Server-Sent Events, not a WebSocket, and it is why the browser half of this
is `new EventSource("/live/positions")` and nothing else.

The interesting part is not the transport. It is that **two people watching the
same map are not shown the same map**, and the difference is decided by the same
Cedar policy set that guards the REST routes. A ranger sees the collar's own
coordinate; a volunteer beside them sees a 10 km cell centre; and neither has a
different subscription, a different room or a different feed. There is one
broadcast.

## Where the degradation happens, and why it is not earlier

The payload that crosses `RoomRegistry` carries the **exact** coordinate, and
each subscriber coarsens it as it frames its own events. That is deliberate and
it is the only arrangement that works:

* Degrading before the broadcast would mean one room per grade, and then the
  fan-out cost multiplies by the number of grades rather than staying one
  `NOTIFY` per batch. It also puts the policy decision on the *writer's* side,
  where nobody knows who is reading.
* The bus is inside the trust boundary. It is a PostgreSQL `NOTIFY` on the
  application's own database, reaching the application's own workers. The
  boundary this example defends is the one between the application and its
  readers, and that boundary is the edge of the response -- which is exactly
  where `Subscriber` sits.

## What `RoomRegistry` is doing here

`RoomRegistry.join` takes "anything with an awaitable `send(payload)`; it need
not be a `WebSocket`". :class:`Subscriber` is that anything: `send` drops the
payload on a bounded queue and the SSE generator drains it. So the cross-worker
fan-out that wreath ships for chat rooms carries a live map with no adapter and
no second mechanism -- a position ingested by worker 3 reaches a browser
connected to worker 1 because the bus already does that.

Delivery is at-most-once and unordered across workers, because `NOTIFY` is
ephemeral. For a live map that is the right trade: a dropped frame is a dot that
appears at the next fix, and the authoritative history is in `fixes` where a
reader can go and ask for it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from wreath.geospatial import Coordinate
from wreath.response import ServerSentEvent

from .place import Precision, degrade

#: The one room. Rooms are for partitioning a fan-out, and this application has
#: nothing to partition: everybody watching the map wants every animal, and who
#: may see what is a *precision* question rather than a subscription one. A room
#: per animal would put the access-control decision in the join, where it would
#: have to be re-evaluated when a policy changed and nobody would remember to.
ROOM = "positions"

#: How long an idle stream waits before sending a `:`-prefixed comment. Nothing
#: in `SSEResponse` is on a timer, so a stream that must survive an intermediary
#: yields its own keep-alive; 20 seconds is under the 30-second idle timeout the
#: common proxies default to.
KEEPALIVE_SECONDS = 20.0

#: What a browser is told to wait before reconnecting. `EventSource` reconnects
#: on its own; this only sets how eagerly.
RETRY_MS = 3_000

#: Events a slow reader may fall behind by before frames are dropped. A live map
#: wants the newest position, not a complete history -- the history is in the
#: table -- so the queue is small on purpose and overflow is counted rather than
#: buffered.
BUFFER = 64

#: Put on the queue by `close`. A private object rather than `None`, so a bug
#: that enqueued a null payload could never be mistaken for a shutdown.
_CLOSED = object()


class Subscriber:
    """One live-map reader. Joins a room like a socket; frames like a stream.

    Holds the precision grid this reader is entitled to -- the answer
    `tracking.policies.precision_grid` computed once when the stream opened --
    and applies it to every event it frames.

    **The grid is fixed for the life of the stream**, which is a real and
    statable limitation rather than an oversight. A reader whose role is revoked
    keeps their resolution until their `EventSource` reconnects, which the
    `retry` hint above puts at a few seconds after any deploy or restart. A
    stream that re-evaluated the policy per frame would ask Cedar once per
    position per reader, and the honest fix for a revocation that must take
    effect *now* is to close the stream, not to re-decide inside it.

    Args:
        grid: Protection tier -> the grade this reader gets, or None for absent.
        buffer: Events to hold for a slow reader before dropping the oldest.
    """

    __slots__ = ("_grid", "_queue", "dropped")

    def __init__(self, grid: dict[str, Precision | None], *, buffer: int = BUFFER) -> None:
        self._grid = grid
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=buffer)
        #: Frames this reader never saw. Counted, not swallowed: a live map that
        #: silently skips half its positions looks like a quiet afternoon.
        self.dropped = 0

    async def send(self, payload: str | bytes) -> None:
        """Accept one broadcast. Called by `RoomRegistry`, never by a handler.

        **This must not raise.** `RoomRegistry._deliver_local` treats an
        exception from `send` as a dead peer and drops the member from the room,
        so a `QueueFull` propagating from here would silently unsubscribe every
        reader who fell one frame behind -- and their stream would stay open,
        empty, forever. The overflow is counted instead.
        """
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped += 1

    def close(self) -> None:
        """End this reader's stream at the next frame.

        Makes room for the sentinel by discarding the oldest queued payload if
        it has to: a shutdown that could be blocked by a full queue is a
        shutdown that hangs on exactly the reader who was already in trouble.
        """
        while True:
            try:
                self._queue.put_nowait(_CLOSED)
                return
            except asyncio.QueueFull:
                self._queue.get_nowait()
                self.dropped += 1

    async def events(self) -> AsyncIterator[ServerSentEvent]:
        """Frame this reader's stream, at this reader's precision.

        One SSE event per position, from one broadcast per batch. A station
        draining a week's spool therefore costs the bus one message and the
        browser two hundred `message` events, which is the split that keeps a
        live map live without making the fan-out proportional to batch size.
        """
        yield ServerSentEvent(retry=RETRY_MS, comment="tracking live map")
        while True:
            try:
                payload = await asyncio.wait_for(self._queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ServerSentEvent(comment="keep-alive")
                continue
            if payload is _CLOSED:
                return
            for event in self.frame(payload):
                yield event

    def frame(self, payload: str) -> list[ServerSentEvent]:
        """One broadcast payload as this reader's events.

        Separated from `events` so a test can hold a payload against a grid
        without an event loop, a room or a response -- and so the interesting
        half of this class is a pure function of (payload, grid).
        """
        batch = json.loads(payload)
        return [
            ServerSentEvent(
                data=json.dumps(self._position(item), separators=(",", ":")),
                event="position",
                # A collar and an instant identify a fix, and they are the
                # primary key, so `Last-Event-ID` names a real row.
                id=f"{item['collar_id']}:{item['recorded_at']}",
            )
            for item in batch["positions"]
        ]

    def _position(self, item: dict[str, Any]) -> dict[str, Any]:
        """One position, coarsened.

        Reads the same `protection` -> grade grid the REST routes read, and
        withholds by *omitting the key* rather than nulling it -- see
        `tracking.wire` for the whole argument. An event with no `position` is
        not a useless event: it says this animal was heard from, when, and how
        its collar is doing, which is what a welfare dashboard wants and is not
        what anybody hunting it does.
        """
        shown = {
            "animal_id": item["animal_id"],
            "animal": item["animal"],
            "collar_id": item["collar_id"],
            "recorded_at": item["recorded_at"],
            "battery_pct": item["battery_pct"],
        }
        precision = self._grid.get(item["protection"])
        if precision is None:
            return shown
        point = degrade(Coordinate(lat=item["lat"], lon=item["lon"]), precision)
        shown["position"] = {"lat": point.lat, "lon": point.lon}
        shown["precision_m"] = precision.metres
        return shown


class LiveMap:
    """The application's one live-map fan-out, over a `RoomRegistry`.

    Owns the subscribers so that a shutdown can end their streams: an SSE
    response only finishes when its generator does, and a generator parked on a
    queue nobody will ever fill again is a connection that outlives the process
    trying to stop.

    Args:
        rooms: A `wreath.rooms.RoomRegistry`. Give it the application's message
            bus for cross-worker fan-out; without one it is single-process,
            which is the right default for a test and for one worker.
    """

    __slots__ = ("_rooms", "_subscribers")

    def __init__(self, rooms: Any) -> None:
        self._rooms = rooms
        self._subscribers: set[Subscriber] = set()

    async def subscribe(self, grid: dict[str, Precision | None]) -> Subscriber:
        """Open a reader at `grid`'s resolutions and put it in the room."""
        subscriber = Subscriber(grid)
        self._subscribers.add(subscriber)
        await self._rooms.join(ROOM, subscriber)
        return subscriber

    async def unsubscribe(self, subscriber: Subscriber) -> None:
        """Remove a reader. Safe for one that already left, so it belongs in a
        `finally` without a guard."""
        self._subscribers.discard(subscriber)
        await self._rooms.leave(ROOM, subscriber)

    async def publish(self, positions: list[dict[str, Any]]) -> int:
        """Broadcast one ingested batch to every worker's readers.

        Returns the number of *local* subscribers reached, which is what
        `RoomRegistry.broadcast` can honestly report -- other workers hold their
        own members and this registry never asks them.

        The payload carries exact coordinates; see the module docstring for why
        that is the correct side of the boundary to coarsen on.
        """
        if not positions:
            return 0
        return await self._rooms.broadcast(
            ROOM, json.dumps({"positions": positions}, separators=(",", ":"))
        )

    def close_all(self) -> None:
        """End every open stream. Called on shutdown."""
        for subscriber in tuple(self._subscribers):
            subscriber.close()

    def readers(self) -> int:
        """Open streams on this worker."""
        return len(self._subscribers)
