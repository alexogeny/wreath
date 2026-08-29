"""Immutable read-mostly application cache with atomic snapshot publication.

Distinct from `wreath.CacheControl` (HTTP response caching), this is an
in-process store for configuration, reference data, and database-backed
read-mostly datasets:

```python
cache: SnapshotCache[int, Widget] = SnapshotCache()
await cache.refresh(load_widgets)        # single-flight
widget = cache.get(widget_id)            # no I/O; explicit miss
```
Readers always observe one complete generation; a refresh publishes a new one
atomically and leaves the previous generation intact on failure.

The read path is a dict lookup that CPython already services in C, and a native
port was measured against `kv.c` and came out slower, so there is one
implementation rather than a selection. See `wreath._snapshot`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any

from ._orm_events import (
    WRITE_CHANNEL,
    WriteBroadcast,
    subscribe_writes,
    unsubscribe_writes,
)
from ._snapshot import SnapshotCache as SnapshotCache
from .kv import KV, stats
from .kv import Stats as CacheStats


class BoundedCache(KV):
    """A bounded LRU cache with optional per-entry TTL.

    **This is `wreath.kv.KV`**, with one convenience on top. It kept its own
    name because that is the spelling the response cache, the idempotency layer
    and `wreath port`'s `TTLCache`/`LRUCache` translation all already use -- but
    it is no longer a second implementation, or a second set of semantics, or a
    second thing to learn. Everything it does, `KV` does.

    `get`, `set`, `delete`, `clear`, `snapshot`, `__contains__`, `__len__`,
    `ttl` and `max_entries` are inherited from `KV`. The convenience adds only
    `as_dict`; `clock` is `KV`'s own parameter.

    Args:
        max_entries: hard ceiling; the least-recently-used entry is evicted once
            a set would exceed it. Must be positive.
        ttl: seconds an entry stays fresh, or `None` to never expire by time.
            Expiry is lazy -- checked on read -- so nothing runs in the
            background.
        clock: monotonic time source, injectable for deterministic tests.
    """

    @property
    def stats(self) -> CacheStats:
        """A point-in-time view of this cache's activity.

        The one thing `KV` does not offer under this name: it exposes the same
        four counters individually, and `wreath.kv.stats(table)` builds the same
        value from any table. This is the property spelling those callers use.
        """
        return stats(self)


def invalidate_across_workers(bus: Any, *, channel: str = WRITE_CHANNEL) -> WriteBroadcast:
    """Make ORM-driven cache invalidation fleet-wide, over the message bus:

    ```python
    invalidate_across_workers(app.messaging("bus", database="app"))

    @app.get("/herd/report")
    @cached(ttl=300, invalidate_on=[Llama])
    async def herd_report(request): ...
    ```
    Without it, a write on worker A clears only worker A. With it, the model
    names the committing session announces are carried to every worker on one
    channel and applied there, so four workers behave like one -- and still no
    Redis, because the bus is the database you already have.

    Returns the `WriteBroadcast` carrying them, whose
    `close()` stops it. Call once per process, before startup: the bus
    collects its subscriptions before it begins listening.

    Delivery is at-most-once, as ephemeral fan-out is. A worker that misses the
    notification holds its entries until they expire, which is what the `ttl`
    is now for -- a backstop rather than the mechanism.
    """
    return WriteBroadcast(bus, channel=channel)


class RefreshWatch:
    """The handle `refresh_on` returns. Call it to stop watching.

    It also carries what an operator needs when reloads are failing. A snapshot
    cache degrades *upwards*: a broken loader leaves every read succeeding
    against the last good generation, so the usual signals -- errors, latency,
    a falling hit rate -- all stay quiet while the data silently ages. These two
    attributes are the only place that shows.
    """

    __slots__ = ("_stop", "last_error", "refresh_errors")

    def __init__(self, stop: Callable[[], None]) -> None:
        self._stop = stop
        #: Reloads that raised. Non-zero means the cache is serving data older
        #: than the last write it was told about.
        self.refresh_errors = 0
        #: The most recent reload failure, kept so the cause is diagnosable
        #: without reproducing it. `None` until one happens.
        self.last_error: BaseException | None = None

    def __call__(self) -> None:
        self._stop()


def refresh_on(
    cache: Any,
    models: Iterable[Any],
    *,
    load: Callable[[], Any],
) -> RefreshWatch:
    """Reload `cache` whenever one of `models` is written:

    ```python
    countries: SnapshotCache[str, Country] = SnapshotCache()
    await countries.refresh(load_countries)
    refresh_on(countries, [Country], load=load_countries)
    ```
    The snapshot cache holds reference data, so the right response to a write is
    to *reload* rather than to drop -- a dropped generation would leave readers
    with an explicit miss on data that has not gone anywhere.

    This is the same announcement the response cache listens to, so
    `invalidate_across_workers` makes it fleet-wide too: a write on any
    worker reloads every worker's reference data, over one bus channel.

    Refresh is single-flight and a failing loader leaves the previous generation
    in place, so a database blip cannot empty a cache that readers depend on.
    The reload is scheduled rather than awaited -- the write has already
    committed and must not wait on it.

    Returns a `RefreshWatch`: call it to stop watching, and read
    `refresh_errors` to find out whether the reloads are actually working.
    """
    watched = frozenset(getattr(model, "__name__", str(model)) for model in models)
    inflight: set[Any] = set()

    def _on_write(written: frozenset[str]) -> None:
        if not (written & watched):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # nothing to schedule the reload on
        future = loop.create_task(_reload())
        inflight.add(future)
        future.add_done_callback(inflight.discard)

    async def _reload() -> None:
        # A loader that raises must not surface as a failed write, and must not
        # disturb the generation readers are already on.
        try:
            await cache.refresh(load)
        except Exception as error:  # noqa: BLE001
            # `load` is application code and may raise anything, so this is the
            # exceptional case a broad catch is for. What it must not be is
            # silent: a permanently failing loader leaves readers on the last
            # good generation *forever*, and because every read still succeeds,
            # nothing else in the system degrades to signal it. The count is
            # what makes that visible; the exception is what makes it
            # diagnosable without reproducing it.
            # `CancelledError` is a `BaseException` and still propagates, so a
            # reload cancelled at shutdown is not recorded as a loader fault.
            watch.refresh_errors += 1
            watch.last_error = error

    watch = RefreshWatch(lambda: unsubscribe_writes(_on_write))
    subscribe_writes(_on_write)
    return watch


__all__ = [
    "BoundedCache",
    "CacheStats",
    "RefreshWatch",
    "SnapshotCache",
    "WriteBroadcast",
    "invalidate_across_workers",
    "refresh_on",
]
