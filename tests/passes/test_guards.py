"""What a pass claims it will establish, and who can read that claim.

The gate publishes a fact when verification passes. But a migration asking "may
I narrow this column?" needs an answer *before* that, and the dangerous state is
the one in between: a pass exists, it is converting the column, and it has not
finished. Recording the claim only at publication makes that state look
identical to "no pass has ever touched this", so the claim is written when the
ledger row is seeded.
"""

from __future__ import annotations

import pytest

from wreath._passes import ledger as _ledger
from wreath.passes import Gate, NoRowsMatch, PassDeclarationError, column_fact

from .test_gate import convert_pass
from .test_progress import purge_pass

# --- the canonical spelling --------------------------------------------------


def test_a_column_fact_names_schema_table_and_column() -> None:
    assert column_fact("app", "treks", "grade") == "column:app.treks.grade"


def test_column_fact_refuses_a_missing_part() -> None:
    """A fact with a blank in it would match nothing and refuse nothing."""
    with pytest.raises(PassDeclarationError):
        column_fact("app", "", "grade")


def test_both_sides_of_the_contract_spell_it_the_same_way() -> None:
    """The reason this helper exists rather than a documented convention.

    ``migrations.py`` cannot import ``wreath.passes`` on its scan path, so it
    has a private twin. If the two ever drift, a migration sails through a
    refusal it should have hit -- so the twin is pinned here rather than trusted.
    """
    from wreath.migrations import _column_fact

    assert _column_fact("app", "treks", "grade") == column_fact("app", "treks", "grade")


# --- what the pass declares --------------------------------------------------


def test_a_pass_with_no_gate_guards_nothing() -> None:
    assert purge_pass().guards is None


def test_a_pass_whose_gate_publishes_nothing_guards_nothing() -> None:
    """``then=`` without ``publishes=`` runs a step here and tells nobody."""
    walk = purge_pass(
        gate=Gate(verify=NoRowsMatch("expires < now()"), then=_noop, scope="unit")
    )
    assert walk.guards is None


def test_a_pass_guards_the_fact_its_gate_publishes() -> None:
    """A whole-pass gate needs a fixed ceiling, so this is the converting shape."""
    fact = column_fact("app", "treks", "grade")
    walk = convert_pass(
        gate=Gate(verify=NoRowsMatch("grade_text IS NULL"), publishes=fact)
    )
    assert walk.guards == fact


# --- what reaches the ledger -------------------------------------------------


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_seeding_records_the_claim_alongside_the_row() -> None:
    fact = column_fact("app", "treks", "grade")
    executor = RecordingExecutor()
    ledger = _ledger.Ledger(schema="wreath", name="normalize_grades")
    await ledger.seed(executor, chunk_limit=1000, guards=fact)
    sql, args = executor.calls[0]
    assert "guards" in sql
    assert fact in args


@pytest.mark.asyncio
async def test_a_redeploy_updates_the_claim_without_restarting_the_walk() -> None:
    """``DO UPDATE`` on the claim only -- the cursor is not in that SET list.

    A pass whose declaration changed between releases should start guarding the
    new column immediately, but rewinding its position because someone edited a
    gate would be a data-loss bug wearing a config change.
    """
    executor = RecordingExecutor()
    ledger = _ledger.Ledger(schema="wreath", name="normalize_grades")
    await ledger.seed(executor, chunk_limit=1000, guards="column:app.treks.grade")
    sql, _ = executor.calls[0]
    assert "DO UPDATE SET guards = EXCLUDED.guards" in sql
    assert "cursor" not in sql.split("DO UPDATE")[1]


# --- the reader a migration uses ---------------------------------------------


class FactExecutor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        self.queries.append(sql)
        return self.rows


@pytest.mark.asyncio
async def test_pending_facts_asks_only_about_the_columns_it_was_given() -> None:
    executor = FactExecutor([])
    await _ledger.pending_facts(
        executor, schema="wreath", facts=("column:app.treks.grade",)
    )
    # One placeholder per fact, not `= ANY($1)` with a bound array: the driver
    # infers a parameter's type from its Python value and has no case for
    # `list`, so the array form fails against a real server and a fake cannot
    # tell. The property being pinned is that the read is proportional to the
    # migration -- one placeholder, because one fact was asked about.
    assert "guards IN ($1)" in executor.queries[0]
    assert "ANY(" not in executor.queries[0]
    assert "verified_at IS NULL" in executor.queries[0]


@pytest.mark.asyncio
async def test_no_candidate_columns_reads_nothing_at_all() -> None:
    """An empty candidate list honestly means "nothing to check", not "everything"."""
    executor = FactExecutor([{"name": "x"}])
    assert await _ledger.pending_facts(executor, schema="wreath", facts=()) == []
    assert executor.queries == []


@pytest.mark.asyncio
async def test_the_operator_overview_is_a_separate_call_not_an_empty_filter() -> None:
    executor = FactExecutor(
        [
            {
                "name": "normalize_grades",
                "tenant": "",
                "guards": "column:app.treks.grade",
                "phase": "walking",
                "holes_open": 0,
            }
        ]
    )
    entries = await _ledger.all_pending_facts(executor, schema="wreath")
    assert [entry.fact for entry in entries] == ["column:app.treks.grade"]
    assert "guards IS NOT NULL" in executor.queries[0]


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None
