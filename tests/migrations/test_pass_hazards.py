"""A migration may not narrow a column a chunked pass is still converting.

The fifth refusal in ``apply_single_artifact``, and the one design 24 (deferred
data migrations) cannot ship without: dropping ``grade`` while a pass is still
filling ``grade_next`` loses every row behind the cursor, and the failure is
silent because the DDL succeeds.

The interesting half is what these tests prove the scan does *not* do. A scan
that refused whenever a column was narrowed would be trivially safe and useless
-- it would block every ordinary migration forever -- so "an unguarded column is
allowed" and "a published pass stops guarding" are as load-bearing as the
refusal itself.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

import wreath.migrations as migrations
from wreath.migrations import MigrationBlockedByPass, NativeCatalogSnapshot
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text
from wreath.passes import column_fact

native: Any = importlib.import_module("wreath._native._postgres")
MIGRATION_ID = bytes.fromhex("00112233445566778899aabbccddeeff")

GRADE_FACT = column_fact("app", "treks", "grade")


class Database:
    name = "main"


class Wide(Model, table="treks", schema="app"):
    """The shape before the narrowing migration: ``grade`` still present."""

    id: Mapped[int] = column(Int64, primary_key=True)
    grade: Mapped[str] = column(Text, nullable=True)


class Narrow(Model, table="treks", schema="app"):
    """The shape after: ``grade`` dropped."""

    id: Mapped[int] = column(Int64, primary_key=True)


def _descriptor(model: type) -> bytes:
    return migrations._registry_descriptor(
        Registry(Database(), [model], validate_schema="off")
    )


def narrowing_artifact() -> tuple[bytes, NativeCatalogSnapshot, NativeCatalogSnapshot]:
    """An artifact that drops ``app.treks.grade``."""
    wide = _descriptor(Wide)
    narrow = _descriptor(Narrow)
    wide_image = native._migration_compile_desired(wide)
    narrow_image = native._migration_compile_desired(narrow)
    plan = native._migration_plan_descriptors(narrow, wide)
    artifact = migrations._build_native_artifact(
        migration_id=MIGRATION_ID,
        parent_checksum=bytes(32),
        source_fingerprint=migrations._fingerprint_image(wide_image),
        target_fingerprint=migrations._fingerprint_image(narrow_image),
        operation_tape=native._migration_operations_from_plan(plan),
        named_plan=plan,
        sql_tape=native._migration_render_sql(plan),
    )
    return (
        artifact.data,
        NativeCatalogSnapshot(wide_image, wide),
        NativeCatalogSnapshot(narrow_image, narrow),
    )


class LedgerConnection:
    """Answers the two questions the scan asks, and records that it asked."""

    def __init__(self, *, table_exists: bool = True, rows: list[dict] | None = None):
        self.table_exists = table_exists
        self.rows = rows or []
        self.executed: list[tuple[object, ...]] = []
        self.asked_for: list[Any] = []

    async def execute(self, *args: object) -> str:
        self.executed.append(args)
        return "OK"

    async def fetchval(self, sql: str, *args: object) -> Any:
        self.executed.append((sql, *args))
        if "to_regclass" in sql:
            return self.table_exists
        return None

    async def fetch(self, sql: str, *args: object) -> Any:
        self.executed.append((sql, *args))
        # Every fact is its own placeholder now, so record the whole tuple:
        # `= ANY($1)` with one bound array does not survive the real driver,
        # which infers a parameter's type from its Python value and has no
        # case for `list`.
        self.asked_for.append(list(args))
        return list(self.rows)

    async def fetchrow(self, *args: object) -> Any:
        self.executed.append(args)
        return None


def pending_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "name": "normalize_trek_grades",
        "tenant": "",
        "guards": GRADE_FACT,
        "phase": "walking",
        "holes_open": 0,
    }
    row.update(overrides)
    return row


# --- what the plan decoder sees ----------------------------------------------


def test_a_dropped_column_is_a_narrowing() -> None:
    artifact_data, _, _ = narrowing_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    narrowed = migrations._narrowed_columns(artifact.named_plan)
    assert ("app", "treks", "grade", "drop") in narrowed


def test_an_added_column_is_not_a_narrowing() -> None:
    """The inverse artifact adds ``grade``; nothing is being taken away."""
    wide, narrow = _descriptor(Wide), _descriptor(Narrow)
    plan = native._migration_plan_descriptors(wide, narrow)
    assert migrations._narrowed_columns(plan) == ()


# --- the scan ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pass_still_converting_the_column_is_a_hazard() -> None:
    artifact_data, _, _ = narrowing_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    connection = LedgerConnection(rows=[pending_row()])
    hazards = await migrations._pending_pass_hazards(connection, artifact.named_plan)
    assert len(hazards) == 1
    assert hazards[0].column == "grade"
    assert hazards[0].pass_name == "normalize_trek_grades"
    assert hazards[0].action == "drop"
    # The scan asks only about the columns this migration touches, so a large
    # ledger costs nothing to a migration that narrows one column.
    assert connection.asked_for == [[GRADE_FACT]]  # one fact -> one placeholder


@pytest.mark.asyncio
async def test_an_unguarded_column_is_not_a_hazard() -> None:
    """No pass claims it, so nothing is refused. The scan is not a blanket no."""
    artifact_data, _, _ = narrowing_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    connection = LedgerConnection(rows=[])
    assert await migrations._pending_pass_hazards(connection, artifact.named_plan) == ()


@pytest.mark.asyncio
async def test_a_published_pass_stops_guarding_its_column() -> None:
    """The gate's whole purpose: publication is what unblocks the narrowing.

    ``pending_facts`` filters on ``verified_at IS NULL`` in SQL, so a published
    pass simply does not come back -- which is why this asserts on an empty
    result rather than on a flag in the row.
    """
    artifact_data, _, _ = narrowing_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    connection = LedgerConnection(rows=[])
    hazards = await migrations._pending_pass_hazards(connection, artifact.named_plan)
    assert hazards == ()
    sql = [str(entry[0]) for entry in connection.executed if "guards" in str(entry[0])]
    assert any("verified_at IS NULL" in statement for statement in sql)


@pytest.mark.asyncio
async def test_a_database_that_never_ran_a_pass_is_not_an_error() -> None:
    """No ledger table is the answer "nothing is converting", not a failure."""
    artifact_data, _, _ = narrowing_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    connection = LedgerConnection(table_exists=False)
    assert await migrations._pending_pass_hazards(connection, artifact.named_plan) == ()
    assert connection.asked_for == []


# --- what the operator is told -----------------------------------------------


def test_the_refusal_names_the_pass_the_column_and_the_way_out() -> None:
    hazard = migrations.PendingPassHazard(
        schema="app", table="treks", column="grade", action="drop",
        pass_name="normalize_trek_grades", tenant="", phase="walking", holes_open=0,
    )
    error = MigrationBlockedByPass("app", (hazard,))
    message = str(error)
    assert "app.treks.grade" in message
    assert "normalize_trek_grades" in message
    assert "wreath passes status" in message


def test_a_barred_gate_is_called_out_because_waiting_will_not_clear_it() -> None:
    """A hole means the pass *cannot* publish, so "wait for it" is wrong advice."""
    hazard = migrations.PendingPassHazard(
        schema="app", table="treks", column="grade", action="drop",
        pass_name="normalize_trek_grades", tenant="", phase="blocked", holes_open=2,
    )
    message = str(MigrationBlockedByPass("app", (hazard,)))
    assert "wreath passes retry" in message
    assert "2 chunk(s) given up on" in message


def test_a_retype_is_described_as_a_retype_not_a_drop() -> None:
    hazard = migrations.PendingPassHazard(
        schema="app", table="treks", column="grade", action="alter",
        pass_name="p", tenant="", phase="walking", holes_open=0,
    )
    assert "changes the type of" in hazard.describe()


# --- through apply_single_artifact -------------------------------------------


@pytest.mark.asyncio
async def test_apply_refuses_and_rolls_back_before_running_any_ddl(monkeypatch) -> None:
    """The refusal sits inside the same transaction as the other four checks."""
    artifact_data, source, _ = narrowing_artifact()
    registry = Registry(Database(), [Narrow], validate_schema="off")
    connection = LedgerConnection(rows=[pending_row()])

    async def fake_snapshot(*_args: object, **_kwargs: object) -> NativeCatalogSnapshot:
        return source

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", fake_snapshot)
    monkeypatch.setattr(
        migrations, "_bootstrap_migration_history", _noop
    )

    # Destructive approval is a *separate* gate, checked before the transaction
    # opens: a drop without it never gets this far. Granting it here is what
    # makes this test about the pass refusal rather than about that one.
    with pytest.raises(MigrationBlockedByPass) as raised:
        await migrations.apply_single_artifact(
            registry, connection, artifact_data, allow_destructive=True
        )

    assert "normalize_trek_grades" in str(raised.value)
    statements = [str(entry[0]) for entry in connection.executed]
    assert "ROLLBACK" in statements
    assert "COMMIT" not in statements
    # Nothing ran: no DDL block, and no history row for a migration that was
    # refused. A rejected narrowing must leave the database exactly as it was.
    assert not any("ALTER TABLE" in statement for statement in statements)
    assert not any("INSERT INTO" in statement for statement in statements)


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None
