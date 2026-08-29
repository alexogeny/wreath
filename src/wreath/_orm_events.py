"""Publish committed ORM writes to local subscribers and worker bridges."""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable
from typing import Any

from ._busbridge import BusBridge

__all__ = [
    "WRITE_CHANNEL",
    "WriteBroadcast",
    "bridge_errors",
    "has_subscribers",
    "publish_write",
    "register_bridge",
    "subscribe_writes",
    "subscriber_errors",
    "unregister_bridge",
    "unsubscribe_writes",
]

WRITE_CHANNEL = "wreath_writes"

type _CallbackKey = tuple[int, object]
type _SubscriptionKey = tuple[_CallbackKey, int | None]

_subscribers: dict[_SubscriptionKey, Callable[[frozenset[str]], None]] = {}
_subscriber_keys: dict[_CallbackKey, dict[_SubscriptionKey, None]] = {}
_bridges: dict[_CallbackKey, Callable[[frozenset[str]], None]] = {}

_errors = {"subscriber": 0, "bridge": 0}

_lock = threading.Lock()


def subscriber_errors() -> int:
    """How many subscriber deliveries have raised. Never resets."""
    return _errors["subscriber"]


def bridge_errors() -> int:
    """How many bridge deliveries have raised. Never resets."""
    return _errors["bridge"]


class _OwnedSubscriber:
    __slots__ = ("callback", "owner")

    def __init__(
        self,
        owner: Any,
        callback: Callable[[frozenset[str]], None],
        key: _SubscriptionKey,
    ) -> None:
        self.callback = callback
        self.owner = weakref.ref(owner, lambda dead: _reap(key, dead))

    def __call__(self, model_names: frozenset[str]) -> None:
        self.callback(model_names)


def _callback_key(callback: Callable[[frozenset[str]], None]) -> _CallbackKey:
    try:
        hash(callback)
    except TypeError:
        return 1, id(callback)
    return 0, callback


def _remove_subscription(key: _SubscriptionKey) -> None:
    entry = _subscribers.pop(key, None)
    if entry is None:
        return
    callback_key = key[0]
    keys = _subscriber_keys[callback_key]
    del keys[key]
    if not keys:
        del _subscriber_keys[callback_key]


def _reap(key: _SubscriptionKey, dead: weakref.ref[Any]) -> None:
    with _lock:
        entry = _subscribers.get(key)
        if isinstance(entry, _OwnedSubscriber) and entry.owner is dead:
            _remove_subscription(key)


def subscribe_writes(callback: Callable[[frozenset[str]], None], *, owner: Any = None) -> None:
    """Call `callback(model_names)` after each committed flush that wrote.

    `owner` is held weakly and removes the subscription when collected.
    """
    callback_key = _callback_key(callback)
    key = callback_key, None if owner is None else id(owner)
    with _lock:
        if key in _subscribers:
            return
        entry = callback if owner is None else _OwnedSubscriber(owner, callback, key)
        _subscribers[key] = entry
        _subscriber_keys.setdefault(callback_key, {})[key] = None


def unsubscribe_writes(callback: Callable[[frozenset[str]], None]) -> None:
    """Stop delivering to `callback`, however it was registered."""
    with _lock:
        _unsubscribe(callback)


def _unsubscribe(callback: Callable[[frozenset[str]], None]) -> None:
    keys = _subscriber_keys.get(_callback_key(callback))
    if keys:
        _remove_subscription(next(iter(keys)))


def register_bridge(callback: Callable[[frozenset[str]], None]) -> None:
    """Call `callback(model_names)` for **locally** originated writes only."""
    with _lock:
        _bridges.setdefault(_callback_key(callback), callback)


def unregister_bridge(callback: Callable[[frozenset[str]], None]) -> None:
    with _lock:
        _bridges.pop(_callback_key(callback), None)


def publish_write(model_names: frozenset[str], *, remote: bool = False) -> None:
    """Announce committed writes.

    Remote announcements never reach bridges. Listener failures are counted
    because the committed write can no longer be rolled back.
    """
    if not model_names:
        return
    with _lock:
        subscribers = tuple(_subscribers.values())
        bridges = () if remote else tuple(_bridges.values())
    for callback in subscribers:
        try:
            callback(model_names)
        except Exception:  # noqa: BLE001 - see above; the write already committed
            _errors["subscriber"] += 1
    if remote:
        return
    for bridge in bridges:
        try:
            bridge(model_names)
        except Exception:  # noqa: BLE001 - as above
            _errors["bridge"] += 1


def has_subscribers() -> bool:
    """Return whether a subscriber or bridge needs written model names."""
    return bool(_subscribers or _bridges)


class WriteBroadcast:
    """Relay write announcements over an at-most-once message bus."""

    __slots__ = ("_bridge", "_closed")

    def __init__(self, bus: Any, *, channel: str = WRITE_CHANNEL) -> None:
        self._closed = False
        self._bridge = BusBridge(bus, channel=channel, apply=self._apply)
        register_bridge(self._carry)

    def close(self) -> None:
        """Stop carrying and applying writes without removing the bus listener."""
        unregister_bridge(self._carry)
        self._closed = True

    def _carry(self, model_names: frozenset[str]) -> None:
        self._bridge.publish_soon({"models": sorted(model_names)})

    async def _apply(self, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        models = payload.get("models")
        if not isinstance(models, list):
            return
        names = frozenset(name for name in models if isinstance(name, str))
        if names:
            publish_write(names, remote=True)

    def __repr__(self) -> str:
        return f"<WriteBroadcast channel={self._bridge.channel!r} origin={self._bridge.origin}>"
