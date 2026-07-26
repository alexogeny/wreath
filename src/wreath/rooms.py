"""WebSocket rooms: broadcast to a named group of sockets, across workers.

A room is a name and the sockets currently in it. Joining and leaving are
local bookkeeping; broadcasting reaches every worker through the PostgreSQL
message bus, so a four-worker deployment behaves like a one-worker one with no
Redis and no extra process::

    rooms = RoomRegistry(app.message_bus("main"))

    @app.websocket("/chat/{room}")
    async def chat(ws):
        room = ws.path_params["room"]
        await ws.accept()
        await rooms.join(room, ws)
        try:
            async for text in ws:
                await rooms.broadcast(room, text)
        finally:
            await rooms.leave(room, ws)

Without a bus it still works, single-process: broadcasts go to local members
only. That is the right default for tests and single-worker deployments.

**One channel, not one per room.** The bus registers its subscriptions at
startup and validates channel names as SQL identifiers, so a room cannot own a
channel. Every worker instead subscribes once and filters locally by room name.
The trade is that a worker receives traffic for rooms it holds no members of and
drops it — fine at chat scale, and it keeps `LISTEN` count at one regardless of
how many rooms exist. A deployment with very high cross-room traffic and many
workers should shard by running separate registries on separate channels.

**Delivery is at-most-once and unordered across workers**, because the transport
is ephemeral `NOTIFY`. A room is for live fan-out, not for durable messaging --
if a message must survive a disconnect, persist it and use `wreath.messaging`
durable delivery.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import monotonic_ns as _monotonic_ns
from typing import Any

from ._flight_markers import COV_PYTHON as _COV_PYTHON
from ._flight_markers import PH_WS_FANOUT as _PH_WS_FANOUT
from ._flight_markers import phase_marker as _phase_marker

__all__ = ["RoomRegistry"]

#: Default bus channel carrying every room's traffic. A valid SQL identifier,
#: because `wreath.messaging` validates channel names as one.
DEFAULT_CHANNEL = "wreath_rooms"

Sender = Callable[[Any], Awaitable[None]]


class RoomRegistry:
    """Named groups of WebSockets, with cross-worker broadcast over the bus.

    One registry per application. Pass the message bus to reach other workers;
    omit it for single-process fan-out.
    """

    __slots__ = ("_bus", "_channel", "_origin", "_rooms")

    def __init__(self, bus: Any = None, *, channel: str = DEFAULT_CHANNEL) -> None:
        self._bus = bus
        self._channel = channel
        self._rooms: dict[str, set[Any]] = {}
        # Identifies broadcasts this registry published, so the copy that comes
        # back over the bus is not delivered to local members twice. `id(self)`
        # is unique per process and never crosses a process boundary as an
        # identity claim -- it only ever has to differ from other workers'.
        self._origin = f"{id(self):x}"
        if bus is not None:
            # Registered at construction: the bus collects subscriptions before
            # it starts, so a registry built after startup would never listen.
            bus.subscribe(channel)(self._on_bus_message)

    # -- membership ----------------------------------------------------------

    async def join(self, room: str, websocket: Any) -> None:
        """Add ``websocket`` to ``room`` (idempotent)."""
        members = self._rooms.get(room)
        if members is None:
            members = self._rooms[room] = set()
        members.add(websocket)

    async def leave(self, room: str, websocket: Any) -> None:
        """Remove ``websocket`` from ``room``; drop the room when it empties.

        Safe to call for a socket that never joined, so it belongs in a
        ``finally`` without a guard.
        """
        members = self._rooms.get(room)
        if members is None:
            return
        members.discard(websocket)
        if not members:
            del self._rooms[room]

    async def leave_all(self, websocket: Any) -> None:
        """Remove ``websocket`` from every room it is in."""
        for room in [name for name, members in self._rooms.items() if websocket in members]:
            await self.leave(room, websocket)

    def members(self, room: str) -> int:
        """How many sockets this worker holds for ``room``."""
        return len(self._rooms.get(room, ()))

    def rooms(self) -> list[str]:
        """Room names this worker holds at least one socket for."""
        return sorted(self._rooms)

    # -- broadcast -----------------------------------------------------------

    async def broadcast(self, room: str, payload: str | bytes) -> int:
        """Send ``payload`` to every socket in ``room``, on every worker.

        Returns the number of *local* sockets it was delivered to; remote
        delivery is fire-and-forget, as ephemeral fan-out is.
        """
        delivered = await self._deliver_local(room, payload)
        if self._bus is not None:
            await self._bus.publish(
                self._channel,
                {
                    "room": room,
                    "data": payload.decode("utf-8") if isinstance(payload, bytes) else payload,
                    "binary": isinstance(payload, bytes),
                    "origin": self._origin,
                },
            )
        return delivered

    async def _deliver_local(self, room: str, payload: str | bytes) -> int:
        members = self._rooms.get(room)
        if not members:
            return 0
        # Fan-out is the one part of a room that scales with something the
        # application does not control, so it is worth its own phase: the
        # recorder shows both how long the room took and how big it was.
        marker = _phase_marker.get(None)
        started = _monotonic_ns() if marker is not None else 0
        # Snapshot: a send may close a socket, and `leave` mutates the set.
        # The payload object is built once and shared by every recipient.
        delivered = 0
        dead: list[Any] = []
        for websocket in tuple(members):
            try:
                await websocket.send(payload)
            except Exception:  # noqa: BLE001 - one dead socket is not a failure
                # A peer that vanished mid-broadcast must not abort the rest of
                # the room, and must not stay in it.
                dead.append(websocket)
            else:
                delivered += 1
        for websocket in dead:
            await self.leave(room, websocket)
        if marker is not None:
            # `dependency_id` carries the member count, so a slow broadcast can
            # be told apart from a merely large one.
            marker(
                _PH_WS_FANOUT, delivered, _COV_PYTHON, _monotonic_ns() - started
            )
        return delivered

    async def _on_bus_message(self, message: Any) -> None:
        """Fan a bus message out locally, unless this worker published it."""
        payload = message.payload
        if not isinstance(payload, dict):
            return
        if payload.get("origin") == self._origin:
            return  # already delivered locally by `broadcast`
        room = payload.get("room")
        data = payload.get("data")
        if not isinstance(room, str) or not isinstance(data, str):
            return
        await self._deliver_local(
            room, data.encode("utf-8") if payload.get("binary") else data
        )

    # -- introspection -------------------------------------------------------

    def snapshot(self) -> dict[str, int]:
        """Room name -> local member count. For health and debug endpoints."""
        return {name: len(members) for name, members in self._rooms.items()}

    def __repr__(self) -> str:
        return f"<RoomRegistry rooms={len(self._rooms)} channel={self._channel!r}>"
