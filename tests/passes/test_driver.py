from __future__ import annotations

import asyncio
import datetime
import re

import pytest

from wreath._passes.ledger import DONE, WALKING
from wreath.passes import (
    Apply,
    Ceiling,
    ChunkedPass,
    Declared,
    DutyCycle,
    Key,
    Purge,
    Rewrite,
    Rows,
    Sealed,
    Sql,
    Table,
)

from .conftest import NOW
from .fakes import FakeDatabase, World

EXPIRES = Key("expires", "timestamptz", indexed=True)
KEY = Key("key", "text", unique=True)
ID = Key("id", "int8", indexed=True, unique=True, monotone=True)


async def _nap(_seconds):
    """Pacing without the wall clock: assert the rest happened, do not serve it."""
    await asyncio.sleep(0)


def purge_pass(**overrides):
    options = {
        "over": Table("replays"),
        "units": Rows(key=(EXPIRES, KEY), limit=3, within="2s"),
        "frontier": Sealed(),
        "work": Purge(),
        "pace": DutyCycle(1.0),
    }
    options.update(overrides)
    return ChunkedPass("purge_replays", **options)


async def test_a_walk_removes_everything_behind_the_frontier_and_nothing_ahead(database, world):
    walk = purge_pass()

    result = await walk.run(database, sleep=_nap)

    assert result.complete is True
    assert result.rows == 10
    # The three rows that have not expired are ahead of the frontier, and a pass
    # never touches what it was not pointed at.
    assert sorted(row["key"] for row in world.rows) == ["live0", "live1", "live2"]


async def test_the_walk_is_chunked_rather_than_one_statement(database, world):
    walk = purge_pass()

    result = await walk.run(database, sleep=_nap)

    # Ten rows in chunks of three: four chunks, four transactions, four deletes.
    assert result.chunks == 4
    assert len([sql for sql, _ in world.statements if sql == "BEGIN"]) == 4
    assert len(world.sql_of("DELETE FROM replays")) == 4


async def test_every_chunk_runs_in_its_own_transaction_and_commits(world, database):
    await purge_pass().run(database, sleep=_nap)

    tags = [sql for sql, _ in world.statements if sql in ("BEGIN", "COMMIT", "ROLLBACK")]
    # Perfectly alternating: no transaction is ever left open across a chunk
    # boundary, which is the property the whole design is built to buy.
    assert tags == ["BEGIN", "COMMIT"] * 4


async def test_each_chunk_bounds_its_own_statement_time(world, database):
    await purge_pass().run(database, sleep=_nap)

    timeouts = world.sql_of("SET LOCAL statement_timeout")
    # A chunk that hits a lock wait must die as a chunk failure rather than
    # becoming the long transaction a pass exists to avoid.
    assert timeouts and all("2000" in sql for sql in timeouts)
    assert world.sql_of("SET LOCAL idle_in_transaction_session_timeout")


async def test_the_cursor_is_bound_never_interpolated(world, database):
    await purge_pass().run(database, sleep=_nap)

    deletes = world.sql_of("DELETE FROM replays")
    # The first chunk opens at the start of the domain, so it carries no lower
    # bound and its text differs. Every chunk after it is textually identical,
    # which is what keeps the driver's prepared-statement cache at one entry for
    # the rest of the walk however long it runs.
    assert len(set(deletes[1:])) == 1
    assert all(str(NOW.year) not in sql for sql in deletes)
    assert all("$1" in sql for sql in deletes)


async def test_a_composite_walk_emits_one_row_comparison(world, database):
    await purge_pass().run(database, sleep=_nap)

    # The first probe has no cursor to compare against; the row comparison is
    # what every probe after it uses to resume.
    probe = next(
        sql
        for sql in world.sql_of("SELECT expires, key")
        if "OFFSET" in sql and "(expires, key)" in sql
    )
    # A row constructor compared to a row constructor, whatever the binds are
    # numbered -- that is the form PostgreSQL answers with one index scan.
    assert re.search(r"\(expires, key\) > \(\$\d+, \$\d+\)", probe)
    # Never the hand-expanded OR form: it means the same thing and costs the
    # single index descent the complexity argument rests on.
    assert " OR " not in probe


async def test_a_chunk_whose_work_fails_leaves_the_cursor_where_it_was(database, world):
    walk = purge_pass()

    def explode(sql, args):
        if sql.startswith("DELETE FROM replays"):
            raise RuntimeError("the delete failed")

    world.before = explode
    result = await walk.run(database, sleep=_nap)

    # A chunk that keeps failing exhausts its attempts and becomes a hole; the
    # default policy halts there rather than walking past work it did not do.
    assert result.stopped == "blocked"
    assert "the delete failed" in result.error
    # The cursor and the work commit together, so a failed chunk moves neither.
    status = await walk.status(database)
    assert status.cursor is None
    assert status.units_done == 0
    assert len(world.rows) == 13


async def test_a_transient_chunk_failure_is_absorbed_inside_the_shift(database, world):
    walk = purge_pass()
    failures = {"left": 1}

    def explode_once(sql, args):
        if sql.startswith("DELETE FROM replays") and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("transient")

    world.before = explode_once
    result = await walk.run(database, sleep=_nap)

    # Per-chunk retry is the pass's own: a job retry would re-run the whole
    # shift, and a lock wait that clears in fifty milliseconds does not deserve
    # a trip through the queue.
    assert result.complete is True
    assert result.rows == 10
    assert result.holes == 0
    # Nothing was lost and nothing was done twice: the retry starts from the
    # cursor, which never moved.
    assert sorted(row["key"] for row in world.rows) == ["live0", "live1", "live2"]


async def test_a_walk_resumes_from_the_cursor_after_the_shift_that_moved_it(database, world):
    # One chunk, then a shutdown at the boundary, then a fresh shift: the second
    # one starts from the ledger rather than from the beginning.
    walk = purge_pass()
    stopping = asyncio.Event()

    def stop_after_one(sql, args):
        if sql.startswith("DELETE FROM replays"):
            stopping.set()

    world.before = stop_after_one
    first = await walk.run_shift(database, stopping=stopping, sleep=_nap)
    world.before = None
    assert first.chunks == 1
    assert first.stopped == "stopping"

    after_first = await walk.status(database)
    assert after_first.cursor is not None

    second = await walk.run(database, sleep=_nap)

    assert second.complete is True
    assert sorted(row["key"] for row in world.rows) == ["live0", "live1", "live2"]


async def test_a_failed_chunk_records_its_error_on_the_ledger(database, world):
    walk = purge_pass()

    def explode(sql, args):
        if sql.startswith("DELETE FROM replays"):
            raise RuntimeError("boom")

    world.before = explode
    await walk.run(database, sleep=_nap)

    status = await walk.status(database)
    assert "boom" in status.last_error


async def test_a_recovered_chunk_clears_the_recorded_error(database, world):
    walk = purge_pass()
    failures = {"left": 1}

    def explode_once(sql, args):
        if sql.startswith("DELETE FROM replays") and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("transient")

    world.before = explode_once
    await walk.run(database, sleep=_nap)

    # A stale error beside a moving cursor is how a healthy pass looks broken.
    assert (await walk.status(database)).last_error is None


async def test_the_swap_is_the_chunk_transactions_first_statement(world, database):
    await purge_pass().run(database, sleep=_nap)

    after_begin = None
    for index, (sql, _) in enumerate(world.statements):
        if sql == "BEGIN":
            rest = [text for text, _ in world.statements[index + 1 :]]
            after_begin = next(text for text in rest if not text.startswith("SET LOCAL"))
            break
    # It takes the ledger row's lock for the rest of the transaction, which is
    # what serialises two workers on one pass without a lock of its own.
    assert "SET cursor = $3::jsonb" in after_begin


async def test_a_worker_that_loses_the_swap_does_nothing_observable(database, world):
    walk = purge_pass()

    # Another worker advanced the cursor between our range probe and our swap.
    def steal(sql, args):
        if "SET cursor = $3::jsonb" in sql:
            world.before = None
            row = world.ledger[("purge_replays", "")]
            row["cursor"] = ["2026-01-01T00:00:00+00:00", "stolen"]

    world.before = steal
    result = await walk.run(database, sleep=_nap)

    assert result.stopped == "lost"
    assert result.chunks == 0
    # The loser blocked on the row lock, saw its swap match nothing, and rolled
    # the whole chunk back -- work included. Nothing was deleted.
    assert len(world.rows) == 13
    assert ("ROLLBACK", ()) in world.statements


async def test_a_lost_swap_rolls_the_work_back_rather_than_committing_it(database, world):
    walk = purge_pass()
    world.ledger[("purge_replays", "")] = None  # placeholder, replaced by seed

    def steal(sql, args):
        if "SET cursor = $3::jsonb" in sql:
            world.before = None
            world.ledger[("purge_replays", "")]["cursor"] = ["2026-01-01T00:00:00+00:00", "x"]

    world.ledger.clear()
    world.before = steal
    await walk.run(database, sleep=_nap)

    tags = [sql for sql, _ in world.statements if sql in ("BEGIN", "COMMIT", "ROLLBACK")]
    assert tags == ["BEGIN", "ROLLBACK"]


async def test_completion_is_compare_and_swapped_so_it_happens_once(database):
    walk = purge_pass()
    await walk.run(database, sleep=_nap)

    # A second shift arriving after the walk finished finds it done and says so
    # rather than re-completing it.
    again = await walk.run(database, sleep=_nap)
    assert again.chunks == 0


async def test_a_short_chunk_is_not_the_end_of_the_walk():
    # A gap wider than the chunk limit: the first probe at OFFSET 2 finds
    # nothing, and a walk that took that for completion would stop here with
    # nine rows left and report success.
    rows = [
        {"key": "a", "expires": NOW - datetime.timedelta(seconds=500)},
        {"key": "b", "expires": NOW - datetime.timedelta(seconds=499)},
    ]
    rows += [
        {"key": f"z{index}", "expires": NOW - datetime.timedelta(seconds=100 - index)}
        for index in range(9)
    ]
    world = World("replays", rows)
    database = FakeDatabase(world)

    result = await purge_pass().run(database, sleep=_nap)

    assert result.complete is True
    assert result.rows == 11
    assert world.rows == []


async def test_the_end_of_the_walk_is_settled_by_a_probe_not_by_a_row_count(world, database):
    await purge_pass().run(database, sleep=_nap)

    # The honest test for "is there more" is one indexed probe from the far end
    # of the range, which is why a reverse-ordered SELECT appears.
    assert any("ORDER BY expires DESC" in sql for sql in world.sql_of("SELECT expires, key"))


async def test_a_walk_over_an_empty_table_completes_immediately():
    database = FakeDatabase(World("replays", []))

    result = await purge_pass().run(database, sleep=_nap)

    assert result.complete is True
    assert result.chunks == 0


async def test_a_shift_yields_at_a_chunk_boundary_when_asked_to_stop(database, world):
    walk = purge_pass()
    stopping = asyncio.Event()
    chunks = {"seen": 0}

    def stop_after_one(sql, args):
        if sql.startswith("DELETE FROM replays"):
            chunks["seen"] += 1
            if chunks["seen"] == 1:
                stopping.set()

    world.before = stop_after_one
    result = await walk.run_shift(database, stopping=stopping, sleep=_nap)

    assert result.stopped == "stopping"
    assert result.chunks == 1
    # It yielded between chunks rather than being cancelled inside one, so a
    # redeploy mid-pass costs at most the chunk that was already committed.
    tags = [sql for sql, _ in world.statements if sql in ("BEGIN", "COMMIT", "ROLLBACK")]
    assert tags == ["BEGIN", "COMMIT"]


async def test_a_shift_ends_on_its_budget_and_says_it_has_more_to_do(database):
    walk = purge_pass()
    ticks = iter([0.0, 0.0, 0.0, 99.0, 99.0, 99.0, 99.0])

    from wreath._passes import driver

    result = await driver.run_shift(
        walk, database, budget=1.0, sleep=_nap, clock=lambda: next(ticks)
    )

    assert result.stopped == "budget"
    assert result.should_continue is True


async def test_failing_to_acquire_a_connection_is_pacing_not_a_chunk_failure(database):
    database.fail_acquire = True

    result = await purge_pass().run_shift(database, sleep=_nap)

    # Counting it would let a traffic spike dead-letter a run of perfectly good
    # chunks -- the pass punishing itself for behaving correctly.
    assert result.stopped == "pool"
    assert result.should_continue is True


async def test_the_pass_rests_between_chunks_in_proportion_to_its_duty_cycle(database):
    walk = purge_pass(pace=DutyCycle(0.25))
    naps: list[float] = []

    async def record(seconds):
        naps.append(seconds)

    from wreath._passes import driver

    elapsed = iter([0.0, 1.0] * 12)
    await driver.run_shift(walk, database, budget=None, sleep=record, clock=lambda: next(elapsed))

    # A quarter of wall time is three seconds of rest per second of work.
    assert naps and all(nap == pytest.approx(3.0) for nap in naps)


async def test_a_full_duty_cycle_never_calls_the_sleeper(database):
    naps: list[float] = []

    async def record(seconds):
        naps.append(seconds)

    await purge_pass(pace=DutyCycle(1.0)).run(database, sleep=record)
    assert naps == []


async def test_the_connection_is_released_even_when_a_chunk_fails(database, world):
    def explode(sql, args):
        if sql.startswith("DELETE FROM replays"):
            raise RuntimeError("boom")

    world.before = explode
    await purge_pass().run_shift(database, sleep=_nap)

    assert database.acquired == database.released == 1


async def test_a_recurring_pass_rewinds_and_finds_what_expired_behind_the_cursor(database, world):
    walk = purge_pass()
    await walk.run(database, sleep=_nap)
    assert (await walk.status(database)).phase == DONE

    # A row that expired while the last cycle was running sits behind the
    # cursor. A fixed ceiling would never see it; a re-derived frontier does,
    # because the cycle starts again from the beginning of the domain.
    world.rows.append({"key": "late", "expires": NOW - datetime.timedelta(seconds=5)})
    result = await walk.run(database, sleep=_nap)

    assert result.rows == 1
    assert sorted(row["key"] for row in world.rows) == ["live0", "live1", "live2"]


async def test_a_new_cycle_resets_the_cursor_and_re_derives_the_frontier(database, world):
    walk = purge_pass()
    await walk.run(database, sleep=_nap)
    first = await walk.status(database)

    world.rows.append({"key": "late", "expires": NOW - datetime.timedelta(seconds=5)})
    await walk.run(database, sleep=_nap)
    second = await walk.status(database)

    assert first.phase == DONE
    assert second.cycle_started is not None
    assert len(world.sql_of("SELECT clock_timestamp() - make_interval")) == 2


async def test_a_pass_that_finishes_stays_finished_when_it_does_not_recur():
    world = World("treks", [{"id": index, "grade": 3} for index in range(5)])
    database = FakeDatabase(world)
    walk = ChunkedPass(
        "normalise_grades",
        over=Table("treks"),
        units=Rows(key=ID, limit=2, within="2s"),
        frontier=Ceiling.at_launch(),
        work=Rewrite({"grade": Sql("?", ["easy"])}),
        pace=DutyCycle(1.0),
    )

    assert (await walk.run(database, sleep=_nap)).complete is True
    again = await walk.run(database, sleep=_nap)

    assert again.complete is True
    assert again.chunks == 0
    assert (await walk.status(database)).phase == DONE


async def test_a_fixed_ceiling_ignores_rows_written_after_it_was_captured():
    world = World("treks", [{"id": index, "grade": 3} for index in range(4)])
    database = FakeDatabase(world)
    walk = ChunkedPass(
        "normalise_grades",
        over=Table("treks"),
        units=Rows(key=ID, limit=2, within="2s"),
        frontier=Ceiling.at_launch(),
        work=Rewrite({"grade": Sql("?", ["easy"])}),
        pace=DutyCycle(1.0),
    )
    seen = {"chunks": 0}

    def insert_after_first_chunk(sql, args):
        if sql.startswith("UPDATE treks"):
            seen["chunks"] += 1
            if seen["chunks"] == 1:
                world.rows.append({"id": 99, "grade": 3})

    world.before = insert_after_first_chunk
    await walk.run(database, sleep=_nap)

    # A pass converts the past; the application writes the future in the shape
    # the pass is converting to. Row 99 is not this pass's problem.
    assert [row["grade"] for row in world.rows if row["id"] == 99] == [3]
    assert all(row["grade"] == "easy" for row in world.rows if row["id"] < 4)


async def test_a_ceiling_over_an_empty_table_does_no_work():
    database = FakeDatabase(World("treks", []))
    walk = ChunkedPass(
        "normalise_grades",
        over=Table("treks"),
        units=Rows(key=ID, limit=2, within="2s"),
        frontier=Ceiling.at_launch(),
        work=Rewrite({"grade": Sql("?", ["easy"])}),
        pace=DutyCycle(1.0),
    )

    result = await walk.run(database, sleep=_nap)

    assert result.complete is True
    assert result.rows == 0


async def test_a_purge_can_carry_an_extra_filter(world, database):
    walk = purge_pass(work=Purge(where=Sql("key <> ?", ["k000"])))

    result = await walk.run(database, sleep=_nap)

    assert result.complete is True
    # The filter narrows what the chunk deletes without narrowing what it walks,
    # so k000 is passed over rather than blocking the cursor.
    assert sorted(row["key"] for row in world.rows) == ["k000", "live0", "live1", "live2"]


async def test_a_callback_runs_inside_the_chunk_transaction(database, world):
    seen: list[tuple] = []

    async def note(tx, chunk, binds):
        seen.append((chunk.cursor_from, chunk.cursor_to))
        await tx.execute(f"DELETE FROM {chunk.table} WHERE {chunk.where}", *binds.args)
        return 3

    walk = purge_pass(
        work=Apply(note, idempotent=Declared("delete of an already-expired row is a no-op"))
    )
    result = await walk.run(database, sleep=_nap)

    assert result.rows == 12  # the callback's declared count, three per chunk
    assert seen[0][0] is None  # the first chunk opens at the start of the domain
    assert world.rows and all(row["key"].startswith("live") for row in world.rows)


async def test_a_chunk_is_a_half_open_range_over_one_ordered_domain(database, world):
    ranges: list[tuple] = []

    async def note(tx, chunk, binds):
        ranges.append((chunk.cursor_from, chunk.cursor_to))
        return 0

    walk = purge_pass(work=Apply(note, idempotent=Declared("it does nothing")))
    await walk.run_shift(database, budget=None, sleep=_nap)

    # Open at the low end so the row the last chunk finished on is not seen
    # twice; closed at the high end so the cursor is always a key that exists.
    assert ranges[0][0] is None
    for previous, current in zip(ranges, ranges[1:], strict=False):
        assert previous[1] == current[0]


async def test_the_ledger_records_pacing_so_a_paced_pass_never_looks_broken(database):
    walk = purge_pass(pace=DutyCycle(0.5))

    await walk.run(database, sleep=_nap)

    status = await walk.status(database)
    assert status.paced_reason == "duty cycle 0.5"
    assert status.chunk_limit == 3


async def test_status_is_none_before_a_pass_has_ever_run():
    database = FakeDatabase(World("replays", []))

    assert await purge_pass().status(database) is None


async def test_the_ledger_row_is_seeded_walking(database):
    walk = purge_pass()
    await walk.run_shift(database, budget=0.0, sleep=_nap)

    status = await walk.status(database)
    assert status.phase == WALKING
    assert status.started_at is not None
