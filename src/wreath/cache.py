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

The read path is a dict lookup that CPython already services in C, so the pure
implementation is the shipped one; the facade still selects a native
`SnapshotCache` if a measured one is ever added to `_core`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any

from ._native import _core
from ._orm_events import (
    WRITE_CHANNEL,
    WriteBroadcast,
    subscribe_writes,
    unsubscribe_writes,
)

if _core is not None and hasattr(_core, "SnapshotCache"):
    SnapshotCache = _core.SnapshotCache
else:
    from ._pure.snapshot import SnapshotCache

# A small bounded LRU/TTL store for hot request-path caching (response cache,
# idempotency replay). No external backend, and deliberately still pure.
#
# Measured (ablation, 25 interleaved rounds against an A/A control, 2026-07-26):
# `get` on a TTL'd hit is ~0.14us, against a ~0.02us floor for the bare dict
# lookup underneath it. Inlining the `_live` helper and hoisting the clock
# recovers ~0.02us of that; the remaining ~0.11us is method-call overhead,
# `OrderedDict.move_to_end`, and the `monotonic()` reading, none of which pure
# Python can shed. So a native twin is the only way to close it -- and it is not
# worth building: every caller here is skipping work measured in tens to
# hundreds of microseconds (a rendered response, a replayed handler), so 0.11us
# is three to four orders of magnitude below what the cache saves. Re-open this
# only with an end-to-end benchmark that shows the lookup mattering, not a
# microbenchmark of the lookup alone. A native `BoundedCache` can be selected
# here exactly the way `SnapshotCache` is above if that day comes.
from ._pure.bounded import BoundedCache, CacheStats


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
            #
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
