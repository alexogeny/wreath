from __future__ import annotations

import datetime

import pytest

from wreath.passes import (
    Ceiling,
    ChunkedPass,
    DutyCycle,
    Key,
    PassDeclarationError,
    Purge,
    Rows,
    Sealed,
    Table,
)

from .fakes import FakeDatabase, World

NOW = datetime.datetime(2026, 7, 27, 12, 0, tzinfo=datetime.UTC)

EXPIRES = Key("expires", "timestamptz", indexed=True)
KEY = Key("key", "text", unique=True)


async def _nap(_seconds):
    return None


def purge_pass(**overrides):
    options = {
        "over": Table("replays"),
        "units": Rows(key=(EXPIRES, KEY), limit=3, within="2s"),
        "frontier": Sealed(),
        "work": Purge(),
        "pace": DutyCycle(1.0),
        "chunk_retries": 2,
    }
    options.update(overrides)
    return ChunkedPass("purge_replays", **options)


@pytest.fixture
def world():
    rows = [
        {"key": f"k{index:03d}", "expires": NOW - datetime.timedelta(hours=index + 1)}
        for index in range(9)
    ]
    return World("replays", rows)


@pytest.fixture
def database(world):
    return FakeDatabase(world)


def test_a_failure_policy_that_is_neither_halt_nor_skip_is_refused():
    with pytest.raises(PassDeclarationError) as caught:
        purge_pass(on_chunk_failure="continue")

    message = str(caught.value)
    assert "'halt' or 'skip'" in message
    # The message has to say what each one costs, because the choice is the
    # whole point and there is no default that suits both callers.
    assert "bars the terminal gate" in message


def test_zero_retries_is_refused_because_it_would_dead_letter_everything():
    with pytest.raises(PassDeclarationError) as caught:
        purge_pass(chunk_retries=0)

    assert "without running it" in str(caught.value)


def test_halt_is_the_default_because_nothing_should_be_skipped_by_omission():
    assert purge_pass().on_chunk_failure == "halt"


async def test_retry_before_a_pass_has_run_queues_nothing(database, world):
    assert await purge_pass().retry(database) == 0
    assert world.ledger == {}


def test_a_store_purge_opts_into_skip_because_it_has_no_terminal_step():
    from wreath._passes.stores import keyed_purge_pass

    class _Declaration:
        table = "wreath_sessions"
        stamp = "expires"
        key = "id"
        index_stamp = True

    walk = keyed_purge_pass(_Declaration(), name="session_purge")

    # One undeletable row must not stop an expiry purge from keeping the table
    # small forever -- and with no gate, a skip cannot buy anything unsafe.
    assert walk.on_chunk_failure == "skip"


async def test_a_chunk_that_keeps_failing_is_recorded_with_its_position(database, world):
    walk = purge_pass()
    world.before = _boom_on_delete

    await walk.run(database, sleep=_nap)
    holes = await walk.holes(database)

    assert len(holes) == 1
    hole = holes[0]
    assert hole.attempts == 2
    assert "chunk is cursed" in hole.error or "boom" in hole.error
    # The range, not just the error: a hole without a position is an epitaph.
    assert hole.cursor_to is not None


async def test_a_hole_carries_a_statement_an_operator_can_actually_run(database, world):
    walk = purge_pass()
    world.before = _boom_on_delete

    await walk.run(database, sleep=_nap)
    hole = (await walk.holes(database))[0]

    # This is what turns a hole into a task: paste it into psql, in a
    # transaction, and see the real error rather than a three-week-old repr.
    assert hole.predicate.startswith("SELECT * FROM replays WHERE ")
    assert "(expires, key) <=" in hole.predicate
    # Values inlined, because a statement with $1 in it is not runnable by hand.
    assert "$" not in hole.predicate


def _boom_on_delete(sql, args):
    if sql.startswith("DELETE FROM replays"):
        raise RuntimeError("boom")


async def test_halt_stops_at_the_hole_and_runs_nothing_after_it(database, world):
    walk = purge_pass(on_chunk_failure="halt")
    world.before = _boom_on_delete

    result = await walk.run(database, sleep=_nap)

    assert result.stopped == "blocked"
    status = await walk.status(database)
    assert status.phase == "blocked"
    assert status.state == "blocked"
    # Nothing after the hole ran, which is what makes "a backfill with a hole
    # never reaches the terminal gate" structural rather than remembered.
    assert len(world.rows) == 9


async def test_retry_can_clear_a_hole_on_a_halted_pass(database, world):
    walk = purge_pass(on_chunk_failure="halt")
    world.before = _boom_on_delete
    await walk.run(database, sleep=_nap)

    status = await walk.status(database)
    assert status.phase == "blocked"
    assert status.holes_open == 1

    world.before = None
    assert await walk.retry(database) == 1
    await walk.run(database, sleep=_nap)

    cleared = await walk.status(database)
    assert cleared.holes_open == 0
    assert cleared.gate_barred is False
    assert len(world.rows) == 0


async def test_a_blocked_pass_is_not_retried_by_the_next_shift(database, world):
    walk = purge_pass(on_chunk_failure="halt")
    world.before = _boom_on_delete
    await walk.run(database, sleep=_nap)

    world.before = None
    again = await walk.run(database, sleep=_nap)

    # Automatically retrying a halted pass turns `halt` back into `skip` with
    # extra steps. Someone has to clear the hole.
    assert again.stopped == "blocked"
    assert len(world.rows) == 9


async def test_skip_moves_past_the_hole_and_keeps_going(database, world):
    walk = purge_pass(on_chunk_failure="skip")
    failures = {"left": 2}

    def poison_first_chunk(sql, args):
        if sql.startswith("DELETE FROM replays") and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("first chunk is cursed")

    world.before = poison_first_chunk
    result = await walk.run(database, sleep=_nap)

    assert result.holes == 1
    assert result.complete is True
    # The first chunk's rows survive; everything after it was still purged.
    assert len(world.rows) == 3


async def test_a_skipped_chunk_bars_the_terminal_gate(database, world):
    walk = purge_pass(on_chunk_failure="skip")
    failures = {"left": 2}

    def poison_first_chunk(sql, args):
        if sql.startswith("DELETE FROM replays") and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("cursed")

    world.before = poison_first_chunk
    await walk.run(database, sleep=_nap)
    status = await walk.status(database)

    # Skipping bought throughput. It must not have bought the irreversible step.
    assert status.holes_open == 1
    assert status.gate_barred is True


async def test_a_skipped_chunk_does_not_count_as_a_unit_of_work_done(database, world):
    walk = purge_pass(on_chunk_failure="skip")
    failures = {"left": 2}

    def poison_first_chunk(sql, args):
        if sql.startswith("DELETE FROM replays") and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("cursed")

    world.before = poison_first_chunk
    await walk.run(database, sleep=_nap)

    # Three chunks' worth of range, one of them skipped: counting the skip
    # would have the percentage claim progress the pass did not make.
    assert (await walk.status(database)).units_done == 2


async def test_a_later_skip_compares_against_the_cursor_it_followed(database, world):
    walk = purge_pass(on_chunk_failure="skip")
    delete_calls = 0

    def poison_second_chunk(sql, args):
        nonlocal delete_calls
        if sql.startswith("DELETE FROM replays"):
            delete_calls += 1
            if delete_calls in (2, 3):
                raise RuntimeError("second chunk is cursed")

    world.before = poison_second_chunk
    result = await walk.run(database, sleep=_nap)
    skips = [
        args for sql, args in world.statements if "SET cursor = $3::jsonb, last_advance" in sql
    ]
    assert result.complete is True
    assert len(skips) == 1
    assert skips[0][3] is not None


async def test_a_skip_that_loses_its_cursor_swap_stops_as_lost(database, world):
    walk = purge_pass(on_chunk_failure="skip")
    failures = 2

    def poison_then_steal(sql, args):
        nonlocal failures
        if sql.startswith("DELETE FROM replays") and failures:
            failures -= 1
            raise RuntimeError("first chunk is cursed")
        if "SET cursor = $3::jsonb, last_advance" in sql:
            world.before = None
            world.ledger[("purge_replays", "")]["cursor"] = [
                "2026-01-01T00:00:00+00:00",
                "stolen",
            ]

    world.before = poison_then_steal
    result = await walk.run(database, sleep=_nap)
    assert result.stopped == "lost"
    assert result.chunks == 0
    assert result.holes == 0


async def test_retry_clears_the_hole_only_when_the_chunk_succeeds(database, world):
    walk = purge_pass(on_chunk_failure="skip")
    failures = {"left": 2}

    def poison_first_chunk(sql, args):
        if sql.startswith("DELETE FROM replays") and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("cursed")

    world.before = poison_first_chunk
    await walk.run(database, sleep=_nap)
    assert (await walk.status(database)).gate_barred is True

    world.before = None
    queued = await walk.retry(database)
    assert queued == 1
    # Queued is not cleared: the gate is still barred until the work is done.
    assert (await walk.status(database)).gate_barred is True

    await walk.run(database, sleep=_nap)

    status = await walk.status(database)
    assert status.holes_open == 0
    assert status.gate_barred is False
    # And the rows the skip left behind are finally gone.
    assert len(world.rows) == 0


async def test_retry_batches_every_hole_into_one_ledger_update(database, world):
    walk = purge_pass(on_chunk_failure="skip")

    def poison_every_chunk(sql, _args):
        if sql.startswith("DELETE FROM replays"):
            raise RuntimeError("cursed")

    world.before = poison_every_chunk
    await walk.run(database, sleep=_nap)
    assert (await walk.status(database)).holes_open == 3

    world.before = None
    world.statements.clear()
    assert await walk.retry(database) == 3

    assert len(world.statements) == 1
    assert "pass_holes" in world.statements[0][0]
    assert "SET pending" in world.statements[0][0]
    assert await walk.retry(database) == 0
    assert len(world.ledger_row()["pending"]) == 3


async def test_a_requeued_unit_is_walked_without_rewinding_the_cursor(database, world):
    # A pass that finishes and stays finished, so the only thing that could move
    # the cursor backwards is the requeue itself.
    walk = purge_pass(
        frontier=Ceiling.at_launch(monotone="expiry stamps are assigned by now()"),
        pace=DutyCycle(0.25),
    )
    await walk.run(database, sleep=_nap)
    assert len(world.rows) == 0
    before = (await walk.status(database)).cursor
    assert before is not None

    # A row arrives behind the cursor after the walk went past it -- the late
    # correction case. Rewinding to collect it would redo the whole walk.
    world.rows.append({"key": "late", "expires": NOW - datetime.timedelta(hours=5)})

    queued = await walk.requeue(
        database,
        (NOW - datetime.timedelta(hours=5), "late"),
        after=(NOW - datetime.timedelta(hours=6), "k000"),
    )
    assert queued is True

    naps: list[float] = []

    async def record_rest(seconds: float) -> None:
        naps.append(seconds)

    await walk.run(database, sleep=record_rest)

    assert len(world.rows) == 0
    assert naps and all(seconds > 0 for seconds in naps)
    # The cursor never moved backwards to do it.
    assert (await walk.status(database)).cursor == before


async def test_a_requeued_unit_failure_is_counted_as_a_hole(database, world):
    walk = purge_pass(frontier=Ceiling.at_launch(monotone="expiry stamps are assigned by now()"))
    await walk.run(database, sleep=_nap)
    world.rows.append({"key": "late", "expires": NOW - datetime.timedelta(hours=5)})
    assert await walk.requeue(
        database,
        (NOW - datetime.timedelta(hours=5), "late"),
        after=(NOW - datetime.timedelta(hours=6), "k000"),
    )

    def fail_delete(sql, args):
        if sql.startswith("DELETE FROM replays"):
            raise RuntimeError("cannot delete late row")

    world.before = fail_delete
    naps: list[float] = []

    async def record_rest(seconds: float) -> None:
        naps.append(seconds)

    result = await walk.run(database, sleep=record_rest)
    assert result.complete is True
    assert result.holes == 1
    assert len(world.rows) == 1
    assert len(naps) == 1
    assert naps[0] > 0


async def test_requeueing_the_same_unit_twice_is_harmless(database, world):
    walk = purge_pass()
    unit = (NOW - datetime.timedelta(hours=5), "k004")

    assert await walk.requeue(database, unit) is True
    assert await walk.requeue(database, unit) is False
    assert (await walk.status(database)).pending == 1


async def test_a_unit_with_the_wrong_number_of_key_values_is_refused(database):
    walk = purge_pass()

    with pytest.raises(PassDeclarationError) as caught:
        await walk.requeue(database, NOW)

    assert "2 value(s)" in str(caught.value)


async def test_a_requeued_unit_that_still_fails_stops_being_pending(database, world):
    walk = purge_pass(on_chunk_failure="skip")
    world.before = _boom_on_delete
    await walk.run(database, sleep=_nap)

    await walk.retry(database)
    assert (await walk.status(database)).pending >= 1

    # It fails again. It must not sit at the head of the queue forever, taking
    # every shift's first chunk and starving the walk.
    await walk.run(database, sleep=_nap)

    status = await walk.status(database)
    assert status.pending == 0
    assert status.holes_open >= 1


async def test_a_hole_that_cannot_be_recorded_stops_the_walk(database, world):
    walk = purge_pass(on_chunk_failure="skip")

    def poison_the_chunk_and_its_hole(sql, args):
        if sql.startswith("DELETE FROM replays"):
            raise RuntimeError("cursed chunk")
        if "pass_holes" in sql and sql.lstrip().upper().startswith("INSERT"):
            raise RuntimeError("hole table unreachable")

    world.before = poison_the_chunk_and_its_hole

    with pytest.raises(RuntimeError, match="hole table unreachable"):
        await walk.run(database, sleep=_nap)

    # And the gate is barred by the pass not having progressed, rather than by
    # a hole nobody managed to write.
    status = await walk.status(database)
    assert status.cursor is None
