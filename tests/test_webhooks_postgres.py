from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from wreath.postgres import connect
from wreath.webhooks import PostgresWebhookInbox, PostgresWebhookOutbox, WebhookEnvelope

pytestmark = [pytest.mark.asyncio, pytest.mark.network]
_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")


class _Raw:
    def __init__(self, connection: Any, sql: str, args: tuple[Any, ...]) -> None:
        self.connection = connection
        self.sql = sql
        self.args = args

    async def execute(self) -> Any:
        return await self.connection.execute(self.sql, *self.args)

    async def fetchrow(self) -> Any:
        return await self.connection.fetchrow(self.sql, *self.args)

    async def fetchval(self) -> Any:
        return await self.connection.fetchval(self.sql, *self.args)


class _Session:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def raw(self, sql: str, *args: Any) -> _Raw:
        return _Raw(self.connection, sql, args)


async def _connection() -> Any:
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for real PostgreSQL webhook tests")
    return await connect(_DSN)


def _envelope(event_id: str) -> WebhookEnvelope:
    return WebhookEnvelope(
        id=event_id,
        type="integration.changed",
        version="1",
        timestamp=datetime.now(UTC),
        content_type="application/json",
        body=b'{"value":1}',
    )


async def test_inbox_claim_handler_effect_and_completion_commit_atomically() -> None:
    suffix = uuid.uuid4().hex
    inbox = PostgresWebhookInbox(f"webhook_inbox_{suffix}")
    effects = f"webhook_effects_{suffix}"
    connection = await _connection()
    session = _Session(connection)
    try:
        await connection.execute(inbox.schema_sql())
        await connection.execute(
            f"CREATE TABLE {effects} (event_id text PRIMARY KEY, value integer NOT NULL)"
        )
        await connection.execute("BEGIN")
        claim = await inbox.claim(
            session,
            source="integration",
            envelope=_envelope("evt-atomic"),
            lease_owner="replica-a",
            lease_seconds=5,
        )
        assert claim.outcome == "claimed"
        await connection.execute(
            f"INSERT INTO {effects} (event_id,value) VALUES ($1,$2)",
            "evt-atomic",
            1,
        )
        await inbox.complete(
            session,
            source="integration",
            message_id="evt-atomic",
            fencing_token=claim.fencing_token,
            result_status=204,
        )
        await connection.execute("COMMIT")

        duplicate = await inbox.claim(
            session,
            source="integration",
            envelope=_envelope("evt-atomic"),
            lease_owner="replica-b",
            lease_seconds=5,
        )
        assert duplicate.outcome == "duplicate"
        assert await connection.fetchval(f"SELECT count(*) FROM {effects}") == 1
    finally:
        await connection.execute(f"DROP TABLE IF EXISTS {effects}")
        await connection.execute(f"DROP TABLE IF EXISTS {inbox.table}")
        await connection.close()


async def test_multi_replica_skip_locked_claims_each_intent_once() -> None:
    outbox = PostgresWebhookOutbox(f"webhook_outbox_{uuid.uuid4().hex}")
    seed = await _connection()
    try:
        await seed.execute(outbox.schema_sql())
        for index in range(24):
            await outbox.enqueue(
                _Session(seed),
                destination="receiver",
                envelope=_envelope(f"evt-{index}"),
                key_id="current",
            )

        observed: list[str] = []
        lock = asyncio.Lock()

        async def worker(worker_id: str) -> None:
            connection = await _connection()
            session = _Session(connection)
            try:
                while True:
                    delivery = await outbox.claim_due(
                        session, lease_owner=worker_id, lease_seconds=5
                    )
                    if delivery is None:
                        return
                    await outbox.mark_sending(session, delivery)
                    async with lock:
                        observed.append(delivery.delivery_id)
                    await outbox.mark_delivered(session, delivery, status=204)
            finally:
                await connection.close()

        await asyncio.gather(*(worker(f"replica-{index}") for index in range(4)))
        assert len(observed) == 24
        assert len(set(observed)) == 24
        assert await seed.fetchval(
            f"SELECT count(*) FROM {outbox.table} WHERE state='delivered'"
        ) == 24
    finally:
        await seed.execute(f"DROP TABLE IF EXISTS {outbox.table}")
        await seed.close()


async def test_process_loss_and_ack_rollback_reclaim_with_new_fence() -> None:
    outbox = PostgresWebhookOutbox(f"webhook_outbox_{uuid.uuid4().hex}")
    first = await _connection()
    observer = await _connection()
    try:
        await first.execute(outbox.schema_sql())
        await outbox.enqueue(
            _Session(first),
            destination="receiver",
            envelope=_envelope("evt-process-loss"),
            key_id="current",
        )
        original = await outbox.claim_due(
            _Session(first), lease_owner="replica-a", lease_seconds=0.05
        )
        assert original is not None
        await outbox.mark_sending(_Session(first), original)

        # The peer accepted the request, then this process lost its connection
        # before acknowledgement persistence. At-least-once recovery may resend.
        await first.close()
        await asyncio.sleep(0.08)
        recovered = await outbox.claim_due(
            _Session(observer), lease_owner="replica-b", lease_seconds=1
        )
        assert recovered is not None
        assert recovered.delivery_id == original.delivery_id
        assert recovered.fencing_token > original.fencing_token
        with pytest.raises(RuntimeError, match="stale webhook outbox"):
            await outbox.mark_delivered(_Session(observer), original, status=204)

        await outbox.mark_sending(_Session(observer), recovered)
        await observer.execute("BEGIN")
        await outbox.mark_delivered(_Session(observer), recovered, status=204)
        await observer.execute("ROLLBACK")
        assert await observer.fetchval(
            f"SELECT state FROM {outbox.table} WHERE delivery_id=$1",
            recovered.delivery_id,
        ) == "sending"
    finally:
        await observer.execute(f"DROP TABLE IF EXISTS {outbox.table}")
        if not getattr(first, "closed", False):
            await first.close()
        await observer.close()
