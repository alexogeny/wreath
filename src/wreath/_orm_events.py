"""Which models a session wrote, published to whoever cares.

Caching is easy until invalidation. A TTL is a guess: too short and the cache
does nothing, too long and the application serves stale data. The correct
answer -- drop exactly what changed, the moment it changes -- needs something
that sees both the writes and the cached responses, and in most stacks nothing
does. Wreath owns the ORM *and* the response cache, so it can.

A session that flushes publishes the set of model names it touched. Subscribers
(the response cache today; anything else later) map that to what they hold.

Deliberately **model-grained, not row-grained**. Row-grained invalidation needs
the cache to know which rows fed which response, which means recording a read
set per request -- real bookkeeping on the hot path to save a few cache misses
on the cold one. Dropping a model's cached responses when that model is written
is one set lookup per write and no per-read cost at all.

Publication happens **after** the flush's transaction commits, never before. An
invalidation published from inside a transaction that then rolls back would
have evicted correct data for a write that never happened.

Two kinds of listener, kept apart on purpose:

* **Subscribers** react to a write -- a response cache drops its entries.
* **Bridges** carry a write somewhere else -- to the other workers, over the
  message bus. A bridge is told about local writes only. Re-broadcasting an
  announcement that arrived from another worker is how a fan-out storm starts,
  and separating the two lists makes that impossible by construction rather
  than by a flag someone has to remember to check.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

__all__ = [
    "WRITE_CHANNEL",
    "WriteBroadcast",
    "has_subscribers",
    "publish_write",
    "register_bridge",
    "subscribe_writes",
    "unregister_bridge",
    "unsubscribe_writes",
]

#: Default bus channel carrying every worker's write announcements. A valid SQL
#: identifier, because `wreath.messaging` validates channel names as one.
WRITE_CHANNEL = "wreath_writes"

#: Subscribers, in registration order. Small and process-local by design: this
#: is a same-process seam between two Wreath subsystems, not a message bus.
_subscribers: list[Callable[[frozenset[str]], None]] = []

#: Bridges to other workers. Separate from `_subscribers` so a write that
#: *arrived* from another worker is delivered without being sent back out.
_bridges: list[Callable[[frozenset[str]], None]] = []


def subscribe_writes(callback: Callable[[frozenset[str]], None]) -> None:
    """Call ``callback(model_names)`` after each committed flush that wrote."""
    if callback not in _subscribers:
        _subscribers.append(callback)


def unsubscribe_writes(callback: Callable[[frozenset[str]], None]) -> None:
    _subscribers.remove(callback) if callback in _subscribers else None


def register_bridge(callback: Callable[[frozenset[str]], None]) -> None:
    """Call ``callback(model_names)`` for **locally** originated writes only."""
    if callback not in _bridges:
        _bridges.append(callback)


def unregister_bridge(callback: Callable[[frozenset[str]], None]) -> None:
    _bridges.remove(callback) if callback in _bridges else None


def publish_write(model_names: frozenset[str], *, remote: bool = False) -> None:
    """Announce that ``model_names`` were written.

    ``remote`` marks an announcement that arrived from another worker: local
    subscribers see it exactly as they see a local write, and bridges do not,
    so it stops here instead of bouncing around the fleet.

    A subscriber that raises is not allowed to fail the write: the data is
    already committed, and a broken cache listener must not turn a successful
    transaction into an application error. The exception is swallowed here
    because there is no correct alternative -- the write cannot be undone.
    """
    if not model_names:
        return
    for callback in tuple(_subscribers):
        try:
            callback(model_names)
        except Exception:  # noqa: BLE001 - see above; the write already committed
            pass
    if remote:
        return
    # Local caches first, other workers second: this worker is never the one
    # left serving stale data while a NOTIFY is in flight.
    for bridge in tuple(_bridges):
        try:
            bridge(model_names)
        except Exception:  # noqa: BLE001 - as above
            pass


def has_subscribers() -> bool:
    """Whether anything is listening, so the session can skip collecting names.

    A bridge counts: a worker whose own caches are cold still has to tell the
    workers whose caches are not.
    """
    return bool(_subscribers or _bridges)


class WriteBroadcast:
    """Carries write announcements between workers over the message bus.

    Local invalidation is exact -- the session knows what it wrote. Across a
    fleet it is exact too, but **at-most-once**, because the transport is an
    ephemeral ``NOTIFY``: a worker whose listen connection was down for the
    moment keeps its stale entries until their TTL. So the TTL stops being the
    invalidation mechanism and becomes the backstop behind it, which is the
    right job for a guess.

    **One channel, not one per model.** Every worker subscribes once and
    filters nothing -- the payload is a handful of model names, and a bus
    channel is a `LISTEN`, not free. Built through
    :func:`wreath.cache.invalidate_across_workers`.
    """

    __slots__ = ("_bus", "_channel", "_inflight", "_origin")

    def __init__(self, bus: Any, *, channel: str = WRITE_CHANNEL) -> None:
        self._bus = bus
        self._channel = channel
        self._inflight: set[asyncio.Future[Any]] = set()
        # Identifies announcements this worker published, so the copy NOTIFY
        # hands back to the sender is not re-delivered locally. `id(self)` never
        # crosses a process boundary as an identity claim -- it only ever has to
        # differ from the other workers'.
        self._origin = f"{id(self):x}"
        # Registered at construction: the bus collects subscriptions before it
        # starts, so a bridge built after startup would never listen.
        bus.subscribe(channel)(self._on_bus_message)
        register_bridge(self._carry)

    def close(self) -> None:
        """Stop carrying local writes. The bus subscription outlives the bus."""
        unregister_bridge(self._carry)

    def _carry(self, model_names: frozenset[str]) -> None:
        """Hand a local write to the bus, without making the writer wait."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # The ORM only ever publishes from async code; a synchronous caller
            # has no loop to publish on and there is nothing useful to do here.
            return
        future = asyncio.ensure_future(self._publish(sorted(model_names)))
        self._inflight.add(future)
        future.add_done_callback(self._inflight.discard)

    async def _publish(self, models: list[str]) -> None:
        # A bus that is down must not surface as a failed write: the row is
        # already committed, and the other workers have their TTLs.
        with contextlib.suppress(Exception):
            await self._bus.publish(
                self._channel, {"models": models, "origin": self._origin}
            )

    async def _on_bus_message(self, message: Any) -> None:
        """Replay another worker's write into this worker's subscribers."""
        payload = message.payload
        if not isinstance(payload, dict):
            return
        if payload.get("origin") == self._origin:
            return  # our own NOTIFY, already applied locally
        models = payload.get("models")
        if not isinstance(models, list):
            return
        names = frozenset(name for name in models if isinstance(name, str))
        if names:
            publish_write(names, remote=True)

    def __repr__(self) -> str:
        return f"<WriteBroadcast channel={self._channel!r} origin={self._origin}>"
