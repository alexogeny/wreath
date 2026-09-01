"""One bus channel, one origin tag, and the rule that keeps fan-out bounded.

Three subsystems broadcast across workers over `wreath.messaging` --
WebSocket rooms, ORM write announcements, and task progress. Each needs the same
four things, and each of them is a place to be quietly wrong:

* **One channel, subscribed at construction.** The bus collects its
  subscriptions before it starts, so a bridge built later never listens at all.
* **An origin tag.** `NOTIFY` hands the sender its own message back, and
  applying that copy is at best wasted work and at worst a double delivery.
* **A publish that cannot hurt the caller.** No running loop, or a bus that is
  down, must not surface as a failed write or a failed broadcast.
* **Never relay what arrived.** A bridge that re-publishes an inbound message
  turns one write into a storm that grows with the worker count.

That last one is why this module exists rather than three careful copies of the
same reasoning. There is no path from `BusBridge._receive` to a publish
here, so a caller cannot create one by forgetting -- it would have to write the
relay itself.

What stays with each caller is its payload shape and what it does locally with
one: a room fans out to sockets, the write registry notifies its subscribers,
the progress registry stores a snapshot. Only the transport discipline is
shared, and the callers differ in one respect that is deliberate -- see the two
publish methods below.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

__all__ = ["BusBridge"]

#: What a bridge hands a foreign payload to. Async because two of the three
#: callers need to await their local apply (a room sends to every socket).
Apply = Callable[[dict[str, Any]], Awaitable[None]]


class BusBridge:
    """Carries one subsystem's payloads between workers on one channel.

    `bus` may be `None`, and that is a supported configuration rather than a
    degraded one: a single-worker deployment or a test wants the local half
    without a database behind it. A detached bridge subscribes to nothing and
    both publish methods are no-ops.

    `apply` receives each payload that came from *another* worker, already
    filtered for the two failures every caller shares: a payload that is not a
    mapping, and the echo of this worker's own publish. Everything past that --
    what the payload's fields mean, whether they are well formed, what to do
    with them -- belongs to the caller, because it differs every time.
    """

    __slots__ = (
        "_apply",
        "_bus",
        "_channel",
        "_inflight",
        "_max_inflight",
        "_origin",
        "dropped_publishes",
        "publish_errors",
        "untagged_applied",
    )

    def __init__(
        self,
        bus: Any = None,
        *,
        channel: str,
        apply: Apply,
        max_inflight: int = 1024,
    ) -> None:
        if max_inflight < 1:
            raise ValueError("max_inflight must be at least 1")
        self._bus = bus
        self._channel = channel
        self._apply = apply
        self._inflight: set[asyncio.Future[Any]] = set()
        self._max_inflight = max_inflight
        #: Deferred publishes dropped because too many were already in flight.
        self.dropped_publishes = 0
        #: Deferred publishes the bus refused. Climbing means fan-out is down.
        self.publish_errors = 0
        #: Inbound payloads that carried no origin tag -- i.e. published by
        #: something that is not a bridge. Delivered anyway (see `_receive`);
        #: non-zero on a healthy fleet means somebody else is writing to this
        #: channel, which is worth knowing either way.
        self.untagged_applied = 0
        # Identifies messages this worker published, so the copy NOTIFY hands
        # back to the sender is dropped. Random, not `id(self)`: the tag goes on
        # the wire to every other worker, and an address is wrong on both
        # counts. Two workers are separate processes running the same binary
        # with similar heap layouts, so they can genuinely allocate their bridge
        # at the same address -- and the worker that loses that coin flip
        # silently discards every message from its twin as its own echo, which
        # presents as invalidations that *mostly* work. It would also publish
        # this process's heap layout to anything that can read the channel.
        self._origin = secrets.token_hex(8)
        if bus is not None:
            # Registered at construction: the bus collects subscriptions before
            # it starts, so a bridge built after startup would never listen.
            bus.subscribe(channel)(self._receive)

    @property
    def channel(self) -> str:
        return self._channel

    @property
    def origin(self) -> str:
        """This worker's tag, as it appears on the wire."""
        return self._origin

    @property
    def attached(self) -> bool:
        """Whether there is a bus to reach other workers through."""
        return self._bus is not None

    @property
    def inflight(self) -> int:
        """Deferred publishes not yet finished. For tests and health output."""
        return len(self._inflight)

    async def publish(self, payload: Mapping[str, Any]) -> None:
        """Publish inline, letting a bus failure reach the caller.

        For a caller who is already awaiting the fan-out and can do something
        about it going wrong -- `wreath.rooms.RoomRegistry.broadcast`,
        whose caller asked for the broadcast and is waiting on its result.
        """
        if self._bus is None:
            return
        await self._bus.publish(self._channel, self._tagged(payload))

    def publish_soon(self, payload: Mapping[str, Any]) -> None:
        """Publish without making the caller wait, and without letting the bus
        fail their work.

        For a caller whose real work is already finished and durable: the row
        has committed, the progress percentage is commentary. A bus that is down
        costs the fleet one update; raising here would instead turn a successful
        transaction into an application error, which is strictly worse.
        """
        if self._bus is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # These callers only ever publish from async code; a synchronous
            # caller has no loop to publish on and there is nothing useful to
            # do about it here.
            return
        if len(self._inflight) >= self._max_inflight:
            # Bounded. A caller whose real work is already durable publishes
            # without waiting, so nothing here applies backpressure to it -- and
            # against a bus that is slow or wedged, the set grew with every
            # write until the process did. Dropping the newest update is the
            # cheap failure: these payloads are commentary (a progress
            # percentage, an invalidation with a TTL behind it), and the count
            # says how much was lost.
            self.dropped_publishes += 1
            return
        future = asyncio.ensure_future(self._publish_quietly(payload))
        # Held until it finishes: a task nobody references can be collected
        # mid-flight, which loses the message silently.
        self._inflight.add(future)
        future.add_done_callback(self._inflight.discard)

    async def _publish_quietly(self, payload: Mapping[str, Any]) -> None:
        try:
            await self._bus.publish(self._channel, self._tagged(payload))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the caller's work is already durable
            # Still swallowed -- raising here would turn a committed transaction
            # into an application error -- but counted, because a bus that has
            # been refusing every publish for a week looked exactly like one
            # with nothing to say.
            self.publish_errors += 1

    def _tagged(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """The payload with this worker's origin on it.

        The tag is written last so a caller's field cannot shadow it. The echo
        guard is the only thing standing between a `NOTIFY` and a double
        delivery, and it should not be defeatable by a key collision.
        """
        return {**payload, "origin": self._origin}

    async def _receive(self, message: Any) -> None:
        """Hand a foreign payload to the caller. Deliberately never publishes."""
        payload = message.payload
        if not isinstance(payload, dict):
            return
        origin = payload.get("origin")
        if origin == self._origin:
            return  # our own NOTIFY, already applied locally
        if not isinstance(origin, str) or not origin:
            # Applied, and counted. Every bridge tags what it publishes, so an
            # untagged payload came from something that is not a bridge -- which
            # is either an ops script or somebody with NOTIFY rights on this
            # database driving invalidations, room broadcasts, and progress
            # writes. Dropping it would close that seam, and would also lose a
            # real update from a publisher on an older wire format; the shipped
            # decision is to deliver (see the test named for it). What was
            # missing is that the situation was invisible, so it is now counted:
            # a non-zero value on a healthy fleet means somebody else is writing
            # to this channel.
            self.untagged_applied += 1
        await self._apply(payload)

    def __repr__(self) -> str:
        return f"<BusBridge channel={self._channel!r} origin={self._origin}>"
