"""Existing schemas become verified roots without application DDL replay."""

from __future__ import annotations

from typing import Any

import pytest

import wreath.migrations as migrations
from wreath.migrations import NativeCatalogSnapshot
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64

MIGRATION_ID = bytes.fromhex("00112233445566778899aabbccddeeff")


class Trail(Model, table="trails", schema="llama_trek"):
    id: Mapped[int] = column(Int64, primary_key=True)


class Database:
    name = "main"


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


def _matching_snapshot(registry: Registry) -> NativeCatalogSnapshot:
    descriptor = migrations._registry_descriptor(registry)
    image = migrations._postgres._migration_compile_desired(descriptor)
    return NativeCatalogSnapshot(image, descriptor)


@pytest.mark.asyncio
async def test_generate_baseline_is_a_reviewable_zero_operation_root(monkeypatch) -> None:
    registry = Registry(Database(), [Trail], validate_schema="off")
    snapshot = _matching_snapshot(registry)

    async def decode(*args: Any) -> NativeCatalogSnapshot:
        return snapshot

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)

    baseline = await migrations.generate_single_baseline(
        registry, Connection(), migration_id=MIGRATION_ID
    )

    artifact = migrations._load_native_artifact(baseline.artifact.data)
    assert artifact.parent_checksum == bytes(32)
    assert artifact.source_fingerprint == artifact.target_fingerprint
    assert artifact.operation_tape == b"WMO1\x01\x00\x00\x00\x00\x00\x00\x00"
    assert artifact.named_plan == b"WMP1\x01\x00\x00\x00\x00\x00\x00\x00"
    assert artifact.sql_tape == b"WMS1\x01\x00\x00\x00\x00\x00\x00\x00"
    assert baseline.object_count == len(migrations.unpack_catalog_descriptor(baseline.descriptor))


@pytest.mark.asyncio
async def test_generate_baseline_refuses_catalog_drift(monkeypatch) -> None:
    registry = Registry(Database(), [Trail], validate_schema="off")
    empty = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    snapshot = NativeCatalogSnapshot(migrations._postgres._migration_compile_desired(empty), empty)

    async def decode(*args: Any) -> NativeCatalogSnapshot:
        return snapshot

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)

    with pytest.raises(ValueError, match="cannot baseline schema with drift"):
        await migrations.generate_single_baseline(registry, Connection(), migration_id=MIGRATION_ID)


@pytest.mark.asyncio
async def test_adopt_baseline_records_history_without_application_ddl(monkeypatch) -> None:
    registry = Registry(Database(), [Trail], validate_schema="off")
    snapshot = _matching_snapshot(registry)

    async def decode(*args: Any) -> NativeCatalogSnapshot:
        return snapshot

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    generation_connection = Connection()
    baseline = await migrations.generate_single_baseline(
        registry, generation_connection, migration_id=MIGRATION_ID
    )
    connection = Connection()

    result = await migrations.adopt_single_baseline(registry, connection, baseline.artifact.data)

    statements = [call[0] for call in connection.executed]
    application_ddl = [
        statement
        for statement in statements
        if isinstance(statement, str)
        and statement.lstrip().upper().startswith(("ALTER ", "DROP ", "CREATE INDEX"))
    ]
    assert application_ddl == []
    assert any(
        isinstance(statement, str) and "INSERT INTO" in statement for statement in statements
    )
    assert statements[-1] == "COMMIT"
    assert result.migration_id == MIGRATION_ID


def test_catalog_descriptor_rejects_trailing_bytes() -> None:
    descriptor = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00extra"

    with pytest.raises(ValueError, match="trailing bytes"):
        migrations.unpack_catalog_descriptor(descriptor)
