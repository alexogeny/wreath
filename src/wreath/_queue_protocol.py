"""What a queue raises, and what a non-blocking `get` hands back.

There is exactly one `QueueEmpty` and one `QueueFull` in the process, and there
has to be: `wreath.queue` re-exports them, and `except QueueEmpty` around a ring
that raised the *other* class simply does not catch. They live here, outside
both, because `_native/queue.c` imports this module by name at init and holds
the two classes for the process lifetime. That was found the expensive way, by a
facade whose `_blocked` caught one class while the ring raised the other, which
turned a wait into an unhandled exception.

`_Ready` is here because `RoundRobin` in `wreath.queue` is a Python composition
over lanes rather than a ring, so its `get()` hands back a `_Ready` while the C
ring hands back a `_QueueValue`. One class, in the module both agree on.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class QueueEmpty(Exception):
    """Raised by `get_nowait()` when the queue holds nothing."""


class QueueFull(Exception):
    """Raised by `put_nowait()` when the queue is at capacity."""


class _Ready:
    """An awaitable that resolves to an already-available item.

    Awaiting it never suspends the calling coroutine, so a queue that already
    has an item costs no Future and no trip back through the event loop.
    Delivering the value through `StopIteration` is the awaitable protocol
    rather than a trick -- `__await__` must hand back an iterator, and an
    iterator's return value is its `StopIteration`.

    Unlike the C arm's `_QueueValue` this needs no special case for a tuple or
    an exception instance: constructing `StopIteration(value)` from Python
    always stores one argument, where `PyErr_SetObject` would have unpacked the
    first and re-raised the second.
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
