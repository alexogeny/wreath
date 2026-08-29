from __future__ import annotations

import pytest

from wreath._passes import ledger as _ledger
from wreath.passes import Gate, NoRowsMatch, PassDeclarationError, column_fact

from .test_gate import convert_pass
from .test_progress import purge_pass


def test_a_column_fact_names_schema_table_and_column() -> None:
    assert column_fact("app", "treks", "grade") == "column:app.treks.grade"


def test_column_fact_refuses_a_missing_part() -> None:
    with pytest.raises(PassDeclarationError):
        column_fact("app", "", "grade")


def test_both_sides_of_the_contract_spell_it_the_same_way() -> None:
    from wreath.migrations import _column_fact

    assert _column_fact("app", "treks", "grade") == column_fact("app", "treks", "grade")


def test_a_pass_with_no_gate_guards_nothing() -> None:
    assert purge_pass().guards is None


def test_a_pass_whose_gate_publishes_nothing_guards_nothing() -> None:
    walk = purge_pass(gate=Gate(verify=NoRowsMatch("expires < now()"), then=_noop, scope="unit"))
    assert walk.guards is None


def test_a_pass_guards_the_fact_its_gate_publishes() -> None:
    fact = column_fact("app", "treks", "grade")
    walk = convert_pass(gate=Gate(verify=NoRowsMatch("grade_text IS NULL"), publishes=fact))
    assert walk.guards == fact


class RecordingExecutor:
    #: Whether this stand-in's ledger has the version-2 ``trace_context``
    #: column. `seed` asks once per `Ledger` and caches, so the answer is read
    #: at most once per test here.
    trace_column = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        #: Catalog reads, kept apart from `calls` so the assertions below still
        #: read the statement they are about rather than counting past a probe.
        self.probes: list[str] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return "INSERT 0 1"

    async def fetchval(self, sql: str, *args: object) -> object:
        # Only the column probe reaches here. Answering `None` for "absent" and
        # not `False` is the shape a real `SELECT true ... WHERE` produces: no
        # rows at all, which the driver reads as None.
        self.probes.append(sql)
        return True if self.trace_column else None


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
    executor = RecordingExecutor()
    ledger = _ledger.Ledger(schema="wreath", name="normalize_grades")
    await ledger.seed(executor, chunk_limit=1000, guards="column:app.treks.grade")
    sql, _ = executor.calls[0]
    assert "DO UPDATE SET guards = EXCLUDED.guards" in sql
    assert "cursor" not in sql.split("DO UPDATE")[1]


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
    await _ledger.pending_facts(executor, schema="wreath", facts=("column:app.treks.grade",))
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
