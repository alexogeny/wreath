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
    plan = native._migration_plan_descriptors(migrations._registry_descriptor(registry), empty)
    return [sql for _flags, sql in _statements(native._migration_render_sql(plan))]


def test_composite_unique_and_indexes_render_without_manual_operations() -> None:
    registry = Registry(Database(), [Membership], validate_schema="off")
    empty = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    plan = native._migration_plan_descriptors(migrations._registry_descriptor(registry), empty)
    statements = _statements(native._migration_render_sql(plan))
    assert not any(flags & 2 for flags, _sql in statements)  # nothing MANUAL


def test_composite_unique_names_both_columns() -> None:
    assert any('unique ("org_id", "user_id")' in sql for sql in _forward_sql())


def test_multi_column_and_unique_indexes_render() -> None:
    forward = _forward_sql()
    assert any(sql.startswith("create index ") and '("user_id", "email")' in sql for sql in forward)
    assert any(sql.startswith("create unique index ") and '("email")' in sql for sql in forward)


def test_downgrade_drops_composite_constraints_and_indexes() -> None:
    registry = Registry(Database(), [Membership], validate_schema="off")
    empty = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    plan = native._migration_plan_descriptors(migrations._registry_descriptor(registry), empty)
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
    plan = native._migration_plan_descriptors(migrations._registry_descriptor(registry), empty)
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
    plan = native._migration_plan_descriptors(migrations._registry_descriptor(registry), empty)
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


# `render_column_type` maps a built-in's OID to its SQL spelling through a switch
# in `migration_sql.c`, and a type missing from that switch does not fail loudly:
# it becomes an empty MANUAL statement, so `generate` silently omits the column
# and then emits the indexes and constraints that reference it. Applying such a
# plan fails on a column that was never added.
# Three types were missing when this test was written -- `character varying`,
# `json`, and `bit(n)` -- and `Varchar` is not an obscure corner. The point of
# enumerating rather than adding three cases is that the next type added to
# `wreath.orm.types` is checked without anyone remembering to check it.


def _renders_as(pg_type: Any) -> str:
    """The single `add column` statement a one-column model of this type yields."""

    class Subject(Model, table="subjects", schema="app"):
        id: Mapped[int] = column(Int64, primary_key=True)
        value: Mapped[Any] = column(pg_type)

    registry = Registry(Database(), [Subject], validate_schema="off")
    empty = b"WMD1\x01\x00\x00\x00\x00\x00\x00\x00"
    plan = native._migration_plan_descriptors(migrations._registry_descriptor(registry), empty)
    emitted = _statements(native._migration_render_sql(plan))
    assert not any(flags & 2 for flags, _sql in emitted), (
        f"{pg_type.sql!r} produced a MANUAL statement; its OID {pg_type.oid} is "
        f"probably missing from sql_type_for_oid in migration_sql.c"
    )
    value_statements = [sql for _flags, sql in emitted if '"value"' in sql]
    assert len(value_statements) == 1, emitted
    return value_statements[0]


def _declarable_builtin_types() -> list[tuple[str, Any]]:
    """Every built-in `PgType` `wreath.orm.types` exports, plus `Bit`'s factory.

    Extension types are excluded: their OID is database-assigned and the suite
    that covers them (`test_vector.py`) has to bind one first. Everything here
    has a compile-time OID and needs no server.
    """
    from wreath.orm import types as declared
    from wreath.orm.types import Bit, PgType

    found = [
        (name, getattr(declared, name))
        for name in declared.__all__
        if isinstance(getattr(declared, name), PgType)
    ]
    # `Bit` is a factory rather than a singleton, and is the one built-in whose
    # modifier is part of the type, so it is the case the OID switch cannot serve.
    found.append(("Bit(8)", Bit(8)))
    return sorted(found)


@pytest.mark.parametrize(
    "name,pg_type", _declarable_builtin_types(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_every_declarable_builtin_type_renders_rather_than_going_manual(
    name: str, pg_type: Any
) -> None:
    statement = _renders_as(pg_type)
    assert pg_type.sql in statement, (name, statement)


def test_the_enumeration_actually_found_the_types() -> None:
    names = [name for name, _type in _declarable_builtin_types()]
    assert len(names) > 15, names
    for expected in ("Varchar", "Json", "Bit(8)", "Numeric", "Text"):
        assert expected in names, names
