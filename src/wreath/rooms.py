"""WebSocket rooms: broadcast to a named group of sockets, across workers.

A room is a name and the sockets currently in it. Joining and leaving are
local bookkeeping; broadcasting reaches every worker through the PostgreSQL
message bus, so a four-worker deployment behaves like a one-worker one with no
Redis and no extra process:

```python
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
```

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

from base64 import b64decode, b64encode

from ._native import _core

if _core is not None and hasattr(_core, "b64encode"):
    # A broadcast encodes the whole payload once per room, so this is the one
    # base64 call in the tree that meets large inputs; the native encoder is
    # about ten times `base64.b64encode` there and returns the `str` this needs
    # rather than bytes to decode.
    _b64encode_str = _core.b64encode
else:
    def _b64encode_str(payload: bytes) -> str:
        return b64encode(payload).decode("ascii")
from collections.abc import Awaitable, Callable
from time import monotonic_ns as _monotonic_ns
from typing import Any

from ._busbridge import BusBridge
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

    Membership is per worker and lives only in memory: a restart empties every
    room, and `members` and `snapshot` report this process's share
    rather than the fleet's. Nothing here is a durable channel -- see the module
    docstring for what `NOTIFY` does and does not guarantee.

    Args:
        bus: A `wreath.messaging` bus, or None for local-only broadcast.
        channel: Bus channel carrying every room; must be a valid SQL identifier.
    """

    __slots__ = ("_bridge", "_grade_errors", "_rooms")

    def __init__(self, bus: Any = None, *, channel: str = DEFAULT_CHANNEL) -> None:
        self._rooms: dict[str, set[Any]] = {}
        #: Sockets skipped because their grade callable raised. Counted rather
        #: than swallowed: a grade that always raises delivers to nobody, which
        #: is fail-closed and completely silent without this.
        self._grade_errors = 0
        # The channel, the origin tag that drops this worker's own echo, and the
        # malformed-payload rejection are all the bridge's; what stays here is
        # the room name in the payload and the local fan-out.
        self._bridge = BusBridge(bus, channel=channel, apply=self._apply)

    # -- membership ----------------------------------------------------------

    async def join(self, room: str, websocket: Any) -> None:
        """Add `websocket` to `room` (idempotent).

        The room is created on first join. Members are held in a set, so joining
        twice leaves one member and one delivery per broadcast.

        Args:
            websocket: Anything with an awaitable `send(payload)`; it need not be a `WebSocket`.
        """
        members = self._rooms.get(room)
        if members is None:
            members = self._rooms[room] = set()
        members.add(websocket)

    async def leave(self, room: str, websocket: Any) -> None:
        """Remove `websocket` from `room`; drop the room when it empties.

        Safe to call for a socket that never joined, so it belongs in a
        `finally` without a guard.
        """
        members = self._rooms.get(room)
        if members is None:
            return
        members.discard(websocket)
        if not members:
            del self._rooms[room]

    async def leave_all(self, websocket: Any) -> None:
        """Remove `websocket` from every room it is in, on this worker.

        The one call a disconnect handler needs when the socket's rooms are not
        tracked separately. Safe for a socket in no rooms.
        """
        for room in [name for name, members in self._rooms.items() if websocket in members]:
            await self.leave(room, websocket)

    def members(self, room: str) -> int:
        """How many sockets this worker holds for `room`. 0 for an unknown room.

        A local count, not a fleet-wide one: other workers hold their own
        members and this registry never asks them.
        """
        return len(self._rooms.get(room, ()))

    def rooms(self) -> list[str]:
        """Room names this worker holds at least one socket for, sorted.

        A room disappears from this list as soon as its last local member
        leaves, so it tracks local membership rather than rooms in existence.
        """
        return sorted(self._rooms)

    # -- broadcast -----------------------------------------------------------

    @property
    def grade_errors(self) -> int:
        """Sockets skipped because their `grade` callable raised.

        A grade that raises for every socket delivers to nobody. That is the
        fail-closed direction and the right one, but it is indistinguishable
        from an empty room without a number to read.
        """
        return self._grade_errors

    async def broadcast(
        self,
        room: str,
        payload: str | bytes,
        *,
        grade: Callable[[Any], Any] | None = None,
        render: Callable[[Any, Any], Any] | None = None,
    ) -> int:
        """Send `payload` to every socket in `room`, on every worker.

        Pass `grade` and `render` together to deliver **one event at each
        subscriber's own authorization outcome** -- two watchers of one incident
        seeing the same position at different precisions, because the authorizer
        said so. `grade(websocket)` runs per socket and answers what that
        connection may see; `render(grade, payload)` runs once per distinct
        grade and shapes the payload for it, returning `None` to send that grade
        nothing. Passing only one of the two is refused, since a grade nothing
        renders would silently deliver the ungraded payload to everyone.

        Grading is **local**. A graded broadcast is not published to other
        workers, because the grade of a socket on another worker cannot be
        computed here and shipping the ungraded payload for the far side to
        shape would put the authorization decision on the wire. Fan a graded
        event out from each worker, or grade after receipt.

        Local delivery happens first and is complete before this returns. A
        socket whose `send` raises is dropped from the room and not counted;
        one dead peer never aborts the rest of a broadcast.

        The cross-worker publish is awaited, unlike Wreath's other bus bridges:
        the caller asked for this fan-out and is waiting on its result, so a bus
        failure is theirs to see and propagates from here. What is *not*
        guaranteed is that other workers receive it -- the transport is `NOTIFY`,
        so remote delivery is at-most-once and unordered.

        **Arbitrary `bytes` cross the bus intact.** The cross-worker payload is
        JSON, which carries no binary type, so a `bytes` payload travels as a
        string: decoded UTF-8 when it is valid UTF-8, and base64 when it is not.
        Either way the far side reconstructs the original bytes, so nothing here
        refuses a payload for its contents and no encoding failure can leave a
        broadcast half-delivered. The bus form is built *before* the first local
        send, so if that ever does become able to fail it fails with nothing
        delivered.

        Args:
            payload: `str` or `bytes`; `bytes` is delivered to sockets as a binary frame.
            grade: `(websocket) -> hashable`, this connection's authorization outcome.
            render: `(grade, payload) -> payload | None`, once per distinct grade.

        Returns:
            The number of sockets on *this* worker the payload was delivered to.

        Raises:
            ValueError: exactly one of `grade` and `render` was given.
        """
        if (grade is None) != (render is None):
            raise ValueError(
                "broadcast(grade=..., render=...) takes both or neither: a grade "
                "with nothing to render it would deliver the ungraded payload to "
                "every subscriber, which is the failure grading exists to prevent"
            )
        # Built first, and deliberately: a payload transform that raised after
        # local delivery would report failure for a broadcast that had already
        # partly happened. Nothing in `_bus_payload` raises today; the ordering
        # is what keeps that true for whoever edits it next.
        remote = (
            self._bus_payload(room, payload)
            if self._bridge.attached and grade is None
            else None
        )
        delivered = await self._deliver_local(room, payload, grade, render)
        if remote is not None:
            # Awaited, not deferred: the caller asked for this fan-out and is
            # waiting on its result, so a bus failure is theirs to see. That is
            # the opposite of the write and progress bridges, whose callers have
            # already finished the work being announced.
            await self._bridge.publish(remote)
        return delivered

    @staticmethod
    def _bus_payload(room: str, payload: str | bytes) -> dict[str, Any]:
        """The JSON-safe cross-worker form of one broadcast.

        `encoding` is absent for text and for UTF-8 bytes, which is the wire
        format rooms already published; only the case that used to raise adds a
        field. A worker still running the older code therefore keeps receiving
        every payload it used to receive, unchanged.
        """
        if not isinstance(payload, bytes):
            return {"room": room, "data": payload, "binary": False}
        try:
            return {"room": room, "data": payload.decode("utf-8"), "binary": True}
        except UnicodeDecodeError:
            # Not a text frame that happens to be bytes: real binary. Base64
            # costs a third more bytes on the wire and is only paid by payloads
            # that could not travel at all before.
            return {
                "room": room,
                "data": _b64encode_str(payload),
                "binary": True,
                "encoding": "base64",
            }

    async def _deliver_local(
        self,
        room: str,
        payload: str | bytes,
        grade: Callable[[Any], Any] | None = None,
        render: Callable[[Any, Any], Any] | None = None,
    ) -> int:
        members = self._rooms.get(room)
        if not members:
            return 0
        if grade is not None and render is not None:
            return await self._deliver_graded(room, payload, grade, render)
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

    async def _deliver_graded(
        self,
        room: str,
        payload: str | bytes,
        grade: Callable[[Any], Any],
        render: Callable[[Any, Any], Any],
    ) -> int:
        """Deliver one event at each subscriber's own authorization outcome.

        Two callables with deliberately different costs, because the two halves
        of this problem scale differently:

        * `grade(websocket)` runs **once per socket per broadcast**. It answers
          "what may this connection see?" and is expected to be a read of state
          the connection already holds.
        * `render(grade, payload)` runs **once per distinct grade**. It is the
          expensive half -- coarsening a position, dropping a field -- and a
          room of two hundred watchers at three grades pays for three.

        **Why `grade` is not cached across broadcasts.** Grouping subscribers by
        authorization outcome is only sound while the outcome holds, and a
        policy or a flag can change mid-stream. Calling `grade` per broadcast
        makes a grade backed by live state fresh by construction, so the
        framework never serves an event under a grant that has been revoked.
        An application that chooses to cache its own grade has made that
        trade-off explicitly, and `regrade` is how it takes it back. Silently
        reusing a stale grant is the one option not on offer.

        A `grade` that raises drops that socket from the broadcast rather than
        aborting it, on the same argument as a `send` that raises: one bad peer
        does not end a fan-out. The socket is *not* removed from the room,
        because failing to answer "what may you see" is not evidence the
        connection is dead.
        """
        members = self._rooms.get(room)
        if not members:
            return 0
        marker = _phase_marker.get(None)
        started = _monotonic_ns() if marker is not None else 0
        graded: dict[Any, list[Any]] = {}
        for websocket in tuple(members):
            try:
                key = grade(websocket)
            except Exception:  # noqa: BLE001 - counted below, and per-socket
                self._grade_errors += 1
                continue
            graded.setdefault(key, []).append(websocket)
        delivered = 0
        dead: list[Any] = []
        for key, sockets in graded.items():
            shaped = render(key, payload)
            if shaped is None:
                # This grade sees nothing at all -- the fan-out equivalent of a
                # withheld field. Not an error, and not an empty frame either:
                # sending "nothing" would still announce that an event happened.
                continue
            for websocket in sockets:
                try:
                    await websocket.send(shaped)
                except Exception:  # noqa: BLE001 - one dead socket is not a failure
                    dead.append(websocket)
                else:
                    delivered += 1
        for websocket in dead:
            await self.leave(room, websocket)
        if marker is not None:
            marker(_PH_WS_FANOUT, delivered, _COV_PYTHON, _monotonic_ns() - started)
        return delivered

    async def _apply(self, payload: dict[str, Any]) -> None:
        """Fan another worker's broadcast out to local members.

        Never republished: a room that relayed what it received would multiply
        one message by the worker count, every hop.

        A payload this worker cannot reconstruct -- an encoding it does not know,
        or base64 that does not decode -- is dropped rather than delivered as
        whatever the bytes happened to spell, for the same reason the bridge
        drops a non-mapping.
        """
        room = payload.get("room")
        data = payload.get("data")
        if not isinstance(room, str) or not isinstance(data, str):
            return
        if not payload.get("binary"):
            await self._deliver_local(room, data)
            return
        encoding = payload.get("encoding", "utf-8")
        if encoding == "base64":
            try:
                body = b64decode(data, validate=True)
            except ValueError:
                # `binascii.Error` is a ValueError; so is the padding failure.
                return
        elif encoding == "utf-8":
            try:
                body = data.encode("utf-8")
            except UnicodeEncodeError:
                # A lone surrogate survives `loads` and cannot be re-encoded.
                return
        else:
            return
        await self._deliver_local(room, body)

    # -- introspection -------------------------------------------------------

    def snapshot(self) -> dict[str, int]:
        """Room name -> local member count. For health and debug endpoints.

        A point-in-time copy, safe to serialize. Counts are this worker's; a
        four-worker deployment needs four of these to see the whole fleet.
        """
        return {name: len(members) for name, members in self._rooms.items()}

    def __repr__(self) -> str:
        return f"<RoomRegistry rooms={len(self._rooms)} channel={self._bridge.channel!r}>"
