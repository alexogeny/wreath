"""A downgrade may not revert a column whose values a re-encode has changed.

The counterpart to ``test_pass_hazards.py``, running the other direction and
keyed on the opposite sense. That refusal blocks *applying* a migration while a
pass is unfinished, and relents the moment the pass publishes -- publication
means every row converted, so the narrowing is then safe.

This one cannot relent, and that is the whole point. A ``Recode`` overwrites
values in place: once it finishes, no original value survives anywhere. So a
*finished* re-encode is the dangerous state rather than the settled one, and the
refusal reads the ledger with no ``verified_at`` filter at all.

What made it worth a refusal rather than a paragraph in the guide is that every
existing check passes. A re-encode touches no schema, so the reverse DDL applies
cleanly and the catalog fingerprint returns to the artifact's source exactly as
it should. ``revert_single_artifact`` verified everything it knows how to verify
and reported success over a column full of values the restored schema was never
written for.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

import wreath.migrations as migrations
from wreath.migrations import DowngradeWouldStrandRecodedData, NativeCatalogSnapshot
from wreath.orm import Mapped, Model, column
from wreath.orm.registry import Registry
from wreath.orm.types import Int64, Text
from wreath.passes import column_fact

native: Any = importlib.import_module("wreath._native._postgres")
MIGRATION_ID = bytes.fromhex("ffeeddccbbaa99887766554433221100")

STATUS_FACT = column_fact("app", "treks", "status")


class Database:
    name = "main"


class Before(Model, table="treks", schema="app"):
    """Before the migration: ``status`` is nullable."""

    id: Mapped[int] = column(Int64, primary_key=True)
    status: Mapped[str] = column(Text, nullable=True)


class After(Model, table="treks", schema="app"):
    """After: ``status`` is NOT NULL. Reverting restores the nullable form."""

    id: Mapped[int] = column(Int64, primary_key=True)
    status: Mapped[str] = column(Text, nullable=False)


def _descriptor(model: type) -> bytes:
    return migrations._registry_descriptor(
        Registry(Database(), [model], validate_schema="off")
    )


def altering_artifact() -> tuple[bytes, bytes, bytes]:
    """An artifact that alters ``app.treks.status``, plus both catalog images."""
    before = _descriptor(Before)
    after = _descriptor(After)
    before_image = native._migration_compile_desired(before)
    after_image = native._migration_compile_desired(after)
    plan = native._migration_plan_descriptors(after, before)
    artifact = migrations._build_native_artifact(
        migration_id=MIGRATION_ID,
        parent_checksum=bytes(32),
        source_fingerprint=migrations._fingerprint_image(before_image),
        target_fingerprint=migrations._fingerprint_image(after_image),
        operation_tape=native._migration_operations_from_plan(plan),
        named_plan=plan,
        sql_tape=native._migration_render_sql(plan),
    )
    return artifact.data, before, after


class RevertConnection:
    """A connection that lets a downgrade run all the way to its DDL.

    Every catalog read is answered so the fingerprint checks pass, because that
    is precisely the condition under which the defect appeared: the schema
    verifies while the data does not.
    """

    def __init__(
        self,
        *,
        artifact: Any,
        before: bytes,
        after: bytes,
        rewritten: list[dict] | None = None,
        ledger_exists: bool = True,
    ):
        self._artifact = artifact
        self._before = before
        self._after = after
        self.rewritten = rewritten or []
        self.ledger_exists = ledger_exists
        self.executed: list[str] = []
        self.ddl_ran = False
        self._catalog_reads = 0

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append(sql)
        if "ALTER" in sql.upper() or "DO $$" in sql:
            self.ddl_ran = True
        return "OK"

    async def fetchval(self, sql: str, *args: object) -> Any:
        if "to_regclass" in sql:
            return self.ledger_exists
        return 1

    async def fetch(self, sql: str, *args: object) -> Any:
        if "rewrites" in sql:
            return list(self.rewritten)
        return []

    def snapshot(self) -> NativeCatalogSnapshot:
        """The catalog as the downgrade would read it: target, then source."""
        self._catalog_reads += 1
        descriptor = self._after if self._catalog_reads == 1 else self._before
        image = native._migration_compile_desired(descriptor)
        return NativeCatalogSnapshot(image, descriptor)

    async def fetchrow(self, sql: str, *args: object) -> Any:
        if "FROM" in sql and "history" in sql.lower():
            return (self._artifact.checksum, self._artifact.target_fingerprint)
        return None


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route ``_decode_catalog_snapshot`` at the fake connection.

    The real one goes through ``connection._fetch_into`` with a native builder;
    reproducing that here would be faking the one thing these tests do not
    exercise.
    """

    async def _snapshot(connection: Any, sql: str, args: tuple = ()) -> Any:
        return connection.snapshot()

    monkeypatch.setattr(migrations, "_decode_catalog_snapshot", _snapshot)


def rewritten_row(**overrides: Any) -> dict[str, Any]:
    """One row as ``rewritten_columns``' query returns it.

    ``ledger_row_present`` is the tie-break that makes the refusal survive a
    purge: the row comes from the append-only ``pass_rewrites`` record joined to
    the ledger, so a missing ledger row is a ``false`` here rather than an
    absent row entirely.
    """
    row = {
        "name": "recode_app_treks_status",
        "tenant": "",
        "fact": STATUS_FACT,
        "phase": "done",
        "ledger_row_present": True,
    }
    row.update(overrides)
    return row


# --- what the plan decoder sees ----------------------------------------------


def test_an_altered_column_is_touched_even_though_it_is_not_narrowed() -> None:
    """``_touched_columns`` is deliberately wider than ``_narrowed_columns``.

    Adding a NOT NULL is not a narrowing in the sense the apply-side refusal
    means -- it loses no rows behind a cursor -- but it absolutely contradicts
    values written under the old definition, so the downgrade side has to see it.
    """
    artifact_data, _, _ = altering_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    reverse = migrations._metal()._migration_reverse_plan(artifact.named_plan)
    touched = migrations._touched_columns(reverse)
    assert ("app", "treks", "status") in {(s, t, c) for s, t, c, _ in touched}


# --- the ledger reader --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_finished_recode_is_still_a_hazard() -> None:
    """The inverse of the apply-side rule, and the reason this exists.

    ``_pending_pass_hazards`` filters on ``verified_at IS NULL`` because a
    published pass has converted everything and is therefore safe. Here
    "converted everything" is exactly the state in which no original value
    remains, so filtering it out would filter out the danger.
    """
    artifact_data, before, after = altering_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    reverse = migrations._metal()._migration_reverse_plan(artifact.named_plan)
    connection = RevertConnection(
        artifact=artifact,
        before=before,
        after=after,
        rewritten=[rewritten_row(phase="done")],
    )
    hazards = await migrations._recoded_column_hazards(connection, reverse)
    assert len(hazards) == 1
    assert hazards[0].finished is True
    assert "has finished" in hazards[0].describe()


@pytest.mark.asyncio
async def test_a_running_recode_is_a_hazard_too() -> None:
    artifact_data, before, after = altering_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    reverse = migrations._metal()._migration_reverse_plan(artifact.named_plan)
    connection = RevertConnection(
        artifact=artifact,
        before=before,
        after=after,
        rewritten=[rewritten_row(phase="walking")],
    )
    hazards = await migrations._recoded_column_hazards(connection, reverse)
    assert len(hazards) == 1
    assert hazards[0].finished is False
    assert "mixture" in hazards[0].describe()


@pytest.mark.asyncio
async def test_a_recode_whose_ledger_row_was_purged_is_still_a_hazard() -> None:
    """The belt failing does not stop the braces.

    A ledger row is working state, and a "tidy up finished passes" job is a
    reasonable thing for someone to write. If the refusal read only that row,
    such a job would silently re-enable the downgrade this module exists to
    prevent -- and it would look like nothing had happened, because a column
    that was never re-encoded is *also* the absence of a row.

    The append-only ``pass_rewrites`` record is what tells those two apart. It
    survives the purge, so the hazard survives with it; the phase does not,
    which is why the message says so rather than inventing one.
    """
    artifact_data, before, after = altering_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    reverse = migrations._metal()._migration_reverse_plan(artifact.named_plan)
    connection = RevertConnection(
        artifact=artifact,
        before=before,
        after=after,
        rewritten=[rewritten_row(phase="", ledger_row_present=False)],
    )
    hazards = await migrations._recoded_column_hazards(connection, reverse)
    assert len(hazards) == 1
    assert hazards[0].ledger_row_present is False
    described = hazards[0].describe()
    assert "ledger row is gone" in described
    assert "values were still changed" in described
    # And it must not claim a phase it cannot read.
    assert "still running" not in described and "has finished" not in described


@pytest.mark.asyncio
async def test_a_column_no_recode_touched_is_not_a_hazard() -> None:
    """The load-bearing negative: this must not block ordinary downgrades."""
    artifact_data, before, after = altering_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    reverse = migrations._metal()._migration_reverse_plan(artifact.named_plan)
    connection = RevertConnection(
        artifact=artifact, before=before, after=after, rewritten=[]
    )
    assert await migrations._recoded_column_hazards(connection, reverse) == ()


@pytest.mark.asyncio
async def test_a_database_that_never_ran_a_pass_has_no_ledger_and_no_hazard() -> None:
    artifact_data, before, after = altering_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    reverse = migrations._metal()._migration_reverse_plan(artifact.named_plan)
    connection = RevertConnection(
        artifact=artifact, before=before, after=after, ledger_exists=False
    )
    assert await migrations._recoded_column_hazards(connection, reverse) == ()


# --- the refusal, end to end --------------------------------------------------


@pytest.mark.asyncio
async def test_reverting_a_recoded_column_is_refused_before_the_ddl_runs(catalog: None) -> None:
    """The defect, pinned.

    Before the fix this call returned a ``MigrationRevertResult`` -- the DDL ran,
    the fingerprint verified, the history row was deleted, and nothing anywhere
    reported a problem. The assertion on ``ddl_ran`` is the part that matters:
    refusing *after* running the DDL would be no fix at all.
    """
    artifact_data, before, after = altering_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    connection = RevertConnection(
        artifact=artifact,
        before=before,
        after=after,
        rewritten=[rewritten_row()],
    )
    # The models have been rolled back with the code, which is what makes this
    # a legitimate downgrade -- `_downgrade_hazards` finds nothing. The data
    # hazard is then the only thing between here and a silently wrong revert.
    registry = Registry(Database(), [Before], validate_schema="off")

    with pytest.raises(DowngradeWouldStrandRecodedData) as caught:
        await migrations.revert_single_artifact(registry, connection, artifact_data)

    assert connection.ddl_ran is False
    message = str(caught.value)
    assert "app.treks.status" in message
    assert "recode_app_treks_status" in message
    assert "Recode declaring the inverse mapping" in message


@pytest.mark.asyncio
async def test_force_does_not_skip_this_refusal(catalog: None) -> None:
    """``force=True`` exists to rewind a local stack past a *code* hazard.

    It cannot mean "and also discard the data", so this refusal is deliberately
    outside the ``if not force`` branch that guards ``DowngradeWouldStrandCode``.
    """
    artifact_data, before, after = altering_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    connection = RevertConnection(
        artifact=artifact,
        before=before,
        after=after,
        rewritten=[rewritten_row()],
    )
    # `After` still maps the column, so `force=True` is needed to clear the code
    # hazard -- and it must not clear this one.
    registry = Registry(Database(), [After], validate_schema="off")

    with pytest.raises(DowngradeWouldStrandRecodedData):
        await migrations.revert_single_artifact(
            registry, connection, artifact_data, force=True
        )
    assert connection.ddl_ran is False


@pytest.mark.asyncio
async def test_an_untouched_schema_still_downgrades(catalog: None) -> None:
    """Without a recode the downgrade proceeds, DDL and all."""
    artifact_data, before, after = altering_artifact()
    artifact = migrations._load_native_artifact(artifact_data)
    connection = RevertConnection(
        artifact=artifact, before=before, after=after, rewritten=[]
    )
    registry = Registry(Database(), [After], validate_schema="off")

    result = await migrations.revert_single_artifact(
        registry, connection, artifact_data, force=True
    )
    assert result.migration_id == MIGRATION_ID
    assert connection.ddl_ran is True
