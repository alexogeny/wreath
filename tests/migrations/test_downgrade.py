from __future__ import annotations

import importlib
from typing import Any

import pytest

import wreath.migrations as migrations
from wreath.migrations import (
    DowngradeWouldStrandCode,
    NativeCatalogSnapshot,
)
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text

native: Any = importlib.import_module("wreath._native._postgres")
MIGRATION_ID = bytes.fromhex("00112233445566778899aabbccddeeff")


class Widget(Model, table="widgets", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    label: Mapped[str] = column(Text)


class Database:
    name = "main"


def _registry() -> Registry:
    return Registry(Database(), [Widget], validate_schema="off")


def _statements(tape: bytes) -> list[tuple[int, str]]:
    import struct

    offset = 12
    out: list[tuple[int, str]] = []
    for _ in range(struct.unpack_from("<I", tape, 8)[0]):
        flags, length = struct.unpack_from("<II", tape, offset)
        offset += 8
        out.append((flags, tape[offset : offset + length].decode()))
        offset += length
    return out


def _forward_plan() -> bytes:
    descriptor = migrations._registry_descriptor(_registry())
    empty = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    return native._migration_plan_descriptors(descriptor, empty)


def test_reverse_plan_round_trips_to_the_forward_operation_tape() -> None:
    plan = _forward_plan()
    twice = native._migration_reverse_plan(native._migration_reverse_plan(plan))
    assert native._migration_operations_from_plan(twice) == native._migration_operations_from_plan(
        plan
    )


def test_reverse_sql_drops_what_forward_added_inner_to_outer_and_is_destructive() -> None:
    reverse = native._migration_reverse_plan(_forward_plan())
    statements = _statements(native._migration_render_sql(reverse))
    kinds = [sql.split(" ", 3)[:3] for _flags, sql in statements]
    assert kinds[0][:2] == ["alter", "table"]  # drop constraint
    assert "drop constraint" in statements[0][1]
    assert statements[-1][1].startswith('drop table "app"."widgets"')
    assert all(flags & 1 for flags, _sql in statements)  # every step destructive


def test_forward_and_reverse_name_the_primary_key_identically() -> None:
    forward = _statements(native._migration_render_sql(_forward_plan()))
    reverse = _statements(
        native._migration_render_sql(native._migration_reverse_plan(_forward_plan()))
    )
    (added,) = [sql for _f, sql in forward if "add constraint" in sql]
    (dropped,) = [sql for _f, sql in reverse if "drop constraint" in sql]
    name = added.split('add constraint "', 1)[1].split('"', 1)[0]
    assert name in dropped and name.startswith("wreath_")


def test_hazards_flag_every_object_the_live_orm_still_maps() -> None:
    reverse = native._migration_reverse_plan(_forward_plan())
    image = migrations._compile_registry_image(_registry())
    hazards = native._migration_downgrade_hazards(reverse, image)
    stranded = {(table, name, reason) for _s, table, name, _k, reason in hazards}
    assert ("widgets", "id", "removed") in stranded
    assert ("widgets", "label", "removed") in stranded
    assert ("widgets", "", "removed") in stranded  # the table itself


def test_no_hazards_when_the_code_was_rolled_back_with_the_schema() -> None:
    reverse = native._migration_reverse_plan(_forward_plan())
    rolled_back = native._migration_compile_desired(b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00")
    assert native._migration_downgrade_hazards(reverse, rolled_back) == []


def test_retyped_column_is_a_hazard_only_while_code_expects_the_new_type() -> None:
    # forward: widgets.id changes bigint -> integer; reverse restores bigint.
    new = migrations._descriptor_record(
        "app", "widgets", "id", 2, "column\x1f23\x1f\x1f1\x1f\x1f\x1f"
    )
    old = migrations._descriptor_record(
        "app", "widgets", "id", 2, "column\x1f20\x1f\x1f1\x1f\x1f\x1f"
    )
    header = b"WMD1\x01\x00\x00\x00\x01\x00\x00\x00"
    forward = native._migration_plan_descriptors(header + new, header + old)
    reverse = native._migration_reverse_plan(forward)
    code_wants_new = native._migration_compile_desired(header + new)
    code_wants_old = native._migration_compile_desired(header + old)
    assert any(
        reason == "retyped"
        for *_x, reason in native._migration_downgrade_hazards(reverse, code_wants_new)
    )
    assert native._migration_downgrade_hazards(reverse, code_wants_old) == []


def _forward_artifact() -> tuple[bytes, NativeCatalogSnapshot, NativeCatalogSnapshot]:
    registry = _registry()
    desired_descriptor = migrations._registry_descriptor(registry)
    empty_descriptor = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    desired_image = native._migration_compile_desired(desired_descriptor)
    empty_image = native._migration_compile_desired(empty_descriptor)
    plan = native._migration_plan_descriptors(desired_descriptor, empty_descriptor)
    artifact = migrations._build_native_artifact(
        migration_id=MIGRATION_ID,
        parent_checksum=bytes(32),
        source_fingerprint=migrations._fingerprint_image(empty_image),
        target_fingerprint=migrations._fingerprint_image(desired_image),
        operation_tape=native._migration_operations_from_plan(plan),
        named_plan=plan,
        sql_tape=native._migration_render_sql(plan),
    )
    return (
        artifact.data,
        NativeCatalogSnapshot(empty_image, empty_descriptor),
        NativeCatalogSnapshot(desired_image, desired_descriptor),
    )


class Connection:
    def __init__(self, tip: tuple[bytes, bytes] | None) -> None:
        self.executed: list[tuple[object, ...]] = []
        self._tip = tip

    async def execute(self, *args: object) -> str:
        self.executed.append(args)
        return "OK"

    async def fetchval(self, *args: object) -> None:
        self.executed.append(args)

    async def fetchrow(self, *args: object) -> tuple[bytes, bytes] | None:
        self.executed.append(args)
        return self._tip

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_revert_verifies_target_runs_reverse_deletes_tip_and_commits(
    monkeypatch,
) -> None:
    artifact, source, target = _forward_artifact()
    loaded = migrations._load_native_artifact(artifact)
    snapshots = iter((target, source))  # at target, then back to source

    async def decode(connection, sql, args):
        return next(snapshots)

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    connection = Connection(tip=(loaded.checksum, loaded.target_fingerprint))

    # force=True stands in for "the code was rolled back too" — the transactional
    # machinery is identical either way.
    result = await migrations.revert_single_artifact(
        _registry(), connection, artifact, allow_destructive=True, force=True
    )

    statements = [call[0] for call in connection.executed]
    assert statements[0] == "BEGIN"
    assert any(
        isinstance(sql, str) and sql.startswith("DO $wreath_migration_") for sql in statements
    )
    assert any(isinstance(sql, str) and sql.startswith("DELETE FROM") for sql in statements)
    assert statements[-1] == "COMMIT"
    assert "ROLLBACK" not in statements
    assert result.migration_id == MIGRATION_ID and result.destructive_approved
    assert result.forced


@pytest.mark.asyncio
async def test_revert_refuses_when_live_orm_still_references_dropped_objects() -> None:
    artifact, _source, _target = _forward_artifact()
    connection = Connection(tip=None)

    with pytest.raises(DowngradeWouldStrandCode) as caught:
        await migrations.revert_single_artifact(
            _registry(), connection, artifact, allow_destructive=True
        )

    # Refused before any transaction opened.
    assert connection.executed == []
    assert "widgets" in str(caught.value)


class Gadget(Model, table="gadgets", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)


def _wmd1_append(descriptor: bytes, record: bytes) -> bytes:
    import struct

    count = struct.unpack_from("<I", descriptor, 8)[0]
    return b"WMD1" + struct.pack("<II", 1, count + 1) + descriptor[12:] + record


@pytest.mark.asyncio
async def test_force_free_revert_succeeds_once_the_code_no_longer_maps_the_column(
    monkeypatch,
) -> None:
    # Forward migration added gadgets.note to an existing gadgets(id) table; the
    # rolled-back code (Gadget without note) no longer maps it, so no force is
    # needed and the hazard scan stays quiet.
    rolled_back = Registry(Database(), [Gadget], validate_schema="off")
    source_descriptor = migrations._registry_descriptor(rolled_back)
    note = migrations._descriptor_record(
        "app", "gadgets", "note", 2, "column\x1f25\x1f\x1f0\x1f\x1f\x1f"
    )
    target_descriptor = _wmd1_append(source_descriptor, note)
    source_image = native._migration_compile_desired(source_descriptor)
    target_image = native._migration_compile_desired(target_descriptor)
    plan = native._migration_plan_descriptors(target_descriptor, source_descriptor)
    artifact = migrations._build_native_artifact(
        migration_id=MIGRATION_ID,
        parent_checksum=bytes(32),
        source_fingerprint=migrations._fingerprint_image(source_image),
        target_fingerprint=migrations._fingerprint_image(target_image),
        operation_tape=native._migration_operations_from_plan(plan),
        named_plan=plan,
        sql_tape=native._migration_render_sql(plan),
    ).data
    loaded = migrations._load_native_artifact(artifact)
    snapshots = iter(
        (
            NativeCatalogSnapshot(target_image, target_descriptor),
            NativeCatalogSnapshot(source_image, source_descriptor),
        )
    )

    async def decode(connection, sql, args):
        return next(snapshots)

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    connection = Connection(tip=(loaded.checksum, loaded.target_fingerprint))

    result = await migrations.revert_single_artifact(
        rolled_back, connection, artifact, allow_destructive=True
    )

    assert not result.forced
    assert [call[0] for call in connection.executed][-1] == "COMMIT"


@pytest.mark.asyncio
async def test_revert_rolls_back_when_reverse_does_not_reach_the_source(
    monkeypatch,
) -> None:
    artifact, _source, target = _forward_artifact()
    loaded = migrations._load_native_artifact(artifact)
    snapshots = iter((target, target))  # DDL "ran" but catalog never left target

    async def decode(connection, sql, args):
        return next(snapshots)

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    connection = Connection(tip=(loaded.checksum, loaded.target_fingerprint))

    with pytest.raises(RuntimeError, match="source verification failed"):
        await migrations.revert_single_artifact(
            _registry(), connection, artifact, allow_destructive=True, force=True
        )

    assert connection.executed[-1] == ("ROLLBACK",)


@pytest.mark.asyncio
async def test_revert_requires_the_artifact_to_be_the_current_history_tip(
    monkeypatch,
) -> None:
    artifact, source, target = _forward_artifact()
    snapshots = iter((target, source))

    async def decode(connection, sql, args):
        return next(snapshots)

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", decode)
    # Tip is some other migration's checksum.
    connection = Connection(tip=(bytes(32), target.image[:0] or bytes(32)))

    with pytest.raises(RuntimeError, match="current history tip"):
        await migrations.revert_single_artifact(
            _registry(), connection, artifact, allow_destructive=True, force=True
        )

    assert connection.executed[-1] == ("ROLLBACK",)
