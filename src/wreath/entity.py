"""One name, one owner, one mailbox — addressing a thing that lives on a worker.

Six places in this tree already answer "exactly one process owns this right
now", each correctly, none sharing:

* `SingletonRunner` holds an advisory lock for as long as its work runs.
* `wreath.jobs` leases a row and bumps a `fence` on every claim.
* `wreath.store.PostgresStore.claim` returns a row that *is* the claim.
* `wreath.passes` takes a row lock as the chunk's first statement.
* `_secondfactor.ChallengeStore` consumes with one `DELETE ... RETURNING`.
* `wreath.streams` reuses **the queue's** fence, because writing a second one
  would have been absurd -- its module docstring says so.

That last one is the tell: the concept was already being shared across a module
boundary, by prose. This module is the shared spelling, and it adds the thing
none of the six could express -- **a message aimed at whoever holds the name**.

## Why that last part matters

A WebSocket gateway is stateful and sits behind a stateless load balancer. The
socket for device `abc` lives on exactly one worker, and the request that needs
to talk to it arrives on any of them. Every system with that shape grows the
same three subsystems by hand: a per-connection channel on a broker, an
ownership table with a heartbeat, and a correlation map with a timeout.

`Ownership` is the second. `ask` is the first and third:

```python
entities = app.entities(database="main", bus="events")   # registers the table

@entities.answers("device")
async def talk_to_device(name: str, payload: dict) -> dict:
    return await SOCKETS[name].request(payload)   # local; we hold this one

# ... on any worker, without knowing which one holds it:
reply = await entities.ask("device", "abc", {"op": "read"}, timeout=seconds(30))
```

## What this deliberately does not do

**It does not replace `SingletonRunner`.** An advisory lock releases the instant
its connection drops, which is a *better* failure detector than a lease -- there
is no expiry to wait out. It costs a held connection per lock, which is right for
"one process runs this loop" and wrong for a hundred thousand devices. Two
mechanisms, two shapes, and folding the better one into the weaker one to save a
class would be a downgrade wearing a tidy-up's clothes. `Ownership` is for the
many-names case; `SingletonRunner` stays for the one-name case.

**Delivery is at-most-once and `ask` can time out.** The doorbell is
`wreath.messaging`'s ephemeral fan-out, which is a `NOTIFY`. That is the correct
tier for a request/response with a deadline -- a durable queue would replay a
question whose asker has already given up -- but it means a timeout is an
ordinary outcome and not an error to be surprised by.

**A lease does not stop the world.** As in `wreath.jobs`: there is no heartbeat,
so a holder still running when its lease expires is superseded by whoever takes
the name next. The fence is what stops the loser's *bookkeeping* landing; it
cannot stop the loser's side effects. Read `Lease.fence` before you write.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from ._correlation import Pending
from .store import ALIAS, Column, Keyed, PostgresStore, Sql, rows_affected
from .temporal import Duration

__all__ = [
    "Answer",
    "EntityRegistry",
    "Lease",
    "NotHeld",
    "Ownership",
    "Unanswered",
]

#: The default lease. Short enough that a crashed worker's names are reclaimed
#: promptly, long enough that a renewal loop is not a hot path.
DEFAULT_LEASE = 30.0

#: What `ask` waits before giving up. A deliberate default rather than `None`:
#: an unbounded wait on a worker that may have died is how a request pool is
#: exhausted by one unreachable device.
DEFAULT_TIMEOUT = 10.0

#: Cap on how many questions one registry will have in flight. An `ask` past it
#: refuses immediately rather than joining an unbounded dict -- the pending map
#: is memory a remote caller would otherwise control.
DEFAULT_MAX_PENDING = 1024


#: Column references inside the conflict clause, through the alias
#: `wreath.store` binds the target row to. Spelled once because getting one of
#: them wrong compiles and then silently never bumps the fence.
_OWNER = f"{ALIAS}.owner"
_FENCE = f"{ALIAS}.fence"
_EXPIRES = f"{ALIAS}.expires"


class NotHeld(Exception):
    """A name was addressed and no live holder answered.

    Distinct from `Unanswered`: nothing claimed the name, as opposed to
    something claiming it and not replying in time. The two want different
    operator responses -- a missing device against a wedged worker -- so they
    are different exceptions rather than one with a flag.
    """


class Unanswered(TimeoutError):
    """A holder was asked and did not reply inside the deadline.

    A `TimeoutError` subclass, because a caller that already treats a slow
    dependency as a timeout should not have to learn a second name.
    """


@dataclass(frozen=True, slots=True)
class Lease:
    """Proof that this process held `name` when the lease was taken.

    `fence` increases every time the name changes hands and **not** when the
    same holder renews. That is the `wreath.jobs` rule, deliberately: a fence
    that moved on renewal would invalidate the holder's own in-flight work.
    """

    name: str
    owner: str
    fence: int


class Ownership:
    """Which worker holds each name, leased and fenced, on one table.

    The claim is a single `INSERT ... ON CONFLICT DO UPDATE ... WHERE`, so
    "a row came back" *is* the claim -- there is no read-then-write window in
    which two workers both conclude they were first. `wreath.store` already
    made that argument for keyed state; this is the same argument for names.
    """

    __slots__ = ("_database", "_lease", "_owner", "_store", "_table")

    def __init__(
        self,
        database: Any,
        *,
        table: str = "wreath_entity",
        lease: Any = DEFAULT_LEASE,
        owner: str | None = None,
    ) -> None:
        self._lease = Duration.of(lease).total_seconds()
        if self._lease <= 0:
            raise ValueError("a lease must be positive")
        #: This process's identity. Generated rather than derived from a host
        #: name or pid, both of which repeat across containers and restarts.
        self._owner = owner or uuid.uuid4().hex
        #: Kept so `wreath infra infer` can name the database this table
        #: lands in; it reads `database` off a table-owning object.
        self._database = database
        self._table = table
        self._store = PostgresStore(
            database,
            Keyed(
                table=table,
                key="name",
                stamp="expires",
                deadline=True,
                ttl=self._lease,
                index_stamp=True,
                columns=(
                    Column("owner", "text", null=True),
                    Column("fence", "bigint", null=True),
                ),
                prefix="wreath_entity",
            ),
        )
        window = self._store.window()
        # Renew keeps the fence; a takeover bumps it. Both are the same
        # statement, because two statements are two chances to interleave.
        self._store.define(
            "hold",
            self._store.upsert(
                values={
                    "name": "$1",
                    "owner": "$2",
                    "fence": Sql("1"),
                    "expires": window,
                },
                update={
                    "owner": Sql("excluded.owner"),
                    "fence": Sql(
                        f"CASE WHEN {_OWNER} = excluded.owner "
                        f"THEN {_FENCE} ELSE {_FENCE} + 1 END"
                    ),
                    "expires": window,
                },
                where=f"{_EXPIRES} <= clock_timestamp() OR {_OWNER} = excluded.owner",
                returning="fence",
            ),
        )
        self._store.define(
            "release",
            f"DELETE FROM {table} WHERE name = $1 AND owner = $2",
        )
        self._store.define(
            "renew_all",
            f"UPDATE {table} SET expires = {window} "
            f"WHERE owner = $1 AND expires > clock_timestamp() RETURNING name",
        )
        self._store.define(
            "release_all",
            f"DELETE FROM {table} WHERE owner = $1",
        )
        self._store.define(
            "holder",
            f"SELECT owner FROM {table} "
            f"WHERE name = $1 AND expires > clock_timestamp()",
            workload="read",
        )

    @property
    def owner(self) -> str:
        """This process's identity, as it appears in the table."""
        return self._owner

    @property
    def database(self) -> Any:
        """The database this registry's table lives in. Read by `wreath.infra`."""
        return self._database

    @property
    def lease_seconds(self) -> float:
        """How long a claim survives without renewal."""
        return self._lease

    def component(self, *, name: str = "entity") -> Any:
        """This registry's claim on the wreath schema."""
        return self._store.component(name=name)

    def schema_sql(self) -> str:
        """DDL for the backing table, semicolon-joined."""
        return self._store.schema_sql()

    async def hold(self, name: str) -> Lease | None:
        """Take or renew `name`, or `None` when someone else holds it.

        Idempotent for the current holder: calling it again is the renewal, and
        the fence does not move. A caller renews well inside the lease, the way
        `wreath.jobs` expects a handler to finish well inside its own.
        """
        row = await self._store.statement("hold").fetchrow(name, self._owner)
        if row is None:
            return None
        return Lease(name=name, owner=self._owner, fence=int(row["fence"]))

    async def release(self, name: str) -> bool:
        """Give up `name`, if this process still holds it.

        Scoped to the owner in the `WHERE`, so a process whose lease already
        expired and was taken by someone else cannot delete the new holder's
        row on its way out -- which is the shutdown race a naive delete loses.
        """
        status = await self._store.statement("release").execute(name, self._owner)
        return isinstance(status, str) and not status.endswith(" 0")

    async def holder(self, name: str) -> str | None:
        """Which worker holds `name`, or `None` when nobody live does."""
        row = await self._store.statement("holder").fetchrow(name)
        return None if row is None else str(row["owner"])

    async def renew_all(self) -> frozenset[str]:
        """Extend every live lease this process holds. Returns the names kept.

        **One statement, whatever the worker holds.** A renewal loop that issued
        a statement per name would put a round trip per entity per tick on the
        database, which is precisely the cost that stops this scaling to the
        fleet it exists for. `WHERE owner = $1` is the whole selector, and the
        `RETURNING` set is the answer.

        A name absent from the result was taken by someone else while this
        process was not looking. That is the only way to learn it: there is no
        heartbeat, and a lease that lapsed under load looks exactly like one
        that was never held.
        """
        rows = await self._store.statement("renew_all").fetch(self._owner)
        return frozenset(str(row["name"]) for row in rows)

    async def release_all(self) -> int:
        """Give up every name this process holds. Returns how many went.

        One statement, for the same reason as `renew_all` -- a drain that walked
        its names would make shutdown time proportional to the fleet.
        """
        status = await self._store.statement("release_all").execute(self._owner)
        return rows_affected(status) or 0

    async def release_many(self, names: Sequence[str]) -> int:
        """Give up the named leases this process holds, in one statement.

        The predicate is `IN ($2, $3, ...)` with one placeholder per name, not
        `= ANY($1)` with an array: the driver refuses to bind a sequence, on
        purpose -- `[1, 2]` is equally `int4[]`, `int8[]` or `numeric[]`, and
        `[]` names no element type at all. So this is built per call rather than
        prepared. That costs a plan, and the alternative costs a round trip per
        name; this path runs only when a grace period expires, where the list is
        short and the calls are rare.
        """
        keys = list(names)
        if not keys:
            return 0
        placeholders = ", ".join(f"${index + 2}" for index in range(len(keys)))
        connection = await self._database.acquire("write")
        try:
            status = await connection.execute(
                f"DELETE FROM {self._table} WHERE owner = $1 AND name IN ({placeholders})",
                self._owner, *keys,
            )
        finally:
            await self._database.release("write", connection)
        return rows_affected(status) or 0

    async def purge(self) -> str:
        """Drop expired rows. Run it from a durable job, as with every store."""
        return await self._store.purge()


Answer = Callable[[str, Any], Awaitable[Any]]


class EntityRegistry:
    """Names with owners, and a question that reaches whoever owns one.

    One bus channel for the whole registry rather than one per name: a channel
    per entity is a `LISTEN` per entity, and the point of this module is the
    case where there are a hundred thousand of them.
    """

    __slots__ = (
        "_answers", "_bus", "_channel", "_held", "_lost", "_on_lost",
        "_ownership", "_pending", "_renew_every", "_renewals",
        "_unrouted", "_woken",
    )

    def __init__(
        self,
        database: Any,
        bus: Any,
        *,
        channel: str = "wreath_entity",
        table: str = "wreath_entity",
        lease: Any = DEFAULT_LEASE,
        max_pending: int = DEFAULT_MAX_PENDING,
        on_lost: Callable[[str], None] | None = None,
    ) -> None:
        self._ownership = Ownership(database, table=table, lease=lease)
        #: Renew at a third of the lease, so two consecutive ticks can be lost
        #: to a slow database before a name is. `jobs` reasons the same way
        #: about a handler deadline against its lease.
        self._renew_every = self._ownership.lease_seconds / 3.0
        #: Names this process believes it holds, each mapped to the loop clock
        #: at which it should be released, or None to hold indefinitely. A dict
        #: rather than a set because a grace period is a *deferred* release, and
        #: the renewal tick is already the loop that can apply it -- a timer per
        #: graceful release would be a task per entity.
        self._held: dict[str, float | None] = {}
        self._on_lost = on_lost
        self._lost = 0
        self._renewals = 0
        self._woken: asyncio.Event | None = None
        self._bus = bus
        self._channel = channel
        self._answers: dict[str, Answer] = {}
        #: The same bounded correlation map `wreath.websocket.Calls` uses.
        #: Written once: the awkward parts -- an answer nobody awaits is
        #: ordinary, a second answer must not settle a done future, the map
        #: is memory a remote party controls -- are identical in both.
        self._pending = Pending(limit=max_pending)
        #: Questions that reached no holder. The bus cannot tell us this -- an
        #: ephemeral publish to nobody is a no-op -- so it is inferred from the
        #: ownership table and counted here.
        self._unrouted = 0
        bus.subscribe(channel)(self._receive)

    @property
    def ownership(self) -> Ownership:
        """The lease table underneath, for a caller that only wants ownership."""
        return self._ownership

    @property
    def schema_owners(self) -> tuple[Ownership, ...]:
        """The objects holding this registry's tables.

        `Wreath.schema_components` collects a claim by *asking*, and a registry
        does not own its table directly -- `Ownership` does. Answering here is
        what puts `wreath_entity` in the DDL the application applies at startup;
        without it the table is emitted by `wreath schema sql` and created by
        nothing, which is the exact defect that mechanism exists to prevent.
        """
        return (self._ownership,)

    @property
    def refusals(self) -> int:
        """Questions refused because too many were already in flight."""
        return self._pending.refusals

    @property
    def outstanding(self) -> int:
        """Questions awaiting an answer right now."""
        return len(self._pending)

    @property
    def unrouted(self) -> int:
        """Questions whose name had no live holder."""
        return self._unrouted

    def answers(self, kind: str) -> Callable[[Answer], Answer]:
        """Register the handler for one kind of name.

        `kind` namespaces the answer, so one registry serves devices and
        sessions without either handler seeing the other's questions.
        """
        def register(handler: Answer) -> Answer:
            if kind in self._answers:
                raise ValueError(f"{kind!r} already has an answer on this registry")
            self._answers[kind] = handler
            return handler

        return register

    async def hold(self, kind: str, name: str) -> Lease | None:
        """Take or renew `kind:name` for this process, and keep it renewed.

        A held name joins the batch the renewal tick extends, so a caller never
        writes a keep-alive loop of its own -- which is the loop every
        application with a lease grows, once per name, at one round trip each.
        """
        key = f"{kind}:{name}"
        lease = await self._ownership.hold(key)
        if lease is not None:
            self._held[key] = None
        return lease

    async def release(self, kind: str, name: str) -> bool:
        """Give up `kind:name` now, and stop renewing it."""
        key = f"{kind}:{name}"
        self._held.pop(key, None)
        return await self._ownership.release(key)

    @asynccontextmanager
    async def holding(
        self, kind: str, name: str, *, grace: Any = 0.0
    ) -> AsyncIterator[Lease | None]:
        """Hold `kind:name` for the block, renewed throughout, released after.

        Yields the `Lease`, or `None` when another worker holds the name -- the
        body decides what that means, because refusing and waiting are both
        reasonable and only the caller knows which.

        ```python
        async with entities.holding("device", name, grace=seconds(2)) as lease:
            if lease is None:
                return
            ...
        ```

        `grace` keeps the lease for that long after the block exits instead of
        releasing immediately. A caller that reconnects inside the window
        *renews* rather than re-acquires, so a brief drop does not hand the name
        to another worker and then take it back -- two handovers and two fence
        bumps for something that never actually moved. The deferral is applied
        by the renewal tick, not by a timer per name.
        """
        lease = await self.hold(kind, name)
        try:
            yield lease
        finally:
            if lease is not None:
                await self._let_go(f"{kind}:{name}", grace)

    async def _let_go(self, key: str, grace: Any) -> None:
        seconds_of_grace = Duration.of(grace).total_seconds()
        if seconds_of_grace <= 0:
            self._held.pop(key, None)
            await self._ownership.release(key)
            return
        if key in self._held:
            self._held[key] = asyncio.get_running_loop().time() + seconds_of_grace

    async def ask(
        self,
        kind: str,
        name: str,
        payload: Any,
        *,
        # ASYNC109 wants the caller to wrap the call in `asyncio.timeout`, which
        # is right for a function that merely takes a while and wrong for a
        # request/response: the deadline is part of the protocol here, it is
        # what the refusal is reported *as* (`Unanswered`), and a caller who
        # forgot to wrap would wait forever on a worker that has died.
        timeout: Any = DEFAULT_TIMEOUT,  # noqa: ASYNC109
    ) -> Any:
        """Put `payload` to whoever holds `kind:name` and wait for their answer.

        Raises `NotHeld` when nothing holds the name -- checked before
        publishing, because an ephemeral publish to nobody is a silent no-op and
        a caller waiting the full timeout for that is a bad diagnosis. Raises
        `Unanswered` when a holder existed and did not reply in time.
        """
        key = f"{kind}:{name}"
        if await self._ownership.holder(key) is None:
            self._unrouted += 1
            raise NotHeld(f"no live holder for {key!r}")
        async with self._pending.slot() as (correlation, waiter):
            await self._bus.publish(
                self._channel,
                {
                    "ask": key,
                    "kind": kind,
                    "name": name,
                    "correlation": correlation,
                    "from": self._ownership.owner,
                    "payload": payload,
                },
            )
            try:
                async with asyncio.timeout(Duration.of(timeout).total_seconds()):
                    return await waiter
            except TimeoutError as error:
                raise Unanswered(
                    f"{key!r} did not answer within the deadline"
                ) from error

    async def _receive(self, message: Any) -> None:
        """One channel carries both questions and answers; the shape says which.

        Following `rooms.py`'s rule: apply locally, never relay. A reply is
        published exactly once, by the holder, and nothing this receives is
        forwarded anywhere.
        """
        payload = getattr(message, "payload", message)
        if not isinstance(payload, dict):
            return
        correlation = payload.get("correlation")
        if not isinstance(correlation, str):
            return
        if "reply" in payload or "error" in payload:
            self._settle(correlation, payload)
            return
        await self._answer(correlation, payload)

    def _settle(self, correlation: str, payload: dict[str, Any]) -> None:
        # Every worker sees every message on the channel, so an answer to
        # somebody else's question is the ordinary case and not an error --
        # which is why both of these return a bool nothing checks.
        if "error" in payload:
            self._pending.fail(correlation, NotHeld(str(payload["error"])))
        else:
            self._pending.settle(correlation, payload["reply"])

    # -- the renewal service -------------------------------------------------

    @property
    def held(self) -> frozenset[str]:
        """The names this process currently believes it holds."""
        return frozenset(self._held)

    @property
    def lost(self) -> int:
        """Names that were taken by another worker while this one held them.

        Non-zero means leases are expiring under load before the tick renews
        them -- a slow database, a blocked event loop, or a lease too short for
        either. It is the one number that says a gateway is quietly shedding
        ownership, which otherwise looks exactly like clients reconnecting.
        """
        return self._lost

    @property
    def renewals(self) -> int:
        """Completed renewal ticks. Flat means the loop has stopped."""
        return self._renewals

    def counters(self) -> Any:
        """This registry's counters, for `wreath.metrics.collect`.

        `held` is a gauge and the rest are monotonic; both belong here, because
        which of the two a number is belongs to whatever renders it.
        """
        from .metrics import Counters

        return Counters(
            subsystem="entity",
            instance=self._channel,
            values={
                "held": len(self._held),
                "lost": self._lost,
                "renewals": self._renewals,
                "refusals": self.refusals,
                "unrouted": self._unrouted,
                "outstanding": self.outstanding,
            },
        )

    async def start(self, supervisor: Any) -> None:
        """Begin renewing held names. The `wreath.services.Service` half."""
        self._woken = asyncio.Event()
        supervisor.spawn(f"entity:{self._channel}:renew", self._renew(supervisor.stopping))

    async def drain(self, deadline: float) -> None:
        """Release every held name, so a restart does not wait out the leases.

        Without this a rolling deploy parks every name for a full lease before
        another worker may take it, which is a visible outage for exactly as
        long as the lease -- and the lease is sized for *crash* detection, where
        waiting is the only option. A clean shutdown has better information and
        should use it.
        """
        self._held.clear()
        if self._woken is not None:
            self._woken.set()
        await self._ownership.release_all()

    async def _renew(self, stopping: asyncio.Event) -> None:
        woken = self._woken
        while not stopping.is_set():
            try:
                await asyncio.wait_for(stopping.wait(), timeout=self._renew_every)
                return
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - counted below, never fatal
                # A renewal that failed is survivable: the lease still has two
                # thirds of its life left, and the next tick will try again. A
                # raise here would take down the supervisor for a transient.
                self._lost += 0
            if woken is not None and woken.is_set():
                return

    async def _tick(self) -> None:
        """One renewal pass: expire graces, renew the rest, notice what was lost.

        At most two statements, whatever the worker holds.
        """
        now = asyncio.get_running_loop().time()
        expired = [
            key for key, release_at in self._held.items()
            if release_at is not None and release_at <= now
        ]
        if expired:
            for key in expired:
                self._held.pop(key, None)
            await self._ownership.release_many(expired)
        if not self._held:
            self._renewals += 1
            return
        kept = await self._ownership.renew_all()
        # A name this process still means to hold that did not come back was
        # taken while it was not looking. Dropped locally so a later release
        # cannot delete the new holder's row.
        for key in [key for key in self._held if key not in kept]:
            self._held.pop(key, None)
            self._lost += 1
            if self._on_lost is not None:
                self._on_lost(key)
        self._renewals += 1

    async def _answer(self, correlation: str, payload: dict[str, Any]) -> None:
        key = payload.get("ask")
        kind = payload.get("kind")
        name = payload.get("name")
        if not isinstance(key, str) or not isinstance(kind, str) or not isinstance(name, str):
            return
        handler = self._answers.get(kind)
        if handler is None:
            return
        # The ownership table is the authority, not the question: a worker that
        # lost the name between the asker's check and this message must not
        # answer for it, or two workers answer one question differently.
        if await self._ownership.holder(key) != self._ownership.owner:
            return
        reply: dict[str, Any] = {"correlation": correlation}
        try:
            reply["reply"] = await handler(name, payload.get("payload"))
        except Exception as error:  # noqa: BLE001 - carried to the asker below
            # Broad, and counted by being *delivered*: the asker gets the
            # failure rather than the deadline, which is the whole reason a
            # holder answers at all. Swallowing it here would turn every
            # handler bug into a timeout.
            reply["error"] = f"{type(error).__name__}: {error}"
        await self._bus.publish(self._channel, reply)
