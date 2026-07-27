"""Real PostgreSQL proof for direct catalog decode and single-schema detection."""

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
from wreath.orm.types import Int64, Text
from wreath.postgres import connect

pytestmark = [pytest.mark.asyncio, pytest.mark.database]
_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")


async def connection() -> Any:
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for real migration catalog tests")
    return await connect(_DSN)


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
