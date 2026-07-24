"""Broader migration object coverage: composite unique and multi-column indexes."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

import wreath.migrations as migrations
from wreath.orm import DeclarationError, Mapped, Model, column, index, unique
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text

native: Any = importlib.import_module("wreath._native._postgres")


class Database:
    name = "main"


class Membership(Model, table="memberships", schema="app"):
    org_id: Mapped[int] = column(Int64, primary_key=True)
    user_id: Mapped[int] = column(Int64)
    email: Mapped[str] = column(Text)
    _identity = unique("org_id", "user_id")
    _lookup = index("user_id", "email")
    _one_email = index("email", unique=True)


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


def _forward_sql() -> list[str]:
    registry = Registry(Database(), [Membership], validate_schema="off")
    empty = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    plan = native._migration_plan_descriptors(
        migrations._registry_descriptor(registry), empty
    )
    return [sql for _flags, sql in _statements(native._migration_render_sql(plan))]


def test_composite_unique_and_indexes_render_without_manual_operations() -> None:
    registry = Registry(Database(), [Membership], validate_schema="off")
    empty = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    plan = native._migration_plan_descriptors(
        migrations._registry_descriptor(registry), empty
    )
    statements = _statements(native._migration_render_sql(plan))
    assert not any(flags & 2 for flags, _sql in statements)  # nothing MANUAL


def test_composite_unique_names_both_columns() -> None:
    assert any(
        'unique ("org_id", "user_id")' in sql for sql in _forward_sql()
    )


def test_multi_column_and_unique_indexes_render() -> None:
    forward = _forward_sql()
    assert any(sql.startswith("create index ") and '("user_id", "email")' in sql for sql in forward)
    assert any(sql.startswith("create unique index ") and '("email")' in sql for sql in forward)


def test_downgrade_drops_composite_constraints_and_indexes() -> None:
    registry = Registry(Database(), [Membership], validate_schema="off")
    empty = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    plan = native._migration_plan_descriptors(
        migrations._registry_descriptor(registry), empty
    )
    reverse = _statements(native._migration_render_sql(native._migration_reverse_plan(plan)))
    text = "\n".join(sql for _flags, sql in reverse)
    assert "drop constraint" in text  # unique + pk
    assert "drop index" in text
    assert all(flags & 1 for flags, _sql in reverse)  # every reverse step destructive
    # reverse-of-reverse round-trips to the forward operation tape
    assert native._migration_operations_from_plan(
        native._migration_reverse_plan(native._migration_reverse_plan(plan))
    ) == native._migration_operations_from_plan(plan)


def test_unique_and_index_reject_unknown_columns() -> None:
    class Bad(Model, table="bad", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        _oops = unique("id", "missing")

    with pytest.raises(DeclarationError, match="unknown column"):
        Registry(Database(), [Bad], validate_schema="off")


class Account(Model, table="accounts", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)


class Entry(Model, table="entries", schema="app"):
    id: Mapped[int] = column(Int64, primary_key=True)
    account_id: Mapped[int] = column(
        Int64, references=Account.id, on_delete="cascade", on_update="restrict"
    )
    parent_id: Mapped[int] = column(Int64, references=Account.id, deferrable=True)
    plain_id: Mapped[int] = column(Int64, references=Account.id)


def _entry_fk_sql() -> list[str]:
    registry = Registry(Database(), [Account, Entry], validate_schema="off")
    empty = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    plan = native._migration_plan_descriptors(
        migrations._registry_descriptor(registry), empty
    )
    return [
        sql
        for _flags, sql in _statements(native._migration_render_sql(plan))
        if "foreign key" in sql
    ]


def test_foreign_key_actions_render() -> None:
    (with_actions,) = [sql for sql in _entry_fk_sql() if '"account_id"' in sql]
    assert "on delete cascade on update restrict" in with_actions


def test_deferrable_foreign_key_renders() -> None:
    (deferred,) = [sql for sql in _entry_fk_sql() if '"parent_id"' in sql]
    assert deferred.endswith("deferrable initially deferred;")


def test_plain_foreign_key_has_no_action_clause() -> None:
    (plain,) = [sql for sql in _entry_fk_sql() if '"plain_id"' in sql]
    assert "on delete" not in plain and "deferrable" not in plain


def test_foreign_key_actions_have_no_manual_operations_and_reverse() -> None:
    registry = Registry(Database(), [Account, Entry], validate_schema="off")
    empty = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    plan = native._migration_plan_descriptors(
        migrations._registry_descriptor(registry), empty
    )
    forward = _statements(native._migration_render_sql(plan))
    assert not any(flags & 2 for flags, _sql in forward)
    reverse = _statements(native._migration_render_sql(native._migration_reverse_plan(plan)))
    assert any("drop constraint" in sql for _flags, sql in reverse)


def test_foreign_key_action_change_is_visible_as_drift() -> None:
    # Same FK, different ON DELETE -> different signature -> the diff sees it.
    class EntryA(Model, table="entries", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        account_id: Mapped[int] = column(Int64, references=Account.id, on_delete="cascade")

    class EntryB(Model, table="entries", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        account_id: Mapped[int] = column(Int64, references=Account.id, on_delete="set null")

    a = migrations._registry_descriptor(
        Registry(Database(), [Account, EntryA], validate_schema="off")
    )
    b = migrations._registry_descriptor(
        Registry(Database(), [Account, EntryB], validate_schema="off")
    )
    plan = native._migration_plan_descriptors(a, b)
    # The differing foreign key appears as an operation rather than being ignored.
    assert plan[8:12] != b"\x00\x00\x00\x00"


def test_invalid_fk_action_is_rejected() -> None:
    with pytest.raises(DeclarationError, match="on_delete"):

        class Broken(Model, table="broken", schema="app"):
            id: Mapped[int] = column(Int64, primary_key=True)
            ref: Mapped[int] = column(Int64, references=Account.id, on_delete="explode")


def test_table_constraints_change_the_deployment_fingerprint() -> None:
    class Plain(Model, table="widgets", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        a: Mapped[int] = column(Int64)
        b: Mapped[int] = column(Int64)

    class Constrained(Model, table="widgets", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        a: Mapped[int] = column(Int64)
        b: Mapped[int] = column(Int64)
        _uq = unique("a", "b")

    plain = Registry(Database(), [Plain], validate_schema="off")
    constrained = Registry(Database(), [Constrained], validate_schema="off")
    assert plain.deployment_fingerprint != constrained.deployment_fingerprint
