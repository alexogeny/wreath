"""Subscribe to a query, and be told when its answer moves.

A client that renders a list has two bad options and no good one. It can poll,
which is a request per interval per client whether or not anything changed; or
it can hold a websocket and re-fetch the whole list whenever anything at all is
written, which is the same poll with extra steps. What it wants is *this query's
answer changed, here is the difference* -- and that needs something that can see
both the query and the write.

The local-first products all converged on the same architecture and all of them
sit **beside** the database: Zero replicates query results, Electric pushes
shapes, PowerSync evaluates sync rules per connected user. Because they are
outside the application, each one re-implements authorization, and the drift
between "what the sync service thinks you may see" and "what the route thinks
you may see" is the leak the whole category has fought. Wreath is inside it. A
shape is an ordinary `Select` built from the principal, evaluated by the
ordinary ORM, under the ordinary policies.

```python
from wreath.sync import Sync

photos = Sync(Photo, key=lambda row: row.id)

@photos.shape("mine")
def mine(principal):
    return (
        Photo.select()
        .where(Photo.owner_id == principal.sub)
        .order_by(Photo.taken_at.desc())
        .limit(500)
    )
```

**Read-only, deliberately.** There is no client write path here, no CRDT and no
conflict resolution. Writes go through the ordinary route, where validation,
Cedar and the ORM already live. That one restriction removes the hardest
correctness problem in the category and keeps the claim honest: this is
*subscribe to a query*, not a distributed database. The moment two clients can
write one field, Wreath owns merge semantics forever.

## The three properties that decide whether this is correct

**A shape must be bounded, and an unbounded one is refused when it is
declared.** `limit` is not a courtesy here -- it is what makes the next property
tractable, and a shape without one is refused at import rather than discovered
in production against a table that has since grown.

**Authorization is evaluated on the change, not on the subscription.** The shape
is a closure over the principal, and it is re-run against current data every
time something moves. A subscription therefore cannot outlive the permission
that opened it: the moment a row stops matching -- because the policy changed,
because the row's owner changed, because somebody was removed from a team -- the
next evaluation simply does not return it.

**A row that leaves the shape produces a tombstone.** This is the property every
product in this category has struggled with, and the bound is what makes it
easy. Because the answer is at most `limit` rows, the subscription can hold the
key set it last sent and compare: keys that arrived are upserts, keys that left
are removals. Revocation is not a special search, it is the ordinary diff -- so
the hard case and the easy case run the same code, and there is no revocation
path that can be correct in tests and wrong in production because nobody
exercised it.

## What the doorbell is, and what it is not

`wreath._livedoc` supplies the wake-up: a bounded per-principal subscription
registry, cleanup on disconnect, and cross-worker fan-out over the message bus,
all of it already listening to `wreath._orm_events`. This module does not
re-implement any of that. The signal it delivers is **model-grained** and means
only *something moved, go and look* -- it is never the delta. The delta is the
shape, re-evaluated.

That distinction is what keeps this honest under a write that arrives on another
worker, out of order, or twice: none of those can produce a wrong answer,
because the answer is always recomputed rather than patched.

## The limit this ships with, stated plainly

**A reconnecting client is sent a fresh snapshot, not a resumed delta.** The
held key set lives in the subscription, and a subscription ends when the
connection does, so a server that has forgotten you cannot tell you what you
missed. Sending the snapshot again is correct -- it carries the authoritative
key set, so the client drops whatever is no longer in it, which is the tombstone
rule applied wholesale -- and it costs one bounded query.

Resuming from a durable cursor instead would need a row-grained change feed
appended *inside* the writing transaction, which is `wreath.audit_log`'s hook
rather than one this module may add on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any, Final

from ._auth.models import qualified_identity_key, qualified_identity_value
from ._json import dumps as _json_dumps
from ._livedoc import DEFAULT_KEEPALIVE, LiveDocument, Subscription
from ._native import _core
from ._native import _postgres as _storage
from .orm.model import Model
from .response import ServerSentEvent, SSEResponse

__all__ = [
    "DEFAULT_KEEPALIVE",
    "DEFAULT_MAX_ROWS",
    "Delta",
    "Shape",
    "Snapshot",
    "Sync",
    "SyncError",
    "SyncSubscription",
    "UnboundedShape",
    "sync_events",
    "sync_stream",
]

#: The largest `limit` a shape may declare. A bound that is too generous is the
#: same defect as no bound at all -- the key set is held in memory per open
#: subscription, so a shape of a million rows is a megabyte per tab. Five
#: thousand is comfortably more than a client renders and comfortably less than
#: a leak.
DEFAULT_MAX_ROWS: Final = 5000


def _keepalive_seconds(value: Any) -> float:
    if type(value) not in (int, float) or not isfinite(value) or value <= 0:
        raise ValueError("keepalive must be finite and positive")
    return float(value)


class SyncError(RuntimeError):
    """A shape or a subscription that cannot be honoured."""


class UnboundedShape(SyncError):
    """A shape with no `limit`, refused where it was declared.

    Not a runtime failure on purpose. An unbounded shape works perfectly against
    a table with fifty rows in it and stops working, silently and much later,
    against the same table with five million -- by which time the declaration
    that caused it is a year old. Refusing at declaration puts the error next to
    the line that has to change.
    """


def _row_key(value: Any) -> str:
    """A row identity as a string, whatever the key function returned.

    Composite keys arrive as a tuple, which is the shape `_orm_primary_key`
    hands back; single keys arrive bare. Both become one string, because the
    protocol carries keys to a client that has no tuples.
    """
    if isinstance(value, tuple):
        if len(value) == 1:
            return str(value[0])
        return "".join(f"{len(text)}:{text}" for text in map(str, value))
    return str(value)


def _default_key(row: Any) -> str:
    """The primary key of an ORM row.

    The default because it is the only identity the ORM guarantees is stable
    across a reload, which is precisely what a client's local copy is keyed by.
    """
    key = row._orm_primary_key()
    if key is None:
        raise SyncError(
            f"{type(row).__name__} has no loaded primary key, so it cannot be "
            "synced; select the key column, or pass key= to Sync()"
        )
    return _row_key(key)


def _loaded_values(row: Any) -> dict[str, Any]:
    """Every loaded, non-null column on an ORM row, by its Python name.

    Unloaded columns are omitted rather than sent as null: a projection that did
    not select a column has not told us it is empty, and a client that stored
    the null would have been handed a fact the query never established.
    """
    owned = getattr(row, "_orm_loaded_values", None)
    if owned is not None:
        return owned()
    values: dict[str, Any] = {}
    for spec in type(row).__wreath_columns__:
        if not row._orm_is_loaded(spec.index):
            continue
        values[spec.python_name] = (
            None if row._orm_is_null(spec.index) else row._orm_get(spec.index)
        )
    return values


def _version_of(values: Mapping[str, Any]) -> str:
    """A digest over a row's values, for "did this row change".

    Compared, never inverted, so the cost that matters is one pass over a row
    already in memory. `blake2b` with an explicit digest size rather than
    `hash()`: the built-in is randomised per process, so two workers would
    disagree about whether a row moved and a client hopping between them would
    see spurious upserts forever.
    """
    return _core.sync_version(values)


@dataclass(frozen=True, slots=True)
class Shape:
    """One named, bounded query over a synced model.

    `build(principal)` returns the `Select`. It is called again on every
    evaluation rather than cached, which is what makes authorization current:
    a shape closing over a role, a team membership or a plan re-reads all three
    each time it runs.
    """

    name: str
    build: Callable[[Any], Any]
    limit: int

    def evaluate(self, principal: Any) -> Any:
        """The `Select` for this principal, re-derived now."""
        return self.build(principal)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The whole current answer, plus the key set that makes it authoritative.

    `keys` is not merely `[row.key for row in rows]` in disguise: it is the
    statement *these and no others*, which is what lets a client drop what it
    holds and is no longer sent. A client that applies `rows` and ignores `keys`
    has kept every row it was ever revoked.
    """

    rows: tuple[Mapping[str, Any], ...]
    keys: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"rows": list(self.rows), "keys": list(self.keys)}

    def __jsonable__(self) -> dict[str, Any]:
        # Tuples and lists have the same JSON spelling.  Keep the immutable
        # snapshot intact until egress rather than allocating two throwaway
        # lists only for the encoder to consume them immediately.
        return {"rows": self.rows, "keys": self.keys}


@dataclass(frozen=True, slots=True)
class Delta:
    """What moved between two evaluations of one shape.

    `removed` is the tombstone half and it is the half that matters. A delta
    that carries only `upserted` describes a world where nothing is ever
    revoked, which is the world every sync product's happy-path test is written
    in.
    """

    upserted: tuple[Mapping[str, Any], ...]
    removed: tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.upserted or self.removed)

    def as_dict(self) -> dict[str, Any]:
        return {"upserted": list(self.upserted), "removed": list(self.removed)}


class Sync:
    """The shapes declared over one model, and the doorbell that wakes them.

    `watch` names models *besides* this one whose writes can move an answer --
    a membership table that decides which photos are visible, a role table that
    decides who may see them. They are watched for the same reason the model
    itself is, and forgetting one is the bug where a revocation is only noticed
    the next time the row itself happens to change.

    `bus` is optional and its absence is supported: a single-worker deployment
    or a test wants the local half without a message bus behind it. With one, a
    write taken on any worker wakes the subscriptions held on every worker.

    `keepalive` is how long an idle stream waits before emitting an SSE comment.
    That comment is what stops a proxy closing the stream and how a vanished
    client is discovered, so it is a liveness setting rather than a cosmetic
    one; lower it in a test that cannot wait fifteen seconds for a tick.
    """

    __slots__ = (
        "_document",
        "_key",
        "_max_rows",
        "_model",
        "_native_snapshot",
        "_shapes",
        "_stale",
    )

    def __init__(
        self,
        model: type,
        *,
        key: Callable[[Any], Any] | None = None,
        watch: Iterable[Any] = (),
        bus: Any = None,
        channel: str | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_subscribers: int = 1024,
        max_per_principal: int = 4,
        keepalive: float = DEFAULT_KEEPALIVE,
    ) -> None:
        if type(max_rows) is not int or max_rows <= 0:
            raise ValueError("Sync(max_rows=...) must be a positive integer")
        keepalive = _keepalive_seconds(keepalive)
        self._model = model
        self._native_snapshot = (
            key is None
            and isinstance(model, type)
            and issubclass(model, Model)
            and model._orm_primary_key is Model._orm_primary_key
        )
        self._key = key or _default_key
        self._max_rows = max_rows
        self._shapes: dict[str, Shape] = {}
        self._stale = 0
        self._document = LiveDocument(
            channel=channel or f"wreath_sync_{model.__name__.lower()}",
            bus=bus,
            watch=(model, *watch),
            watch_reason="write",
            max_subscribers=max_subscribers,
            max_per_principal=max_per_principal,
            keepalive=keepalive,
        )

    @property
    def model(self) -> type:
        """The model these shapes select from."""
        return self._model

    @property
    def shapes(self) -> Mapping[str, Shape]:
        """The declared shapes, by name."""
        return dict(self._shapes)

    @property
    def document(self) -> LiveDocument:
        """The doorbell. Exposed for `close_all()` at shutdown, and for tests."""
        return self._document

    @property
    def subscribers(self) -> int:
        """How many streams are open right now, across every principal."""
        return self._document.subscribers

    @property
    def max_rows(self) -> int:
        """The largest `limit` a shape here may declare."""
        return self._max_rows

    def stale_evaluations(self) -> int:
        """Evaluations that raised and left the client holding its last answer.

        Counted rather than swallowed silently. A shape that has been raising
        since deploy -- a renamed column, a policy that now refuses -- is
        otherwise indistinguishable from a shape whose answer never changes,
        and the client sees the same thing in both cases: nothing.
        """
        return self._stale

    def shape(self, name: str) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
        """Declare a named shape. The function is returned unchanged.

        The bound is checked here, by building the shape once with a `None`
        principal. A shape whose `Select` cannot be built without a real
        principal may pass `limit` explicitly instead; see `add_shape`.
        """

        def decorate(build: Callable[[Any], Any]) -> Callable[[Any], Any]:
            self.add_shape(name, build)
            return build

        return decorate

    def add_shape(
        self, name: str, build: Callable[[Any], Any], *, limit: int | None = None
    ) -> Shape:
        """Register a shape, refusing an unbounded or over-large one.

        `limit` is normally read off the `Select` the function returns, which is
        checked once here so the error lands at declaration. Pass it explicitly
        for a shape that cannot be built without a live principal -- the bound
        is then a promise the caller makes. Every evaluation verifies that its
        query carries an equal or tighter limit before issuing database I/O.
        """
        if name in self._shapes:
            raise SyncError(f"shape {name!r} is already declared on {self._model.__name__}")
        if limit is None:
            limit = _declared_limit(name, build, self._model)
        if type(limit) is not int or limit <= 0:
            raise UnboundedShape(
                f"shape {name!r} must declare a positive limit as an integer"
            )
        if limit > self._max_rows:
            raise UnboundedShape(
                f"shape {name!r} declares limit={limit}, above this Sync's "
                f"max_rows={self._max_rows}; the key set of every open "
                "subscription is held in memory, so the bound is a memory bound"
            )
        shape = Shape(name=name, build=build, limit=limit)
        self._shapes[name] = shape
        return shape

    def get(self, name: str) -> Shape:
        """The named shape, or `SyncError` naming what is declared."""
        shape = self._shapes.get(name)
        if shape is None:
            known = ", ".join(sorted(self._shapes)) or "none"
            raise SyncError(f"no shape named {name!r}; declared: {known}")
        return shape

    async def evaluate(self, session: Any, name: str, principal: Any) -> Snapshot:
        """Run a shape now and return the whole current answer.

        The rebuilt query is refused before database I/O if it drops or widens
        the declared limit. The returned sequence is still truncated in case a
        custom session violates the query contract.
        """
        shape = self.get(name)
        select = shape.evaluate(principal)
        runtime_limit = getattr(select, "limit_", None)
        if (
            type(runtime_limit) is not int
            or runtime_limit <= 0
            or runtime_limit > shape.limit
        ):
            raise SyncError(
                f"shape {name!r} returned runtime limit={runtime_limit!r}; "
                f"it must be a positive integer no greater than its declared "
                f"limit={shape.limit}"
            )
        rows = await session.fetch(select)
        return self._snapshot(rows[: shape.limit])

    def _snapshot(self, rows: Sequence[Any]) -> Snapshot:
        if self._native_snapshot and rows:
            payload, keys = _storage.sync_snapshot_rows(rows, SyncError)
        else:
            payload = []
            key_list: list[str] = []
            for row in rows:
                values = _loaded_values(row)
                key = _row_key(self._key(row))
                key_list.append(key)
                payload.append({"key": key, "values": values})
            keys = tuple(key_list)
            payload = tuple(payload)
        duplicate = _core.first_duplicate(keys)
        if duplicate is not None:
            raise SyncError(f"duplicate row key {duplicate!r} in sync snapshot")
        return Snapshot(payload, keys)

    def subscribe(self, principal: Any, name: str) -> SyncSubscription | None:
        """A subscription over one shape, or `None` when the registry is full.

        Refused rather than evicted, for `_livedoc`'s reason: evicting somebody
        else's tab invites a reconnect that evicts the next one.
        """
        shape = self.get(name)
        slot = self._document.subscribe(_principal_id(principal))
        if slot is None:
            return None
        return SyncSubscription(self, shape, principal, slot)

    def notify_all(self, reason: str = "write") -> None:
        """Wake every subscription here, on this worker and the others.

        For a change no ORM write announces -- a policy set replaced, a feature
        flag flipped, a tenant reconfigured. The shape is re-evaluated and the
        diff is empty when nothing actually moved, so calling this
        unnecessarily costs one bounded query per open subscription and can
        never produce a wrong answer.
        """
        self._document.notify_all(reason)

    def close_all(self) -> None:
        """End every open stream. For shutdown, and for tests."""
        self._document.close_all()

    def _count_stale(self) -> None:
        self._stale += 1

    def __repr__(self) -> str:
        return (
            f"<Sync {self._model.__name__} shapes={sorted(self._shapes)} "
            f"subscribers={self.subscribers}>"
        )


def _declared_limit(name: str, build: Callable[[Any], Any], model: type) -> int:
    """The `limit` on the `Select` a shape function builds, refusing none.

    Built once with a `None` principal. A shape that dereferences the principal
    to construct its query legitimately fails here, and the error says to pass
    `limit=` rather than pretending the shape is malformed -- the bound is what
    is being established, not the query.
    """
    try:
        select = build(None)
    except Exception as error:
        raise UnboundedShape(
            f"shape {name!r} on {model.__name__} could not be built with a None "
            f"principal, so its bound could not be read ({error!r}); declare it "
            "with add_shape(..., limit=N)"
        ) from error
    limit = getattr(select, "limit_", None)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise UnboundedShape(
            f"shape {name!r} on {model.__name__} declares no limit. A shape has "
            "to be bounded: the bound is what makes a revoked row an ordinary "
            "diff instead of an unbounded search, and it is what bounds the key "
            "set held per open subscription. Add .limit(n) to the query."
        )
    return limit


def _principal_id(principal: Any) -> str:
    """The registry key for a principal, for `_livedoc`'s per-principal caps.

    `sub` first because that is what an identity carries; `id` next for a bare
    user object; the string form last so a test may subscribe as `"alice"`.
    """
    for attribute in ("sub", "id"):
        value = getattr(principal, attribute, None)
        if value is not None:
            identity_type = getattr(principal, "type", None)
            if identity_type is not None:
                return qualified_identity_key(
                    str(identity_type),
                    str(getattr(principal, "namespace", "")),
                    str(value),
                )
            return qualified_identity_value(
                str(getattr(principal, "namespace", "")), str(value)
            )
    return str(principal)


class SyncSubscription:
    """One open subscription: a shape, a principal, and the keys last sent.

    The held state is exactly two things -- the key set and a version per key --
    both bounded by the shape's `limit`. Nothing durable, and nothing that has
    to be cleaned up beyond the `_livedoc` slot, which the stream releases in
    its `finally`.
    """

    __slots__ = ("_held", "_shape", "_slot", "_sync", "principal")

    def __init__(self, sync: Sync, shape: Shape, principal: Any, slot: Subscription) -> None:
        self._sync = sync
        self._shape = shape
        self._slot = slot
        self.principal = principal
        self._held = _core.sync_state(())

    @property
    def shape(self) -> Shape:
        return self._shape

    @property
    def held(self) -> frozenset[str]:
        """The keys this client is believed to hold. Bounded by the shape."""
        return _core.sync_state_keys(self._held)

    @property
    def keepalive(self) -> float:
        """The idle tick this subscription's stream uses, from the doorbell."""
        return self._slot.document.keepalive

    @property
    def sync(self) -> Sync:
        """The registry this subscription belongs to."""
        return self._sync

    @property
    def closed(self) -> bool:
        return self._slot.closed

    async def snapshot(self, session: Any) -> Snapshot:
        """Evaluate the shape and adopt the result as what the client holds."""
        result = await self._sync.evaluate(session, self._shape.name, self.principal)
        self._held = _core.sync_state(result.rows)
        return result

    async def poll(self, session: Any) -> Delta:
        """Re-evaluate, and return what moved since the last evaluation.

        The whole of revocation is the `removed` line below. A key that is no
        longer in the answer is no longer visible to this principal, whatever
        the reason -- the row was deleted, its owner changed, the policy
        changed, or it fell out of the ordered window because something newer
        arrived. All four are the same event to a client, and all four are
        correct to send.
        """
        result = await self._sync.evaluate(session, self._shape.name, self.principal)
        self._held, upserted, removed = _core.sync_state_diff(self._held, result.rows)
        return Delta(upserted, removed)

    async def wait(self) -> bool:
        """Block until something may have moved. `False` once closed."""
        return await self._slot.wait() is not None

    def close(self) -> None:
        """Release the `_livedoc` slot. Idempotent."""
        self._slot.close()

    def __repr__(self) -> str:
        return (
            f"<SyncSubscription shape={self._shape.name!r} "
            f"held={_core.sync_state_size(self._held)} closed={self.closed}>"
        )


def _as_text(data: Any) -> str:
    encoded = _json_dumps(data)
    return encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded


async def sync_events(
    subscription: SyncSubscription,
    session_for: Callable[[], Any],
    *,
    keepalive: float | None = None,
) -> AsyncGenerator[ServerSentEvent]:
    """Stream a shape: one `snapshot`, then a `delta` whenever it moves.

    `session_for()` supplies a **fresh** ORM session per evaluation, as an async
    context manager. A long-lived stream must not hold a database connection
    while it is idle -- that is how a hundred open tabs exhaust a pool of
    twenty -- and it must not hold a snapshot either, because a session that
    opened before the write it is about to be told about would re-read the same
    stale rows out of its identity map and report that nothing changed.

    An evaluation that raises does not end the stream. The client keeps the last
    answer it was given, which is the same position it is in between two
    unrelated writes, and the failure is counted on the `Sync`
    (`stale_evaluations()`) so a shape that has been broken since deploy is
    visible as a number rather than as silence.
    """
    sync = subscription.sync
    try:
        if keepalive is None:
            keepalive = subscription.keepalive
        keepalive = _keepalive_seconds(keepalive)
        async with session_for() as session:
            first = await subscription.snapshot(session)
        yield ServerSentEvent(data=_as_text(first.as_dict()), event="snapshot")
        while True:
            try:
                moved = await asyncio.wait_for(subscription.wait(), keepalive)
            except TimeoutError:
                # The comment stops a proxy closing an idle stream, and it is
                # also how a vanished client is discovered: the write fails and
                # this generator is closed.
                yield ServerSentEvent(comment="keepalive")
                continue
            if not moved:
                return  # closed: shutdown, or the registry let this one go
            try:
                async with session_for() as session:
                    delta = await subscription.poll(session)
            except Exception:  # noqa: BLE001 - counted below; see the docstring
                # Broad on purpose and counted, per the `MessageBus` reference:
                # what a shape can raise is the caller's query, the caller's
                # policy and the driver, and none of them should end a stream
                # that will evaluate correctly again on the next write.
                sync._count_stale()
                continue
            if delta:
                yield ServerSentEvent(data=_as_text(delta.as_dict()), event="delta")
    finally:
        subscription.close()


def sync_stream(
    subscription: SyncSubscription,
    session_for: Callable[[], Any],
    *,
    keepalive: float | None = None,
) -> SSEResponse:
    """An SSE response over `sync_events`."""
    return SSEResponse(sync_events(subscription, session_for, keepalive=keepalive))
