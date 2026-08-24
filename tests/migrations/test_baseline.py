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


class OtherTrail(Model, table="other_trails", schema="other_trek"):
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


def _rebuild_artifact(artifact, **changes):
    fields = {
        "migration_id": artifact.migration_id,
        "parent_checksum": artifact.parent_checksum,
        "source_fingerprint": artifact.source_fingerprint,
        "target_fingerprint": artifact.target_fingerprint,
        "operation_tape": artifact.operation_tape,
        "named_plan": artifact.named_plan,
        "sql_tape": artifact.sql_tape,
    }
    fields.update(changes)
    return migrations._build_native_artifact(**fields)


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
async def test_generate_baseline_refuses_more_than_one_physical_schema() -> None:
    registry = Registry(Database(), [Trail, OtherTrail], validate_schema="off")

    with pytest.raises(ValueError, match="exactly one resolved physical schema"):
        await migrations.generate_single_baseline(
            registry,
            Connection(),
            migration_id=MIGRATION_ID,
        )


@pytest.mark.asyncio
async def test_generate_baseline_refuses_a_fingerprint_mismatch_even_with_no_diff(
    monkeypatch,
) -> None:
    registry = Registry(Database(), [Trail], validate_schema="off")
    snapshot = _matching_snapshot(registry)

    async def decode(*args: Any) -> NativeCatalogSnapshot:
        return snapshot

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    monkeypatch.setattr(
        migrations,
        "_diff_packed_images",
        lambda desired, actual: migrations.NativeMigrationDiff(0, b""),
    )
    monkeypatch.setattr(
        migrations,
        "_fingerprint_image",
        lambda image: b"a" * 32 if image is snapshot.image else b"d" * 32,
    )

    with pytest.raises(ValueError, match="cannot baseline schema with drift"):
        await migrations.generate_single_baseline(
            registry,
            Connection(),
            migration_id=MIGRATION_ID,
        )


@pytest.mark.asyncio
async def test_generate_baseline_refuses_a_diff_even_if_fingerprints_match(
    monkeypatch,
) -> None:
    registry = Registry(Database(), [Trail], validate_schema="off")
    snapshot = _matching_snapshot(registry)

    async def decode(*args: Any) -> NativeCatalogSnapshot:
        return snapshot

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    monkeypatch.setattr(
        migrations,
        "_diff_packed_images",
        lambda desired, actual: migrations.NativeMigrationDiff(1, b"difference"),
    )
    monkeypatch.setattr(migrations, "_fingerprint_image", lambda image: b"f" * 32)

    with pytest.raises(ValueError, match="cannot baseline schema with drift"):
        await migrations.generate_single_baseline(
            registry,
            Connection(),
            migration_id=MIGRATION_ID,
        )


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


@pytest.mark.asyncio
async def test_bootstrap_rolls_back_when_history_creation_fails() -> None:
    class FailingConnection(Connection):
        async def execute(self, *args: object) -> str:
            self.executed.append(args)
            if isinstance(args[0], str) and "CREATE TABLE" in args[0]:
                raise RuntimeError("catalog unavailable")
            return "OK"

    connection = FailingConnection()
    with pytest.raises(RuntimeError, match="catalog unavailable"):
        await migrations._bootstrap_migration_history(connection)

    assert connection.executed[-1] == ("ROLLBACK",)


@pytest.mark.asyncio
async def test_adopt_baseline_refuses_a_non_root_artifact(monkeypatch) -> None:
    registry = Registry(Database(), [Trail], validate_schema="off")
    snapshot = _matching_snapshot(registry)

    async def decode(*args: Any) -> NativeCatalogSnapshot:
        return snapshot

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    baseline = await migrations.generate_single_baseline(
        registry,
        Connection(),
        migration_id=MIGRATION_ID,
    )
    artifact = migrations._load_native_artifact(baseline.artifact.data)
    non_root = _rebuild_artifact(artifact, parent_checksum=b"p" * 32)

    with pytest.raises(ValueError, match="baseline must be a root artifact"):
        await migrations.adopt_single_baseline(registry, Connection(), non_root.data)


@pytest.mark.asyncio
async def test_adopt_baseline_refuses_a_different_current_orm_image(monkeypatch) -> None:
    registry = Registry(Database(), [Trail], validate_schema="off")
    snapshot = _matching_snapshot(registry)

    async def decode(*args: Any) -> NativeCatalogSnapshot:
        return snapshot

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    baseline = await migrations.generate_single_baseline(
        registry,
        Connection(),
        migration_id=MIGRATION_ID,
    )
    artifact = migrations._load_native_artifact(baseline.artifact.data)
    reviewed_elsewhere = _rebuild_artifact(
        artifact,
        source_fingerprint=b"z" * 32,
        target_fingerprint=b"z" * 32,
    )

    with pytest.raises(RuntimeError, match="current ORM fingerprint"):
        await migrations.adopt_single_baseline(
            registry,
            Connection(),
            reviewed_elsewhere.data,
        )


@pytest.mark.asyncio
async def test_adopt_baseline_refuses_live_catalog_drift(monkeypatch) -> None:
    registry = Registry(Database(), [Trail], validate_schema="off")
    matching = _matching_snapshot(registry)

    async def matching_decode(*args: Any) -> NativeCatalogSnapshot:
        return matching

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", matching_decode)
    baseline = await migrations.generate_single_baseline(
        registry,
        Connection(),
        migration_id=MIGRATION_ID,
    )
    empty = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    drifted = NativeCatalogSnapshot(
        migrations._postgres._migration_compile_desired(empty),
        empty,
    )

    async def drifted_decode(*args: Any) -> NativeCatalogSnapshot:
        return drifted

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", drifted_decode)
    connection = Connection()
    with pytest.raises(RuntimeError, match="live catalog fingerprint"):
        await migrations.adopt_single_baseline(
            registry,
            connection,
            baseline.artifact.data,
        )

    assert connection.executed[-1] == ("ROLLBACK",)


def test_catalog_descriptor_rejects_trailing_bytes() -> None:
    descriptor = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00extra"

    with pytest.raises(ValueError, match="trailing bytes"):
        migrations.unpack_catalog_descriptor(descriptor)
