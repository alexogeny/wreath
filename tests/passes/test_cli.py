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
    def __init__(self, database, schema="wreath"):
        self._db = database
        self._schema = schema


class _Application:
    def __init__(self, database, schema="wreath"):
        self._databases = {"main": database}
        self._job_runners = {"work": _Runner(database, schema)}


def _seed(world, **overrides):
    row = {
        "name": "purge_replays", "tenant": "", "phase": "walking",
        "cursor": ["2026-07-27T11:00:00+00:00", "k042"], "ceiling": None, "pending": [],
        "units_done": 14, "rows_done": 14_000, "denominator": None,
        "denominator_kind": None, "chunk_limit": 1000,
        "paced_reason": "duty cycle 0.25", "started_at": NOW,
        "last_advance": NOW - datetime.timedelta(seconds=4), "cycle_started": NOW,
        "verified_at": None, "verified_fact": None, "last_error": None,
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


def test_stage_one_ships_status_and_not_the_stage_three_verbs():
    # retry/pause/resume belong with the dead-letter machinery; offering them
    # here would promise a hole could be cleared when nothing records one yet.
    parser = build_parser()
    for verb in ("retry", "pause", "resume"):
        with pytest.raises(SystemExit):
            parser.parse_args(["passes", verb, "app:app"])


# --- reading ------------------------------------------------------------------


def test_status_prints_a_row_per_pass(cli, capsys):
    world, _, _ = cli
    _seed(world)

    assert execute_passes(_namespace()) == 0

    out = capsys.readouterr().out
    assert "purge_replays" in out
    assert "walking" in out
    assert "14000" in out
    # A paced pass that does not say it is paced is indistinguishable from a
    # broken one, so the reason is on the row.
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


def test_a_blocked_pass_says_nothing_is_driving_it(cli, capsys):
    world, _, _ = cli
    _seed(world, phase="blocked", last_error=None)

    execute_passes(_namespace())

    # The state a hand-rolled backfill has no name for: it will silently never
    # finish, and nothing else in the system is going to mention it.
    assert "nothing is driving this pass" in capsys.readouterr().out


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
