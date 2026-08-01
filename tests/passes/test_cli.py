"""``wreath passes status``: the answer to "is it still going?".

A pass runs for hours across thousands of job deliveries, so the question people
actually have is not whether a job succeeded but where the walk has got to. The
ledger row is the durable answer, and this reads it.
"""

from __future__ import annotations

import datetime
import json

import pytest

from wreath._cli import CliError, build_parser, execute_passes

from .conftest import NOW
from .fakes import FakeDatabase, World


class _StartableDatabase(FakeDatabase):
    """A fake database the CLI can start and stop like the real one."""

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _Runner:
    def __init__(self, database, schema="wreath", passes=()):
        self._db = database
        self._schema = schema
        self._passes = list(passes)


class _Application:
    def __init__(self, database, schema="wreath"):
        self._databases = {"main": database}
        self._job_runners = {"work": _Runner(database, schema)}


def _seed(world, **overrides):
    row = {
        "name": "purge_replays", "tenant": "", "phase": "walking",
        "cursor": ["2026-07-27T11:00:00+00:00", "k042"], "ceiling": None,
        "keyspace_from": None, "pending": [],
        "units_done": 14, "rows_done": 14_000, "denominator": None,
        "denominator_kind": None, "chunk_limit": 1000,
        "paced_reason": "duty cycle 0.25", "started_at": NOW,
        "window_started": NOW - datetime.timedelta(seconds=30),
        "window_rows": 3_000, "window_units": 3,
        "last_advance": NOW - datetime.timedelta(seconds=4), "cycle_started": NOW,
        "driven_at": NOW - datetime.timedelta(seconds=4),
        "last_drive_error": None,
        "verified_at": None, "verified_fact": None, "last_error": None,
        "trace_context": None,
    }
    row.update(overrides)
    world.ledger[(row["name"], row["tenant"])] = row
    return row


#: The one flag whose command-line spelling is not its destination.
_FLAGS = {"as_json": "--json"}


def _namespace(**overrides):
    parser = build_parser()
    argv = ["passes", "status", "app:app"]
    for name, value in overrides.items():
        flag = _FLAGS.get(name, f"--{name.replace('_', '-')}")
        if value is True:
            argv.append(flag)
        elif value is not None:
            argv.extend([flag, str(value)])
    return parser.parse_args(argv)


@pytest.fixture
def cli(monkeypatch):
    world = World("replays", [])
    database = _StartableDatabase(world)
    application = _Application(database)
    monkeypatch.setattr(
        "wreath._cli.load_application", lambda target, factory=False: application
    )
    return world, database, application


# --- parsing ------------------------------------------------------------------


def test_the_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["passes"])


def test_status_takes_a_target_and_the_usual_filters():
    namespace = _namespace(name="purge_replays", schema="wreath", as_json=None)

    assert namespace.command == "passes"
    assert namespace.passes_action == "status"
    assert namespace.target == "app:app"
    assert namespace.name == "purge_replays"


def test_retry_ships_now_that_a_hole_is_recorded():
    # Stage one deliberately withheld this: offering it would have promised a
    # hole could be cleared when nothing recorded one. The dead-letter table is
    # what makes the promise true.
    namespace = build_parser().parse_args(["passes", "retry", "app:app"])

    assert namespace.passes_action == "retry"
    assert namespace.target == "app:app"


def test_pause_and_resume_are_still_not_offered():
    # They are not stage three's to give. `retry` clears a hole the pass already
    # recorded; pausing is a *new* durable state with no writer and no reader,
    # and a verb that parses but changes nothing is worse than one that does not
    # parse. Blocking a pass by hand today means clearing its schedule.
    parser = build_parser()
    for verb in ("pause", "resume"):
        with pytest.raises(SystemExit):
            parser.parse_args(["passes", verb, "app:app"])


# --- reading ------------------------------------------------------------------


def test_status_prints_a_row_per_pass(cli, capsys):
    world, _, _ = cli
    _seed(world)

    assert execute_passes(_namespace()) == 0

    out = capsys.readouterr().out
    assert "purge_replays" in out
    assert "14000" in out
    # A paced pass that does not say it is paced is indistinguishable from a
    # broken one, so it reports `slow` and names the policy holding it back.
    assert "slow" in out
    assert "duty cycle 0.25" in out


def test_status_says_so_when_nothing_has_run_yet(cli, capsys):
    assert execute_passes(_namespace()) == 0

    assert "no passes have run yet" in capsys.readouterr().out


def test_status_emits_json_on_request(cli, capsys):
    world, _, _ = cli
    _seed(world)

    assert execute_passes(_namespace(as_json=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["passes"][0]["name"] == "purge_replays"
    assert payload["passes"][0]["rows_done"] == 14_000
    # Timestamps cross the boundary as strings, so the output is JSON that a
    # caller can actually parse rather than a repr.
    assert isinstance(payload["passes"][0]["started_at"], str)


def test_status_surfaces_a_recorded_error(cli, capsys):
    world, _, _ = cli
    _seed(world, last_error="RuntimeError('deadlock detected')")

    execute_passes(_namespace())

    assert "deadlock detected" in capsys.readouterr().out


def test_a_failed_pass_prints_the_trace_that_started_the_walk(cli, capsys):
    """Plan 01 stage 4: the thread from a chunk that broke back to its cause.

    A backfill's third day is a long way from whatever started it, and the
    ledger row is the only thing that still holds the link.
    """
    parent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    world, _, _ = cli
    _seed(world, last_error="RuntimeError('deadlock')", trace_context=parent)

    execute_passes(_namespace())

    out = capsys.readouterr().out
    assert f"trace: {'a' * 32}" in out
    assert f"wreath doctor trace {'a' * 32}" in out


def test_a_healthy_pass_does_not_print_a_trace_line(cli, capsys):
    """Deliberate: on a pass that is fine the id is noise on every line.

    The column is a forensic aid, and printing it unconditionally would make
    the surface an operator scans for trouble noisier rather than clearer.
    """
    parent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    world, _, _ = cli
    _seed(world, trace_context=parent)

    execute_passes(_namespace())

    assert "trace:" not in capsys.readouterr().out


def test_a_pass_stopped_at_a_hole_prints_its_trace(cli, capsys):
    """`holes_open` is its own reason, not a spelling of `last_error`.

    A pass that skipped a chunk reaches `done` with the hole still barring its
    gate and no error on the row -- and that is precisely when somebody needs
    the thread back to whatever started the walk. A mutant sweep found this
    operand of the guard untested.
    """
    parent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    world, _, _ = cli
    _seed(world, trace_context=parent)
    world.holes[("purge_replays", "", "x")] = {}

    execute_passes(_namespace())

    assert f"trace: {'a' * 32}" in capsys.readouterr().out


def test_a_pass_blocked_without_an_error_still_prints_its_trace(cli, capsys):
    """The third operand: `blocked` with nothing in `last_error`.

    `wreath passes retry` clears a hole and the row's error with it, so a pass
    can sit blocked with no error text at all -- and it is still stopped.
    """
    parent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    world, _, _ = cli
    _seed(world, phase="blocked", last_error=None, trace_context=parent)

    execute_passes(_namespace())

    assert f"trace: {'a' * 32}" in capsys.readouterr().out


def test_a_version_one_ledger_is_never_asked_for_the_column(cli, capsys):
    """`read_all` has its own projection, and its own version-1 arm.

    Asserted on the statement rather than the value: the fake hands back
    whatever row it holds regardless of what the `SELECT` named, so only the
    emitted SQL can tell the two arms apart -- which a mutant sweep found.
    """
    world, database, _ = cli
    world.trace_column = False
    _seed(world)
    del world.ledger[("purge_replays", "")]["trace_context"]

    execute_passes(_namespace())

    reads = [
        sql for sql, _args in world.statements
        if "trace_context" in sql and "pg_attribute" not in sql
    ]
    assert not reads, reads


def test_a_version_two_ledger_is_asked_for_the_column(cli, capsys):
    world, _, _ = cli
    _seed(world)

    execute_passes(_namespace())

    assert any(
        "trace_context" in sql and "pg_attribute" not in sql
        for sql, _args in world.statements
    )


def test_a_pass_with_no_trace_prints_no_trace_line(cli, capsys):
    """A pass driven only by `cron` has no originating request and mints none."""
    world, _, _ = cli
    _seed(world, last_error="RuntimeError('deadlock')", trace_context=None)

    execute_passes(_namespace())

    assert "trace:" not in capsys.readouterr().out


def test_the_json_status_carries_the_traceparent_and_its_id(cli, capsys):
    parent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    world, _, _ = cli
    _seed(world, trace_context=parent)

    execute_passes(_namespace(as_json=True))

    body = json.loads(capsys.readouterr().out)["passes"][0]
    assert body["trace_context"] == parent
    assert body["trace_id"] == "a" * 32


def test_a_blocked_pass_says_so(cli, capsys):
    world, _, _ = cli
    _seed(world, phase="blocked", last_error=None)

    execute_passes(_namespace())

    # The state a hand-rolled backfill has no name for: it will silently never
    # finish, and nothing else in the system is going to mention it.
    out = capsys.readouterr().out
    assert "blocked" in out
    assert "the pass is blocked" in out


def test_a_pass_nothing_has_driven_is_blocked_and_says_which_way(cli, capsys):
    world, _, _ = cli
    # Walking, no error, cursor moving -- and yet nothing has enqueued a shift
    # for it in ten minutes. That is the scheduler being down, and without this
    # the row reads as perfectly healthy.
    _seed(world, driven_at=NOW - datetime.timedelta(minutes=10))

    execute_passes(_namespace())

    out = capsys.readouterr().out
    assert "blocked" in out
    assert "nothing has driven this pass" in out


def test_a_drive_failure_names_itself_rather_than_only_incrementing_a_counter(
    cli, capsys
):
    world, _, _ = cli
    _seed(world, last_drive_error="ConnectionRefusedError('no route to host')")

    execute_passes(_namespace())

    out = capsys.readouterr().out
    assert "blocked" in out
    assert "nothing is driving this pass" in out
    assert "no route to host" in out


def test_a_tenant_is_shown_beside_the_pass_name(cli, capsys):
    world, _, _ = cli
    _seed(world, tenant="acme")

    execute_passes(_namespace())

    assert "purge_replays@acme" in capsys.readouterr().out


def test_the_database_is_started_and_stopped_around_the_read(cli):
    world, database, _ = cli
    _seed(world)

    execute_passes(_namespace())

    assert database.started is True
    assert database.stopped is True


def test_an_unknown_database_is_named_along_with_the_ones_that_exist(cli):
    with pytest.raises(CliError) as error:
        execute_passes(_namespace(database="replica"))

    message = str(error.value)
    assert "unknown database 'replica'" in message
    assert "main" in message


def test_an_application_with_no_job_runner_and_no_database_is_refused(monkeypatch):
    class Bare:
        _databases: dict = {}
        _job_runners: dict = {}

    monkeypatch.setattr("wreath._cli.load_application", lambda target, factory=False: Bare())

    with pytest.raises(CliError) as error:
        execute_passes(_namespace())

    assert "no pass ledger to read" in str(error.value)
    assert error.value.exit_code == 2


# --- holes and retry ----------------------------------------------------------


def _seed_hole(world, **overrides):
    hole = {
        "name": "purge_replays", "tenant": "",
        "cursor_from": ["2026-07-27T10:00:00+00:00", "k000"],
        "cursor_to": ["2026-07-27T11:00:00+00:00", "k042"],
        "attempts": 3, "error": "RuntimeError('deadlock detected')",
        "predicate": (
            "SELECT * FROM replays WHERE (expires, key) > ('2026-07-27T10:00:00+00:00', "
            "'k000') AND (expires, key) <= ('2026-07-27T11:00:00+00:00', 'k042')"
        ),
        "failed_at": NOW,
    }
    hole.update(overrides)
    world.holes[("purge_replays", "", "x")] = hole
    return hole


def test_a_hole_is_counted_on_the_status_row_with_what_to_do_about_it(cli, capsys):
    world, _, _ = cli
    _seed(world)
    _seed_hole(world)

    execute_passes(_namespace())

    out = capsys.readouterr().out
    assert "1 dead-lettered chunk(s)" in out
    # The gate rule stated where somebody will read it, not only in the design.
    assert "terminal gate is barred" in out
    assert "wreath passes retry" in out


def test_holes_lists_the_statement_that_reproduces_each_one(cli, capsys):
    world, _, _ = cli
    _seed(world)
    _seed_hole(world)

    execute_passes(_namespace(holes=True))

    out = capsys.readouterr().out
    assert "after 3 attempt(s)" in out
    assert "deadlock detected" in out
    assert "reproduce: SELECT * FROM replays WHERE" in out


def test_holes_says_so_when_there_are_none(cli, capsys):
    world, _, _ = cli
    _seed(world)

    execute_passes(_namespace(holes=True))

    assert "no dead-lettered chunks" in capsys.readouterr().out


def test_retry_reports_what_it_queued_and_that_queuing_is_not_clearing(capsys):
    world = World("replays", [])
    database = _StartableDatabase(world)

    class _Walk:
        name = "purge_replays"

        async def retry(self, _database):
            return 2

    application = _Application(database)
    application._job_runners["work"] = _Runner(database, passes=[("task", _Walk())])

    namespace = build_parser().parse_args(["passes", "retry", "app:app"])
    import wreath._cli as cli_module

    original = cli_module.load_application
    cli_module.load_application = lambda *a, **k: application
    try:
        assert execute_passes(namespace) == 0
    finally:
        cli_module.load_application = original

    out = capsys.readouterr().out
    assert "purge_replays: requeued 2 chunk(s)" in out
    # The distinction that matters: a queued hole is not a cleared one, and the
    # gate stays barred until the chunk actually succeeds.
    assert "not when it is queued" in out
