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

import weakref
from collections.abc import Callable
from typing import Any

from ._busbridge import BusBridge

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
#: An entry is the callback itself, or an `_OwnedSubscriber` wrapping it when
#: the caller tied the subscription to an object's lifetime; both are called
#: the same way, and the second removes itself when its owner is collected.
_subscribers: list[Callable[[frozenset[str]], None]] = []

#: Bridges to other workers. Separate from `_subscribers` so a write that
#: *arrived* from another worker is delivered without being sent back out.
_bridges: list[Callable[[frozenset[str]], None]] = []


class _OwnedSubscriber:
    """A subscription whose life is its owner's, not the process's.

    Some subscribers have an obvious moment to unsubscribe at and use
    :func:`unsubscribe_writes` -- a live-doc stream closes, a snapshot cache's
    handle is stopped. A ``@cached`` handler has none: it subscribes when it is
    *decorated* and its only end is becoming unreachable. Registering a plain
    closure for it would mean `_subscribers` accumulates one entry per decorated
    handler for the life of the process, and -- worse than the memory --
    :func:`has_subscribers` would stay true forever, so the session's "collect
    nothing when nobody is listening" fast path would be dead in any application
    that caches at all.

    Holding the owner weakly ends the subscription where the thing it serves
    ends. A handler decorated at module scope legitimately lives for the
    process; one that goes out of scope takes its subscription with it.
    """

    __slots__ = ("callback", "owner")

    def __init__(self, owner: Any, callback: Callable[[frozenset[str]], None]) -> None:
        self.callback = callback
        # `_reap` is a module-level function rather than a bound method of this
        # object: a callback holding `self` would make a reference cycle, and a
        # cycle is only broken by a gc pass -- `has_subscribers()` would then be
        # wrong for however long that takes.
        self.owner = weakref.ref(owner, _reap)

    def __call__(self, model_names: frozenset[str]) -> None:
        self.callback(model_names)


def _reap(dead: weakref.ref[Any]) -> None:
    """Drop the subscription whose owner has just been collected."""
    for entry in tuple(_subscribers):
        if isinstance(entry, _OwnedSubscriber) and entry.owner is dead:
            _subscribers.remove(entry)


def subscribe_writes(
    callback: Callable[[frozenset[str]], None], *, owner: Any = None
) -> None:
    """Call ``callback(model_names)`` after each committed flush that wrote.

    ``owner`` binds the subscription's lifetime to an object instead of to the
    process, for a caller that has no later moment to unsubscribe at. It is
    held weakly and the subscription is dropped the moment it is collected, so
    ``callback`` must not be the only thing keeping ``owner`` alive.
    """
    if owner is None:
        if callback not in _subscribers:
            _subscribers.append(callback)
        return
    reference = weakref.ref(owner)
    for entry in _subscribers:
        if (
            isinstance(entry, _OwnedSubscriber)
            and entry.owner == reference
            and entry.callback == callback
        ):
            return
    _subscribers.append(_OwnedSubscriber(owner, callback))


def unsubscribe_writes(callback: Callable[[frozenset[str]], None]) -> None:
    """Stop delivering to ``callback``, however it was registered."""
    for entry in tuple(_subscribers):
        if entry == callback or (
            isinstance(entry, _OwnedSubscriber) and entry.callback == callback
        ):
            _subscribers.remove(entry)
            return


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

    The channel, the origin tag, and the deferred publish are
    :class:`~wreath._busbridge.BusBridge`'s; what is local to this class is the
    payload shape and the *bridge* registration below, which is what keeps a
    received announcement from going back out.
    """

    __slots__ = ("_bridge",)

    def __init__(self, bus: Any, *, channel: str = WRITE_CHANNEL) -> None:
        self._bridge = BusBridge(bus, channel=channel, apply=self._apply)
        register_bridge(self._carry)

    def close(self) -> None:
        """Stop carrying local writes. The bus subscription outlives the bus."""
        unregister_bridge(self._carry)

    def _carry(self, model_names: frozenset[str]) -> None:
        """Hand a local write to the bus, without making the writer wait.

        Deferred rather than awaited because the row has already committed: a
        bus that is down must not surface as a failed write, and the other
        workers have their TTLs behind them.
        """
        self._bridge.publish_soon({"models": sorted(model_names)})

    async def _apply(self, payload: dict[str, Any]) -> None:
        """Replay another worker's write into this worker's subscribers.

        ``remote=True`` is the half that matters: local subscribers see it
        exactly as they see a local write, and bridges do not, so it stops here
        instead of bouncing around the fleet.
        """
        models = payload.get("models")
        if not isinstance(models, list):
            return
        names = frozenset(name for name in models if isinstance(name, str))
        if names:
            publish_write(names, remote=True)

    def __repr__(self) -> str:
        return (
            f"<WriteBroadcast channel={self._bridge.channel!r} "
            f"origin={self._bridge.origin}>"
        )
