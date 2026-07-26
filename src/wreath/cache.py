"""Immutable read-mostly application cache with atomic snapshot publication.

Distinct from :class:`wreath.CacheControl` (HTTP response caching), this is an
in-process store for configuration, reference data, and database-backed
read-mostly datasets::

    cache: SnapshotCache[int, Widget] = SnapshotCache()
    await cache.refresh(load_widgets)        # single-flight
    widget = cache.get(widget_id)            # no I/O; explicit miss

Readers always observe one complete generation; a refresh publishes a new one
atomically and leaves the previous generation intact on failure.

The read path is a dict lookup that CPython already services in C, so the pure
implementation is the shipped one; the facade still selects a native
``SnapshotCache`` if a measured one is ever added to ``_core``.
"""

from __future__ import annotations

from ._native import _core

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

__all__ = ["BoundedCache", "CacheStats", "SnapshotCache"]
