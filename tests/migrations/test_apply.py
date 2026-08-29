from __future__ import annotations

import importlib
from typing import Any

import pytest

import wreath.migrations as migrations
from wreath.migrations import NativeCatalogSnapshot
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64

native: Any = importlib.import_module("wreath._native._postgres")
MIGRATION_ID = bytes.fromhex("00112233445566778899aabbccddeeff")


class Widget(Model, table="widgets", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)


class Database:
    name = "main"


def artifact_and_snapshots() -> tuple[bytes, NativeCatalogSnapshot, NativeCatalogSnapshot]:
    registry = Registry(Database(), [Widget], validate_schema="off")
    desired_descriptor = migrations._registry_descriptor(registry)
    empty_descriptor = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    desired_image = native._migration_compile_desired(desired_descriptor)
    empty_image = native._migration_compile_desired(empty_descriptor)
    plan = native._migration_plan_descriptors(desired_descriptor, empty_descriptor)
    operations = native._migration_operations_from_plan(plan)
    sql = native._migration_render_sql(plan)
    artifact = migrations._build_native_artifact(
        migration_id=MIGRATION_ID,
        parent_checksum=bytes(32),
        source_fingerprint=migrations._fingerprint_image(empty_image),
        target_fingerprint=migrations._fingerprint_image(desired_image),
        operation_tape=operations,
        named_plan=plan,
        sql_tape=sql,
    )
    return (
        artifact.data,
        NativeCatalogSnapshot(empty_image, empty_descriptor),
        NativeCatalogSnapshot(desired_image, desired_descriptor),
    )


class Connection:
    def __init__(self) -> None:
        self.executed: list[tuple[object, ...]] = []

    async def execute(self, *args: object) -> str:
        self.executed.append(args)
        return "OK"

    async def fetchval(self, *args: object) -> None:
        self.executed.append(args)

    async def fetchrow(self, *args: object) -> None:
        self.executed.append(args)


@pytest.mark.asyncio
async def test_apply_locks_checks_source_runs_one_block_verifies_and_commits(
    monkeypatch,
) -> None:
    artifact, source, target = artifact_and_snapshots()
    snapshots = iter((source, target))

    async def decode(connection, sql, args):
        return next(snapshots)

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    registry = Registry(Database(), [Widget], validate_schema="off")
    connection = Connection()

    result = await migrations.apply_single_artifact(registry, connection, artifact)

    statements = [call[0] for call in connection.executed]
    assert statements[0] == "BEGIN"
    assert any(
        isinstance(sql, str) and sql.startswith("DO $wreath_migration_") for sql in statements
    )
    assert statements[-1] == "COMMIT"
    assert "ROLLBACK" not in statements
    assert result.migration_id == MIGRATION_ID


@pytest.mark.asyncio
async def test_apply_rolls_back_when_post_ddl_catalog_misses_target(monkeypatch) -> None:
    artifact, source, _target = artifact_and_snapshots()
    snapshots = iter((source, source))

    async def decode(connection, sql, args):
        return next(snapshots)

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    registry = Registry(Database(), [Widget], validate_schema="off")
    connection = Connection()

    with pytest.raises(RuntimeError, match="target verification failed"):
        await migrations.apply_single_artifact(registry, connection, artifact)

    assert connection.executed[-1] == ("ROLLBACK",)
    assert sum(call[0] == "COMMIT" for call in connection.executed) == 1
