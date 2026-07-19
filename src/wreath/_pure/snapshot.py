"""Pure-Python twin for :class:`SnapshotCache`.

An immutable read-mostly cache built by atomic generation publication rather
than in-place mutation. A refresh assembles a whole new generation off to the
side; publishing it swaps a single reference, so a reader ever only sees one
complete generation and never a half-applied update. Old generations stay alive
as long as a reader still references the value it read. Reads never perform I/O:
a miss is an explicit miss, not a lazy load.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from sys import getsizeof
from time import time
from typing import Any


class _Generation[K, V]:
    """One immutable published generation: a private dict plus its metadata."""

    __slots__ = ("data", "generation", "refreshed_at")

    def __init__(self, data: dict[K, V], generation: int, refreshed_at: float) -> None:
        self.data = data
        self.generation = generation
        self.refreshed_at = refreshed_at


_DEFAULT_MAX_ENTRIES = 65_536
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024


class SnapshotCache[K, V]:
    """A read-mostly cache with atomic snapshot publication.

    ``replace`` builds and publishes a new generation. ``get``/``require``/
    ``get_many`` read the current generation with no I/O. ``max_entries`` bounds
    a published generation; a violation raises before anything is published, so
    the previous generation survives intact.
    """

    __slots__ = ("_current", "_max_bytes", "_max_entries", "_refresh_lock")

    def __init__(
        self,
        *,
        max_entries: int | None = _DEFAULT_MAX_ENTRIES,
        max_bytes: int | None = _DEFAULT_MAX_BYTES,
    ) -> None:
        if max_entries is not None and max_entries < 0:
            raise ValueError("max_entries must be non-negative")
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._current: _Generation[K, V] = _Generation({}, 0, time())
        self._refresh_lock = asyncio.Lock()

    # --- read path (no I/O) ------------------------------------------------

    def get(self, key: K, default: V | None = None) -> V | None:
        return self._current.data.get(key, default)

    def require(self, key: K) -> V:
        data = self._current.data
        if key not in data:
            raise KeyError(key)
        return data[key]

    def get_many(self, keys: Iterable[K], default: V | None = None) -> list[V | None]:
        """Look up each key, preserving input order and duplicate positions.

        A duplicate key yields a value at each position it was requested; a
        miss yields ``default`` at that position.
        """
        data = self._current.data
        return [data.get(key, default) for key in keys]

    def __contains__(self, key: object) -> bool:
        return key in self._current.data

    def __len__(self) -> int:
        return len(self._current.data)

    def __iter__(self) -> Iterator[tuple[K, V]]:
        # The dict-items iterator retains this generation's dict. A concurrent
        # replace publishes a different dict and never mutates this one, so no
        # O(N) tuple copy is needed for snapshot isolation.
        return iter(self._current.data.items())

    @property
    def generation(self) -> int:
        return self._current.generation

    @property
    def refreshed_at(self) -> float:
        return self._current.refreshed_at

    # --- publication -------------------------------------------------------

    def replace(self, entries: Mapping[K, V] | Iterable[tuple[K, V]]) -> int:
        """Publish ``entries`` as a new generation and return its number.

        The new generation is fully materialized and size-checked before it is
        published, so an oversized refresh fails without disturbing readers.
        """
        data: dict[K, V] = dict(entries)
        if self._max_entries is not None and len(data) > self._max_entries:
            raise ValueError(
                f"snapshot has {len(data)} entries, exceeding max_entries "
                f"{self._max_entries}"
            )
        if self._max_bytes is not None:
            retained = getsizeof(data)
            retained += sum(getsizeof(key) + getsizeof(value) for key, value in data.items())
            if retained > self._max_bytes:
                raise ValueError(
                    f"snapshot retains approximately {retained} shallow bytes, "
                    f"exceeding max_bytes {self._max_bytes}"
                )
        generation = self._current.generation + 1
        # Single-reference swap: readers see either the old or new generation.
        self._current = _Generation(data, generation, time())
        return generation

    async def refresh(
        self,
        loader: Callable[..., Mapping[K, V] | Iterable[tuple[K, V]] | Awaitable[Any]],
        *args: object,
        **kwargs: object,
    ) -> int:
        """Single-flight refresh: call ``loader`` and publish the result.

        Concurrent refreshes coalesce — while one is in flight, the others wait
        and observe the generation it publishes rather than each loading again.
        ``loader`` may be a coroutine function or a plain callable returning the
        new entries. A loader failure leaves the previous generation in place.
        """
        starting_generation = self._current.generation
        async with self._refresh_lock:
            # Another waiter already refreshed while we queued; reuse its work.
            if self._current.generation != starting_generation:
                return self._current.generation
            result: Any = loader(*args, **kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            return self.replace(result)
