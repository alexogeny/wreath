from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from wreath.migrations import (
    _build_native_artifact,
    apply_single_artifact,
    detect_single,
    generate_single_plan,
)
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import (
    Bit,
    Bool,
    Bytea,
    Date,
    Float32,
    Float64,
    Int16,
    Int32,
    Int64,
    Json,
    Jsonb,
    Numeric,
    Text,
    TextArray,
    Timestamp,
    TimestampTz,
    Uuid,
    Varchar,
)
from wreath.postgres import connect

pytestmark = [pytest.mark.asyncio, pytest.mark.database]
_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")


async def connection() -> Any:
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for real migration catalog tests")
    return await connect(_DSN)


def _statements(tape: bytes) -> list[tuple[int, str]]:
    """The `(flags, sql)` pairs in a rendered SQL tape. Flag 2 is MANUAL."""
    import struct

    offset = 12
    out: list[tuple[int, str]] = []
    for _ in range(struct.unpack_from("<I", tape, 8)[0]):
        flags, length = struct.unpack_from("<II", tape, offset)
        offset += 8
        out.append((flags, tape[offset : offset + length].decode()))
        offset += length
    return out


async def test_real_apply_locks_records_executes_and_verifies_target() -> None:
    schema = f"wreath_apply_{uuid.uuid4().hex[:12]}"

    class Widget(Model, table="widgets", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)
        name: Mapped[str] = column(Text, index=True)

    class Database:
        name = "migration-apply-test"

    registry = Registry(Database(), [Widget], validate_schema="off")
    db = await connection()
    try:
        await db.execute(f'CREATE SCHEMA "{schema}"')
        generation = await generate_single_plan(registry, db)
        artifact = _build_native_artifact(
            migration_id=uuid.uuid4().bytes,
            parent_checksum=bytes(32),
            source_fingerprint=generation.actual_fingerprint,
            target_fingerprint=generation.desired_fingerprint,
            operation_tape=generation.diff.tape,
            named_plan=generation.plan.tape,
            sql_tape=generation.sql.tape,
        )

        result = await apply_single_artifact(registry, db, artifact.data)

        assert result.checksum == artifact.checksum
        assert (await detect_single(registry, db)).current
        recorded = await db.fetchval(
            'SELECT count(*) FROM "wreath_migrations"."history" '
            "WHERE target_schema = $1 AND checksum = $2",
            schema,
            artifact.checksum,
        )
        assert recorded == 1
        with pytest.raises(RuntimeError, match="does not match database history tip"):
            await apply_single_artifact(registry, db, artifact.data)
    finally:
        await db.execute(
            'DELETE FROM "wreath_migrations"."history" WHERE target_schema = $1',
            schema,
        )
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()


async def test_real_catalog_decodes_without_records_and_detects_drift() -> None:
    schema = f"wreath_migration_{uuid.uuid4().hex[:12]}"

    class Widget(Model, table="widgets", schema=schema):
        id: Mapped[int] = column(Int64, primary_key=True)

    class Database:
        name = "migration-test"

    registry = Registry(Database(), [Widget], validate_schema="off")
    db = await connection()
    try:
        await db.execute(f'CREATE SCHEMA "{schema}"')
        await db.execute(f'CREATE TABLE "{schema}"."widgets" (id bigint PRIMARY KEY)')

        current = await detect_single(registry, db)
        assert current.current
        assert current.diff.operation_count == 0

        await db.execute(f'ALTER TABLE "{schema}"."widgets" ADD COLUMN extra text')
        drifted = await detect_single(registry, db)
        assert not drifted.current
        assert drifted.diff.operation_count == 1
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()


async def test_every_builtin_column_type_survives_a_catalog_round_trip() -> None:
    db = await connection()
    schema = f"wreath_types_{uuid.uuid4().hex[:12]}"
    try:
        await db.execute(f'CREATE SCHEMA "{schema}"')

        class Every(Model, table="every", schema=schema):
            id: Mapped[int] = column(Int64, primary_key=True)
            a_bool: Mapped[bool] = column(Bool)
            a_bytea: Mapped[bytes] = column(Bytea)
            a_date: Mapped[Any] = column(Date)
            a_float32: Mapped[float] = column(Float32)
            a_float64: Mapped[float] = column(Float64)
            a_int16: Mapped[int] = column(Int16)
            a_int32: Mapped[int] = column(Int32)
            a_json: Mapped[Any] = column(Json)
            a_jsonb: Mapped[Any] = column(Jsonb)
            a_numeric: Mapped[Any] = column(Numeric)
            a_text: Mapped[str] = column(Text)
            a_text_array: Mapped[Any] = column(TextArray)
            a_timestamp: Mapped[Any] = column(Timestamp)
            a_timestamptz: Mapped[Any] = column(TimestampTz)
            a_uuid: Mapped[Any] = column(Uuid)
            a_varchar: Mapped[str] = column(Varchar)
            a_bit: Mapped[str] = column(Bit(8))

        class Database:
            name = "migration-types-test"

        registry = Registry(Database(), [Every], validate_schema="off")
        generation = await generate_single_plan(registry, db)
        emitted = _statements(generation.sql.tape)
        assert not any(flags & 2 for flags, _sql in emitted), emitted
        for _flags, statement in emitted:
            await db.execute(statement)

        # The round trip. `current` means the catalog image and the desired image
        # agree; an empty second plan means nothing was rediscovered as drift.
        assert (await detect_single(registry, db)).current
        assert _statements((await generate_single_plan(registry, db)).sql.tape) == []

        # And the column PostgreSQL actually built is the width that was asked
        # for, which a matching descriptor alone would not prove.
        rows = await db.fetch(
            "SELECT pg_catalog.format_type(a.atttypid, a.atttypmod) AS spelled "
            "FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname = 'every' AND a.attname = 'a_bit'",
            schema,
        )
        assert rows[0]["spelled"] == "bit(8)"
    finally:
        await db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await db.close()
