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


async def _apply_schema(connection: Any, sql: str) -> None:
    """Run a multi-statement ``schema_sql()`` one command at a time.

    The driver speaks the extended query protocol exclusively, so it prepares
    every statement and PostgreSQL refuses `cannot insert multiple commands into
    a prepared statement`. Passing `schema_sql()` straight to `execute` therefore
    cannot work, and these three tests did it -- they had simply never run,
    because `network` deselects them and a deselection exits 0.

    Splitting on `";\\n"` is what `tests/jobs/test_integration.py`,
    `tests/postgres/test_passes_integration.py` and
    `tests/postgres/test_series_integration.py` already do. That four call sites
    need the same workaround is tracked separately as a question about the shape
    `schema_sql()` should emit.
    """
    for statement in (part.strip() for part in sql.split(";\n")):
        if statement:
            await connection.execute(statement)


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
        await _apply_schema(connection, inbox.schema_sql())
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
        await _apply_schema(seed, outbox.schema_sql())
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
        await _apply_schema(first, outbox.schema_sql())
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


async def test_outbox_purge_pass_runs_against_a_real_database() -> None:
    """`purge_pass()` builds and drives with no database argument.

    The builder used to take one positionally and discard it, which read as a
    wiring step that existed. A pass is handed the database when it is *driven*,
    and this is that, end to end on PostgreSQL.
    """
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for real PostgreSQL webhook tests")

    import datetime

    from wreath.passes import schema_sql
    from wreath.postgres import Database

    schema = f"wreath_hooks_{uuid.uuid4().hex[:8]}"
    outbox = PostgresWebhookOutbox(f"webhook_outbox_{uuid.uuid4().hex}")
    database = Database("main", _DSN, pools={"write": {"min_size": 1, "max_size": 2}})
    await database.start()
    connection = await database.acquire("write")
    try:
        await connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        await _apply_schema(connection, schema_sql(schema))
        await _apply_schema(connection, outbox.schema_sql())
        past = datetime.datetime.now(UTC) - datetime.timedelta(days=1)
        for index in range(5):
            await outbox.enqueue(
                _Session(connection),
                destination="receiver",
                envelope=_envelope(f"evt-purge-{index}"),
                key_id="current",
            )
        await connection.execute(
            f"UPDATE {outbox.table} SET state='delivered', retention_until=$1", past
        )

        walk = outbox.purge_pass(chunk=2, schema=schema)
        result = await walk.run(database, sleep=lambda _s: asyncio.sleep(0))

        assert result.rows == 5
        assert await connection.fetchval(
            f"SELECT count(*) FROM {outbox.table}"
        ) == 0
    finally:
        await connection.execute(f"DROP TABLE IF EXISTS {outbox.table}")
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await database.release("write", connection)
        await database.stop()


async def test_inbox_purge_keeps_its_chunk_bounded_across_sources() -> None:
    """One inbox serves every source, so `message_id` alone is not a row.

    The pass once walked `(retention_until, message_id)` and declared
    `message_id` unique. It is not -- the primary key is `(source,
    message_id)` -- and two senders using the same event id put two rows on one
    boundary.

    Nothing is *skipped*: a chunk is the half-open range `(from, to]`, so ties
    at the top are swept in by `<=` and ties at the bottom excluded by `>`.
    What breaks is the bound. `cursor_to` is the key of the limit-th row, and
    every row sharing that key joins the same chunk however many there are.
    Measured against PostgreSQL, 60 rows sharing a retention stamp and a
    message id with `chunk=5`: **one chunk of 60** before, twelve chunks of
    five after. An unbounded DELETE in a single transaction is the exact thing
    `ChunkedPass` exists to prevent, so the declaration was buying nothing and
    costing the guarantee.

    A shared stamp is ordinary rather than contrived -- one `UPDATE ... SET
    retention_until` stamps every row it touches with the same transaction
    timestamp.
    """
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for real PostgreSQL webhook tests")

    from wreath.passes import schema_sql
    from wreath.postgres import Database

    schema = f"wreath_hooks_{uuid.uuid4().hex[:8]}"
    inbox = PostgresWebhookInbox(f"webhook_inbox_{uuid.uuid4().hex}")
    database = Database("main", _DSN, pools={"write": {"min_size": 1, "max_size": 2}})
    await database.start()
    connection = await database.acquire("write")
    try:
        await connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        await _apply_schema(connection, schema_sql(schema))
        await _apply_schema(connection, inbox.schema_sql())
        # Sixty sources, one message id, one literal stamp: under the old key
        # every row carried the identical boundary.
        for index in range(60):
            await connection.execute(
                f"INSERT INTO {inbox.table} (source, message_id, payload_version,"
                " payload_hash, state, lease_owner, lease_expires_at,"
                " retention_until) VALUES ($1, 'evt-shared', 'v1',"
                " '\\x00'::bytea, 'completed', 'w', now(),"
                " timestamptz '2020-01-01 00:00:00+00')",
                f"src-{index:03d}",
            )

        walk = inbox.purge_pass(chunk=5, schema=schema)
        result = await walk.run(database, sleep=lambda _s: asyncio.sleep(0))

        assert result.rows == 60
        # The property, not the row count: twelve bounded chunks rather than
        # one transaction holding every row.
        assert result.chunks == 12, (
            f"{result.rows} rows purged in {result.chunks} chunk(s) at chunk=5; "
            "a non-unique boundary collapses the whole tie group into one"
        )
        assert await connection.fetchval(
            f"SELECT count(*) FROM {inbox.table}"
        ) == 0
    finally:
        await connection.execute(f"DROP TABLE IF EXISTS {inbox.table}")
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await database.release("write", connection)
        await database.stop()
