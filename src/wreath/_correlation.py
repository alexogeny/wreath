"""Questions in flight, bounded, with a deadline.

Two places need the same thing: send something, remember it by an identifier,
and wait for the answer that carries the identifier back. `wreath.entity` does
it across workers over the bus; `wreath.websocket.Calls` does it down one
socket. The mechanism is identical and the failure modes are the ones that are
easy to get wrong twice:

* an answer for an identifier nobody is waiting on is **ordinary**, not an
  error -- a shared channel carries other callers' replies, and a superseded
  peer can answer late;
* settling a future twice raises `InvalidStateError`, so the second answer has
  to be dropped rather than applied;
* the map is memory a *remote* party controls, so it is bounded and a refusal
  is counted rather than logged and forgotten.

Private because the shape is an implementation detail of the two public
surfaces, not a thing to build against.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

__all__ = ["Pending", "TooManyPending"]


class TooManyPending(RuntimeError):
    """The in-flight map is full and this question was refused.

    Refused rather than queued: a queue in front of a full correlation map
    turns a bounded amount of memory into an unbounded one with extra steps.
    """


class Pending:
    """Identifiers awaiting an answer, bounded and counted.

    Not thread-safe and deliberately unsynchronised: every caller is
    event-loop-local, and a lock would charge every settle for a race no reader
    has. `wreath.kv` makes the same argument for the same reason.
    """

    __slots__ = ("_futures", "_limit", "_refusals")

    def __init__(self, *, limit: int) -> None:
        if type(limit) is not int:
            raise ValueError("pending limit must be a positive integer")
        if limit < 1:
            raise ValueError("a pending map must admit at least one question")
        self._futures: dict[str, asyncio.Future[Any]] = {}
        self._limit = limit
        self._refusals = 0

    def __len__(self) -> int:
        return len(self._futures)

    @property
    def refusals(self) -> int:
        """Questions refused because the map was full."""
        return self._refusals

    @property
    def limit(self) -> int:
        """The ceiling this map admits."""
        return self._limit

    @asynccontextmanager
    async def slot(
        self, *, identifier: str | None = None, new_id: Callable[[], str] = lambda: uuid.uuid4().hex
    ) -> AsyncIterator[tuple[str, asyncio.Future[Any]]]:
        """Reserve an identifier and its future for the length of the block.

        The slot is released on exit whatever happened -- answered, timed out,
        cancelled -- because a map that leaks on the failure path fills up
        exactly when it is most needed.
        """
        if len(self._futures) >= self._limit:
            self._refusals += 1
            raise TooManyPending(
                f"{len(self._futures)} questions already in flight (limit={self._limit}); "
                f"refusing rather than growing a map a remote party controls"
            )
        key = identifier if identifier is not None else new_id()
        if key in self._futures:
            raise ValueError(f"identifier {key!r} is already in flight")
        waiter: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._futures[key] = waiter
        try:
            yield key, waiter
        finally:
            self._futures.pop(key, None)

    def settle(self, identifier: str, result: Any) -> bool:
        """Deliver `result`. False when nobody was waiting, or already answered."""
        waiter = self._futures.get(identifier)
        if waiter is None or waiter.done():
            return False
        waiter.set_result(result)
        return True

    def fail(self, identifier: str, error: BaseException) -> bool:
        """Deliver `error`. False when nobody was waiting, or already answered."""
        waiter = self._futures.get(identifier)
        if waiter is None or waiter.done():
            return False
        waiter.set_exception(error)
        return True

    def fail_all(self, error: BaseException) -> int:
        """Fail every outstanding question. Returns how many were waiting.

        What a closing transport owes its callers: without it they wait out
        their individual deadlines for an answer that provably cannot arrive.
        """
        count = 0
        for waiter in list(self._futures.values()):
            if not waiter.done():
                waiter.set_exception(error)
                count += 1
        return count
