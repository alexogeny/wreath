"""One bounded in-process queue, for every hand-off that needed one.

A log record going from the projector thread to the writer, a finished trace
going to the exporter, a WebSocket frame going to an application task: all of
them want the same thing, which is a queue that cannot grow without bound and
that says out loud when it has dropped something.

```python
from wreath.queue import Queue

records = Queue(capacity=4096)
records.offer(record)                 # False, and counts, when full
batch = records.drain(limit=256)      # one call, not a pop per item
item = await records.get()            # no Future when an item is already there
```

**Loss is counted, never silent.** Offering to a full queue returns `False` and
increments `dropped`. That is the only policy compatible with the promise a
bounded hand-off makes -- bounded memory, bounded latency, and a number an
operator can look at when a consumer falls behind. `drop_oldest=True` evicts the
front instead, for a consumer that would rather have the newest; it still
counts, because something was lost either way. `put_nowait` is the other
posture: raise `QueueFull` rather than lose the item.

**Safe across threads.** Unlike `wreath.kv` this genuinely crosses them, so the
ring is locked. `await get()` may only be used from one event loop, which is
the loop the first waiter parks on; offering from any other thread wakes that
loop through `call_soon_threadsafe`.

The awaiting half is written once here and runs on both arms. A queue that
already has an item resolves without suspending the caller and without building
a Future -- the native `get()` hands back a pre-resolved awaitable, and only a
genuinely empty queue reaches `_blocked` below.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from ._native import _core

# One pair of exception classes on *both* arms, so `except QueueEmpty` catches
# whichever ring raised, and one `_Ready` for the rotation below to hand back.
# Only the ring itself is selected here; see `wreath._queue_protocol`.
from ._queue_protocol import QueueEmpty, QueueFull, _Ready

_Ring: Any = _core.Queue
_Heap: Any = _core.PriorityQueue


class Awaiting:
    """The awaiting half, over whichever ring it is mixed into.

    A mixin rather than a base class with the ring built in, because the FIFO
    ring and the priority heap are separate C-level layouts and a class cannot
    inherit from both. `Queue` and `PriorityQueue` below are the two
    compositions callers want.

    Carries no instance layout of its own -- `__slots__` is empty here, and
    **every concrete class that mixes this in must declare `_loop`, `_waiters`
    and `_active_waiters` in its own `__slots__`**. A mixin that declared them
    would give
    itself a `tp_basicsize` larger than `object`'s, and combining two such bases
    is the "multiple bases have instance lay-out conflict" this class exists to
    avoid. `Queue`, `PriorityQueue` and `RoundRobin` all do so.
    """

    __slots__ = ()

    if TYPE_CHECKING:
        # Supplied by whichever ring this is mixed into. Declared here so the
        # mixin checks on its own -- it is never instantiated alone, and both
        # rings provide all three.
        waiting: bool
        closed: bool

        def get_nowait(self) -> Any: ...

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Chains normally, because both rings now configure themselves in
        # `__init__` exactly as `wreath.kv.KV` does. They used to build in
        # `__new__`, which meant this could not call `super().__init__` at all
        # -- a rule that lived only in a comment.
        #
        # `*args, **kwargs` rather than a restated signature: the ring beneath
        # validates them, and a second copy here is a second thing to keep in
        # step for no reader's benefit.
        super().__init__(*args, **kwargs)
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._active_waiters: set[asyncio.Future[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def _blocked(self) -> Any:
        """Wait for an item. Called by `get()` only when the queue is empty.

        The parked Future carries no value: it is a signal, and the getter
        re-reads the queue after waking. Handing the item through the Future
        instead would lose it whenever the awaiting task was cancelled between
        the producer choosing a waiter and the callback running -- a window that
        exists on every cross-thread wake and cannot be closed from the
        producer's side.
        """
        loop = asyncio.get_running_loop()
        self._loop = loop
        while True:
            waiter = loop.create_future()
            self._waiters.append(waiter)
            self._active_waiters.add(waiter)
            self.waiting = True
            # Re-checked *after* parking, which is the whole point of doing it
            # here as well as in `get()`: an item offered between `get()`
            # finding the queue empty and this waiter becoming visible would
            # otherwise wake nobody and sit there until the next offer.
            try:
                return self.get_nowait()
            except QueueEmpty:
                pass
            if self.closed:
                self._discard(waiter)
                raise QueueEmpty("queue is closed and empty")
            try:
                await waiter
            except BaseException:
                # Cancellation included: leaving a cancelled Future in the deque
                # would make the next producer hand its wake-up to a waiter that
                # will never read the queue.
                self._discard(waiter)
                raise
            try:
                return self.get_nowait()
            except QueueEmpty:
                if self.closed:
                    raise QueueEmpty("queue is closed and empty") from None
                # Another getter was awake first and took it. Park again rather
                # than returning, so `get()` keeps its one promise: it resolves
                # with an item or it raises.
                continue

    def _wake(self) -> None:
        """Called by the ring when an item lands and a getter is parked.

        Runs on whatever thread offered, which is why the loop is asked whether
        it is the current one rather than assumed.
        """
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._resolve()
            return
        try:
            loop.call_soon_threadsafe(self._resolve)
        except RuntimeError:
            # The loop closed while a producer still held a reference to it.
            # Nothing is waiting on a closed loop, so there is nothing to wake
            # and nothing to report.
            self._loop = None

    def _resolve(self) -> None:
        """Hand the wake-up to the first waiter that can still use it."""
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter in self._active_waiters:
                self._active_waiters.discard(waiter)
                waiter.set_result(None)
                if not self._active_waiters:
                    self.waiting = False
                return
        self.waiting = False

    def _discard(self, waiter: asyncio.Future[None]) -> None:
        self._active_waiters.discard(waiter)
        if not self._active_waiters:
            # Cancelled futures remain as FIFO tombstones until the last live
            # waiter leaves. One clear makes the whole cancellation batch O(n).
            self._waiters.clear()
            self.waiting = False


class Queue(Awaiting, _Ring):
    """A bounded queue with counted loss and an awaitable `get`.

    First in, first out by default, which is what a hand-off almost always
    wants: the oldest item has been waiting longest.

    `lifo=True` makes it a stack instead. That is not a fairness choice but a
    latency one -- under a backlog a LIFO serves the *newest* item first, so the
    items it serves are the freshest, at the cost of the oldest possibly never
    being served at all. Reach for it where a stale item is worthless anyway (a
    live metric, a frame of a video feed) and never where every item must
    eventually be handled.

    Args:
        capacity: items held before offering starts dropping. Must be positive.
        drop_oldest: evict the front when full instead of refusing the new item.
        lifo: take the newest item first rather than the oldest.
    """

    __slots__ = ("_active_waiters", "_loop", "_waiters")


class PriorityQueue(Awaiting, _Heap):
    """A bounded priority queue with counted loss and an awaitable `get`.

    Lowest number first, and items sharing a priority come out in the order they
    went in. That stability matters more than it sounds: most items in a real
    workload share a priority, so without it the common case is unordered and a
    test that pins ordering is flaky rather than wrong.

    The item itself is never compared, so anything is queueable -- including
    objects that define no ordering at all, and objects whose `__lt__` would run
    arbitrary Python at an inconvenient moment.

    Args:
        capacity: items held before offering starts dropping. Must be positive.
        drop_lowest: when full, displace the worst queued item rather than
            refusing the new one, so a high-priority arrival is never lost to a
            backlog of low-priority work. Refuses only when the new item is
            itself the worst. Costs a scan of the heap's leaves, which is the
            honest price of asking a min-heap for its maximum.
    """

    __slots__ = ("_active_waiters", "_loop", "_waiters")


class _Lane(Queue):
    """One `RoundRobin` lane, which tells its scheduler when something lands.

    A lane never parks a waiter of its own -- the scheduler does that, on behalf
    of whichever lane answers first -- so overriding `_wake` to forward is safe
    and is the whole of the wiring.
    """

    __slots__ = ("scheduler",)

    def _wake(self) -> None:
        self.scheduler._wake()


class RoundRobin(Awaiting):
    """Fair scheduling across named lanes, each its own bounded queue.

    One queue shared by many producers is fair only in the sense that it is
    first-come-first-served, which is exactly the wrong fairness when the
    producers are tenants: whoever offers fastest gets the most service, and one
    busy tenant starves the rest. A lane per tenant and a rotating cursor
    converts that into "everyone gets a turn".

    ```python
    work = RoundRobin(capacity=1024, max_lanes=256)
    work.offer("tenant-a", job)
    job = work.get_nowait()          # the next lane in the rotation
    job = await work.get()           # ... or wait for whichever lane fills
    ```

    **`capacity` is per lane, not total**, which is the point: one lane filling
    up drops that lane's work and nobody else's. `max_lanes` bounds the number of
    lanes, because lanes are created on demand and a lane name that arrives from
    a request is otherwise an unbounded allocation an unauthenticated caller
    controls -- the same failure mode every other bound here exists to prevent.

    It carries the same surface as the other three: `offer`, `get`, `get_nowait`,
    `peek`, `drain`, `snapshot`, `clear`, `close`, `closed`, `capacity`,
    `offered`, `dropped`, `len()`. It used to carry about half of them, which
    made it the one queue in this module a reader had to learn separately.

    The rotation is deliberately plain Python. It moves a cursor over a list once
    per item and is nowhere near the cost of the queue operation it wraps, so
    there is nothing here for C to do. The waiting half is not rewritten either:
    `Awaiting` is the same mixin `Queue` and `PriorityQueue` use, so cancellation
    and cross-thread wake-ups are solved once for all four.
    """

    # `_loop` and `_waiters` belong to `Awaiting` and are declared here for the
    # reason its docstring gives: the mixin cannot carry a layout of its own.
    __slots__ = ("_active_waiters", "_capacity", "_closed", "_cursor",
                 "_drop_oldest", "_lanes", "_loop", "_max_lanes", "_order",
                 "_waiters")

    def __init__(
        self,
        capacity: int = 4096,
        *,
        max_lanes: int = 1024,
        drop_oldest: bool = False,
        lanes: Iterable[str] = (),
    ) -> None:
        super().__init__()
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if max_lanes < 1:
            raise ValueError("max_lanes must be positive")
        self._capacity = capacity
        self._max_lanes = max_lanes
        self._drop_oldest = drop_oldest
        self._closed = False
        self._lanes: dict[str, _Lane] = {}
        self._order: list[str] = []
        self._cursor = 0
        for lane in lanes:
            self._lane(lane)

    def _lane(self, name: str) -> _Lane:
        lane = self._lanes.get(name)
        if lane is not None:
            return lane
        if len(self._lanes) >= self._max_lanes:
            raise RuntimeError(
                f"this RoundRobin already holds its ceiling of {self._max_lanes} "
                "lanes. Raise max_lanes if that is a legitimate number of "
                "producers, and check where the lane names come from if it is not "
                "-- a lane name taken from a request is an unbounded allocation."
            )
        lane = _Lane(self._capacity, drop_oldest=self._drop_oldest)
        lane.scheduler = self
        # A lane created while a getter is parked has to be armed on the spot,
        # or the very first item offered to a brand-new lane wakes nobody.
        lane.waiting = bool(self._active_waiters)
        self._lanes[name] = lane
        self._order.append(name)
        return lane

    @property
    def lanes(self) -> tuple[str, ...]:
        """The lane names, in rotation order."""
        return tuple(self._order)

    @property
    def capacity(self) -> int:
        """Items each lane holds before dropping. Per lane, not in total."""
        return self._capacity

    @property
    def closed(self) -> bool:
        """Whether further items are refused."""
        return self._closed

    @property
    def waiting(self) -> bool:
        """Whether a getter is parked.

        Setting it arms every lane, because a getter waits on *any* lane and the
        rings only call `_wake` for a lane that is armed.
        """
        return bool(self._active_waiters)

    @waiting.setter
    def waiting(self, value: bool) -> None:
        for lane in self._lanes.values():
            lane.waiting = value

    @property
    def offered(self) -> int:
        """Items ever offered, across every lane."""
        return sum(lane.offered for lane in self._lanes.values())

    @property
    def dropped(self) -> int:
        """Items lost to a full lane, across every lane."""
        return sum(lane.dropped for lane in self._lanes.values())

    def offer(self, lane: str, item: Any) -> bool:
        """Enqueue `item` on `lane`, reporting whether it was kept."""
        if self._closed:
            raise RuntimeError("queue is closed")
        return self._lane(lane).offer(item)

    def put_nowait(self, lane: str, item: Any) -> None:
        """Enqueue `item` on `lane`, raising `QueueFull` rather than dropping it."""
        if self._closed:
            raise RuntimeError("queue is closed")
        self._lane(lane).put_nowait(item)

    def _next_index(self) -> int | None:
        """The index of the lane the rotation would serve, or None if all empty."""
        for step in range(len(self._order)):
            index = (self._cursor + step) % len(self._order)
            if len(self._lanes[self._order[index]]):
                return index
        return None

    def get_nowait(self) -> Any:
        """The next item in the rotation, or `QueueEmpty` when every lane is empty.

        The cursor advances past whichever lane answered, so the *next* call
        starts after it. Advancing only on a hit would let one busy lane hold
        the cursor and reinvent the starvation this exists to prevent.
        """
        if not self._order:
            raise QueueEmpty("no lanes")
        index = self._next_index()
        if index is None:
            raise QueueEmpty("every lane is empty")
        item = self._lanes[self._order[index]].get_nowait()
        self._cursor = (index + 1) % len(self._order)
        return item

    def get(self) -> Any:
        """An awaitable for the next item, waiting for one if every lane is empty.

        Resolves without suspending when something is already queued, exactly as
        `Queue.get` does.
        """
        try:
            return _Ready(self.get_nowait())
        except QueueEmpty:
            return self._blocked()

    def peek(self, default: Any = None) -> Any:
        """The item the rotation would serve next, without taking it."""
        index = self._next_index() if self._order else None
        if index is None:
            return default
        return self._lanes[self._order[index]].peek(default)

    def drain(self, limit: int | None = None) -> list[Any]:
        """Take up to `limit` items, one lane at a time in rotation.

        Interleaved rather than lane-by-lane, so a batch is as fair as the
        stream: draining each lane to exhaustion in turn would hand a consumer
        one tenant's entire backlog before any of the next tenant's.
        """
        taken: list[Any] = []
        while limit is None or len(taken) < limit:
            try:
                taken.append(self.get_nowait())
            except QueueEmpty:
                break
        return taken

    def snapshot(self) -> dict[str, list[Any]]:
        """What each lane is holding, without consuming any of it."""
        return {name: lane.snapshot() for name, lane in self._lanes.items()}

    def clear(self) -> int:
        """Drop everything on every lane, returning how many went."""
        return sum(lane.clear() for lane in self._lanes.values())

    def close(self) -> None:
        """Refuse further items; draining the backlog still works."""
        self._closed = True
        for lane in self._lanes.values():
            lane.close()
        self._wake()

    def __len__(self) -> int:
        return sum(len(lane) for lane in self._lanes.values())


__all__ = [
    "Awaiting",
    "PriorityQueue",
    "Queue",
    "QueueEmpty",
    "QueueFull",
    "RoundRobin",
]
