"""Signed inbound and outbound webhook primitives."""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import hmac
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from ._json import dumps, loads
from .binding import _body_validator
from .http_client import ClientError
from .request import Request
from .response import Response

_HEADER_ID = b"wreath-webhook-id"
_HEADER_TYPE = b"wreath-webhook-type"
_HEADER_VERSION = b"wreath-webhook-version"
_HEADER_TIMESTAMP = b"wreath-webhook-timestamp"
_HEADER_KEY_ID = b"wreath-webhook-key-id"
_HEADER_SIGNATURE = b"wreath-webhook-signature"
_HEADER_CORRELATION = b"wreath-correlation-id"
_HEADER_CAUSATION = b"wreath-causation-id"
_HEADER_RELAY_PATH = b"wreath-webhook-relay-path"
_RELAY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True, slots=True)
class WebhookEnvelope:
    id: str
    type: str
    version: str
    timestamp: datetime
    content_type: str
    body: bytes
    correlation_id: str | None = None
    causation_id: str | None = None
    ordering_key: str | None = None
    relay_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.type or not self.version:
            raise ValueError("webhook id, type, and version are required")
        # The signature base joins these with newlines, so a newline inside one
        # of them lets a single MAC cover more than one (timestamp, id, type,
        # body) split -- the fields stop being unambiguously recoverable from
        # what was signed. Refused here rather than escaped, because no real
        # event id or type contains a control character.
        for name, value in (("id", self.id), ("type", self.type),
                            ("version", self.version)):
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
                raise ValueError(f"webhook {name} contains a control character")
        if self.timestamp.tzinfo is None:
            raise ValueError("webhook timestamp must include a timezone")
        if len(self.relay_path) > 32 or any(
            not _RELAY_ID.fullmatch(item) for item in self.relay_path
        ):
            raise ValueError("webhook relay path is invalid or too long")
        if len(set(self.relay_path)) != len(self.relay_path):
            raise ValueError("webhook relay path contains a loop")


@dataclass(frozen=True, slots=True)
class WebhookLimits:
    max_body_bytes: int = 1024 * 1024
    max_headers: int = 32
    max_header_bytes: int = 16 * 1024
    max_event_id_bytes: int = 256

    def __post_init__(self) -> None:
        if min(
            self.max_body_bytes,
            self.max_headers,
            self.max_header_bytes,
            self.max_event_id_bytes,
        ) <= 0:
            raise ValueError("webhook limits must be positive")


@dataclass(frozen=True, slots=True)
class WebhookContext:
    source: str
    envelope: WebhookEnvelope
    request: Request
    session: Any | None = None


@dataclass(frozen=True, slots=True)
class WebhookDeliveryResult:
    outcome: Literal["delivered", "failed", "unknown"]
    event_id: str
    status: int | None = None
    failure: str | None = None


class HMACWebhookSigner:
    """Sign Wreath's versioned exact-body HMAC-SHA256 profile."""

    __slots__ = ("_key_id", "_keys")

    def __init__(self, keys: Mapping[str, bytes], *, key_id: str) -> None:
        if key_id not in keys:
            raise ValueError("webhook signing key id is not configured")
        if not keys[key_id]:
            raise ValueError("webhook signing key cannot be empty")
        self._keys = dict(keys)
        self._key_id = key_id

    @property
    def key_id(self) -> str:
        return self._key_id

    def headers(
        self, envelope: WebhookEnvelope, *, key_id: str | None = None
    ) -> tuple[tuple[bytes, bytes], ...]:
        selected_key = self._key_id if key_id is None else key_id
        key = self._keys.get(selected_key)
        if key is None:
            raise ValueError("recorded webhook signing key is unavailable")
        timestamp = _format_timestamp(envelope.timestamp)
        signature = hmac.new(
            key,
            _signature_base(
                timestamp,
                envelope.id,
                envelope.type,
                envelope.body,
                envelope.relay_path,
            ),
            hashlib.sha256,
        ).hexdigest()
        headers: list[tuple[bytes, bytes]] = [
            (_HEADER_ID, envelope.id.encode("utf-8")),
            (_HEADER_TYPE, envelope.type.encode("utf-8")),
            (_HEADER_VERSION, envelope.version.encode("utf-8")),
            (_HEADER_TIMESTAMP, timestamp),
            (_HEADER_KEY_ID, selected_key.encode("utf-8")),
            (_HEADER_SIGNATURE, f"v1={signature}".encode("ascii")),
        ]
        if envelope.correlation_id is not None:
            headers.append((_HEADER_CORRELATION, envelope.correlation_id.encode("utf-8")))
        if envelope.causation_id is not None:
            headers.append((_HEADER_CAUSATION, envelope.causation_id.encode("utf-8")))
        if envelope.relay_path:
            headers.append((_HEADER_RELAY_PATH, ",".join(envelope.relay_path).encode("ascii")))
        return tuple(headers)


class HMACWebhookVerifier:
    """Verify Wreath's HMAC profile with a bounded timestamp window."""

    __slots__ = ("_keys", "max_age")

    def __init__(self, keys: Mapping[str, bytes], *, max_age: float = 300.0) -> None:
        if not keys or any(not value for value in keys.values()):
            raise ValueError("at least one non-empty webhook verification key is required")
        if max_age <= 0:
            raise ValueError("webhook max_age must be positive")
        self._keys = dict(keys)
        self.max_age = max_age

    def verify(
        self,
        *,
        body: bytes,
        headers: Mapping[bytes, bytes],
        now: datetime | None = None,
    ) -> WebhookEnvelope:
        normalized = {key.lower(): value for key, value in headers.items()}
        return self._verify_normalized(body=body, headers=normalized, now=now)

    def _verify_normalized(
        self,
        *,
        body: bytes,
        headers: Mapping[bytes, bytes],
        now: datetime | None = None,
    ) -> WebhookEnvelope:
        event_id = _required_header(headers, _HEADER_ID)
        event_type = _required_header(headers, _HEADER_TYPE)
        version = _required_header(headers, _HEADER_VERSION)
        timestamp_data = _required_header(headers, _HEADER_TIMESTAMP)
        key_id_data = _required_header(headers, _HEADER_KEY_ID)
        supplied = _required_header(headers, _HEADER_SIGNATURE)
        relay_path = _parse_relay_path(headers.get(_HEADER_RELAY_PATH))
        event_id_text = event_id.decode("utf-8")
        event_type_text = event_type.decode("utf-8")
        version_text = version.decode("utf-8")
        # Checked before the MAC is computed, for the reason in
        # `WebhookEnvelope.__post_init__`: a framing character in a signed field
        # makes the split ambiguous, so it must not reach `_signature_base`.
        for name, value in (("id", event_id_text), ("type", event_type_text),
                            ("version", version_text)):
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
                raise ValueError(f"webhook {name} contains a control character")
        try:
            key_id = key_id_data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("invalid webhook key id") from error
        key = self._keys.get(key_id)
        if key is None:
            raise ValueError("unknown webhook key id")
        timestamp = _parse_timestamp(timestamp_data)
        current = datetime.now(UTC) if now is None else now.astimezone(UTC)
        if abs((current - timestamp).total_seconds()) > self.max_age:
            raise ValueError("webhook timestamp is outside the accepted window")
        expected = b"v1=" + hmac.new(
            key,
            _signature_base(
                timestamp_data,
                event_id_text,
                event_type_text,
                body,
                relay_path,
            ),
            hashlib.sha256,
        ).hexdigest().encode("ascii")
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("invalid webhook signature")
        content_type = headers.get(b"content-type", b"application/json").decode(
            "latin-1"
        )
        return WebhookEnvelope(
            id=event_id_text,
            type=event_type_text,
            version=version_text,
            timestamp=timestamp,
            content_type=content_type,
            body=body,
            correlation_id=_optional_text(headers, _HEADER_CORRELATION),
            causation_id=_optional_text(headers, _HEADER_CAUSATION),
            relay_path=relay_path,
        )


def _format_timestamp(value: datetime) -> bytes:
    utc = value.astimezone(UTC)
    return (
        utc.isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
        .encode("ascii")
    )


def _parse_timestamp(value: bytes) -> datetime:
    try:
        text = value.decode("ascii")
        suffix = "+00:00" if text.endswith("Z") else ""
        parsed = datetime.fromisoformat(text.removesuffix("Z") + suffix)
    except ValueError as error:
        raise ValueError("invalid webhook timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("webhook timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _signature_base(
    timestamp: bytes,
    event_id: str,
    event_type: str,
    body: bytes,
    relay_path: tuple[str, ...] = (),
) -> bytes:
    if relay_path:
        return b"\n".join(
            (
                b"wreath-v1-relay",
                timestamp,
                event_id.encode("utf-8"),
                event_type.encode("utf-8"),
                ",".join(relay_path).encode("ascii"),
                body,
            )
        )
    return b"\n".join(
        (
            b"wreath-v1",
            timestamp,
            event_id.encode("utf-8"),
            event_type.encode("utf-8"),
            body,
        )
    )


def _parse_relay_path(value: bytes | None) -> tuple[str, ...]:
    if value is None:
        return ()
    try:
        path = tuple(value.decode("ascii").split(","))
    except UnicodeDecodeError as error:
        raise ValueError("invalid webhook relay path") from error
    if not path or len(path) > 32 or any(not _RELAY_ID.fullmatch(item) for item in path):
        raise ValueError("invalid webhook relay path")
    if len(set(path)) != len(path):
        raise ValueError("webhook relay path contains a loop")
    return path


def _required_header(headers: Mapping[bytes, bytes], name: bytes) -> bytes:
    value = headers.get(name)
    if value is None or not value:
        raise ValueError(f"missing webhook header {name.decode('ascii')}")
    return value


def _optional_text(headers: Mapping[bytes, bytes], name: bytes) -> str | None:
    value = headers.get(name)
    return None if value is None else value.decode("utf-8")


class LocalReplayStore:
    """Bounded webhook replay protection **in one process**.

    Enough for a single worker. Behind more than one it is a fast path rather
    than the guarantee: each worker has its own view, so the same event
    delivered twice to two workers is claimed twice and handled twice. Use
    :class:`PostgresWebhookInbox` when the deduplication has to hold across
    replicas -- it is the same claim in a table every worker shares.
    """

    __slots__ = ("_entries", "_heap", "_lock", "_sequence", "max_entries", "ttl")

    def __init__(self, *, max_entries: int, ttl: float) -> None:
        if max_entries <= 0 or ttl <= 0:
            raise ValueError("replay store bounds must be positive")
        self.max_entries = max_entries
        self.ttl = ttl
        self._entries: dict[tuple[str, str], tuple[float, str]] = {}
        self._heap: list[tuple[float, int, tuple[str, str]]] = []
        self._sequence = 0
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._entries)

    async def claim(self, source: str, event_id: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        key = (source, event_id)
        async with self._lock:
            self._expire(current)
            if key in self._entries:
                return False
            while len(self._entries) >= self.max_entries:
                self._evict_one()
            expires = current + self.ttl
            self._sequence += 1
            self._entries[key] = (expires, "claimed")
            heapq.heappush(self._heap, (expires, self._sequence, key))
            return True

    async def complete(self, source: str, event_id: str, outcome: str) -> None:
        key = (source, event_id)
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries[key] = (entry[0], outcome)

    def _expire(self, now: float) -> None:
        while self._heap and self._heap[0][0] <= now:
            expires, _sequence, key = heapq.heappop(self._heap)
            entry = self._entries.get(key)
            if entry is not None and entry[0] == expires:
                del self._entries[key]

    def _evict_one(self) -> None:
        while self._heap:
            expires, _sequence, key = heapq.heappop(self._heap)
            entry = self._entries.get(key)
            if entry is not None and entry[0] == expires:
                del self._entries[key]
                return
        self._entries.clear()


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

#: A delivery nobody is waiting on any more. Written once because the bounded
#: purge and the chunked purge pass must agree about it exactly: a row that is
#: still going to be retried is not rubbish.
_SETTLED_STATES = "state IN ('delivered','failed','cancelled','unknown')"


def _retention_purge_pass(
    database: Any,
    *,
    table: str,
    key: str,
    chunk: int = 1000,
    where: str | None = None,
    within: Any = "5s",
    shift: Any = "10s",
    pace: Any = None,
    schema: str = "wreath",
) -> Any:
    """The chunked pass behind the inbox's and the outbox's retention purge.

    Both walk ``(retention_until, <primary key>)``: the retention stamp because
    that is the ordered domain the frontier lives in, and the key appended
    because two rows can share a stamp and a boundary that is not unique either
    skips its siblings or loops on them.
    """
    from .passes import ChunkedPass, DutyCycle, Key, Purge, Rows, Sealed, Table

    return ChunkedPass(
        f"purge_{table}",
        over=Table(table),
        units=Rows(
            key=(
                Key("retention_until", "timestamptz", indexed=True),
                Key(key, "text", unique=True),
            ),
            limit=chunk,
            within=within,
        ),
        frontier=Sealed(),
        work=Purge(where=where),
        pace=pace if pace is not None else DutyCycle(),
        # Stated rather than inherited. `halt` is the right default for a pass
        # with something irreversible at the end of it, because nothing should
        # be skipped by omission -- but a retention purge has no terminal step,
        # so there is no irreversible thing a skip could buy. One undeletable
        # row must not stop the inbox from being kept small forever. The hole is
        # still recorded and `wreath passes retry` still comes back for it; this
        # is the same call `keyed_purge_pass` makes for the three keyed stores,
        # and it is written out here because these two build their pass directly.
        on_chunk_failure="skip",
        shift=shift,
        schema=schema,
    )


@dataclass(frozen=True, slots=True)
class InboxClaim:
    outcome: Literal["claimed", "duplicate", "active", "failed"]
    fencing_token: int
    result_status: int | None = None


class PostgresWebhookInbox:
    """Transactional cross-replica webhook deduplication and fencing."""

    __slots__ = ("table",)

    def __init__(self, table: str = "wreath_webhook_inbox") -> None:
        if not _IDENTIFIER.fullmatch(table):
            raise ValueError("webhook inbox table must be a plain SQL identifier")
        self.table = table

    def schema_sql(self) -> str:
        table = self.table
        return (
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            "    source text NOT NULL,\n"
            "    message_id text NOT NULL,\n"
            "    payload_version text NOT NULL,\n"
            "    payload_hash bytea NOT NULL,\n"
            "    state text NOT NULL CHECK (state IN "
            "('processing','completed','failed')),\n"
            "    lease_owner text NOT NULL,\n"
            "    lease_expires_at timestamptz NOT NULL,\n"
            "    fencing_token bigint NOT NULL DEFAULT 1,\n"
            "    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
            "    completed_at timestamptz,\n"
            "    result_status integer,\n"
            "    failure_code text,\n"
            "    failure_summary text,\n"
            "    retention_until timestamptz,\n"
            "    PRIMARY KEY (source, message_id)\n"
            ");\n"
            f"CREATE INDEX IF NOT EXISTS {table}_retention_idx ON {table} "
            "(retention_until) WHERE retention_until IS NOT NULL;"
        )

    async def claim(
        self,
        session: Any,
        *,
        source: str,
        envelope: WebhookEnvelope,
        lease_owner: str,
        lease_seconds: float,
    ) -> InboxClaim:
        if lease_seconds <= 0:
            raise ValueError("webhook inbox lease_seconds must be positive")
        payload_hash = hashlib.sha256(envelope.body).digest()
        sql = (
            f"INSERT INTO {self.table} AS i "
            "(source, message_id, payload_version, payload_hash, state, "
            "lease_owner, lease_expires_at) "
            "VALUES ($1,$2,$3,$4,'processing',$5,"
            "clock_timestamp() + $6::float8 * interval '1 second') "
            "ON CONFLICT (source, message_id) DO UPDATE SET "
            "state='processing', lease_owner=EXCLUDED.lease_owner, "
            "lease_expires_at=EXCLUDED.lease_expires_at, "
            "fencing_token=i.fencing_token + 1 "
            "WHERE i.state='processing' AND i.lease_expires_at < clock_timestamp() "
            "RETURNING fencing_token"
        )
        row = await session.raw(
            sql,
            source,
            envelope.id,
            envelope.version,
            payload_hash,
            lease_owner,
            lease_seconds,
        ).fetchrow()
        if row is not None:
            return InboxClaim("claimed", int(_row_value(row, "fencing_token", 0)))
        existing = await session.raw(
            f"SELECT state, fencing_token, result_status FROM {self.table} "
            "WHERE source=$1 AND message_id=$2",
            source,
            envelope.id,
        ).fetchrow()
        if existing is None:
            raise RuntimeError("webhook inbox claim disappeared inside transaction")
        state = str(_row_value(existing, "state", 0))
        token = int(_row_value(existing, "fencing_token", 1))
        status_value = _row_value(existing, "result_status", 2)
        status = None if status_value is None else int(status_value)
        if state == "completed":
            return InboxClaim("duplicate", token, status)
        if state == "failed":
            return InboxClaim("failed", token, status)
        return InboxClaim("active", token, status)

    def purge_pass(self, database: Any, *, chunk: int = 1000, **options: Any) -> Any:
        """A recurring pass that drops inbox rows past their retention.

        The supported way to keep the inbox small::

            jobs.drive(inbox.purge_pass(db), cron="23 * * * *")

        :meth:`purge` has a chunk size and nothing else -- no cursor, so it
        starts from the beginning of the index every time; no resumption, so a
        redeploy loses where it was; and no pacing, so it competes with delivery
        for the same pool. The pass supplies all three, and keeps one
        transaction per chunk. See :mod:`wreath.passes`.
        """
        return _retention_purge_pass(
            database, table=self.table, key="message_id", chunk=chunk, **options
        )

    async def purge(self, session: Any, *, limit: int = 1000) -> int:
        """Delete up to *limit* rows past their retention, in the caller's transaction.

        One bounded chunk with no cursor, no resumption, and no pacing. It is
        the right tool when you already hold a session and want a bounded amount
        of work done right now; for keeping the table small forever, use
        :meth:`purge_pass`.
        """
        if limit <= 0:
            raise ValueError("webhook inbox purge limit must be positive")
        deleted = await session.raw(
            f"WITH expired AS (SELECT ctid FROM {self.table} "
            "WHERE retention_until < clock_timestamp() "
            "ORDER BY retention_until FOR UPDATE SKIP LOCKED LIMIT $1), "
            f"removed AS (DELETE FROM {self.table} AS i USING expired "
            "WHERE i.ctid=expired.ctid RETURNING 1) "
            "SELECT count(*) FROM removed",
            limit,
        ).fetchval()
        return int(deleted or 0)

    async def complete(
        self,
        session: Any,
        *,
        source: str,
        message_id: str,
        fencing_token: int,
        result_status: int,
    ) -> None:
        updated = await session.raw(
            f"UPDATE {self.table} SET state='completed', completed_at=clock_timestamp(), "
            "result_status=$4, lease_expires_at=clock_timestamp() "
            "WHERE source=$1 AND message_id=$2 AND fencing_token=$3 "
            "AND state='processing' RETURNING 1",
            source,
            message_id,
            fencing_token,
            result_status,
        ).fetchval()
        if updated is None:
            raise RuntimeError("stale webhook inbox fencing token")


def _row_value(row: Any, key: str, index: int) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        return row[index]


class PostgresWebhookOutbox:
    """Transactional durable intent for a supervised webhook dispatcher."""

    __slots__ = ("table",)

    def __init__(self, table: str = "wreath_webhook_outbox") -> None:
        if not _IDENTIFIER.fullmatch(table):
            raise ValueError("webhook outbox table must be a plain SQL identifier")
        self.table = table

    def schema_sql(self) -> str:
        table = self.table
        return (
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            "    delivery_id text PRIMARY KEY,\n"
            "    event_id text NOT NULL,\n"
            "    destination text NOT NULL,\n"
            "    event_type text NOT NULL,\n"
            "    event_timestamp timestamptz NOT NULL,\n"
            "    payload_version text NOT NULL,\n"
            "    payload_bytes bytea NOT NULL,\n"
            "    content_type text NOT NULL,\n"
            "    signature_profile text NOT NULL,\n"
            "    key_id text NOT NULL,\n"
            "    state text NOT NULL DEFAULT 'pending' CHECK (state IN "
            "('pending','leased','sending','delivered','retry_wait','failed',"
            "'cancelled','unknown')),\n"
            "    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),\n"
            "    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
            "    lease_owner text,\n"
            "    lease_expires_at timestamptz,\n"
            "    fencing_token bigint NOT NULL DEFAULT 0,\n"
            "    idempotency_key text NOT NULL,\n"
            "    ordering_key text,\n"
            "    correlation_id text,\n"
            "    causation_id text,\n"
            "    relay_path text NOT NULL DEFAULT '',\n"
            "    last_response_status integer,\n"
            "    last_failure_code text,\n"
            "    last_failure_summary text,\n"
            "    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),\n"
            "    last_attempt_at timestamptz,\n"
            "    completed_at timestamptz,\n"
            "    retention_until timestamptz\n"
            ");\n"
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS relay_path "
            "text NOT NULL DEFAULT '';\n"
            f"CREATE INDEX IF NOT EXISTS {table}_ready_idx ON {table} "
            "(next_attempt_at, created_at) WHERE state IN ('pending','retry_wait');\n"
            # Retention has always been read by both purges and never had an
            # index under it, so every purge was a sequential scan and a sort.
            # The chunked pass refuses to walk an unindexed key, which is how
            # this surfaced.
            f"CREATE INDEX IF NOT EXISTS {table}_retention_idx ON {table} "
            "(retention_until) WHERE retention_until IS NOT NULL;"
        )

    async def enqueue(
        self,
        session: Any,
        *,
        destination: str,
        envelope: WebhookEnvelope,
        key_id: str,
    ) -> str:
        delivery_id = uuid.uuid4().hex
        sql = (
            f"INSERT INTO {self.table} "
            "(delivery_id, event_id, destination, event_type, event_timestamp, "
            "payload_version, payload_bytes, content_type, signature_profile, key_id, "
            "idempotency_key, ordering_key, correlation_id, causation_id, relay_path) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)"
        )
        await session.raw(
            sql,
            delivery_id,
            envelope.id,
            destination,
            envelope.type,
            envelope.timestamp,
            envelope.version,
            envelope.body,
            envelope.content_type,
            "wreath-v1-hmac-sha256",
            key_id,
            envelope.id,
            envelope.ordering_key,
            envelope.correlation_id,
            envelope.causation_id,
            ",".join(envelope.relay_path),
        ).execute()
        return delivery_id

    def purge_pass(self, database: Any, *, chunk: int = 1000, **options: Any) -> Any:
        """A recurring pass that drops settled deliveries past their retention.

        The supported way to keep the outbox small::

            jobs.drive(outbox.purge_pass(db), cron="43 * * * *")

        Only rows in a settled state are eligible, exactly as :meth:`purge`
        does it -- a delivery still waiting on a retry is not rubbish. What the
        pass adds is the cursor, the resumption, and the pacing that a bare
        ``LIMIT`` does not have. See :mod:`wreath.passes`.
        """
        return _retention_purge_pass(
            database,
            table=self.table,
            key="delivery_id",
            chunk=chunk,
            where=_SETTLED_STATES,
            **options,
        )

    async def purge(self, session: Any, *, limit: int = 1000) -> int:
        """Delete up to *limit* settled rows past retention, in the caller's transaction.

        One bounded chunk with no cursor, no resumption, and no pacing; see
        :meth:`purge_pass` for the form that keeps the table small forever.
        """
        if limit <= 0:
            raise ValueError("webhook outbox purge limit must be positive")
        deleted = await session.raw(
            f"WITH expired AS (SELECT ctid FROM {self.table} "
            "WHERE retention_until < clock_timestamp() "
            f"AND {_SETTLED_STATES} "
            "ORDER BY retention_until FOR UPDATE SKIP LOCKED LIMIT $1), "
            f"removed AS (DELETE FROM {self.table} AS o USING expired "
            "WHERE o.ctid=expired.ctid RETURNING 1) "
            "SELECT count(*) FROM removed",
            limit,
        ).fetchval()
        return int(deleted or 0)

    async def claim_due(
        self,
        session: Any,
        *,
        lease_owner: str,
        lease_seconds: float,
    ) -> OutboxDelivery | None:
        if lease_seconds <= 0:
            raise ValueError("webhook outbox lease_seconds must be positive")
        table = self.table
        sql = (
            "WITH candidate AS ("
            f"SELECT delivery_id FROM {table} WHERE "
            "((state IN ('pending','retry_wait') "
            "AND next_attempt_at <= clock_timestamp()) OR "
            "(state IN ('leased','sending') "
            "AND lease_expires_at < clock_timestamp())) "
            "ORDER BY next_attempt_at, created_at FOR UPDATE SKIP LOCKED LIMIT 1"
            ") "
            f"UPDATE {table} AS o SET state='leased', attempts=o.attempts+1, "
            "lease_owner=$1, lease_expires_at=clock_timestamp() + "
            "$2::float8 * interval '1 second', fencing_token=o.fencing_token+1 "
            "FROM candidate WHERE o.delivery_id=candidate.delivery_id "
            "RETURNING o.delivery_id,o.event_id,o.destination,o.event_type,"
            "o.event_timestamp,o.payload_version,o.payload_bytes,o.content_type,"
            "o.key_id,o.attempts,o.fencing_token,o.ordering_key,"
            "o.correlation_id,o.causation_id,o.relay_path"
        )
        row = await session.raw(sql, lease_owner, lease_seconds).fetchrow()
        return None if row is None else _outbox_delivery(row)

    async def mark_sending(
        self, session: Any, delivery: OutboxDelivery
    ) -> None:
        await self._transition(
            session,
            delivery,
            "state='sending'",
            (),
        )

    async def renew_lease(
        self,
        session: Any,
        delivery: OutboxDelivery,
        *,
        lease_seconds: float,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("webhook outbox lease_seconds must be positive")
        renewed = await session.raw(
            f"UPDATE {self.table} SET lease_expires_at=clock_timestamp()+"
            "$3::float8*interval '1 second' WHERE delivery_id=$1 "
            "AND fencing_token=$2 AND state='sending' RETURNING 1",
            delivery.delivery_id,
            delivery.fencing_token,
            lease_seconds,
        ).fetchval()
        if renewed is None:
            raise RuntimeError("stale webhook outbox fencing token")

    async def mark_delivered(
        self,
        session: Any,
        delivery: OutboxDelivery,
        *,
        status: int,
    ) -> None:
        await self._transition(
            session,
            delivery,
            "state='delivered',completed_at=clock_timestamp(),"
            "last_response_status=$3,lease_owner=NULL,lease_expires_at=NULL",
            (status,),
        )

    async def mark_retry(
        self,
        session: Any,
        delivery: OutboxDelivery,
        *,
        delay: float,
        status: int | None,
        failure: str | None,
    ) -> None:
        if delay < 0:
            raise ValueError("webhook retry delay cannot be negative")
        await self._transition(
            session,
            delivery,
            "state='retry_wait',next_attempt_at=clock_timestamp()+"
            "$3::float8*interval '1 second',last_response_status=$4,"
            "last_failure_code=$5,lease_owner=NULL,lease_expires_at=NULL",
            (delay, status, _bounded_failure(failure)),
        )

    async def mark_unknown(
        self,
        session: Any,
        delivery: OutboxDelivery,
        *,
        failure: str | None,
    ) -> None:
        await self._transition(
            session,
            delivery,
            "state='unknown',completed_at=clock_timestamp(),last_failure_code=$3,"
            "lease_owner=NULL,lease_expires_at=NULL",
            (_bounded_failure(failure),),
        )

    async def mark_failed(
        self,
        session: Any,
        delivery: OutboxDelivery,
        *,
        status: int | None,
        failure: str | None,
    ) -> None:
        await self._transition(
            session,
            delivery,
            "state='failed',completed_at=clock_timestamp(),"
            "last_response_status=$3,last_failure_code=$4,"
            "lease_owner=NULL,lease_expires_at=NULL",
            (status, _bounded_failure(failure)),
        )

    async def _transition(
        self,
        session: Any,
        delivery: OutboxDelivery,
        assignment: str,
        values: tuple[Any, ...],
    ) -> None:
        updated = await session.raw(
            f"UPDATE {self.table} SET {assignment} "
            "WHERE delivery_id=$1 AND fencing_token=$2 "
            "AND state IN ('leased','sending') RETURNING 1",
            delivery.delivery_id,
            delivery.fencing_token,
            *values,
        ).fetchval()
        if updated is None:
            raise RuntimeError("stale webhook outbox fencing token")


@dataclass(frozen=True, slots=True)
class OutboxDelivery:
    delivery_id: str
    event_id: str
    destination: str
    event_type: str
    timestamp: datetime
    version: str
    body: bytes
    content_type: str
    key_id: str
    attempts: int
    fencing_token: int
    ordering_key: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    relay_path: tuple[str, ...] = ()


def _outbox_delivery(row: Any) -> OutboxDelivery:
    return OutboxDelivery(
        delivery_id=str(_row_value(row, "delivery_id", 0)),
        event_id=str(_row_value(row, "event_id", 1)),
        destination=str(_row_value(row, "destination", 2)),
        event_type=str(_row_value(row, "event_type", 3)),
        timestamp=_row_value(row, "event_timestamp", 4),
        version=str(_row_value(row, "payload_version", 5)),
        body=bytes(_row_value(row, "payload_bytes", 6)),
        content_type=str(_row_value(row, "content_type", 7)),
        key_id=str(_row_value(row, "key_id", 8)),
        attempts=int(_row_value(row, "attempts", 9)),
        fencing_token=int(_row_value(row, "fencing_token", 10)),
        ordering_key=_row_value(row, "ordering_key", 11),
        correlation_id=_row_value(row, "correlation_id", 12),
        causation_id=_row_value(row, "causation_id", 13),
        relay_path=_parse_stored_relay_path(
            _optional_row_value(row, "relay_path", 14, "")
        ),
    )


def _optional_row_value(row: Any, key: str, index: int, default: Any) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        try:
            return row[index]
        except (IndexError, KeyError, TypeError):
            return default


def _parse_stored_relay_path(value: Any) -> tuple[str, ...]:
    if value in (None, "", b""):
        return ()
    data = value.encode("ascii") if isinstance(value, str) else bytes(value)
    return _parse_relay_path(data)


def _bounded_failure(value: str | None) -> str | None:
    return None if value is None else value[:256]


WebhookHandler = Callable[[WebhookContext, Any], Awaitable[Any]]
_DEFAULT_WEBHOOK_LIMITS = WebhookLimits()


class WebhookSource:
    __slots__ = (
        "_handlers",
        "_inbox",
        "_lease_owner",
        "_lease_seconds",
        "_limits",
        "_name",
        "_replay",
        "_session_factory",
        "_verifier",
    )

    def __init__(
        self,
        app: Any,
        name: str,
        *,
        path: str,
        verifier: HMACWebhookVerifier,
        replay: LocalReplayStore | None,
        limits: WebhookLimits,
        inbox: PostgresWebhookInbox | None,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None,
        lease_owner: str,
        lease_seconds: float,
    ) -> None:
        if (inbox is None) != (session_factory is None):
            raise ValueError("durable webhook sources require inbox and session_factory")
        if inbox is not None and (not lease_owner or lease_seconds <= 0):
            raise ValueError("durable webhook source lease configuration is invalid")
        self._name = name
        self._verifier = verifier
        self._replay = replay or LocalReplayStore(max_entries=10_000, ttl=verifier.max_age)
        self._limits = limits
        self._inbox = inbox
        self._session_factory = session_factory
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._handlers: dict[str, tuple[Any, WebhookHandler]] = {}

        @app.post(path, tags=("webhooks",), summary=f"Receive {name} webhooks")
        async def receive(request: Request) -> Response:
            return await self._receive(request)

    def event(self, event_type: str, *, payload: Any) -> Callable[[WebhookHandler], WebhookHandler]:
        if not event_type:
            raise ValueError("webhook event type cannot be empty")

        def register(handler: WebhookHandler) -> WebhookHandler:
            if event_type in self._handlers:
                raise ValueError(f"duplicate webhook event handler: {event_type}")
            self._handlers[event_type] = (_body_validator(payload), handler)
            return handler

        return register

    async def _receive(self, request: Request) -> Response:
        raw_headers = request.headers
        if len(raw_headers) > self._limits.max_headers:
            return Response(status=413)
        headers: dict[bytes, bytes] = {}
        header_bytes = 0
        for name, value in raw_headers:
            header_bytes += len(name) + len(value)
            if header_bytes > self._limits.max_header_bytes:
                return Response(status=413)
            headers.setdefault(name.lower(), value)
        body = await request.body()
        if len(body) > self._limits.max_body_bytes:
            return Response(status=413)
        try:
            envelope = self._verifier._verify_normalized(body=body, headers=headers)
        except (UnicodeDecodeError, ValueError):
            return Response(status=401)
        if len(envelope.id.encode("utf-8")) > self._limits.max_event_id_bytes:
            return Response(status=400)
        registered = self._handlers.get(envelope.type)
        if registered is None:
            return Response(status=400)
        payload_validator, handler = registered
        decoded = loads(body)
        payload = payload_validator(decoded, ("body",))
        if self._inbox is not None:
            assert self._session_factory is not None
            async with self._session_factory() as session:
                async with session.begin():
                    claim = await self._inbox.claim(
                        session,
                        source=self._name,
                        envelope=envelope,
                        lease_owner=self._lease_owner,
                        lease_seconds=self._lease_seconds,
                    )
                    if claim.outcome == "duplicate":
                        return Response(status=claim.result_status or 204)
                    if claim.outcome in {"active", "failed"}:
                        return Response(status=409)
                    await handler(
                        WebhookContext(self._name, envelope, request, session), payload
                    )
                    await self._inbox.complete(
                        session,
                        source=self._name,
                        message_id=envelope.id,
                        fencing_token=claim.fencing_token,
                        result_status=204,
                    )
            return Response(status=204)
        if not await self._replay.claim(self._name, envelope.id):
            return Response(status=409)
        try:
            await handler(WebhookContext(self._name, envelope, request), payload)
        except Exception:  # noqa: BLE001 - records the outcome and re-raises
            # Broad and re-raising, which is the shape that earns it: the
            # receiver's own code failed, every way it can fail means the same
            # thing to the replay claim, and nothing is swallowed -- the caller
            # still sees the original exception with its traceback intact.
            await self._replay.complete(self._name, envelope.id, "failed")
            raise
        await self._replay.complete(self._name, envelope.id, "completed")
        return Response(status=204)


class WebhookDestination:
    __slots__ = (
        "_client",
        "_max_relay_hops",
        "_name",
        "_outbox",
        "_path",
        "_relay_id",
        "_signer",
    )

    def __init__(
        self,
        name: str,
        *,
        client: Any,
        path: str,
        signer: HMACWebhookSigner,
        outbox: PostgresWebhookOutbox | None,
        relay_id: str,
        max_relay_hops: int,
    ) -> None:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("webhook destination path must be origin-relative")
        if not _RELAY_ID.fullmatch(relay_id):
            raise ValueError("webhook destination relay_id is invalid")
        if not 1 <= max_relay_hops <= 32:
            raise ValueError("webhook destination max_relay_hops must be between 1 and 32")
        self._name = name
        self._client = client
        self._path = path
        self._signer = signer
        self._outbox = outbox
        self._relay_id = relay_id
        self._max_relay_hops = max_relay_hops

    async def send(
        self,
        event_type: str,
        payload: Any,
        *,
        event_id: str | None = None,
        version: str = "1",
        timestamp: datetime | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        relay_path: tuple[str, ...] = (),
    ) -> WebhookDeliveryResult:
        body = (
            bytes(payload)
            if isinstance(payload, (bytes, bytearray, memoryview))
            else dumps(payload)
        )
        envelope = WebhookEnvelope(
            id=event_id or uuid.uuid4().hex,
            type=event_type,
            version=version,
            timestamp=datetime.now(UTC) if timestamp is None else timestamp,
            content_type="application/json",
            body=body,
            correlation_id=correlation_id,
            causation_id=causation_id,
            relay_path=relay_path,
        )
        return await self._send_envelope(envelope)

    async def _send_envelope(
        self,
        envelope: WebhookEnvelope,
        *,
        key_id: str | None = None,
    ) -> WebhookDeliveryResult:
        headers = (
            *self._signer.headers(envelope, key_id=key_id),
            (b"content-type", envelope.content_type.encode("latin-1")),
        )
        try:
            response = await self._client.post(
                self._path,
                headers=headers,
                body=envelope.body,
                idempotency_key=envelope.id,
            )
        except ClientError as error:
            return WebhookDeliveryResult(
                "unknown", envelope.id, failure=type(error).__name__
            )
        if 200 <= response.status < 300:
            return WebhookDeliveryResult("delivered", envelope.id, response.status)
        return WebhookDeliveryResult("failed", envelope.id, response.status)

    async def enqueue(
        self,
        session: Any,
        event_type: str,
        payload: Any,
        *,
        event_id: str | None = None,
        version: str = "1",
        timestamp: datetime | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        ordering_key: str | None = None,
        relay_path: tuple[str, ...] = (),
    ) -> str:
        """Insert durable delivery intent in the caller's transaction."""
        if self._outbox is None:
            raise RuntimeError("webhook destination has no durable outbox")
        body = (
            bytes(payload)
            if isinstance(payload, (bytes, bytearray, memoryview))
            else dumps(payload)
        )
        envelope = WebhookEnvelope(
            id=event_id or uuid.uuid4().hex,
            type=event_type,
            version=version,
            timestamp=datetime.now(UTC) if timestamp is None else timestamp,
            content_type="application/json",
            body=body,
            correlation_id=correlation_id,
            causation_id=causation_id,
            ordering_key=ordering_key,
            relay_path=relay_path,
        )
        return await self._outbox.enqueue(
            session,
            destination=self._name,
            envelope=envelope,
            key_id=self._signer.key_id,
        )

    async def enqueue_relay(
        self,
        session: Any,
        inbound: WebhookEnvelope,
        event_type: str,
        payload: Any,
        *,
        version: str = "1",
        timestamp: datetime | None = None,
        ordering_key: str | None = None,
    ) -> str:
        """Commit a loop-protected relay intent in the caller transaction."""
        return await self.enqueue(
            session,
            event_type,
            payload,
            version=version,
            timestamp=timestamp,
            correlation_id=inbound.correlation_id or inbound.id,
            causation_id=inbound.id,
            ordering_key=ordering_key,
            relay_path=self._next_relay_path(inbound),
        )

    async def relay(
        self,
        inbound: WebhookEnvelope,
        event_type: str,
        payload: Any,
        *,
        version: str = "1",
        timestamp: datetime | None = None,
    ) -> WebhookDeliveryResult:
        """Emit a separately identified event caused by an inbound event."""
        return await self.send(
            event_type,
            payload,
            version=version,
            timestamp=timestamp,
            correlation_id=inbound.correlation_id or inbound.id,
            causation_id=inbound.id,
            relay_path=self._next_relay_path(inbound),
        )

    def _next_relay_path(self, inbound: WebhookEnvelope) -> tuple[str, ...]:
        if self._relay_id in inbound.relay_path:
            raise ValueError("webhook relay loop detected")
        if len(inbound.relay_path) >= self._max_relay_hops:
            raise ValueError("webhook relay hop limit exceeded")
        return (*inbound.relay_path, self._relay_id)


@dataclass(frozen=True, slots=True)
class DispatcherReadiness:
    ready: bool
    running: bool
    in_flight: int
    last_error: str | None


class WebhookDispatcher:
    """Fenced delivery loop with lease renewal and lifespan supervision hooks."""

    __slots__ = (
        "_destinations",
        "_in_flight",
        "_last_error",
        "_lease_seconds",
        "_managed",
        "_max_attempts",
        "_outbox",
        "_retry_delay",
        "_retry_statuses",
        "_running",
        "_stopping",
        "_task",
        "_worker_id",
    )

    def __init__(
        self,
        outbox: PostgresWebhookOutbox,
        destinations: Mapping[str, WebhookDestination],
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
        max_attempts: int = 6,
        retry_delay: float = 1.0,
        retry_statuses: frozenset[int] = frozenset(
            {408, 425, 429, 500, 502, 503, 504}
        ),
    ) -> None:
        if not worker_id:
            raise ValueError("webhook dispatcher worker_id cannot be empty")
        if lease_seconds <= 0 or max_attempts <= 0 or retry_delay < 0:
            raise ValueError("webhook dispatcher limits are invalid")
        self._outbox = outbox
        self._destinations = dict(destinations)
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay
        self._retry_statuses = retry_statuses
        self._running = False
        self._in_flight = 0
        self._last_error: str | None = None
        self._managed = False
        self._stopping: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def readiness(self) -> DispatcherReadiness:
        return DispatcherReadiness(
            ready=self._running and self._last_error is None,
            running=self._running,
            in_flight=self._in_flight,
            last_error=self._last_error,
        )

    def manage(
        self,
        app: Any,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        *,
        idle_delay: float = 0.25,
    ) -> None:
        """Attach the dispatcher to Wreath lifespan until a full supervisor lands."""
        if self._managed:
            raise RuntimeError("webhook dispatcher is already managed")
        self._managed = True
        state_name = re.sub(r"[^A-Za-z0-9_]", "_", self._worker_id)
        app.state.__setattr__(f"webhook_dispatcher_{state_name}", self)

        async def startup(_app: Any) -> None:
            self._stopping = asyncio.Event()
            self._task = asyncio.create_task(
                self.run(session_factory, self._stopping, idle_delay=idle_delay),
                name=f"wreath-webhook-{self._worker_id}",
            )
            await asyncio.sleep(0)
            if self._task.done():
                await self._task

        async def shutdown(_app: Any) -> None:
            if self._stopping is not None:
                self._stopping.set()
            if self._task is not None:
                await self._task
            self._task = None
            self._stopping = None

        app.on_startup(startup)
        app.on_shutdown(shutdown)

    async def run(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        stopping: asyncio.Event,
        *,
        idle_delay: float = 0.1,
    ) -> None:
        """Run until stopped; the application supervisor owns this coroutine."""
        if idle_delay <= 0:
            raise ValueError("webhook dispatcher idle_delay must be positive")
        self._running = True
        self._last_error = None
        try:
            while not stopping.is_set():
                async with session_factory() as session:
                    result = await self.run_once(
                        session, renewal_session_factory=session_factory
                    )
                if result is None and not stopping.is_set():
                    try:
                        async with asyncio.timeout(idle_delay):
                            await stopping.wait()
                    except TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - records the outcome and re-raises
            # Same shape as the delivery path above: this only makes the failure
            # visible on the sender before letting it propagate unchanged.
            self._last_error = f"{type(error).__name__}: {error}"
            raise
        finally:
            self._running = False

    async def run_once(
        self,
        session: Any,
        *,
        renewal_session_factory: Callable[
            [], AbstractAsyncContextManager[Any]
        ] | None = None,
    ) -> WebhookDeliveryResult | None:
        delivery = await self._outbox.claim_due(
            session,
            lease_owner=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if delivery is None:
            return None
        destination = self._destinations.get(delivery.destination)
        if destination is None:
            result = WebhookDeliveryResult(
                "failed", delivery.event_id, failure="UnknownDestination"
            )
            await self._outbox.mark_failed(
                session,
                delivery,
                status=None,
                failure=result.failure,
            )
            return result
        await self._outbox.mark_sending(session, delivery)
        envelope = WebhookEnvelope(
            id=delivery.event_id,
            type=delivery.event_type,
            version=delivery.version,
            timestamp=delivery.timestamp,
            content_type=delivery.content_type,
            body=delivery.body,
            correlation_id=delivery.correlation_id,
            causation_id=delivery.causation_id,
            ordering_key=delivery.ordering_key,
            relay_path=delivery.relay_path,
        )
        renewal: asyncio.Task[None] | None = None
        if renewal_session_factory is not None:
            renewal = asyncio.create_task(
                self._renew_lease(delivery, renewal_session_factory),
                name=f"wreath-webhook-lease-{delivery.delivery_id}",
            )
        self._in_flight += 1
        try:
            result = await destination._send_envelope(envelope, key_id=delivery.key_id)
        finally:
            self._in_flight -= 1
            if renewal is not None:
                renewal.cancel()
                with suppress(asyncio.CancelledError):
                    await renewal
        if result.outcome == "delivered":
            assert result.status is not None
            await self._outbox.mark_delivered(
                session, delivery, status=result.status
            )
        elif result.outcome == "unknown":
            await self._outbox.mark_unknown(
                session, delivery, failure=result.failure
            )
        elif (
            delivery.attempts < self._max_attempts
            and result.status in self._retry_statuses
        ):
            await self._outbox.mark_retry(
                session,
                delivery,
                delay=self._retry_delay * (2 ** (delivery.attempts - 1)),
                status=result.status,
                failure=result.failure,
            )
        else:
            await self._outbox.mark_failed(
                session,
                delivery,
                status=result.status,
                failure=result.failure,
            )
        return result

    async def _renew_lease(
        self,
        delivery: OutboxDelivery,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    ) -> None:
        interval = max(0.01, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            async with session_factory() as session:
                await self._outbox.renew_lease(
                    session, delivery, lease_seconds=self._lease_seconds
                )


class WebhookHub:
    __slots__ = ("_app", "_destinations", "_name", "_source_paths", "_sources")

    def __init__(self, app: Any, name: str) -> None:
        if not name:
            raise ValueError("webhook hub name cannot be empty")
        self._app = app
        self._name = name
        self._sources: dict[str, WebhookSource] = {}
        self._source_paths: set[str] = set()
        self._destinations: dict[str, WebhookDestination] = {}

    def csrf_exempt(self, request: Request) -> bool:
        """Return true only for registered unsafe webhook receiver routes."""
        return request.method == "POST" and request.path in self._source_paths

    def source(
        self,
        name: str,
        *,
        path: str,
        verifier: HMACWebhookVerifier,
        replay: LocalReplayStore | None = None,
        limits: WebhookLimits = _DEFAULT_WEBHOOK_LIMITS,
        inbox: PostgresWebhookInbox | None = None,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
        lease_owner: str = "webhook-receiver",
        lease_seconds: float = 30.0,
    ) -> WebhookSource:
        if name in self._sources:
            raise ValueError(f"duplicate webhook source: {name}")
        source = WebhookSource(
            self._app,
            name,
            path=path,
            verifier=verifier,
            replay=replay,
            limits=limits,
            inbox=inbox,
            session_factory=session_factory,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )
        self._sources[name] = source
        self._source_paths.add(path)
        return source

    def destination(
        self,
        name: str,
        *,
        client: Any,
        path: str,
        signer: HMACWebhookSigner,
        outbox: PostgresWebhookOutbox | None = None,
        relay_id: str | None = None,
        max_relay_hops: int = 8,
    ) -> WebhookDestination:
        if name in self._destinations:
            raise ValueError(f"duplicate webhook destination: {name}")
        destination = WebhookDestination(
            name,
            client=client,
            path=path,
            signer=signer,
            outbox=outbox,
            relay_id=name if relay_id is None else relay_id,
            max_relay_hops=max_relay_hops,
        )
        self._destinations[name] = destination
        return destination


__all__ = [
    "DispatcherReadiness",
    "HMACWebhookSigner",
    "HMACWebhookVerifier",
    "InboxClaim",
    "LocalReplayStore",
    "OutboxDelivery",
    "PostgresWebhookInbox",
    "PostgresWebhookOutbox",
    "WebhookContext",
    "WebhookDeliveryResult",
    "WebhookDestination",
    "WebhookDispatcher",
    "WebhookEnvelope",
    "WebhookHub",
    "WebhookLimits",
    "WebhookSource",
]
