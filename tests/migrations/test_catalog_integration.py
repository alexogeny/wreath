"""Real PostgreSQL proof for direct catalog decode and single-schema detection."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from wreath.migrations import detect_single
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64
from wreath.postgres import connect

pytestmark = [pytest.mark.asyncio, pytest.mark.network]
_DSN = os.environ.get("WREATH_TEST_POSTGRES_DSN")


async def connection() -> Any:
    if _DSN is None:
        pytest.skip("set WREATH_TEST_POSTGRES_DSN for real migration catalog tests")
    return await connect(_DSN)


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
