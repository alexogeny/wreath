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

__all__ = ["SnapshotCache"]
