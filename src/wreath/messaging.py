"""Pub/sub + event fan-out backed by PostgreSQL — a RabbitMQ-shaped surface.

Two delivery tiers, chosen per publish/subscribe:

* **Ephemeral fan-out** (`durable=False`): `NOTIFY` to every live subscriber
  on this channel. At-most-once, sub-millisecond, no persistence — the direct
  "tell all replicas now" analogue. Payloads are bounded (`NOTIFY` caps at
  8000 bytes); oversized payloads must go durable.
* **Durable** (`durable=True`): a work-queue table consumed with
  `FOR UPDATE SKIP LOCKED` + fencing (the same machinery as `wreath.jobs`),
  with `NOTIFY` used only as a wakeup doorbell. At-least-once, replayable,
  dead-letterable.

Durable fan-out delivers one copy per subscriber *group*, and the groups are
discovered **fleet-wide**: each bus writes its durable subscriptions into a
shared `message_groups` table at startup, and every publisher reads that
table. Discovering them from local registrations instead — as this module once
did — meant a publisher deployed before its consumer, or living in a different
service, enqueued nothing for that group: no error, no dead letter, the message
simply never existed for it.

Local registrations are **unioned** with the persisted ones rather than replaced
by them, because the two failure modes are not symmetric. A duplicate copy goes
to a group that has a consumer, and durable delivery is at-least-once anyway, so
handlers are already idempotent. A lost copy is silent. The union also means a
deployment that has not applied the new table behaves exactly as it did before.

Multi-tenancy: a dedicated system schema + `tenant` column, never
`search_path` (design 01 §5).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import telemetry as _telemetry

# Re-exported under this module's historic names: the supervision moved to
# `_doorbell`, the names callers and tests already reach for did not.
from ._doorbell import BACKOFF_BASE as DOORBELL_BACKOFF_BASE  # noqa: F401
from ._doorbell import BACKOFF_CAP as DOORBELL_BACKOFF_CAP  # noqa: F401
from ._doorbell import Doorbell
from ._doorbell import delay as _doorbell_delay  # noqa: F401
from ._doorbell import sleep_or_stop as _sleep_or_stop
from ._jobcore import (
    check_notify_payload,
    compute_backoff,
    dedup_key,
    validate_identifier,
)

MessageHandler = Callable[["Message"], Awaitable[None]]

_ACK = "ack"
_RETRY = "retry"
_REJECT = "reject"

#: How often a bus re-reads the shared group registry. A group registered by a
#: newly deployed service becomes visible to an already-running publisher within
#: this long -- it is a deploy-time event, so seconds are the right unit and a
#: read on the publish path is not.
DEFAULT_GROUP_REFRESH = 30.0

#: Reconnect backoff for the doorbell's held `LISTEN` connection, re-exported
#: from `wreath._doorbell` where the supervision itself lives. The cap is
#: the default poll interval on purpose: that is how long durable consumers take
#: to notice work without a doorbell, so retrying slower than the fallback would
#: be retrying slower than the damage.


class NoSubscriberGroup(RuntimeError):
    """A durable publish found no subscriber group, and the caller wanted one.

    Only raised for `publish(..., require_group=True)`. Publishing to a
    channel nobody consumes yet is legitimate -- a producer often ships before
    its consumer -- so the default stays a counted no-op.
    """


def _validate_channel(value: str) -> str:
    """A LISTEN/NOTIFY channel is just a bounded SQL-safe identifier."""
    return validate_identifier(value, "channel")


@dataclass(slots=True)
class Message:
    """A delivered message. Durable messages honour ack/nack/reject."""

    channel: str
    group: str | None
    tenant: str
    payload: Any
    id: int | None = None
    fence: int | None = None
    #: Attempts already recorded on the row, so a retry's backoff can grow.
    attempts: int = 0
    #: The traceparent of the publish that produced this row, or `None`. Durable
    #: only: ephemeral fan-out has no row to carry one -- see `MessageBus.publish`
    #: for why that is deferred rather than absent by accident.
    trace_context: str | None = None
    _disposition: str = _ACK

    def ack(self) -> None:
        """Mark handled — the message completes (durable) / no-op (ephemeral)."""
        self._disposition = _ACK

    def nack(self) -> None:
        """Fail transiently — the durable message retries with backoff."""
        self._disposition = _RETRY

    def reject(self) -> None:
        """Fail permanently — the durable message is dead-lettered immediately."""
        self._disposition = _REJECT


@dataclass(frozen=True, slots=True)
class _Subscription:
    channel: str
    group: str | None
    handler: MessageHandler
    concurrency: int
    durable: bool
    retries: int


class MessageBus:
    """A named message bus on one application database. Obtain via
    `wreath.Wreath.messaging`."""

    def __init__(
        self,
        database: Any,
        *,
        name: str,
        workload: str = "write",
        schema: str = "wreath",
        poll_interval: float = 5.0,
        lease: float = 30.0,
        group_refresh: float = DEFAULT_GROUP_REFRESH,
    ) -> None:
        if poll_interval <= 0 or lease <= 0:
            raise ValueError("poll_interval and lease must be positive")
        if group_refresh <= 0:
            raise ValueError("group_refresh must be positive")
        self._db = database
        self._name = name
        self._schema = schema
        self._workload = workload
        self._poll = poll_interval
        self._lease = lease
        self._group_refresh = group_refresh
        self._subs: list[_Subscription] = []
        self._table = f'"{schema}".messages'
        self._groups_table = f'"{schema}".message_groups'
        self._supervisor: Any = None
        # Channels and the dispatch maps below are filled in by `start`, once
        # subscriptions are known.
        self._doorbell = Doorbell(
            database=database, workload=workload, pump=self._pump,
        )
        self._wire_to_channel: dict[str, str] = {}
        self._ephemeral_subs: dict[str, list[_Subscription]] = {}
        # One waiter per consumer; see wreath.jobs for why a single shared
        # Event loses wakes when several consumers park on it.
        self._waiters: list[asyncio.Event] = []
        self._inflight: set[asyncio.Future[Any]] = set()
        # Channel -> the groups other processes registered for it. Refreshed on
        # a timer by `_group_refresher`, never read on the publish path: a
        # query there would put a round trip in front of every durable publish,
        # and inside a caller's transaction a failing one would poison it.
        self._remote_groups: dict[str, frozenset[str]] = {}
        #: Durable publishes that found no group anywhere. Was silent; now the
        #: one remaining way to lose a message is at least countable.
        self.unrouted_publishes = 0
        #: Failed reads of / writes to the group registry. Non-zero almost
        #: always means `schema_sql()` was never applied.
        self.group_registry_errors = 0
        #: Exceptions raised by *ephemeral* subscriber callbacks. Fire-and-forget
        #: delivery has nowhere else to put them -- a durable handler's failure
        #: lands in the row's `last_error` and its retry state -- and counting
        #: them apart from `doorbell_reconnects` is the point: a bug in a
        #: handler must never read as a flapping database.
        self.handler_errors = 0
        #: Failures *after* a message was claimed -- recording its outcome, not
        #: running the handler. Non-zero means messages are being redelivered on
        #: lease expiry rather than completing.
        self.delivery_errors = 0
        #: Failed reclaims of expired leases. Counted for the same reason
        #: `wreath.jobs.JobRunner.sweep_errors` is: a sweeper that keeps failing
        #: leaves messages in `leased` forever with nothing to read, and the
        #: degradation is invisible unless it is countable.
        self.sweep_errors = 0
        #: Tri-state: None until probed, then whether this database's `messages`
        #: table has `trace_context`. See `_carries_trace`.
        self._trace_column: bool | None = None
        # A durable publish hands work to a later process exactly as an outbound
        # call hands it to another service, so the bus is a propagation seam and
        # arms the same latch. **Third arming site**, after `HTTPClient.__init__`
        # and `JobRunner.__init__`: `PROPAGATING` is a process-global that is
        # never cleared, which is fine in production and makes any *measurement*
        # order-dependent -- `_devtools/request_trace.py` sets it from the app in
        # front of it and restores it, which is what a fourth site would have to
        # keep working.
        _telemetry.propagates()

    @property
    def name(self) -> str:
        return self._name

    @property
    def _listen_conn(self) -> Any:
        """The doorbell's held connection. Delegated rather than stored, so
        there is one owner of the connection's lifetime and not two."""
        return self._doorbell.connection

    @property
    def doorbell_reconnects(self) -> int:
        """Times the doorbell's held LISTEN connection was lost, plus every
        attempt to (re)open one that failed -- including the attempt made at
        startup, since a bus that came up against a dead database has no
        doorbell either. A database that stays down keeps this climbing rather
        than reading as a single blip.

        Non-zero means ephemeral fan-out was down for at least a moment and
        those messages are gone (at-most-once has nowhere to replay from);
        climbing means it still is, and durable consumers are running on the
        poll interval. This used to be entirely invisible.

        Kept deliberately apart from `handler_errors`: a bug in a
        subscriber must never read as a flapping database.
        """
        return self._doorbell.reconnects

    async def _carries_trace(self, executor: Any) -> bool:
        """Whether this database's `messages` table has the version-2 column.

        Asked once per bus and cached. A deployment whose role cannot
        `CREATE SCHEMA` applies the DDL by hand, so there is always a window in
        which this build is newer than what the DBA applied -- and an `INSERT`
        naming a column that is not there fails the *publish*. Turning an
        observability feature into a lost message is the wrong trade, so the
        shape of the table is a precondition this checks rather than an error it
        catches: a broad `except` here would swallow a revoked grant and a
        genuine driver fault alongside the one case it means to survive.

        **It runs on the executor the statement itself will use, and never on a
        connection of its own.** That is not a tidiness: `publish(..., tx=tx)`
        is the outbox guarantee, and a probe on a second connection during the
        caller's transaction is a read outside their snapshot issued behind
        their back -- `tests/test_exactly_once.py` asserts nothing does that,
        and it was right to catch this. On the caller's own transaction it is an
        ordinary read that sees exactly what their `INSERT` will.

        `wreath.jobs` reaches the same place by a different road: it resolves
        the answer in `start` so the claim loop never asks. A bus cannot, because
        its `start` is the path three tests assert survives a database that is
        down at boot -- putting a catalog read in front of `Doorbell.open` would
        need a broad `except` on a startup path, which AGENTS.md names as the one
        place that is never the answer.
        """
        if self._trace_column is None:
            self._trace_column = bool(
                await executor.fetchval(
                    "SELECT true FROM pg_attribute a "
                    "JOIN pg_class k ON k.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = k.relnamespace "
                    # `::text` because `nspname` is `name`: without the cast
                    # PostgreSQL infers the parameter as `name` too, which
                    # the driver cannot encode. `wreath-sql-lint` SQL002.
                    "WHERE n.nspname = $1::text AND k.relname = 'messages' "
                    "AND a.attname = 'trace_context' "
                    "AND a.attnum > 0 AND NOT a.attisdropped",
                    self._schema,
                )
            )
        return self._trace_column

    def stats(self) -> dict[str, int]:
        """Every counter this bus keeps, by name.

        The counters were readable one attribute at a time, which means an
        exporter has to know each name and gains nothing when one is added. This
        is the shape a metrics scrape wants.
        """
        return {
            "unrouted_publishes": self.unrouted_publishes,
            "group_registry_errors": self.group_registry_errors,
            "doorbell_reconnects": self.doorbell_reconnects,
            "handler_errors": self.handler_errors,
            "delivery_errors": self.delivery_errors,
            "sweep_errors": self.sweep_errors,
        }

    def known_groups(self, channel: str) -> frozenset[str]:
        """Every durable group a publish to `channel` will reach.

        The deploy-time check: "will anything actually receive this?" is
        answerable before shipping rather than by noticing an empty queue days
        later. Reflects the registry as of the last refresh.
        """
        return frozenset(self._groups_for(channel))

    def _groups_for(self, channel: str) -> list[str]:
        """Local durable groups unioned with the ones other processes declared.

        Union, not replace, because the failures are not symmetric: a duplicate
        copy goes to a group that demonstrably has a consumer (this process
        registered it) and durable delivery is at-least-once regardless, so
        handlers already tolerate one. A missing copy is silent. The union is
        also what makes this change safe to deploy before the DDL is applied --
        with no registry table, the result is exactly what it was before.
        """
        local = {
            sub.group
            for sub in self._subs
            if sub.channel == channel and sub.durable and sub.group
        }
        return sorted(local | self._remote_groups.get(channel, frozenset()))

    def _channel_wire(self, channel: str) -> str:
        """The wire channel for a user channel name.

        Refused rather than truncated: PostgreSQL truncates silently, and two
        channels sharing a wire name means ephemeral payloads are dispatched to
        the wrong subscribers -- a delivery bug that looks like a handler bug.
        """
        wire = f"wm_{self._schema}_{channel}"
        if len(wire.encode("utf-8")) > 63:
            raise ValueError(
                f"the wire channel for {channel!r} is {len(wire.encode('utf-8'))} "
                "bytes; PostgreSQL truncates a channel name at 63, which would "
                "collide with another channel. Shorten the channel or the schema."
            )
        return wire

    # -- registration --------------------------------------------------------

    def subscribe(
        self,
        channel: str,
        *,
        group: str | None = None,
        concurrency: int = 1,
        durable: bool = False,
        retries: int = 5,
    ) -> Callable[[MessageHandler], MessageHandler]:
        """Decorator registering `handler(message)` for `channel`.

        Durable subscriptions require a `group` (the competing-consumer set that
        shares one copy of each message); ephemeral ones ignore it.
        """
        _validate_channel(channel)
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if durable and not group:
            raise ValueError("durable subscriptions require a group")

        def register(handler: MessageHandler) -> MessageHandler:
            self._subs.append(
                _Subscription(channel=channel, group=group, handler=handler,
                              concurrency=concurrency, durable=durable, retries=retries)
            )
            return handler

        return register

    # -- publish -------------------------------------------------------------

    async def publish(
        self,
        channel: str,
        payload: Any,
        *,
        tx: Any = None,
        durable: bool = False,
        tenant: str = "",
        key: str | None = None,
        require_group: bool = False,
    ) -> None:
        """Publish `payload` (JSON-serialisable) to `channel`.

        Ephemeral (default): a single `NOTIFY` fans out to live subscribers.
        Durable: one row per subscriber group is enqueued; pass `tx` to publish
        atomically with your writes (the outbox guarantee).

        `require_group` (durable only) raises `NoSubscriberGroup` when
        no group is known for `channel` anywhere in the fleet, for the caller
        who knows a consumer must exist. Without it the publish is a counted
        no-op, because shipping a producer before its consumer is normal.

        **Trace context rides the durable row and not the ephemeral NOTIFY, and
        the asymmetry is deliberate.** A durable publish writes a row per group,
        so the calling request's `traceparent` goes on it and the consumer --
        often a different service on a different day -- runs its handler under
        that context. Ephemeral fan-out has no row: `pg_notify($1, $2)` carries
        the caller's payload *as* the message, so the only place a traceparent
        could go is inside that payload, which means wrapping every ephemeral
        message in an envelope.

        That is a **breaking change to a live wire format between processes**,
        and it is deferred rather than skipped. A rolling deploy has publishers
        on the new build and subscribers on the old one at the same time, so the
        envelope has to be versioned and the old build has to keep reading plain
        payloads -- which is a design with its own compatibility window, not a
        line of code. The 8000-byte NOTIFY bound is *not* the obstacle; a
        traceparent is 55 bytes. `docs/reference/roadmap.md` carries the row, and
        `tests/messaging/test_trace.py` pins the current format so the deferral
        stays a decision rather than becoming an omission.
        """
        _validate_channel(channel)
        if require_group and not durable:
            raise ValueError(
                "require_group applies to durable publishes; ephemeral fan-out "
                "has no groups, only whoever happens to be listening"
            )
        body = json.dumps(payload)
        if durable:
            await self._publish_durable(
                channel, body, tx=tx, tenant=tenant, key=key,
                require_group=require_group,
            )
            return
        encoded = body.encode("utf-8")
        check_notify_payload(encoded)
        wire = self._channel_wire(channel)
        if tx is not None:
            await tx.execute("SELECT pg_notify($1, $2)", wire, body)
            return
        connection = await self._db.acquire(self._workload)
        try:
            await connection.execute("SELECT pg_notify($1, $2)", wire, body)
        finally:
            await self._db.release(self._workload, connection)

    async def _publish_durable(
        self,
        channel: str,
        body: str,
        *,
        tx: Any,
        tenant: str,
        key: str | None,
        require_group: bool = False,
    ) -> None:
        # Read from the refreshed snapshot, never from the database: a query
        # here would be a round trip in front of every durable publish, and one
        # issued inside the caller's transaction would abort *their* transaction
        # if the registry table were missing.
        groups = self._groups_for(channel)
        if not groups:
            # Nobody, anywhere, consumes this channel. Legitimate before a
            # consumer ships, so still a no-op -- but counted, because this is
            # the one remaining way a durable message can vanish quietly.
            self.unrouted_publishes += 1
            if require_group:
                raise NoSubscriberGroup(
                    f"no durable subscriber group is registered for {channel!r}; "
                    "the consumer has not started against this database, or "
                    "schema_sql() was never applied"
                )
            return
        # One statement for every group. A loop of INSERTs was N round trips on
        # the publish path -- inside the caller's transaction, where each one
        # also holds their locks a little longer -- and the count grows with the
        # fleet, which is exactly when it is least affordable.
        # The traceparent only. `tracestate` is vendor routing for the *next*
        # hop of a live call; a durable message is consumed minutes or hours
        # later, so a routing hint would age in the queue exactly as it would in
        # `wreath.jobs`. `None` rather than `''` when there is nothing to carry:
        # an empty string is a value, and `WHERE trace_context IS NOT NULL` would
        # then match every message ever published.
        bound = _telemetry.outbound_context.get()
        parent = bound[0] if bound else None
        runner = tx
        connection = None
        if runner is None:
            connection = await self._db.acquire(self._workload)
            runner = connection
        try:
            await self._insert_durable(
                runner, channel, body, tenant, key, groups, parent
            )
        finally:
            if connection is not None:
                await self._db.release(self._workload, connection)

    async def _insert_durable(
        self,
        runner: Any,
        channel: str,
        body: str,
        tenant: str,
        key: str | None,
        groups: list[str],
        parent: str | None,
    ) -> None:
        # The column probe runs on `runner` -- the caller's transaction when
        # there is one -- so a durable publish never opens a connection of its
        # own behind an outbox transaction's back. See `_carries_trace`.
        carries = await self._carries_trace(runner)
        rows = []
        params: list[Any] = [channel, body, tenant]
        # One bind for the whole fan-out -- every group's row is the same
        # publish, so they share one context rather than repeating it per row --
        # and it goes *after* the per-group pairs so the `(channel, payload,
        # tenant, group, dedup, group, dedup, ...)` layout everything else reads
        # is unchanged. Its index is arithmetic rather than `len(params)`
        # because the value is appended once the loop below has finished.
        trace_mark = f", ${2 * len(groups) + 4}" if carries else ""
        for group in groups:
            dk = dedup_key(f"{channel}:{group}", key) if key is not None else None
            params.extend([group, dk])
            group_index, dedup_index = len(params) - 1, len(params)
            # A generous default attempt cap; per-subscription retries govern
            # the live consumer's backoff decisions.
            rows.append(
                f"($1, ${group_index}, $2::jsonb, $3, 'ready', now(), 6, "
                f"${dedup_index}{trace_mark})"
            )
        if carries:
            params.append(parent)
        trace_column = ", trace_context" if carries else ""
        sql = (
            f"INSERT INTO {self._table} "
            '(channel, "group", payload, tenant, state, run_at, max_attempts, '
            f"dedup_key{trace_column}) "
            f"VALUES {', '.join(rows)} "
            'ON CONFLICT (channel, "group", dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING'
        )
        await runner.execute(sql, *params)
        # One doorbell for the whole fan-out. The notification carries no
        # payload -- it only sets the consumers' wake event -- so a second one
        # says nothing new, and with fleet-wide discovery a busy channel can
        # have many groups.
        await runner.execute("SELECT pg_notify($1, '')", self._channel_wire(channel))

    # -- the shared group registry -------------------------------------------

    async def _register_groups(self) -> None:
        """Declare this process's durable groups so other publishers find them.

        Runs at `start`, not at `subscribe`: the decorator is called
        at import time, where there is no event loop and no database yet.

        Idempotent by construction. The primary key serialises workers racing to
        register the same group, and the `DO UPDATE` turns a restart into a
        heartbeat rather than a conflict -- which is what makes `seen_at`
        useful: a group nobody has re-registered in months is a decommissioned
        consumer whose queue will never drain.

        Counted rather than raised on failure, like `_refresh_groups`: the
        registry is an optimisation over local registrations, and a missing
        table must not stop a bus from starting and consuming its own work.
        """
        pairs = sorted(
            {(sub.channel, sub.group) for sub in self._subs if sub.durable and sub.group}
        )
        if not pairs:
            return
        sql = (
            f'INSERT INTO {self._groups_table} (channel, "group", bus) '
            "VALUES ($1, $2, $3) "
            'ON CONFLICT (channel, "group") DO UPDATE SET '
            "bus = excluded.bus, seen_at = now()"
        )
        try:
            connection = await self._db.acquire(self._workload)
            try:
                for channel, group in pairs:
                    await connection.execute(sql, channel, group, self._name)
            finally:
                await self._db.release(self._workload, connection)
        except Exception:  # noqa: BLE001 - see above; the bus must still start
            self.group_registry_errors += 1

    async def _deregister_groups(self) -> None:
        """Remove this process's durable groups from the shared registry.

        Run on the way out. A group that is registered and never removed keeps
        every publisher enqueueing one copy per message for a consumer that no
        longer exists -- a queue that only grows, and one nothing surfaces until
        the table does. Counted rather than raised, like the rest of the
        registry: a bus must still shut down against a database that is gone.
        """
        pairs = sorted(
            {(sub.channel, sub.group) for sub in self._subs if sub.durable and sub.group}
        )
        if not pairs:
            return
        sql = f'DELETE FROM {self._groups_table} WHERE channel=$1 AND "group"=$2 AND bus=$3'
        try:
            connection = await self._db.acquire(self._workload)
            try:
                for channel, group in pairs:
                    await connection.execute(sql, channel, group, self._name)
            finally:
                await self._db.release(self._workload, connection)
        except Exception:  # noqa: BLE001 - see above; shutdown must not fail
            self.group_registry_errors += 1

    async def prune_groups(self, *, unseen_for: float) -> None:
        """Drop registry rows nobody has re-registered in `unseen_for` seconds.

        The backstop behind `_deregister_groups`: a consumer that was
        killed rather than drained never deregistered, and `seen_at` is how that
        becomes visible. Run it from a scheduled job.
        """
        if unseen_for <= 0:
            raise ValueError("unseen_for must be positive")
        await self._exec(
            f"DELETE FROM {self._groups_table} "
            "WHERE seen_at < now() - ($1 || ' seconds')::interval",
            f"{float(unseen_for):.3f}",
        )

    async def purge(self, *, older_than: float) -> None:
        """Delete finished messages older than `older_than` seconds.

        As with `wreath.jobs.JobRunner.purge`: caller-driven, `done` and
        `dead` only, and the thing that keeps this table from being append-only.
        """
        if older_than <= 0:
            raise ValueError("older_than must be positive")
        await self._exec(
            f"DELETE FROM {self._table} WHERE state IN ('done', 'dead') "
            "AND updated_at < now() - ($1 || ' seconds')::interval",
            f"{float(older_than):.3f}",
        )

    async def _refresh_groups(self) -> None:
        """Re-read every registered group into the publish-path snapshot.

        Unfiltered on purpose: the groups worth discovering are exactly the ones
        on channels this process does *not* subscribe to, so there is nothing to
        narrow by. The table holds one row per (channel, group) across the whole
        fleet, which is tens of rows, not thousands.

        A failure leaves the previous snapshot in place and is counted rather
        than raised -- the fallback is local registrations, which is what this
        bus did before there was a registry.
        """
        sql = f'SELECT channel, "group" FROM {self._groups_table}'
        try:
            connection = await self._db.acquire(self._workload)
            try:
                rows = await connection.fetch(sql)
            finally:
                await self._db.release(self._workload, connection)
        except Exception:  # noqa: BLE001 - see above; publishing must still work
            self.group_registry_errors += 1
            return
        discovered: dict[str, set[str]] = {}
        for row in rows:
            discovered.setdefault(row["channel"], set()).add(row["group"])
        self._remote_groups = {
            channel: frozenset(groups) for channel, groups in discovered.items()
        }

    async def _group_refresher(self) -> None:
        """Keep the snapshot current, so a new service's consumer is found.

        The visibility window is `group_refresh` seconds (30 by default): a
        group registered by a service deploying now reaches an already-running
        publisher within that. Deploys are minutes apart and a publish is
        microseconds, so the timer belongs here rather than on the write path.
        """
        stopping = self._supervisor.stopping
        while not stopping.is_set():
            await _sleep_or_stop(stopping, self._group_refresh)
            if stopping.is_set():
                break
            await self._refresh_groups()

    # -- schema --------------------------------------------------------------

    def component(self) -> Any:
        """This bus's claim on the wreath schema.

        The durable queue and the group registry are wreath's furniture, not the
        application's data model, so they live in the `wreath` schema and never
        appear in the application's migration artifact. `Wreath` collects this
        during lifespan and brings it up to date before the bus starts, so the
        group registry the fan-out depends on is there rather than absent.
        """
        from .schema import Component, Step

        t = self._table
        g = self._groups_table
        return Component(
            name="messaging",
            schema=self._schema,
            relations=("messages", "message_groups"),
            steps=(
                Step(
                    version=1,
                    statements=(
                        f"CREATE TABLE IF NOT EXISTS {t} (\n"
                        "  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n"
                        "  channel text NOT NULL,\n"
                        '  "group" text NOT NULL,\n'
                        "  payload jsonb NOT NULL,\n"
                        "  tenant text NOT NULL DEFAULT '',\n"
                        "  state text NOT NULL DEFAULT 'ready',\n"
                        "  run_at timestamptz NOT NULL DEFAULT now(),\n"
                        "  attempts int NOT NULL DEFAULT 0,\n"
                        "  max_attempts int NOT NULL DEFAULT 6,\n"
                        "  lease_expiry timestamptz,\n"
                        "  owner text,\n"
                        "  fence bigint NOT NULL DEFAULT 0,\n"
                        "  dedup_key text,\n"
                        "  last_error text,\n"
                        "  created_at timestamptz NOT NULL DEFAULT now(),\n"
                        "  updated_at timestamptz NOT NULL DEFAULT now()\n"
                        ")",
                        f"CREATE INDEX IF NOT EXISTS messages_claim_idx ON {t} "
                        '(channel, "group", run_at) WHERE state = \'ready\'',
                        f"CREATE INDEX IF NOT EXISTS messages_lease_idx ON {t} "
                        "(lease_expiry) WHERE state = 'leased'",
                        f"CREATE UNIQUE INDEX IF NOT EXISTS messages_dedup_idx ON {t} "
                        '(channel, "group", dedup_key) WHERE dedup_key IS NOT NULL',
                        # Keyed on (channel, group) and not on the bus name,
                        # because that is what identifies a competing-consumer
                        # set everywhere else here -- `messages_claim_idx` and
                        # `messages_dedup_idx` use the same pair, and `_claim`
                        # filters on it. `bus` records which named bus most
                        # recently registered the group; `seen_at` is when, so a
                        # long-dead consumer is visible in a SELECT rather than
                        # only in a growing queue.
                        f"CREATE TABLE IF NOT EXISTS {g} (\n"
                        "  channel text NOT NULL,\n"
                        '  "group" text NOT NULL,\n'
                        "  bus text NOT NULL,\n"
                        "  registered_at timestamptz NOT NULL DEFAULT now(),\n"
                        "  seen_at timestamptz NOT NULL DEFAULT now(),\n"
                        '  PRIMARY KEY (channel, "group")\n'
                        ")",
                    ),
                ),
                # Version 1 is left exactly as it shipped: `wreath.schema`
                # records the version rather than the DDL, so rewriting the
                # `CREATE TABLE` would leave a cluster already at 1 without the
                # column forever. Additive, so a publisher on the previous build
                # keeps working against an upgraded database mid-rollout.
                Step(
                    version=2,
                    statements=(
                        f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS trace_context text",
                    ),
                ),
            ),
        )

    def schema_sql(self) -> str:
        """DDL for the durable messages and group-registry tables, joined.

        A derivation of `component()`, not a second copy: the statements live
        there and this joins them, so the tuple and the script cannot disagree.
        Retained for a caller applying the DDL itself; `wreath schema sql` is the
        supported spelling and `component()` is what wreath applies.
        """
        return self.component().sql()

    # -- supervised service protocol ----------------------------------------

    async def start(self, supervisor: Any) -> None:
        self._supervisor = supervisor
        # Declare what this process consumes, then learn what everyone else
        # does, before anything here can publish. Both count their own failures
        # rather than raising: the registry table is never auto-applied, and a
        # bus must still start and drain its own queue without one.
        await self._register_groups()
        await self._refresh_groups()
        # **No schema probe here, deliberately, and this is where the bus
        # departs from `wreath.jobs`.** That runner resolves the column shape in
        # `start` so its claim loop never pays for it; it can, because its probe
        # was already caught narrowly and counted for the boot case. Doing the
        # same here would put a catalog read in front of `Doorbell.open` on a
        # path three tests assert survives a database that is down at boot --
        # the failure that once cost a process its doorbell for its entire life
        # -- and the only way to keep that property would be a broad `except` on
        # a startup path, which is the one place AGENTS.md says a broad catch is
        # never the answer. `_carries_trace` is cached, so resolving it on the
        # first claim or the first durable publish costs one catalog read per
        # bus either way; a claim that fails to resolve it parks and retries
        # through machinery that already exists.
        # Spawned unconditionally, including on a bus with no subscriptions at
        # all: a service that only *publishes* is exactly the one that needs to
        # discover other services' groups.
        supervisor.spawn(f"messaging:{self._name}:groups", self._group_refresher())
        ephemeral_channels = sorted({s.channel for s in self._subs if not s.durable})
        durable_subs = [s for s in self._subs if s.durable]
        # One held connection multiplexing every channel we care about for the
        # doorbell (ephemeral delivery + durable wakeups).
        listen_channels = sorted(
            {self._channel_wire(s.channel) for s in self._subs}
        )
        if listen_channels:
            self._doorbell.channels = listen_channels
            # Map wire channel -> user channel for ephemeral dispatch, once,
            # rather than on every reconnect.
            self._wire_to_channel = {
                self._channel_wire(c): c for c in ephemeral_channels
            }
            self._ephemeral_subs = {}
            for sub in self._subs:
                if not sub.durable:
                    self._ephemeral_subs.setdefault(sub.channel, []).append(sub)
            # Connect once here so a bus starting against a healthy database is
            # listening by the time `start` returns, and spawn the loop
            # regardless: it owns every subsequent connection *including this
            # one having failed*. Spawning it only on a successful connect --
            # as this did -- meant a database that was down at boot left the
            # process with no doorbell for its entire lifetime.
            await self._doorbell.open()
            supervisor.spawn(
                f"messaging:{self._name}:doorbell",
                self._doorbell.run(supervisor.stopping),
            )
        for sub in durable_subs:
            for index in range(sub.concurrency):
                supervisor.spawn(
                    f"messaging:{self._name}:{sub.channel}:{sub.group}:{index}",
                    self._consumer(sub),
                )
            supervisor.spawn(
                f"messaging:{self._name}:{sub.channel}:{sub.group}:sweeper",
                self._sweeper(sub),
            )

    async def drain(self, deadline: float) -> None:
        await self._deregister_groups()
        loop = asyncio.get_running_loop()
        while self._inflight and loop.time() < deadline:
            # No catch, as in `wreath.jobs.JobRunner.drain`: `asyncio.wait` does
            # not raise for a task that failed, and its one documented raise --
            # an empty set -- cannot happen under the `while` above.
            await asyncio.wait(tuple(self._inflight), timeout=max(0.0, deadline - loop.time()))
        await self._doorbell.release()

    # -- loops ---------------------------------------------------------------

    async def _pump(self, connection: Any) -> None:
        """Dispatch notifications until the connection's stream ends.

        Returning is the ordinary end of a dropped connection, and
        `Doorbell` reopens on it.

        **This pump runs user code, so it catches for itself.** Dispatch errors
        are counted and stepped over: letting one out would end the loop, which
        the holder reads as a lost connection -- so a bug in a subscriber would
        reopen the database connection *and* land in `doorbell_reconnects`,
        making the two indistinguishable in the counter that exists to tell them
        apart. `JobRunner._pump` has no user code on its path and so has nothing
        to catch here; that difference is deliberate.
        """
        if connection is None:
            return
        async for note in connection.notifications():
            try:
                self._dispatch(note, self._wire_to_channel, self._ephemeral_subs)
            except Exception:  # noqa: BLE001 - user code, not the connection
                self.handler_errors += 1

    def _dispatch(
        self,
        note: Any,
        wire_to_channel: dict[str, str],
        ephemeral_subs: dict[str, list[_Subscription]],
    ) -> None:
        self._wake_consumers()  # wake durable consumers, whatever the channel was
        channel = wire_to_channel.get(note.channel)
        if channel is None:
            return
        payload: Any = None
        if note.payload:
            # Only a malformed payload is tolerated here -- a publisher on an
            # older wire format, say. Anything else is ours to hear about.
            with contextlib.suppress(ValueError, TypeError):
                payload = json.loads(note.payload)
        for sub in ephemeral_subs.get(channel, ()):  # at-most-once, fire-and-forget
            message = Message(channel=channel, group=sub.group, tenant="",
                              payload=payload)
            self._spawn_ephemeral(sub, message)

    def _spawn_ephemeral(self, sub: _Subscription, message: Message) -> None:
        async def _run() -> None:
            try:
                await sub.handler(message)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - at-most-once; nowhere to retry to
                self.handler_errors += 1
        future = asyncio.ensure_future(_run())
        self._inflight.add(future)
        future.add_done_callback(self._inflight.discard)

    async def _consumer(self, sub: _Subscription) -> None:
        stopping = self._supervisor.stopping
        wake = self._new_waiter()
        try:
            await self._consume(sub, stopping, wake)
        finally:
            self._waiters.remove(wake)

    async def _consume(
        self, sub: _Subscription, stopping: asyncio.Event, wake: asyncio.Event
    ) -> None:
        while not stopping.is_set():
            # Cleared before the claim, so a doorbell that rings mid-claim is
            # still remembered by the park below.
            wake.clear()
            try:
                claimed = await self._claim(sub)
            except Exception:  # noqa: BLE001 - a transient claim failure parks and retries
                # Broad because a claim is a database round trip and every way
                # it can fail has the same answer: park, then try again. Ending
                # the consumer instead would take the group down until a deploy.
                await self._park(wake)
                continue
            if claimed is None:
                await self._park(wake)
                continue
            if stopping.is_set():
                break
            try:
                await self._deliver(sub, claimed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - as in wreath.jobs._worker
                # The claim was guarded and the delivery was not, so a database
                # error while recording ack/retry/dead ended this consumer for
                # the life of the process. The message stays leased and the
                # sweeper reclaims it.
                self.delivery_errors += 1
                await self._park(wake)

    async def _deliver(self, sub: _Subscription, message: Message) -> None:
        # Bound *before* `ensure_future`, because that copies the current context
        # into the task: bind after it and the handler runs with an empty one.
        # Reset in a `finally` so a consumer that runs thousands of messages
        # cannot hand message N+1 the context of message N, and `None` rather
        # than not binding at all so an untraced message does not inherit
        # whatever the worker already held -- a trace naming the wrong cause is
        # worse than one naming none.
        token = _telemetry.outbound_context.set(
            (message.trace_context, "") if message.trace_context else None
        )
        try:
            await self._deliver_bound(sub, message)
        finally:
            _telemetry.outbound_context.reset(token)

    async def _deliver_bound(self, sub: _Subscription, message: Message) -> None:
        future = asyncio.ensure_future(sub.handler(message))
        self._inflight.add(future)
        errored: str | None = None
        try:
            await future
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - handler failures drive retry/dead-letter
            # The subscriber's own code. Any exception is a failed delivery and
            # feeds the retry/dead-letter machinery, which is where a durable
            # handler's failure is supposed to land -- narrowing here would let
            # an unexpected type kill the consumer instead of the message.
            errored = repr(error)
            message._disposition = _RETRY
        finally:
            self._inflight.discard(future)
        if errored is None and message._disposition == _ACK:
            await self._complete(message)
        elif message._disposition == _REJECT:
            await self._dead(message, errored or "rejected")
        else:
            await self._retry(sub, message, errored or "nacked")

    async def _claim(self, sub: _Subscription) -> Message | None:
        connection = await self._db.acquire(self._workload)
        try:
            # Returned only where the column exists, so a build newer than its
            # schema consumes untraced instead of failing on an unknown column.
            # Probed on the claim's own connection and cached, so this is one
            # catalog read for the life of the bus rather than one per message
            # -- the steady state issues no extra query at all.
            trace = ", m.trace_context" if await self._carries_trace(connection) else ""
            sql = (
                f"WITH claimable AS ( SELECT id FROM {self._table} "
                'WHERE channel=$1 AND "group"=$2 AND state=\'ready\' '
                "AND run_at <= now() "
                "ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT 1 ) "
                f"UPDATE {self._table} m SET state='leased', owner=$3, "
                "lease_expiry = now() + ($4 || ' seconds')::interval, "
                "fence = m.fence + 1, "
                "updated_at=now() FROM claimable c WHERE m.id=c.id "
                f"RETURNING m.id, m.payload, m.tenant, m.fence, m.attempts{trace}"
            )
            row = await connection.fetchrow(
                sql, sub.channel, sub.group, self._name, f"{self._lease:.3f}"
            )
        finally:
            await self._db.release(self._workload, connection)
        if row is None:
            return None
        payload = row["payload"]
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        return Message(channel=sub.channel, group=sub.group, tenant=row["tenant"],
                       payload=payload, id=row["id"], fence=row["fence"],
                       attempts=row["attempts"],
                       trace_context=row["trace_context"] if trace else None)

    async def _complete(self, message: Message) -> None:
        await self._exec(
            f"UPDATE {self._table} SET state='done', updated_at=now() "
            "WHERE id=$1 AND fence=$2",
            message.id, message.fence,
        )

    async def _dead(self, message: Message, error: str) -> None:
        await self._exec(
            f"UPDATE {self._table} SET state='dead', last_error=$3, updated_at=now() "
            "WHERE id=$1 AND fence=$2",
            message.id, message.fence, error[:2000],
        )

    async def _retry(self, sub: _Subscription, message: Message, error: str) -> None:
        # attempts is incremented in SQL so concurrent sweeps stay consistent.
        # The *delay* is computed from the attempts the claimed row carried:
        # passing a constant 1 here made every retry wait the same ~1s, so the
        # exponential backoff this calls into never actually backed off.
        delay = compute_backoff(message.attempts + 1, kind="exp", jitter=0.2)
        # `sub.retries` is the consumer's configured budget. The row's
        # `max_attempts` is the publisher's ceiling, chosen without knowing which
        # consumers exist, so the effective cap is whichever is stricter --
        # before this, `retries=` was accepted by `subscribe()` and read by
        # nothing.
        await self._exec(
            f"UPDATE {self._table} SET "
            "attempts = attempts + 1, "
            "state = CASE WHEN attempts + 1 >= LEAST(max_attempts, $5::int) "
            "THEN 'dead' ELSE 'ready' END, "
            "run_at = now() + ($3 || ' seconds')::interval, last_error=$4, "
            "owner=NULL, lease_expiry=NULL, updated_at=now() "
            "WHERE id=$1 AND fence=$2",
            message.id, message.fence, f"{delay:.3f}", error[:2000], sub.retries + 1,
        )

    async def _sweeper(self, sub: _Subscription) -> None:
        # Deliberately the same shape as `wreath.jobs.JobRunner._sweeper`: the
        # two loops do the same job one subsystem apart, and this one used to
        # swallow silently while that one counted. A reclaim that keeps failing
        # leaves messages `leased` forever with nothing to read.
        stopping = self._supervisor.stopping
        while not stopping.is_set():
            try:
                await self._reclaim_expired(sub)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a transient error must not end the loop
                self.sweep_errors += 1
            await _sleep_or_stop(stopping, self._lease)

    async def _reclaim_expired(self, sub: _Subscription) -> None:
        """Return this group's expired leases to `ready`, counting the attempt.

        Same reasoning as `wreath.jobs.JobRunner._reclaim_expired`: a
        consumer that dies mid-handler never reaches `_retry`, so a
        reclaim that did not count the attempt made a message which reliably
        kills its consumer immortal -- redelivered on every sweep, never
        dead-lettered.
        """
        await self._exec(
            f"UPDATE {self._table} SET "
            "attempts = attempts + 1, "
            "state = CASE WHEN attempts + 1 >= max_attempts THEN 'dead' ELSE 'ready' END, "
            "last_error = COALESCE(last_error, 'lease expired before completion'), "
            "owner=NULL, lease_expiry=NULL, fence=fence+1, updated_at=now() "
            'WHERE channel=$1 AND "group"=$2 AND state=\'leased\' '
            "AND lease_expiry < now()",
            sub.channel, sub.group,
        )

    async def _exec(self, sql: str, *args: Any) -> None:
        connection = await self._db.acquire(self._workload)
        try:
            await connection.execute(sql, *args)
        finally:
            await self._db.release(self._workload, connection)

    def _new_waiter(self) -> asyncio.Event:
        wake = asyncio.Event()
        self._waiters.append(wake)
        return wake

    def _wake_consumers(self) -> None:
        """Wake every parked consumer. One doorbell, every waiter."""
        for wake in tuple(self._waiters):
            wake.set()

    async def _park(self, wake: asyncio.Event) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            async with asyncio.timeout(self._poll):
                await wake.wait()


