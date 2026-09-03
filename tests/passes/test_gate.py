from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from wreath._passes.gate import Verification, refuse_reused_predicate
from wreath.passes import (
    Buckets,
    Ceiling,
    ChunkedPass,
    Constraint,
    DutyCycle,
    Gate,
    Key,
    NoRowsMatch,
    PassDeclarationError,
    Purge,
    Reconcile,
    Rewrite,
    Rows,
    Sealed,
    Table,
    published_facts,
)
from wreath.postgres import OperationalError

from .fakes import FakeDatabase, World

NOW = datetime.datetime(2026, 7, 27, 12, 0, tzinfo=datetime.UTC)
GRADE_ID = Key("id", "text", indexed=True, unique=True, monotone=True)
RECORDED = Key("recorded_at", "timestamptz", indexed=True)


def test_a_successful_verification_cannot_also_be_transient():
    with pytest.raises(ValueError, match="successful verification cannot be transient"):
        Verification(True, transient=True)


async def test_no_rows_match_combines_a_unit_scope_with_its_predicate() -> None:
    class Executor:
        def __init__(self) -> None:
            self.sql = ""

        async def fetchrow(self, sql: str) -> None:
            self.sql = sql

    executor = Executor()
    verdict = await NoRowsMatch("ready = false").check(
        executor,
        walk=SimpleNamespace(table="jobs"),
        scope="tenant_id = 7",
    )

    assert verdict.ok is True
    assert "(ready = false) AND (tenant_id = 7)" in executor.sql


async def test_reconcile_refuses_unequal_results() -> None:
    class Executor:
        def __init__(self) -> None:
            self.values = iter((4, 5))

        async def fetchval(self, _sql: str) -> int:
            return next(self.values)

    verdict = await Reconcile("source", "against").check(
        Executor(), walk=SimpleNamespace()
    )

    assert verdict.ok is False
    assert verdict.transient is False
    assert "source = 4" in verdict.detail
    assert "against = 5" in verdict.detail


@pytest.mark.parametrize("name", ["not-valid", "two words"])
def test_constraint_names_must_be_unquoted_identifiers(name: str) -> None:
    with pytest.raises(PassDeclarationError, match="constraint name"):
        Constraint(name, "ready")


@pytest.mark.parametrize("check", [None, 1, "", "   "])
def test_constraint_checks_must_be_nonempty_sql(check: object) -> None:
    with pytest.raises(PassDeclarationError, match="CHECK expression"):
        Constraint("ready_check", cast(Any, check))


async def test_a_constraint_refuses_unit_scope_before_using_the_executor() -> None:
    verdict = await Constraint("ready_check", "ready").check(
        object(),
        walk=SimpleNamespace(table="jobs"),
        scope="tenant_id = 7",
    )

    assert verdict == Verification(
        False,
        "Constraint verifies a whole table, so it cannot be used with Gate(scope='unit')",
    )


async def test_a_non_violation_validation_error_is_transient() -> None:
    class Executor:
        async def execute(self, sql: str) -> None:
            if "VALIDATE CONSTRAINT" in sql:
                raise OperationalError("connection reset")

    verdict = await Constraint("ready_check", "ready").check(
        Executor(), walk=SimpleNamespace(table="jobs")
    )

    assert verdict.ok is False
    assert verdict.transient is True
    assert "could not validate" in verdict.detail


def test_gate_refuses_a_verifier_without_a_check() -> None:
    with pytest.raises(PassDeclarationError, match="must be NoRowsMatch"):
        Gate(verify=object(), publishes="ready")


def test_gate_refuses_a_noncallable_terminal_step() -> None:
    with pytest.raises(PassDeclarationError, match="async callable"):
        Gate(verify=NoRowsMatch("ready"), publishes="ready", then=object())


def test_gate_refuses_a_blank_published_fact() -> None:
    with pytest.raises(PassDeclarationError, match="needs a name"):
        Gate(verify=NoRowsMatch("ready"), publishes="   ")


@pytest.mark.parametrize("where", [None, 1])
def test_predicate_reuse_ignores_work_without_a_sql_predicate(where: object) -> None:
    gate = Gate(verify=NoRowsMatch(str(where)), publishes="ready")

    refuse_reused_predicate(gate, SimpleNamespace(where=where))


async def _nap(_seconds):
    return None


def convert_pass(**overrides):
    """A deferred migration's shape: rewrite the rows that still need it."""
    options = {
        "over": Table("treks"),
        "units": Rows(key=GRADE_ID, limit=3, within="2s"),
        "frontier": Ceiling.at_launch(monotone="ids come from an identity column"),
        # The walk selects on the *source* column and the gate asks about the
        # *target* invariant, which is the arrangement §10.3 exists to force.
        "work": Rewrite({"grade_text": "'moderate'"}, where="grade IS NOT NULL"),
        "gate": Gate(
            verify=NoRowsMatch("grade_text IS NULL"),
            publishes="trek.grade_text",
        ),
        "pace": DutyCycle(1.0),
    }
    options.update(overrides)
    return ChunkedPass("convert_grades", **options)


def treks(count: int, *, converted: int = 0) -> list[dict]:
    return [
        {
            "id": f"t{index:03d}",
            "grade_text": "moderate" if index < converted else None,
            "grade": index,
        }
        for index in range(count)
    ]


@pytest.fixture
def world():
    return World("treks", treks(7))


@pytest.fixture
def database(world):
    return FakeDatabase(world)


def test_a_verification_that_restates_the_walk_is_refused():
    # The seductive mistake: if the walk selected `grade_text IS NULL` and the
    # check asks `grade_text IS NULL`, a walk whose predicate was subtly wrong
    # verifies its own bug and reports success. The same defect as a check that
    # silently had nothing to check.
    with pytest.raises(PassDeclarationError) as caught:
        convert_pass(
            work=Rewrite({"grade_text": "'moderate'"}, where="grade_text IS NULL"),
            gate=Gate(verify=NoRowsMatch("grade_text IS NULL"), publishes="x"),
        )

    message = str(caught.value)
    assert "the same predicate" in message
    assert "verify its own bug" in message


def test_the_refusal_ignores_whitespace_and_case():
    with pytest.raises(PassDeclarationError):
        convert_pass(
            work=Rewrite({"grade_text": "'moderate'"}, where="grade_text IS NULL"),
            gate=Gate(verify=NoRowsMatch("GRADE_TEXT   is null"), publishes="x"),
        )


def test_a_different_predicate_is_allowed():
    # Derived from the invariant the irreversible step needs, rather than from
    # the walk: the migration is about to make the column NOT NULL.
    walk = convert_pass(
        work=Rewrite({"grade_text": "'moderate'"}, where="grade IS NOT NULL"),
        gate=Gate(verify=NoRowsMatch("grade_text IS NULL"), publishes="x"),
    )

    assert walk.gate is not None


def test_a_gate_that_does_nothing_with_its_answer_is_refused():
    with pytest.raises(PassDeclarationError) as caught:
        convert_pass(gate=Gate(verify=NoRowsMatch("grade_text IS NULL")))

    assert "verifies and discards the answer" in str(caught.value)


def test_a_whole_pass_gate_on_a_recurring_pass_is_refused():
    # A recurring pass has no completion for a whole-pass gate to fire at: a
    # cycle completes and the frontier moves on.
    with pytest.raises(PassDeclarationError) as caught:
        ChunkedPass(
            "fold",
            over=Table("treks"),
            units=Buckets(on=RECORDED, zone="UTC"),
            frontier=Sealed(),
            work=Purge(),
            gate=Gate(verify=NoRowsMatch("grade_text IS NULL"), publishes="x"),
        )

    assert "no completion" in str(caught.value)


def test_a_unit_gate_cannot_publish_a_whole_table_fact():
    with pytest.raises(PassDeclarationError) as caught:
        Gate(verify=NoRowsMatch("x IS NULL"), publishes="a.b", scope="unit")

    assert "no whole-pass completion" in str(caught.value)


def test_an_unknown_scope_is_refused():
    with pytest.raises(PassDeclarationError) as caught:
        Gate(verify=NoRowsMatch("x IS NULL"), publishes="a.b", scope="bucket")

    assert "'pass' or 'unit'" in str(caught.value)


async def test_the_gate_publishes_a_fact_when_the_table_agrees(database, world):
    walk = convert_pass()

    result = await walk.run(database, sleep=_nap)

    assert result.complete is True
    status = await walk.status(database)
    assert status.phase == "done"
    assert all(row["grade_text"] == "moderate" for row in world.rows)


async def test_the_published_fact_is_readable_by_something_that_is_not_the_pass(database, world):
    # The whole point of publishing rather than acting: the consumer is a
    # migration deciding whether it may narrow the column, and it holds a
    # connection and a schema, not a pass declaration.
    walk = convert_pass()
    await walk.run(database, sleep=_nap)

    facts = await published_facts(database)

    assert [fact.fact for fact in facts] == ["trek.grade_text"]
    assert facts[0].name == "convert_grades"
    assert facts[0].verified_at is not None

    # And it can be asked about one fact by name, which is the shape the
    # migration hazard scan wants.
    assert len(await published_facts(database, fact="trek.grade_text")) == 1
    assert await published_facts(database, fact="trek.something_else") == []


async def test_nothing_is_published_until_the_walk_finishes(database, world):
    walk = convert_pass()

    await walk.run_shift(database, budget=0.0, sleep=_nap)

    assert await published_facts(database) == []


async def test_a_failed_verification_stops_the_pass_and_does_not_retry(database, world):
    # A row the walk never converted, because its own predicate missed it.
    world.rows.append({"id": "t999", "grade_text": None, "grade": 99})
    walk = convert_pass(
        # A walk that only converts rows whose `grade` is under 50 -- so it
        # reports success having left one row behind.
        work=Rewrite({"grade_text": "'moderate'"}, where="grade < 50"),
    )

    result = await walk.run(database, sleep=_nap)

    assert result.stopped == "blocked"
    status = await walk.status(database)
    assert status.phase == "unverified"
    assert "rows still match" in status.last_error
    assert await published_facts(database) == []

    # And running it again does not re-verify: the walk's logic is wrong, and
    # the check will answer no again at the same row.
    again = await walk.run(database, sleep=_nap)
    assert again.stopped == "blocked"
    assert (await walk.status(database)).phase == "unverified"


async def test_retry_refuses_to_restart_a_pass_that_failed_verification(database, world):
    world.rows.append({"id": "t999", "grade_text": None, "grade": 99})
    walk = convert_pass(work=Rewrite({"grade_text": "'moderate'"}, where="grade < 50"))
    await walk.run(database, sleep=_nap)

    await walk.retry(database)

    # `retry` clears a chunk that was given up on. This is not that: it is a
    # walk whose logic is wrong, and burning a maintenance window to fail at
    # the same row helps nobody.
    assert (await walk.status(database)).phase == "unverified"


async def test_a_verification_that_could_not_run_is_not_a_verdict(database, world):
    walk = convert_pass()
    calls = {"n": 0}

    def refuse_the_check(sql, args):
        if sql.startswith("SELECT 1 AS present"):
            calls["n"] += 1
            # A real dropped connection is an `OperationalError`, not a bare
            # `RuntimeError`. The distinction is load-bearing now that the
            # check narrows its catch: see the test below.
            raise OperationalError("connection reset by peer")

    world.before = refuse_the_check
    result = await walk.run(database, sleep=_nap)

    assert result.stopped == "failed"
    status = await walk.status(database)
    # Still `verifying`, not `unverified`: the check never answered, so nothing
    # has been concluded and the next shift tries again.
    assert status.phase == "verifying"
    assert "could not run" in status.last_error

    world.before = None
    again = await walk.run(database, sleep=_nap)
    assert again.complete is True
    assert (await walk.status(database)).phase == "done"


async def test_a_bug_in_the_verification_is_not_reported_as_could_not_run(database, world):
    walk = convert_pass()

    def a_programming_error(sql, args):
        if sql.startswith("SELECT 1 AS present"):
            raise TypeError("someone built this predicate wrong")

    world.before = a_programming_error
    with pytest.raises(TypeError, match="built this predicate wrong"):
        await walk.run(database, sleep=_nap)


async def test_a_hole_bars_the_gate(database, world):
    walk = convert_pass(on_chunk_failure="skip", chunk_retries=1)
    failures = {"left": 1}

    def poison_first_chunk(sql, args):
        if sql.startswith("UPDATE treks") and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("cursed")

    world.before = poison_first_chunk
    result = await walk.run(database, sleep=_nap)
    world.before = None

    assert result.stopped == "blocked"
    status = await walk.status(database)
    assert status.gate_barred is True
    # Skipping buys throughput and never the irreversible step, so nothing was
    # published even though the walk reached the end.
    assert await published_facts(database) == []


async def test_clearing_the_hole_un_bars_the_gate(database, world):
    walk = convert_pass(on_chunk_failure="skip", chunk_retries=1)
    failures = {"left": 1}

    def poison_first_chunk(sql, args):
        if sql.startswith("UPDATE treks") and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("cursed")

    world.before = poison_first_chunk
    await walk.run(database, sleep=_nap)
    world.before = None

    await walk.retry(database)
    await walk.run(database, sleep=_nap)

    status = await walk.status(database)
    assert status.holes_open == 0
    assert status.gate_barred is False
    assert [fact.fact for fact in await published_facts(database)] == ["trek.grade_text"]


async def test_the_terminal_step_runs_once_after_the_fact_is_published(database, world):
    ran: list[str] = []

    async def detach(executor, walk, unit):
        # The fact is durable before anything irreversible happens, so a crash
        # here leaves a pass that is verified rather than one that is not.
        facts = await published_facts(database)
        ran.append(facts[0].fact if facts else "")

    walk = convert_pass(
        gate=Gate(
            verify=NoRowsMatch("grade_text IS NULL"),
            publishes="trek.grade_text",
            then=detach,
        )
    )

    await walk.run(database, sleep=_nap)

    assert ran == ["trek.grade_text"]
    assert (await walk.status(database)).phase == "done"

    # A second drive does nothing: `done` is absorbing and the phase CAS is what
    # makes that true even if three shifts arrive together.
    await walk.run(database, sleep=_nap)
    assert ran == ["trek.grade_text"]


async def test_a_terminal_step_that_fails_leaves_the_pass_mid_sequence(database, world):
    attempts = {"n": 0}

    async def flaky(executor, walk, unit):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("the object store said no")

    walk = convert_pass(
        gate=Gate(
            verify=NoRowsMatch("grade_text IS NULL"),
            publishes="trek.grade_text",
            then=flaky,
        )
    )

    first = await walk.run(database, sleep=_nap)
    assert first.stopped == "failed"
    status = await walk.status(database)
    assert status.phase == "applying"
    # The fact was already published, so a resume does not re-verify.
    assert status.verified_fact == "trek.grade_text"

    second = await walk.run(database, sleep=_nap)
    assert second.complete is True
    assert attempts["n"] == 2
    assert (await walk.status(database)).phase == "done"


async def test_resuming_mid_gate_re_verifies_rather_than_trusting(database, world):
    walk = convert_pass()
    await walk.run(database, sleep=_nap)
    assert len(world.sql_of("SELECT 1 AS present")) == 1

    # A process that died between `verifying` and `verified`: the walk is over
    # and the check ran, but nothing recorded that it passed.
    connection = await database.acquire("write")
    await walk.ledger.set_phase(connection, expected="done", phase="verifying")

    result = await walk.run(database, sleep=_nap)

    # It verifies again rather than proceeding on trust. That is always the
    # right trade: verification is idempotent and cheap relative to the thing
    # it guards.
    assert result.complete is True
    assert len(world.sql_of("SELECT 1 AS present")) == 2
    assert (await walk.status(database)).phase == "done"


async def test_a_constraint_is_added_not_valid_then_validated(database, world):
    walk = convert_pass(
        gate=Gate(
            verify=Constraint("trek_grade_text_present", "grade_text IS NOT NULL"),
            publishes="trek.grade_text",
        ),
    )

    result = await walk.run(database, sleep=_nap)

    assert result.complete is True
    statements = [sql for sql in world.sql_of("ALTER TABLE")]
    assert "NOT VALID" in statements[0]
    assert statements[1].endswith("VALIDATE CONSTRAINT trek_grade_text_present")
    # The constraint stays, which is the point: the table goes on refusing what
    # the pass just finished ruling out.
    assert "trek_grade_text_present" in world.constraints


async def test_a_constraint_that_does_not_hold_blocks_the_pass(database, world):
    world.rows.append({"id": "t999", "grade_text": None, "grade": 99})
    walk = convert_pass(
        work=Rewrite({"grade_text": "'moderate'"}, where="grade < 50"),
        gate=Gate(
            verify=Constraint("trek_grade_text_present", "grade_text IS NOT NULL"),
            publishes="trek.grade_text",
        ),
    )

    result = await walk.run(database, sleep=_nap)

    assert result.stopped == "blocked"
    status = await walk.status(database)
    assert status.phase == "unverified"
    assert "does not hold" in status.last_error


async def test_a_constraint_verification_cannot_reuse_the_walks_predicate_by_construction():
    # `Constraint` is the one grade where §10.3's concern cannot arise: the
    # check and the thing the database will go on enforcing are the same
    # expression, so there is no second predicate to get wrong. The refusal
    # still fires if someone literally restates the walk, which is a fair
    # thing to catch, but the real answer is that it does not matter here.
    walk = ChunkedPass(
        "convert_grades",
        over=Table("treks"),
        units=Rows(key=GRADE_ID, limit=3, within="2s"),
        frontier=Ceiling.at_launch(monotone="identity column"),
        work=Rewrite({"grade_text": "'moderate'"}, where="grade < 50"),
        gate=Gate(
            verify=Constraint("c", "grade_text IS NOT NULL"),
            publishes="trek.grade_text",
        ),
    )

    assert walk.gate.verify.name == "c"


async def test_reconcile_compares_two_independent_counts(database, world):
    walk = convert_pass(
        gate=Gate(
            verify=Reconcile("SELECT count(*) FROM treks", "SELECT count(*) FROM treks"),
            publishes="trek.rollup",
        ),
    )

    result = await walk.run(database, sleep=_nap)

    assert result.complete is True
    assert [fact.fact for fact in await published_facts(database)] == ["trek.rollup"]


def unit_gate_pass(then, **overrides):
    options = {
        "over": Table("treks"),
        "units": Buckets(on=RECORDED, zone="UTC"),
        "frontier": Sealed(),
        "work": Purge(),
        "gate": Gate(verify=NoRowsMatch("grade_text IS NULL"), then=then, scope="unit"),
        "pace": DutyCycle(1.0),
    }
    options.update(overrides)
    return ChunkedPass("fold_treks", **options)


async def test_a_unit_gate_runs_per_range_as_the_walk_passes_it():
    rows = [
        {
            "id": f"t{day}",
            "grade_text": "moderate",
            "recorded_at": datetime.datetime(2026, 7, day, 6, tzinfo=datetime.UTC),
        }
        for day in (24, 25, 26)
    ]
    world = World("treks", rows)
    database = FakeDatabase(world)
    units: list[tuple] = []

    async def archive(executor, walk, unit):
        units.append(unit)

    walk = unit_gate_pass(archive)
    await walk.run(database, sleep=_nap)

    # One terminal step per sealed bucket, not one for the whole walk -- so one
    # bad bucket cannot freeze the ladder behind it.
    assert len(units) == 3
    assert all(unit is not None for unit in units)
    starts = [unit[0][0].day for unit in units]
    assert starts == [24, 25, 26]


async def test_a_failed_unit_gate_blocks_the_pass_before_any_callback():
    rows = [
        {
            "id": "t1",
            "grade_text": None,
            "recorded_at": datetime.datetime(2026, 7, 24, 6, tzinfo=datetime.UTC),
        }
    ]
    world = World("treks", rows)
    database = FakeDatabase(world)
    called = False

    async def terminal(executor, walk, unit):
        nonlocal called
        called = True

    class Reject:
        async def check(self, executor, *, walk, scope=None):
            class Verdict:
                ok = False
                transient = False
                detail = "unit verification failed"

            return Verdict()

    result = await unit_gate_pass(
        terminal,
        gate=Gate(verify=Reject(), then=terminal, scope="unit"),
    ).run(database, sleep=_nap)
    assert result.stopped == "blocked"
    assert result.error == "unit verification failed"
    assert called is False
