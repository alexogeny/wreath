"""A per-principal document, its `ETag`, and a stream saying when to refetch.

Several features want the same shape: a document derived from *(principal,
some server state)*, an `ETag` over the same inputs so the client stops
asking, and a change signal so it knows when to ask again. The permission
manifest is the first caller. **Feature flags are the intended second one** --
which flags are on for a user is the same document with a different body -- and
that is deliberately not built here; one correct caller first.

Without the third piece the first two are a poll in disguise: a client that
cannot be told "your answer moved" either re-fetches on a timer or re-derives on
every render. What makes the signal possible at all is that Wreath can *see*
both things that move the answer -- its own policy set, and a committed write to
the table the answer is derived from (`wreath._orm_events`). An external
service can see neither.

**The correctness rule, and the reason this is safe.** Delivery is
**at-most-once**: the transport is an ephemeral `NOTIFY` and a stream can be
disconnected, so a *narrowing* change may arrive late or not at all. That is
acceptable only because the document is **chrome, not enforcement**. A stale
permission manifest can draw a button that then 403s -- a cosmetic bug -- and it
can never permit anything, because the policy is evaluated again on the request
itself. Any caller that puts a decision behind one of these documents instead of
behind the route has misused it.

Three things this owes its callers, each of which is a way to be quietly wrong:

* **Bounded subscriptions.** A registry keyed by principal that only ever grows
  is a leak, and one user with fifty tabs must not be able to fill it. Both
  caps are enforced at `LiveDocument.subscribe`, which refuses rather
  than evicts -- evicting the oldest tab invites a reconnect that evicts the
  next one.
* **Cleanup on disconnect.** The slot is released in the stream's `finally`,
  which runs when the client goes away and the generator is closed.
* **No database connection.** A stream is mostly idle and may be open for
  hours. Everything here is in-process; the client's refetch is what talks to
  the database, on a normal request.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable, Iterable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from ._busbridge import BusBridge
from ._json import dumps as _json_dumps
from ._orm_events import subscribe_writes, unsubscribe_writes
from .response import ServerSentEvent, SSEResponse

__all__ = [
    "Change",
    "LiveDocument",
    "Subscription",
    "change_events",
    "change_stream",
]

#: Gap between keep-alive comments on an idle stream. Also how long a policy-set
#: change can sit unnoticed, because the shared fingerprint is re-read on the
#: same tick -- a deploy usually replaces the workers anyway, which drops every
#: stream and makes the clients refetch.
DEFAULT_KEEPALIVE = 15.0

#: How stale the shared fingerprint may be. It is re-read once per worker rather
#: than once per stream, so a thousand open streams cost one computation a
#: second and a thousand string comparisons, not a thousand computations.
_FINGERPRINT_TTL = 1.0

#: Resolves the `ETag` to put on an event, given the reason. Returns `None`
#: when the tag it could compute would be a lie -- see `change_events`.
TagFor = Callable[[str], str | None]


def _model_name(model: Any) -> str:
    """A model name as `wreath._orm_events` announces it: the class name."""
    return model if isinstance(model, str) else getattr(model, "__name__", str(model))


def _as_text(data: Any) -> str:
    encoded = _json_dumps(data)
    return encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded


@dataclass(frozen=True, slots=True)
class Change:
    """"Your copy may be stale", with the new tag when one can be stated.

    `etag is None` means *refetch, we cannot tell you the tag* -- which is an
    honest answer rather than a degraded one, because the client refetches
    conditionally and a `304` costs almost nothing.
    """

    reason: str
    etag: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "etag": self.etag}


class Subscription:
    """One open stream's slot in a `LiveDocument`.

    Holds a single pending `Change` rather than a queue, because the
    signal is idempotent: two "you are stale" notifications mean exactly what
    one means. A slow or paused client therefore costs O(1) memory and can
    never build a backlog to be delivered late.
    """

    __slots__ = ("_closed", "_document", "_loop", "_pending", "_principal", "_wake")

    def __init__(
        self, document: LiveDocument, principal: str, loop: asyncio.AbstractEventLoop
    ) -> None:
        self._document = document
        self._principal = principal
        self._loop = loop
        self._wake = asyncio.Event()
        self._pending: Change | None = None
        self._closed = False

    @property
    def principal(self) -> str:
        return self._principal

    @property
    def document(self) -> LiveDocument:
        return self._document

    @property
    def closed(self) -> bool:
        return self._closed

    def fire(self, change: Change) -> None:
        """Mark this subscriber stale, from any loop or thread.

        A write commits wherever the ORM ran it, which is not necessarily the
        loop this stream is waiting on -- and `asyncio.Event.set` only
        wakes a waiter on its own loop. Hopping through
        `call_soon_threadsafe` is what keeps a background-thread commit from
        leaving the stream asleep until its next keep-alive.
        """
        if self._closed:
            return
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._set(change)
            return
        with contextlib.suppress(RuntimeError):  # the stream's loop is gone
            self._loop.call_soon_threadsafe(self._set, change)

    def _set(self, change: Change) -> None:
        if self._closed:
            return
        pending = self._pending
        if pending is not None and (pending.etag is None or change.etag is None):
            # Merged, and *unknown wins*. Coalescing an unknown tag into a known
            # one would let a client compare tags and skip a refetch for the
            # change we could not describe -- which is how a narrowing gets lost
            # rather than merely delayed.
            change = Change(change.reason, None)
        self._pending = change
        self._wake.set()

    async def wait(self) -> Change | None:
        """The next change, or `None` once this subscription is closed.

        A change accepted *before* the close is still delivered. Swallowing it
        would make a shutdown -- or a registry eviction -- the one way a client
        can be told nothing about a change the server already knew about.
        """
        await self._wake.wait()
        self._wake.clear()
        change, self._pending = self._pending, None
        if self._closed and change is not None:
            self._wake.set()  # re-armed, so the next call ends the stream
        return change

    def close(self) -> None:
        """Release the slot and end any waiter. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._document._release(self)
        self._wake.set()

    def __repr__(self) -> str:
        return f"<Subscription principal={self._principal!r} closed={self._closed}>"


class LiveDocument:
    """The change signal for one kind of per-principal document.

    `bus` is optional and its absence is a supported configuration: a
    single-worker deployment or a test wants the local half without a database
    behind it. With a bus, a notification raised on the worker that took the
    write reaches the worker holding the stream in one hop -- and never travels
    a second one, because `BusBridge` has no path from
    receive to publish.

    `watch` names the models whose writes make a document stale, as
    `wreath._orm_events` announces them. Those announcements are
    **model-grained, not row-grained**, so a write to the roles model wakes
    *every* subscriber rather than the one user who was promoted. That is the
    right trade here: the cost is one conditional refetch each, and a
    conditional refetch that finds nothing changed is a `304`. Row-grained
    would mean recording a read set per request to save it.

    `fingerprint` returns the part of every subscriber's tag that is *not*
    per-principal -- for permissions, the policy set and the route vocabulary.
    An open stream re-reads it on each keep-alive tick, so a policy set replaced
    in-process is noticed without anyone having to remember to call
    `notify_all` from a reload hook.
    """

    __slots__ = (
        "_bridge",
        "_by_principal",
        "_count",
        "_fingerprint",
        "_fingerprint_cache",
        "_keepalive",
        "_max_per_principal",
        "_max_subscribers",
        "_watch",
        "_watch_reason",
        "_watching",
    )

    def __init__(
        self,
        *,
        channel: str,
        bus: Any = None,
        fingerprint: Callable[[], str] | None = None,
        watch: Iterable[Any] = (),
        watch_reason: str = "write",
        max_subscribers: int = 1024,
        max_per_principal: int = 4,
        keepalive: float = DEFAULT_KEEPALIVE,
    ) -> None:
        self._by_principal: dict[str, list[Subscription]] = {}
        self._count = 0
        self._max_subscribers = max_subscribers
        self._max_per_principal = max_per_principal
        self._keepalive = keepalive
        self._fingerprint = fingerprint
        self._fingerprint_cache: tuple[float, str] | None = None
        self._watch = frozenset(_model_name(model) for model in watch)
        self._watch_reason = watch_reason
        self._watching = False
        self._bridge = BusBridge(bus, channel=channel, apply=self._apply)

    # -- introspection ---------------------------------------------------------

    @property
    def channel(self) -> str:
        return self._bridge.channel

    @property
    def attached(self) -> bool:
        """Whether there is a bus to reach the other workers through."""
        return self._bridge.attached

    @property
    def subscribers(self) -> int:
        return self._count

    @property
    def keepalive(self) -> float:
        return self._keepalive

    @property
    def watching(self) -> bool:
        """Whether this document is currently listening for ORM writes."""
        return self._watching

    # -- subscriptions ---------------------------------------------------------

    def subscribe(self, principal: str) -> Subscription | None:
        """A slot for one stream, or `None` when the registry is full.

        Refused rather than queued or evicted. A caller without a stream is not
        broken -- it still revalidates the document with `If-None-Match`,
        which is this feature minus the push -- whereas evicting somebody
        else's tab invites a reconnect that evicts the next one.
        """
        if self._count >= self._max_subscribers:
            return None
        holders = self._by_principal.get(principal)
        if holders is not None and len(holders) >= self._max_per_principal:
            return None
        subscription = Subscription(self, principal, asyncio.get_running_loop())
        if holders is None:
            self._by_principal[principal] = holders = []
        holders.append(subscription)
        self._count += 1
        if self._watch and not self._watching:
            # Registered on demand, and dropped again below when the last stream
            # closes: `_orm_events` keeps its subscribers in a process-global
            # list, so a document that registered at construction would leave an
            # entry there for every application ever built.
            subscribe_writes(self._on_write)
            self._watching = True
        return subscription

    def _release(self, subscription: Subscription) -> None:
        holders = self._by_principal.get(subscription.principal)
        if holders is None or subscription not in holders:
            return
        holders.remove(subscription)
        if not holders:
            del self._by_principal[subscription.principal]
        self._count -= 1
        if self._watching and self._count == 0:
            unsubscribe_writes(self._on_write)
            self._watching = False

    def close_all(self) -> None:
        """End every open stream. For shutdown, and for tests."""
        for holders in tuple(self._by_principal.values()):
            for subscription in tuple(holders):
                subscription.close()

    # -- notifications ---------------------------------------------------------

    def notify(self, principal: str, reason: str, *, etag: str | None = None) -> None:
        """One principal's document is stale, here and on the other workers."""
        self._deliver(principal, Change(reason, etag))
        self._publish(principal, reason, etag)

    def notify_all(self, reason: str, *, etag: str | None = None) -> None:
        """Every document is stale -- a policy reload, a deploy, a config change."""
        self._deliver(None, Change(reason, etag))
        self._publish(None, reason, etag)

    def _deliver(self, principal: str | None, change: Change) -> None:
        if principal is None:
            targets = [
                subscription
                for holders in tuple(self._by_principal.values())
                for subscription in tuple(holders)
            ]
        else:
            targets = list(self._by_principal.get(principal, ()))
        for subscription in targets:
            subscription.fire(change)

    def _publish(self, principal: str | None, reason: str, etag: str | None) -> None:
        if not self._bridge.attached:
            return
        # Deferred, never awaited: the work this describes -- a commit, a reload
        # -- has already happened, and a bus that is down must not turn it into
        # an error. The cost of a lost notification is one client holding a
        # stale document until it revalidates, which is the at-most-once
        # property this feature is designed around.
        self._bridge.publish_soon(
            {"principal": principal, "reason": reason, "etag": etag}
        )

    async def _apply(self, payload: dict[str, Any]) -> None:
        """Deliver another worker's notification. Never republished -- one hop."""
        principal = payload.get("principal")
        reason = payload.get("reason")
        etag = payload.get("etag")
        if not isinstance(reason, str):
            return
        if principal is not None and not isinstance(principal, str):
            return
        self._deliver(principal, Change(reason, etag if isinstance(etag, str) else None))

    # -- what the ORM tells us -------------------------------------------------

    def _on_write(self, model_names: frozenset[str]) -> None:
        """A committed write to a watched model makes these documents stale.

        Every subscriber, not one: the announcement carries model names and not
        row identities, so which principal was affected is not knowable from
        here (see the class docstring for why that trade is the right one).
        """
        if self._watch.isdisjoint(model_names):
            return
        self.notify_all(self._watch_reason)

    # -- the shared half of the tag -------------------------------------------

    def fingerprint(self) -> str:
        """The non-per-principal part of the tag, cached for a moment.

        `""` when the caller declared none, which turns the drift check in
        `change_events` off rather than making it lie.
        """
        if self._fingerprint is None:
            return ""
        now = monotonic()
        cached = self._fingerprint_cache
        if cached is None or now - cached[0] >= _FINGERPRINT_TTL:
            cached = (now, self._fingerprint())
            self._fingerprint_cache = cached
        return cached[1]

    def __repr__(self) -> str:
        return (
            f"<LiveDocument channel={self.channel!r} "
            f"subscribers={self._count} watching={self._watching}>"
        )


async def change_events(
    subscription: Subscription,
    *,
    tag_for: TagFor | None = None,
    event: str = "change",
    keepalive: float | None = None,
) -> AsyncGenerator[ServerSentEvent]:
    """Yield one event per change until the client or the document goes away.

    `tag_for(reason)` supplies the `ETag` for a change that did not carry
    one, and **must return `None` when it cannot state the tag truthfully**.
    That is not a formality: the permission manifest's tag covers the caller's
    roles, so a stream that learns "the roles table was written" is holding an
    identity that may already be out of date, and a tag computed from it would
    tell the client to skip exactly the refetch it needs. Saying nothing costs
    one conditional request.

    The slot is released in `finally`, so a client that disconnects -- which
    closes this generator -- frees it without anything else having to notice.
    """
    if keepalive is None:
        keepalive = subscription.document.keepalive
    document = subscription.document
    seen = document.fingerprint()
    try:
        while True:
            try:
                change = await asyncio.wait_for(subscription.wait(), keepalive)
            except TimeoutError:
                current = document.fingerprint()
                if current == seen:
                    # Idle. The comment stops a proxy from closing the stream,
                    # and it is also how a vanished client is discovered: the
                    # write fails and this generator is closed.
                    yield ServerSentEvent(comment="keepalive")
                    continue
                # The shared half of every tag moved -- a policy set replaced in
                # this process. Nobody had to call `notify_all` for that to be
                # noticed, which is one fewer hook to forget.
                change = Change("policies")
            if change is None:
                return  # closed: shutdown, or the registry let this one go
            etag = change.etag
            if etag is None and tag_for is not None:
                etag = tag_for(change.reason)
            seen = document.fingerprint()
            yield ServerSentEvent(
                data=_as_text(Change(change.reason, etag).as_dict()), event=event
            )
    finally:
        subscription.close()


def change_stream(
    subscription: Subscription,
    *,
    tag_for: TagFor | None = None,
    event: str = "change",
    keepalive: float | None = None,
) -> SSEResponse:
    """An SSE response over `change_events`."""
    return SSEResponse(
        change_events(
            subscription, tag_for=tag_for, event=event, keepalive=keepalive
        )
    )
