from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import wreath._passes.driver as driver
from wreath._passes.ledger import BLOCKED, DONE, UNVERIFIED, WALKING
from wreath.passes import Key

KEY = Key("id", "bigint", unique=True)


def _row(**values):
    defaults = {
        "phase": WALKING,
        "pending": [],
        "trace_context": None,
        "cursor": None,
        "ceiling": None,
        "denominator": 10,
        "denominator_kind": "estimated",
        "keyspace_from": None,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_chunk_retry_bounds_are_pinned() -> None:
    assert driver.RETRY_BASE_SECONDS == 0.05
    assert driver.RETRY_CAP_SECONDS == 2.0


@pytest.mark.asyncio
async def test_measure_keeps_a_current_denominator() -> None:
    ledger = SimpleNamespace(set_denominator=AsyncMock(), set_keyspace_floor=AsyncMock())
    progress = SimpleNamespace(kind="estimated", measure=AsyncMock())
    walk = SimpleNamespace(ledger=ledger, progress=progress, table="items")

    await driver._measure(walk, object(), _row(), (KEY,))

    progress.measure.assert_not_awaited()
    ledger.set_denominator.assert_not_awaited()


@pytest.mark.asyncio
async def test_measure_refreshes_a_missing_denominator() -> None:
    ledger = SimpleNamespace(set_denominator=AsyncMock(), set_keyspace_floor=AsyncMock())
    progress = SimpleNamespace(kind="estimated", measure=AsyncMock(return_value=12))
    walk = SimpleNamespace(ledger=ledger, progress=progress, table="items")

    await driver._measure(walk, object(), _row(denominator=None), (KEY,))

    ledger.set_denominator.assert_awaited_once()


@pytest.mark.asyncio
async def test_measure_refreshes_a_denominator_of_another_kind() -> None:
    ledger = SimpleNamespace(set_denominator=AsyncMock(), set_keyspace_floor=AsyncMock())
    progress = SimpleNamespace(kind="exact", measure=AsyncMock(return_value=12))
    walk = SimpleNamespace(ledger=ledger, progress=progress, table="items")

    await driver._measure(walk, object(), _row(), (KEY,))

    ledger.set_denominator.assert_awaited_once()


@pytest.mark.asyncio
async def test_measure_keeps_an_existing_keyspace_floor() -> None:
    ledger = SimpleNamespace(set_denominator=AsyncMock(), set_keyspace_floor=AsyncMock())
    progress = SimpleNamespace(kind="keyspace", measure=AsyncMock())
    connection = SimpleNamespace(fetchrow=AsyncMock())
    walk = SimpleNamespace(ledger=ledger, progress=progress, table="items")
    row = _row(denominator_kind="keyspace", keyspace_from="existing")

    await driver._measure(walk, connection, row, (KEY,))

    connection.fetchrow.assert_not_awaited()
    ledger.set_keyspace_floor.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_keyspace_measure_never_reads_a_floor() -> None:
    ledger = SimpleNamespace(set_denominator=AsyncMock(), set_keyspace_floor=AsyncMock())
    progress = SimpleNamespace(kind="estimated", measure=AsyncMock())
    connection = SimpleNamespace(fetchrow=AsyncMock())
    walk = SimpleNamespace(ledger=ledger, progress=progress, table="items")

    await driver._measure(walk, connection, _row(), (KEY,))

    connection.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_finished_nonrecurring_pass_with_pending_work_reenters_shift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = driver.ShiftResult(stopped="stopping")
    shift_bound = AsyncMock(return_value=expected)
    monkeypatch.setattr(driver, "_shift_bound", shift_bound)
    ledger = SimpleNamespace(
        seed=AsyncMock(),
        set_pacing=AsyncMock(),
        read=AsyncMock(return_value=_row(phase=DONE, pending=[1])),
    )
    walk = SimpleNamespace(
        ledger=ledger,
        units=SimpleNamespace(limit=1),
        guards=(),
        rewrites=(),
        pace=SimpleNamespace(reason="test"),
        frontier=SimpleNamespace(recurring=False),
    )

    result = await driver._shift(
        walk,
        object(),
        stopping=None,
        deadline=None,
        sleeper=AsyncMock(),
        clock=lambda: 0.0,
    )

    assert result is expected
    shift_call = shift_bound.await_args
    assert shift_call is not None
    assert shift_call.kwargs["finished_but_requeued"] is True


@pytest.mark.asyncio
async def test_recurring_pass_does_not_treat_pending_as_finished_requeue() -> None:
    ledger = SimpleNamespace(
        seed=AsyncMock(),
        set_pacing=AsyncMock(),
        read=AsyncMock(return_value=_row(phase=DONE, pending=[1])),
        begin_cycle=AsyncMock(return_value=False),
    )
    walk = SimpleNamespace(
        ledger=ledger,
        units=SimpleNamespace(limit=1),
        guards=(),
        rewrites=(),
        pace=SimpleNamespace(reason="test"),
        frontier=SimpleNamespace(recurring=True),
    )

    result = await driver._shift(
        walk,
        object(),
        stopping=None,
        deadline=None,
        sleeper=AsyncMock(),
        clock=lambda: 0.0,
    )

    assert result.stopped == "lost"
    ledger.begin_cycle.assert_awaited_once()


@pytest.mark.asyncio
async def test_finished_nonrecurring_pass_without_pending_work_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shift_bound = AsyncMock()
    monkeypatch.setattr(driver, "_shift_bound", shift_bound)
    ledger = SimpleNamespace(
        seed=AsyncMock(), set_pacing=AsyncMock(), read=AsyncMock(return_value=_row(phase=DONE))
    )
    walk = SimpleNamespace(
        ledger=ledger,
        units=SimpleNamespace(limit=1),
        guards=(),
        rewrites=(),
        pace=SimpleNamespace(reason="test"),
        frontier=SimpleNamespace(recurring=False),
    )

    result = await driver._shift(
        walk,
        object(),
        stopping=None,
        deadline=None,
        sleeper=AsyncMock(),
        clock=lambda: 0.0,
    )

    assert result.complete is True
    shift_bound.assert_not_awaited()


@pytest.mark.asyncio
async def test_recurring_cycle_refuses_a_vanished_ledger_row() -> None:
    ledger = SimpleNamespace(
        seed=AsyncMock(),
        set_pacing=AsyncMock(),
        read=AsyncMock(side_effect=[_row(phase=DONE), None]),
        begin_cycle=AsyncMock(return_value=True),
    )
    walk = SimpleNamespace(
        ledger=ledger,
        units=SimpleNamespace(limit=1),
        guards=(),
        rewrites=(),
        pace=SimpleNamespace(reason="test"),
        frontier=SimpleNamespace(recurring=True),
    )

    result = await driver._shift(
        walk,
        object(),
        stopping=None,
        deadline=None,
        sleeper=AsyncMock(),
        clock=lambda: 0.0,
    )

    assert result.stopped == "failed"
    assert result.error == "the ledger row vanished"


@pytest.mark.asyncio
async def test_shift_refuses_a_ledger_row_that_was_not_seeded() -> None:
    ledger = SimpleNamespace(
        seed=AsyncMock(), set_pacing=AsyncMock(), read=AsyncMock(return_value=None)
    )
    walk = SimpleNamespace(
        ledger=ledger,
        units=SimpleNamespace(limit=1),
        guards=(),
        rewrites=(),
        pace=SimpleNamespace(reason="test"),
        frontier=SimpleNamespace(recurring=False),
    )

    result = await driver._shift(
        walk,
        object(),
        stopping=None,
        deadline=None,
        sleeper=AsyncMock(),
        clock=lambda: 0.0,
    )

    assert result.stopped == "failed"
    assert result.error == "the ledger row could not be seeded"


@pytest.mark.asyncio
async def test_stopped_phase_never_enters_the_walk_loop() -> None:
    result = await driver._shift_bound(
        SimpleNamespace(ledger=object(), units=SimpleNamespace(keys=())),
        object(),
        row=_row(phase=BLOCKED),
        finished_but_requeued=False,
        stopping=None,
        deadline=None,
        sleeper=AsyncMock(),
        clock=lambda: 0.0,
    )

    assert result.stopped == "blocked"
    assert result.error == "pass is blocked"


@pytest.mark.asyncio
async def test_unexpected_nonwalking_phase_never_enters_the_walk_loop() -> None:
    result = await driver._shift_bound(
        SimpleNamespace(ledger=object(), units=SimpleNamespace(keys=())),
        object(),
        row=_row(phase=DONE),
        finished_but_requeued=False,
        stopping=None,
        deadline=None,
        sleeper=AsyncMock(),
        clock=lambda: 0.0,
    )

    assert result.stopped == "blocked"
    assert result.error == "pass is done"


def _ceiling_walk(*, recurring: bool, read: object = None) -> tuple[SimpleNamespace, AsyncMock]:
    derive = AsyncMock(return_value="derived")
    ledger = SimpleNamespace(
        set_ceiling=AsyncMock(),
        read=AsyncMock(return_value=read),
    )
    walk = SimpleNamespace(
        ledger=ledger,
        units=SimpleNamespace(keys=(KEY,)),
        frontier=SimpleNamespace(recurring=recurring, derive=derive, predicate=Mock()),
        table="items",
    )
    return walk, derive


async def _run_until_stopping(
    monkeypatch: pytest.MonkeyPatch, walk: SimpleNamespace, row: SimpleNamespace
) -> AsyncMock:
    measure = AsyncMock()
    monkeypatch.setattr(driver, "_measure", measure)
    stopping = asyncio.Event()
    stopping.set()
    result = await driver._shift_bound(
        walk,
        object(),
        row=row,
        finished_but_requeued=False,
        stopping=stopping,
        deadline=None,
        sleeper=AsyncMock(),
        clock=lambda: 0.0,
    )
    assert result.stopped == "stopping"
    return measure


@pytest.mark.asyncio
async def test_fixed_nonrecurring_ceiling_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    walk, derive = _ceiling_walk(recurring=False)

    await _run_until_stopping(monkeypatch, walk, _row(ceiling="fixed"))

    derive.assert_not_awaited()


@pytest.mark.asyncio
async def test_recurring_ceiling_is_reused_after_the_cursor_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    walk, derive = _ceiling_walk(recurring=True)
    cursor = driver.keyset.encode_cursor((KEY,), (1,))

    await _run_until_stopping(monkeypatch, walk, _row(ceiling="fixed", cursor=cursor))

    derive.assert_not_awaited()


@pytest.mark.asyncio
async def test_derived_ceiling_uses_the_refreshed_ledger_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshed = _row(ceiling="derived")
    walk, derive = _ceiling_walk(recurring=False, read=refreshed)

    measure = await _run_until_stopping(monkeypatch, walk, _row(ceiling=None))

    derive.assert_awaited_once()
    measure_call = measure.await_args
    assert measure_call is not None
    assert measure_call.args[2] is refreshed


@pytest.mark.asyncio
async def test_derived_ceiling_keeps_the_original_row_when_refresh_vanishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _row(ceiling=None)
    walk, _derive = _ceiling_walk(recurring=False, read=None)

    measure = await _run_until_stopping(monkeypatch, walk, original)

    measure_call = measure.await_args
    assert measure_call is not None
    assert measure_call.args[2] is original


@pytest.mark.asyncio
async def test_failed_attempt_records_its_lower_cursor() -> None:
    ledger = SimpleNamespace(record_hole=AsyncMock(), block=AsyncMock())
    units = SimpleNamespace(reproduce=Mock(return_value="id > 1"))
    walk = SimpleNamespace(
        ledger=ledger,
        units=units,
        table="items",
        chunk_retries=0,
        on_chunk_failure="halt",
    )

    await driver._attempt(
        walk,
        object(),
        keys=(KEY,),
        cursor_from=(1,),
        cursor_to=(2,),
        expected=(1,),
        holes_open=False,
        frontier_sql=lambda _binds: None,
        sleeper=AsyncMock(),
        pending=None,
    )

    assert ledger.record_hole.await_args.kwargs["cursor_from"] is not None


def _chunk_walk() -> tuple[SimpleNamespace, AsyncMock]:
    ledger = SimpleNamespace(
        advance=AsyncMock(return_value=True),
        count_rows=AsyncMock(),
        clear_hole=AsyncMock(),
    )
    units = SimpleNamespace(within=1.0, chunk_where=Mock(return_value="TRUE"))
    work = SimpleNamespace(apply=AsyncMock(return_value=1))
    return (
        SimpleNamespace(
            ledger=ledger,
            units=units,
            work=work,
            table="items",
            model=None,
            alias="",
        ),
        ledger.clear_hole,
    )


def _connection() -> SimpleNamespace:
    transaction = AsyncMock()
    transaction.__aenter__.return_value = AsyncMock()
    return SimpleNamespace(transaction=Mock(return_value=transaction))


@pytest.mark.asyncio
async def test_clean_ordinary_chunk_does_not_clear_a_hole() -> None:
    walk, clear_hole = _chunk_walk()

    assert await driver._run_chunk(
        walk,
        _connection(),
        keys=(KEY,),
        cursor_from=None,
        cursor_to=(1,),
        expected=None,
        holes_open=False,
        frontier_sql=lambda _binds: None,
    ) == (True, 1)
    clear_hole.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_chunk_clears_an_open_hole() -> None:
    walk, clear_hole = _chunk_walk()

    await driver._run_chunk(
        walk,
        _connection(),
        keys=(KEY,),
        cursor_from=None,
        cursor_to=(1,),
        expected=None,
        holes_open=True,
        frontier_sql=lambda _binds: None,
    )
    clear_hole.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_chunk_clears_its_hole() -> None:
    walk, clear_hole = _chunk_walk()

    await driver._run_chunk(
        walk,
        _connection(),
        keys=(KEY,),
        cursor_from=None,
        cursor_to=(1,),
        expected=None,
        holes_open=False,
        frontier_sql=lambda _binds: None,
        pending={"from": None, "to": "cursor"},
    )
    clear_hole.assert_awaited_once()


@pytest.mark.asyncio
async def test_finish_keeps_the_existing_row_when_refresh_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _row()
    ledger = SimpleNamespace(
        open_holes=AsyncMock(return_value=0),
        set_phase=AsyncMock(return_value=True),
        read=AsyncMock(return_value=None),
    )
    run_gate = AsyncMock(return_value=driver.ShiftResult(complete=True))
    monkeypatch.setattr(driver, "_run_gate", run_gate)
    walk = SimpleNamespace(ledger=ledger, gate=SimpleNamespace(scope="pass"))

    await driver._finish(walk, object(), chunks=1, rows=2, holes=0, row=original)

    gate_call = run_gate.await_args
    assert gate_call is not None
    assert gate_call.kwargs["row"] is original


@pytest.mark.asyncio
async def test_unit_gate_distinguishes_transient_and_permanent_refusals() -> None:
    for transient, phase in ((True, BLOCKED), (False, UNVERIFIED)):
        verdict = SimpleNamespace(ok=False, transient=transient, detail="no")
        ledger = SimpleNamespace(block=AsyncMock())
        verify = SimpleNamespace(check=AsyncMock(return_value=verdict))
        units = SimpleNamespace(chunk_where=Mock(return_value="TRUE"))
        walk = SimpleNamespace(
            ledger=ledger,
            gate=SimpleNamespace(verify=verify, then=None),
            units=units,
        )

        assert await driver._gate_unit(
            walk, object(), cursor_from=None, cursor_to=(1,)
        ) == "no"
        assert ledger.block.await_args.kwargs["phase"] == phase


@pytest.mark.asyncio
async def test_successful_unit_gate_without_a_terminal_step_returns_cleanly() -> None:
    verdict = SimpleNamespace(ok=True, transient=False, detail="ok")
    verify = SimpleNamespace(check=AsyncMock(return_value=verdict))
    walk = SimpleNamespace(
        ledger=SimpleNamespace(block=AsyncMock()),
        gate=SimpleNamespace(verify=verify, then=None),
        units=SimpleNamespace(chunk_where=Mock(return_value="TRUE")),
    )

    assert await driver._gate_unit(walk, object(), cursor_from=None, cursor_to=(1,)) is None
