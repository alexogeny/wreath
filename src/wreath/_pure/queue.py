"""The pure-Python twin of the native bounded `Queue`.

Selected when the extension is absent or `WREATH_PURE=1` is set. The ring is a
`deque` rather than a hand-rolled buffer, for the reason the native module's
own header gives: `deque` is already C and already fast, and the cost this
primitive exists to remove was never the ring but the bookkeeping around it.
Here that bookkeeping is unavoidable, which is exactly why the native arm is
worth having.

`wreath.queue.Queue` subclasses whichever of the two is selected and supplies
the `_wake` and `_blocked` hooks, so the awaiting half is written once and runs
on both.
"""

from __future__ import annotations

import heapq
import threading
from collections import deque
from collections.abc import Iterator
from typing import Any


class QueueEmpty(Exception):
    """Raised by `get_nowait()` when the queue holds nothing."""


class QueueFull(Exception):
    """Raised by `put_nowait()` when the queue is at capacity."""


class _Ready:
    """An awaitable that resolves to an already-available item.

    The pure counterpart of the native `_QueueValue`: awaiting it never
    suspends the calling coroutine, so a queue that already has an item costs
    no Future and no trip back through the event loop. Delivering the value
    through `StopIteration` is the awaitable protocol rather than a trick --
    `__await__` must hand back an iterator, and an iterator's return value is
    its `StopIteration`.

    Unlike the C arm this needs no special case for a tuple or an exception
    instance: constructing `StopIteration(value)` from Python always stores one
    argument, where `PyErr_SetObject` would have unpacked the first and
    re-raised the second.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        self._value = value

    def __await__(self) -> Iterator[Any]:
        return self

    def __iter__(self) -> _Ready:
        return self

    def __next__(self) -> Any:
        raise StopIteration(self._value)


class Queue:
    """A bounded ring with counted loss, safe to offer to from any thread.

    Args:
        capacity: items held before offering starts dropping. Must be positive.
        drop_oldest: evict the front when full instead of refusing the new item.
    """

    __slots__ = ("_capacity", "_closed", "_drop_oldest", "_items", "_lifo",
                 "_lock", "_waiting", "dropped", "offered")

    # Declared, not assigned. The instance is built in `__new__` (see below), and
    # a bare annotation adds nothing to the class, so it coexists with __slots__
    # while giving a checker the shape it cannot infer from `Self`.
    _capacity: int
    _closed: bool
    _drop_oldest: bool
    _lifo: bool
    _items: deque[Any]
    _lock: threading.Lock
    _waiting: bool
    dropped: int
    offered: int

    # An ordinary `__init__`, matching the native arm's `tp_init` and
    # `wreath.kv.KV`. Both arms were built in `__new__` for a while, which
    # forced every subclass to override `__new__` and never chain
    # `super().__init__` -- one family with two construction rules, and the one
    # that was not written down.
    def __init__(
        self, capacity: int = 4096, *, drop_oldest: bool = False, lifo: bool = False
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._items = deque()
        self._drop_oldest = drop_oldest
        self._lifo = lifo
        self._lock = threading.Lock()
        self._closed = False
        self._waiting = False
        self.offered = 0
        self.dropped = 0
        self._capacity = capacity

    @property
    def capacity(self) -> int:
        """Items held before dropping."""
        return self._capacity

    @property
    def closed(self) -> bool:
        """Whether further items are refused."""
        return self._closed

    @property
    def drop_oldest(self) -> bool:
        """Whether a full queue evicts the front rather than refusing."""
        return self._drop_oldest

    @property
    def lifo(self) -> bool:
        """Whether the newest item is taken first rather than the oldest."""
        return self._lifo

    @property
    def waiting(self) -> bool:
        """Whether a getter is parked; set by `wreath.queue.Queue`."""
        return self._waiting

    @waiting.setter
    def waiting(self, value: bool) -> None:
        self._waiting = bool(value)

    def offer(self, item: Any) -> bool:
        """Enqueue `item`, reporting whether it was kept."""
        evicted = None
        with self._lock:
            if self._closed:
                raise RuntimeError("queue is closed")
            self.offered += 1
            if len(self._items) == self._capacity:
                if not self._drop_oldest:
                    self.dropped += 1
                    return False
                # Released outside the lock: a __del__ on the displaced item can
                # run arbitrary code, and running it here would let a producer
                # deadlock against its own queue.
                evicted = self._items.popleft()
                self.dropped += 1
            self._items.append(item)
            waiting = self._waiting
        del evicted
        if waiting:
            self._wake()
        return True

    def put_nowait(self, item: Any) -> None:
        """Enqueue `item`, raising `QueueFull` rather than dropping it."""
        evicted = None
        with self._lock:
            if self._closed:
                raise RuntimeError("queue is closed")
            if len(self._items) == self._capacity and not self._drop_oldest:
                raise QueueFull("queue is full")
            self.offered += 1
            if len(self._items) == self._capacity:
                evicted = self._items.popleft()
                self.dropped += 1
            self._items.append(item)
            waiting = self._waiting
        del evicted
        if waiting:
            self._wake()

    def _take(self) -> Any:
        """The next item under this queue's discipline. Caller holds the lock."""
        return self._items.pop() if self._lifo else self._items.popleft()

    def get_nowait(self) -> Any:
        """The next item under this queue's discipline, or `QueueEmpty`."""
        with self._lock:
            if not self._items:
                raise QueueEmpty("queue is empty")
            return self._take()

    def get(self) -> Any:
        """An awaitable for the next item, waiting for one if empty."""
        with self._lock:
            item = self._take() if self._items else _MISSING
        if item is not _MISSING:
            return _Ready(item)
        return self._blocked()

    def drain(self, limit: int | None = None) -> list[Any]:
        """Remove and return up to `limit` items, in the order `get` would."""
        with self._lock:
            if limit is None or limit >= len(self._items):
                batch = list(reversed(self._items)) if self._lifo else list(self._items)
                self._items.clear()
                return batch
            return [self._take() for _ in range(max(0, limit))]

    def peek(self, default: Any = None) -> Any:
        """The next item under this queue's discipline without removing it.

        The read that does not disturb what it is reading, which is what `peek`
        means on `wreath.kv` and on `PriorityQueue` too.
        """
        with self._lock:
            if not self._items:
                return default
            return self._items[-1] if self._lifo else self._items[0]

    def snapshot(self) -> list[Any]:
        """The queued items in the order `get` would return them.

        Following the discipline rather than the buffer: a snapshot that
        disagreed with the queue's own order would be a debugging aid that lies
        about what happens next.
        """
        with self._lock:
            return list(reversed(self._items)) if self._lifo else list(self._items)

    def clear(self) -> int:
        """Drop every queued item, returning how many went."""
        return len(self.drain())

    def close(self) -> None:
        """Refuse further items; draining the backlog still works."""
        with self._lock:
            self._closed = True
            waiting = self._waiting
        if waiting:
            self._wake()

    def _wake(self) -> None:
        """Overridden by `wreath.queue.Queue`; inert on the bare ring."""

    def _blocked(self) -> Any:
        """Overridden by `wreath.queue.Queue`; inert on the bare ring."""
        raise QueueEmpty("queue is empty and nothing is waiting on it")

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)



class PriorityQueue:
    """A bounded priority queue: lowest number first, insertion order within one.

    The pure twin of the native binary heap. `heapq` over
    `(priority, sequence, item)` triples, and the sequence is doing two jobs at
    once: it makes ties come out in the order they went in, and it means the
    *item* is never compared, so an object with no ordering is still queueable.

    Args:
        capacity: items held before offering starts dropping. Must be positive.
        drop_lowest: displace the worst queued item when full, rather than
            refusing the new one. Refuses only when the new item is the worst.
    """

    __slots__ = ("_capacity", "_closed", "_drop_lowest", "_entries", "_lock",
                 "_sequence", "_waiting", "dropped", "offered")

    _capacity: int
    _closed: bool
    _drop_lowest: bool
    _entries: list[tuple[float, int, Any]]
    _lock: threading.Lock
    _sequence: int
    _waiting: bool
    dropped: int
    offered: int

    def __init__(self, capacity: int = 4096, *, drop_lowest: bool = False) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._entries = []
        self._capacity = capacity
        self._drop_lowest = drop_lowest
        self._lock = threading.Lock()
        self._closed = False
        self._waiting = False
        self._sequence = 0
        self.offered = 0
        self.dropped = 0

    @property
    def capacity(self) -> int:
        """Items held before dropping."""
        return self._capacity

    @property
    def closed(self) -> bool:
        """Whether further items are refused."""
        return self._closed

    @property
    def drop_lowest(self) -> bool:
        """Whether a full queue displaces its worst item rather than refusing."""
        return self._drop_lowest

    @property
    def waiting(self) -> bool:
        """Whether a getter is parked; set by `wreath.queue.PriorityQueue`."""
        return self._waiting

    @waiting.setter
    def waiting(self, value: bool) -> None:
        self._waiting = bool(value)

    def offer(self, item: Any, priority: float = 0.0) -> bool:
        """Enqueue `item` at `priority`, reporting whether it was kept."""
        priority = float(priority)
        if priority != priority:
            raise ValueError(
                "priority must be a number, not NaN: NaN compares false against "
                "everything, which would leave the heap unordered"
            )
        displaced = None
        with self._lock:
            if self._closed:
                raise RuntimeError("queue is closed")
            self.offered += 1
            stored = True
            if len(self._entries) == self._capacity:
                self.dropped += 1
                stored = False
                if self._drop_lowest and self._entries:
                    worst = max(range(len(self._entries)), key=self._entries.__getitem__)
                    if (priority, self._sequence) < self._entries[worst][:2]:
                        displaced = self._entries.pop(worst)
                        heapq.heapify(self._entries)
                        stored = True
            if stored:
                heapq.heappush(self._entries, (priority, self._sequence, item))
                self._sequence += 1
            waiting = self._waiting
        del displaced
        if stored and waiting:
            self._wake()
        return stored

    def put_nowait(self, item: Any, priority: float = 0.0) -> None:
        """Enqueue `item`, raising `QueueFull` rather than dropping anything.

        The other posture, and the one a priority queue often wants: losing an
        urgent item silently is worse than being told the queue is full. Counts
        no drop, because nothing was dropped.
        """
        with self._lock:
            # Under `drop_lowest` a full heap still accepts an item better than
            # its worst, so "full" here is only the case where nothing can be
            # admitted; `offer` below settles which.
            if len(self._entries) == self._capacity and not self._drop_lowest:
                raise QueueFull("queue is full")
        if not self.offer(item, priority):
            raise QueueFull("queue is full")

    def get_nowait(self) -> Any:
        """The best item, or `QueueEmpty` when there is none."""
        with self._lock:
            if not self._entries:
                raise QueueEmpty("queue is empty")
            return heapq.heappop(self._entries)[2]

    def get(self) -> Any:
        """An awaitable for the best item, waiting for one if empty."""
        with self._lock:
            item = heapq.heappop(self._entries)[2] if self._entries else _MISSING
        if item is not _MISSING:
            return _Ready(item)
        return self._blocked()

    def peek(self, default: Any = None) -> Any:
        """The best item without removing it, or `default` when empty."""
        with self._lock:
            return self._entries[0][2] if self._entries else default

    def drain(self, limit: int | None = None) -> list[Any]:
        """Remove and return up to `limit` items, best first."""
        with self._lock:
            if limit is None:
                limit = len(self._entries)
            taken = min(max(0, limit), len(self._entries))
            return [heapq.heappop(self._entries)[2] for _ in range(taken)]

    def snapshot(self) -> list[tuple[float, Any]]:
        """The queued `(priority, item)` pairs in the order `get` would return them."""
        with self._lock:
            ordered = sorted(self._entries, key=lambda entry: entry[:2])
        return [(priority, item) for priority, _sequence, item in ordered]

    def clear(self) -> int:
        """Drop every queued item, returning how many went."""
        return len(self.drain())

    def close(self) -> None:
        """Refuse further items; draining the backlog still works."""
        with self._lock:
            self._closed = True
            waiting = self._waiting
        if waiting:
            self._wake()

    def _wake(self) -> None:
        """Overridden by `wreath.queue.PriorityQueue`; inert on the bare heap."""

    def _blocked(self) -> Any:
        """Overridden by `wreath.queue.PriorityQueue`; inert on the bare heap."""
        raise QueueEmpty("queue is empty and nothing is waiting on it")

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class _Missing:
    __slots__ = ()


_MISSING = _Missing()


__all__ = ["PriorityQueue", "Queue", "QueueEmpty", "QueueFull"]
