from __future__ import annotations

from typing import Any

import pytest

from wreath._passes import ledger as _ledger


class Executor:
    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        row: dict[str, Any] | None = None,
        value: Any = True,
    ) -> None:
        self.rows = [] if rows is None else rows
        self.row = row
        self.value = value
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return "UPDATE 1"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        return self.value

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((sql, args))
        return self.row

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return self.rows


def ledger_record(**values: Any) -> dict[str, Any]:
    record = {
        "name": "refresh",
        "tenant": "tenant-a",
        "phase": "walking",
        "cursor": None,
        "ceiling": None,
        "keyspace_from": None,
        "pending": [],
        "units_done": 2,
        "rows_done": 3,
        "denominator": None,
        "denominator_kind": None,
        "chunk_limit": 4,
        "paced_reason": None,
        "window_started": None,
        "window_rows": 5,
        "window_units": 6,
        "started_at": None,
        "last_advance": None,
        "cycle_started": None,
        "driven_at": None,
        "last_drive_error": None,
        "guards": None,
        "rewrites": None,
        "verified_at": None,
        "verified_fact": None,
        "last_error": None,
        "now": None,
        "holes_open": 7,
        "trace_context": "traceparent",
    }
    record.update(values)
    return record


def test_row_decoding_preserves_window_counters() -> None:
    row = _ledger.row_from_record(ledger_record())
    assert (row.window_rows, row.window_units, row.holes_open) == (5, 6, 7)


@pytest.mark.asyncio
async def test_seed_records_a_rewrite_before_the_ledger_row() -> None:
    executor = Executor()
    ledger = _ledger.Ledger(schema="wreath", name="refresh")
    await ledger.seed(executor, chunk_limit=10, rewrites="column:app.items.value")
    assert len(executor.calls) == 3
    assert "pass_rewrites" in executor.calls[0][0]
    assert executor.calls[0][1] == ("column:app.items.value", "refresh", "")
    assert "pg_attribute" in executor.calls[1][0]
    assert 'INSERT INTO "wreath".passes' in executor.calls[2][0]


@pytest.mark.asyncio
async def test_read_selects_and_decodes_trace_context_when_available() -> None:
    executor = Executor(row=ledger_record())
    row = await _ledger.Ledger(schema="wreath", name="refresh").read(executor)
    assert row is not None
    assert row.trace_context == "traceparent"
    assert ", trace_context FROM" in executor.calls[1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cycle", "fragment"),
    [(False, ""), (True, ", cycle_started = clock_timestamp()")],
)
async def test_set_ceiling_changes_cycle_start_only_at_a_cycle_boundary(
    cycle: bool, fragment: str
) -> None:
    executor = Executor()
    await _ledger.Ledger(schema="wreath", name="refresh").set_ceiling(
        executor, ceiling=20, cycle=cycle
    )
    sql = executor.calls[0][0]
    if fragment:
        assert fragment in sql
    else:
        assert "cycle_started" not in sql


@pytest.mark.asyncio
async def test_mark_driven_records_and_clears_the_latest_error() -> None:
    executor = Executor()
    ledger = _ledger.Ledger(schema="wreath", name="refresh")
    await ledger.mark_driven(executor, error="failed")
    await ledger.mark_driven(executor)
    assert executor.calls[0][1][-1] == "failed"
    assert executor.calls[1][1][-1] is None


@pytest.mark.asyncio
async def test_publish_uses_detail_only_when_no_fact_was_named() -> None:
    executor = Executor()
    ledger = _ledger.Ledger(schema="wreath", name="refresh")
    await ledger.publish(executor, fact=None, detail="verified by count")
    await ledger.publish(executor, fact="column:app.items.value", detail="unused")
    assert executor.calls[0][1][-1] == "verified by count"
    assert executor.calls[1][1][-1] == "column:app.items.value"


@pytest.mark.asyncio
async def test_empty_rewrite_query_reads_nothing() -> None:
    executor = Executor(rows=[{"name": "unexpected"}])
    assert await _ledger.rewritten_columns(executor, schema="wreath") == []
    assert executor.calls == []


@pytest.mark.asyncio
async def test_rewrite_union_keeps_the_row_with_live_ledger_state() -> None:
    identity = {
        "name": "refresh",
        "tenant": "",
        "fact": "column:app.items.value",
    }
    executor = Executor(
        rows=[
            {**identity, "phase": "done", "ledger_row_present": True},
            {**identity, "phase": "", "ledger_row_present": False},
        ]
    )
    entries = await _ledger.rewritten_columns(
        executor, schema="wreath", facts=("column:app.items.value",)
    )
    assert len(entries) == 1
    assert entries[0].phase == "done"
    assert entries[0].ledger_row_present is True


@pytest.mark.asyncio
async def test_rewrite_union_upgrades_a_record_to_live_ledger_state() -> None:
    identity = {
        "name": "refresh",
        "tenant": "",
        "fact": "column:app.items.value",
    }
    executor = Executor(
        rows=[
            {**identity, "phase": "", "ledger_row_present": False},
            {**identity, "phase": "done", "ledger_row_present": True},
        ]
    )
    entries = await _ledger.rewritten_columns(
        executor, schema="wreath", facts=("column:app.items.value",)
    )
    assert entries[0].phase == "done"
    assert entries[0].ledger_row_present is True


@pytest.mark.asyncio
async def test_rewrite_union_retains_a_record_without_a_ledger_row() -> None:
    executor = Executor(
        rows=[
            {
                "name": "refresh",
                "tenant": "",
                "fact": "column:app.items.value",
                "phase": "",
                "ledger_row_present": False,
            }
        ]
    )
    entries = await _ledger.rewritten_columns(
        executor, schema="wreath", facts=("column:app.items.value",)
    )
    assert len(entries) == 1
    assert entries[0].ledger_row_present is False
