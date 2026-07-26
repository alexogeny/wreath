"""Pub/sub + event fan-out backed by PostgreSQL — a RabbitMQ-shaped surface.

Two delivery tiers, chosen per publish/subscribe:

* **Ephemeral fan-out** (``durable=False``): ``NOTIFY`` to every live subscriber
  on this channel. At-most-once, sub-millisecond, no persistence — the direct
  "tell all replicas now" analogue. Payloads are bounded (``NOTIFY`` caps at
  8000 bytes); oversized payloads must go durable.
* **Durable** (``durable=True``): a work-queue table consumed with
  ``FOR UPDATE SKIP LOCKED`` + fencing (the same machinery as :mod:`wreath.jobs`),
  with ``NOTIFY`` used only as a wakeup doorbell. At-least-once, replayable,
  dead-letterable.

Durable fan-out delivers one copy per subscriber *group*. Groups are discovered
from the subscriptions registered on the bus (declared in code deployed
fleet-wide); a shared cross-instance group registry is a follow-up.

Multi-tenancy: a dedicated system schema + ``tenant`` column, never
``search_path`` (design 01 §5).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

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
    :meth:`wreath.Wreath.messaging`."""

    def __init__(
        self,
        database: Any,
        *,
        name: str,
        workload: str = "write",
        schema: str = "wreath",
        poll_interval: float = 5.0,
        lease: float = 30.0,
    ) -> None:
        if poll_interval <= 0 or lease <= 0:
            raise ValueError("poll_interval and lease must be positive")
        self._db = database
        self._name = name
        self._schema = schema
        self._workload = workload
        self._poll = poll_interval
        self._lease = lease
        self._subs: list[_Subscription] = []
        self._table = f'"{schema}".messages'
        self._supervisor: Any = None
        self._listen_conn: Any = None
        self._wake = asyncio.Event()
        self._inflight: set[asyncio.Future[Any]] = set()

    @property
    def name(self) -> str:
        return self._name

    def _channel_wire(self, channel: str) -> str:
        return f"wm_{self._schema}_{channel}"[:63]

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
        """Decorator registering ``handler(message)`` for ``channel``.

        Durable subscriptions require a ``group`` (the competing-consumer set that
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
    ) -> None:
        """Publish ``payload`` (JSON-serialisable) to ``channel``.

        Ephemeral (default): a single ``NOTIFY`` fans out to live subscribers.
        Durable: one row per subscriber group is enqueued; pass ``tx`` to publish
        atomically with your writes (the outbox guarantee).
        """
        _validate_channel(channel)
        body = json.dumps(payload)
        if durable:
            await self._publish_durable(channel, body, tx=tx, tenant=tenant, key=key)
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
        self, channel: str, body: str, *, tx: Any, tenant: str, key: str | None
    ) -> None:
        groups = sorted(
            {s.group for s in self._subs if s.channel == channel and s.durable and s.group}
        )
        if not groups:
            # No durable subscriber groups known on this bus; nothing to fan out
            # to. Documented limitation: groups are discovered from registrations.
            return
        sql = (
            f"INSERT INTO {self._table} "
            '(channel, "group", payload, tenant, state, run_at, max_attempts, dedup_key) '
            "VALUES ($1, $2, $3::jsonb, $4, 'ready', now(), $5, $6) "
            'ON CONFLICT (channel, "group", dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING'
        )
        runner = tx if tx is not None else None
        connection = None
        if runner is None:
            connection = await self._db.acquire(self._workload)
            runner = connection
        try:
            for group in groups:
                dk = dedup_key(f"{channel}:{group}", key) if key is not None else None
                # A generous default attempt cap; per-subscription retries govern
                # the live consumer's backoff decisions.
                await runner.execute(sql, channel, group, body, tenant, 6, dk)
                await runner.execute("SELECT pg_notify($1, '')", self._channel_wire(channel))
        finally:
            if connection is not None:
                await self._db.release(self._workload, connection)

    # -- schema --------------------------------------------------------------

    def schema_sql(self) -> str:
        """DDL for the durable messages table. Never auto-applied."""
        t = self._table
        return (
            f'CREATE SCHEMA IF NOT EXISTS "{self._schema}";\n'
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
            ");\n"
            f"CREATE INDEX IF NOT EXISTS messages_claim_idx ON {t} "
            '(channel, "group", run_at) WHERE state = \'ready\';\n'
            f"CREATE INDEX IF NOT EXISTS messages_lease_idx ON {t} (lease_expiry) "
            "WHERE state = 'leased';\n"
            f"CREATE UNIQUE INDEX IF NOT EXISTS messages_dedup_idx ON {t} "
            '(channel, "group", dedup_key) WHERE dedup_key IS NOT NULL;\n'
        )

    # -- supervised service protocol ----------------------------------------

    async def start(self, supervisor: Any) -> None:
        self._supervisor = supervisor
        ephemeral_channels = sorted({s.channel for s in self._subs if not s.durable})
        durable_subs = [s for s in self._subs if s.durable]
        # One held connection multiplexing every channel we care about for the
        # doorbell (ephemeral delivery + durable wakeups).
        listen_channels = sorted(
            {self._channel_wire(s.channel) for s in self._subs}
        )
        if listen_channels:
            with contextlib.suppress(Exception):
                self._listen_conn = await self._db.acquire(self._workload)
                for wire in listen_channels:
                    await self._listen_conn.listen(wire)
                supervisor.spawn(f"messaging:{self._name}:doorbell",
                                 self._doorbell(ephemeral_channels))
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
        loop = asyncio.get_running_loop()
        while self._inflight and loop.time() < deadline:
            with contextlib.suppress(Exception):
                await asyncio.wait(tuple(self._inflight), timeout=max(0.0, deadline - loop.time()))
        if self._listen_conn is not None:
            with contextlib.suppress(Exception):
                await self._db.release(self._workload, self._listen_conn)
            self._listen_conn = None

    # -- loops ---------------------------------------------------------------

    async def _doorbell(self, ephemeral_channels: list[str]) -> None:
        if self._listen_conn is None:
            return
        # Map wire channel -> user channel for ephemeral dispatch.
        wire_to_channel = {self._channel_wire(c): c for c in ephemeral_channels}
        ephemeral_subs: dict[str, list[_Subscription]] = {}
        for sub in self._subs:
            if not sub.durable:
                ephemeral_subs.setdefault(sub.channel, []).append(sub)
        with contextlib.suppress(Exception):
            async for note in self._listen_conn.notifications():
                self._wake.set()  # wake durable consumers
                channel = wire_to_channel.get(note.channel)
                if channel is None:
                    continue
                payload: Any = None
                if note.payload:
                    with contextlib.suppress(Exception):
                        payload = json.loads(note.payload)
                for sub in ephemeral_subs.get(channel, ()):  # at-most-once, fire-and-forget
                    message = Message(channel=channel, group=sub.group, tenant="",
                                      payload=payload)
                    self._spawn_ephemeral(sub, message)

    def _spawn_ephemeral(self, sub: _Subscription, message: Message) -> None:
        async def _run() -> None:
            with contextlib.suppress(Exception):
                await sub.handler(message)
        future = asyncio.ensure_future(_run())
        self._inflight.add(future)
        future.add_done_callback(self._inflight.discard)

    async def _consumer(self, sub: _Subscription) -> None:
        stopping = self._supervisor.stopping
        while not stopping.is_set():
            try:
                claimed = await self._claim(sub)
            except Exception:  # noqa: BLE001
                await self._park()
                continue
            if claimed is None:
                await self._park()
                continue
            if stopping.is_set():
                break
            await self._deliver(sub, claimed)

    async def _deliver(self, sub: _Subscription, message: Message) -> None:
        future = asyncio.ensure_future(sub.handler(message))
        self._inflight.add(future)
        errored: str | None = None
        try:
            await future
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
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
        sql = (
            f"WITH claimable AS ( SELECT id FROM {self._table} "
            'WHERE channel=$1 AND "group"=$2 AND state=\'ready\' AND run_at <= now() '
            "ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT 1 ) "
            f"UPDATE {self._table} m SET state='leased', owner=$3, "
            "lease_expiry = now() + ($4 || ' seconds')::interval, fence = m.fence + 1, "
            "updated_at=now() FROM claimable c WHERE m.id=c.id "
            "RETURNING m.id, m.payload, m.tenant, m.fence"
        )
        connection = await self._db.acquire(self._workload)
        try:
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
                       payload=payload, id=row["id"], fence=row["fence"])

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
        delay = compute_backoff(1, kind="exp", jitter=0.2)
        await self._exec(
            f"UPDATE {self._table} SET "
            "attempts = attempts + 1, "
            "state = CASE WHEN attempts + 1 >= max_attempts THEN 'dead' ELSE 'ready' END, "
            "run_at = now() + ($3 || ' seconds')::interval, last_error=$4, "
            "owner=NULL, lease_expiry=NULL, updated_at=now() "
            "WHERE id=$1 AND fence=$2",
            message.id, message.fence, f"{delay:.3f}", error[:2000],
        )

    async def _sweeper(self, sub: _Subscription) -> None:
        stopping = self._supervisor.stopping
        while not stopping.is_set():
            with contextlib.suppress(Exception):
                await self._exec(
                    f"UPDATE {self._table} SET state='ready', owner=NULL, "
                    "lease_expiry=NULL, fence=fence+1, updated_at=now() "
                    'WHERE channel=$1 AND "group"=$2 AND state=\'leased\' '
                    "AND lease_expiry < now()",
                    sub.channel, sub.group,
                )
            await _sleep_or_stop(stopping, self._lease)

    async def _exec(self, sql: str, *args: Any) -> None:
        connection = await self._db.acquire(self._workload)
        try:
            await connection.execute(sql, *args)
        finally:
            await self._db.release(self._workload, connection)

    async def _park(self) -> None:
        self._wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            async with asyncio.timeout(self._poll):
                await self._wake.wait()


async def _sleep_or_stop(stopping: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        async with asyncio.timeout(seconds):
            await stopping.wait()
