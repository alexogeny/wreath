"""The pytest-compatible activity grid, including a real nested run.

The subprocess test contains a deliberate failing test.  The outer assertion
expects exit code 1 and a red outcome in the JSON report, which falsifies the
reporting path rather than trusting a pretty all-green fixture.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wreath import _cli as cli_parser
from wreath import _test_runner as runner
from wreath import cli


def _report(nodeid: str, outcome: str, when: str, duration: float) -> SimpleNamespace:
    return SimpleNamespace(
        nodeid=nodeid,
        outcome=outcome,
        when=when,
        duration=duration,
    )


def test_run_activity_aggregates_phase_timings_and_file_outcomes() -> None:
    activity = runner.RunActivity(workers=2)
    passed = "tests/test_one.py::test_passes"
    skipped = "tests/test_one.py::test_skips"
    failed = "tests/test_two.py::test_fails"
    errored = "tests/test_three.py::test_setup_breaks"
    activity.collect((passed, skipped, failed, errored))

    for nodeid in (passed, skipped, failed, errored):
        activity.start_test(nodeid)
    activity.add_report(_report(passed, "passed", "setup", 0.1))
    activity.add_report(_report(passed, "passed", "call", 0.2))
    activity.add_report(_report(passed, "passed", "teardown", 0.3))
    activity.add_report(_report(skipped, "skipped", "setup", 0.4))
    activity.add_report(_report(failed, "failed", "call", 0.5))
    activity.add_report(_report(errored, "failed", "setup", 0.6))
    for nodeid in (passed, skipped, failed, errored):
        activity.finish_test(nodeid)
    activity.finish(1)

    assert activity.counts() == {
        "collected": 4,
        "finished": 4,
        "passed": 1,
        "failed": 1,
        "errors": 1,
        "skipped": 1,
    }
    assert activity.files["tests/test_one.py"].outcome == "mixed"
    assert activity.files["tests/test_two.py"].outcome == "failed"
    assert activity.files["tests/test_three.py"].outcome == "error"
    assert activity.tests[passed].duration == pytest.approx(0.6)


def test_duration_report_names_slowest_tests_and_robust_outliers() -> None:
    activity = runner.RunActivity(workers=1)
    nodeids = tuple(f"tests/test_speed.py::test_{index}" for index in range(5))
    activity.collect(nodeids)
    for nodeid, duration in zip(nodeids, (0.001, 0.001, 0.001, 0.001, 1.0), strict=True):
        activity.start_test(nodeid)
        activity.add_report(_report(nodeid, "passed", "call", duration))
        activity.finish_test(nodeid)
    activity.finish(0)

    report = activity.report(slowest=2)

    assert report["durations"]["mean_seconds"] == pytest.approx(0.2008)
    assert report["durations"]["median_seconds"] == pytest.approx(0.001)
    assert report["durations"]["outliers"] == 1
    assert report["durations"]["over_100ms"] == 1
    assert report["durations"]["over_250ms"] == 1
    assert report["durations"]["over_1s"] == 1
    assert report["slowest"][0]["nodeid"].endswith("test_4")
    assert len(report["slowest"]) == 2


def test_duration_report_practical_tail_boundaries_are_inclusive() -> None:
    activity = runner.RunActivity(workers=1)
    nodeids = tuple(f"tests/test_tail.py::test_{index}" for index in range(3))
    activity.collect(nodeids)
    for nodeid, duration in zip(nodeids, (0.1, 0.25, 1.0), strict=True):
        activity.start_test(nodeid)
        activity.add_report(_report(nodeid, "passed", "call", duration))
        activity.finish_test(nodeid)

    durations = activity.report(slowest=0)["durations"]

    assert durations["over_100ms"] == 3
    assert durations["over_250ms"] == 2
    assert durations["over_1s"] == 1


def test_render_is_a_stable_file_heat_map_with_duration_statistics() -> None:
    activity = runner.RunActivity(workers=1)
    activity.collect((
        "tests/a/test_alpha.py::test_a",
        "tests/b/test_beta.py::test_b",
    ))
    activity.start_test("tests/a/test_alpha.py::test_a")
    activity.add_report(
        _report("tests/a/test_alpha.py::test_a", "passed", "call", 0.25)
    )
    activity.finish_test("tests/a/test_alpha.py::test_a")

    text = runner.render_activity(
        activity,
        width=80,
        height=24,
        colour=False,
        slowest=1,
    )

    assert "Test activity   current run" in text
    assert "2 tests · 2 files" in text
    assert "■ ·" in text
    assert "Duration   Less ■ ■ ■ ■ ■ More" in text
    assert (
        "Outcome    · queued · ◆ running · ■ pass · ▣ mutation testing · "
        "▰ pass + killed mutant · ▲ skip/mixed · ✕ fail/error"
    ) in text
    assert "average 250.0ms" in text
    assert "slow tail   1 >=100ms · 1 >=250ms · 0 >=1s · Tukey 0 >250.0ms" in text
    assert "Slowest tests" in text

    coloured = runner.render_activity(
        activity,
        width=80,
        height=24,
        colour=True,
        slowest=0,
    )
    assert "\x1b[38;5;238m■\x1b[0m queued" in coloured
    assert "\x1b[38;5;51m◆\x1b[0m running" in coloured
    assert "\x1b[38;5;46m■\x1b[0m pass" in coloured
    assert "\x1b[38;5;201m▣\x1b[0m mutation testing" in coloured
    assert "\x1b[38;5;226m▰\x1b[0m pass + killed mutant" in coloured
    assert "\x1b[38;5;202m■\x1b[0m skip/mixed" in coloured
    assert "\x1b[38;5;196m■\x1b[0m fail/error" in coloured


def test_mutation_tiles_advance_from_active_to_verified() -> None:
    activity = runner.RunActivity(workers=1)
    first = "tests/test_policy.py::test_refuses"
    second = "tests/test_other.py::test_passes"
    activity.collect((first, second))
    for nodeid in (first, second):
        activity.start_test(nodeid)
        activity.add_report(_report(nodeid, "passed", "call", 0.01))
        activity.finish_test(nodeid)
    activity.finish(0)

    active = runner.render_activity(
        activity,
        width=100,
        height=30,
        colour=False,
        slowest=0,
        mutation=runner.MutationActivity(
            mode="auto",
            state="running",
            total=1,
            mutating_files=frozenset({"tests/test_policy.py"}),
        ),
    )
    verified = runner.render_activity(
        activity,
        width=100,
        height=30,
        colour=False,
        slowest=0,
        mutation=runner.MutationActivity(
            mode="auto",
            state="complete",
            total=1,
            rating_label="SAMPLE WATCHED",
            rating_action="Keep the assertions that caught these controls.",
            rating_tone="good",
            counts={"killed": 1},
            verified_files=frozenset({"tests/test_policy.py"}),
            live_probes=2,
            live_completed=1,
            live_cancelled_at_seal=1,
            live_first_started_seconds=0.24,
        ),
    )

    assert "■ ▣" in active
    assert "Mutation   ▣ auto · 1 sampled controls · testing controls" in active
    assert "■ ▰" in verified
    assert "Mutation   auto · ■ SAMPLE WATCHED" in verified
    assert "▰ 1 gold test file · 1 killed" in verified
    assert "live overlap · 2 started · 1 completed before seal · first at 0.24s" in verified
    assert "1 stopped at seal" in verified

    plural = runner.render_activity(
        activity,
        width=100,
        height=30,
        colour=False,
        slowest=0,
        mutation=runner.MutationActivity(
            mode="sample",
            state="complete",
            counts={"killed": 2},
            verified_files=frozenset({"tests/test_policy.py", "tests/test_other.py"}),
        ),
    )
    assert "▰ 2 gold test files · 2 killed" in plural


def test_mutation_report_requires_a_complete_rating() -> None:
    report = {
        "counts": {"killed": 1},
        "rating": {"label": "SAMPLE WATCHED", "action": "Keep it."},
    }

    with pytest.raises(ValueError, match="rating is incomplete"):
        runner._mutation_activity_from_report("auto", report)


def test_mutation_event_stream_moves_only_killers_to_verified(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"event":"planned","total":2}\n'
        '{"event":"started","tests":["tests/test_policy.py"]}\n',
        encoding="utf-8",
    )
    state = runner._MutationEventState()

    runner._consume_mutation_events(path, state)

    assert state.total == 2
    assert state.mutating_files == {"tests/test_policy.py"}
    assert state.verified_files == set()

    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            '{"event":"finished","outcome":"killed",'
            '"killers":["tests/test_policy.py::test_refuses"]}\n'
        )
    runner._consume_mutation_events(path, state)

    assert state.mutating_files == set()
    assert state.verified_files == {"tests/test_policy.py"}


def test_mutation_event_stream_keeps_every_parallel_mutant_purple(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"event":"started","ordinal":0,"tests":["tests/test_a.py"]}\n'
        '{"event":"started","ordinal":1,"tests":["tests/test_b.py"]}\n'
        '{"event":"finished","ordinal":0,"outcome":"killed",'
        '"killers":["tests/test_a.py::test_guard"]}\n',
        encoding="utf-8",
    )
    state = runner._MutationEventState()

    runner._consume_mutation_events(path, state)

    assert state.mutating_files == {"tests/test_b.py"}
    assert state.verified_files == {"tests/test_a.py"}

    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            '{"event":"finished","ordinal":1,"outcome":"survived",'
            '"killers":[]}\n'
        )
    runner._consume_mutation_events(path, state)

    assert state.mutating_files == set()


def test_alternate_screen_and_cursor_are_always_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    activity = runner.RunActivity(workers=1)
    stream = io.StringIO()
    renderer = runner.ActivityRenderer(
        activity,
        stream=stream,
        mode="always",
        slowest=0,
    )

    renderer.start()
    renderer.finish()

    output = stream.getvalue()
    assert "\x1b[?1049h" in output
    assert "\x1b[?1049l" in output
    assert "\x1b[?25h" in output
    assert renderer.active is False


def test_mutation_ratings_use_the_grid_palette_only_on_a_colour_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    coloured = runner._format_mutation_rating("REVIEW ASSERTIONS", "attention", Tty())
    plain = runner._format_mutation_rating(
        "REVIEW ASSERTIONS", "attention", io.StringIO()
    )

    assert coloured == "\x1b[38;5;196m✕ REVIEW ASSERTIONS\x1b[0m"
    assert plain == "✕ REVIEW ASSERTIONS"


def test_history_is_atomic_bounded_run_data_and_updates_file_average(tmp_path: Path) -> None:
    activity = runner.RunActivity(workers=1)
    nodeid = "tests/test_one.py::test_timing"
    activity.collect((nodeid,))
    activity.start_test(nodeid)
    activity.add_report(_report(nodeid, "passed", "call", 0.2))
    activity.finish_test(nodeid)
    activity.finish(0)
    path = tmp_path / "history.json"

    first = activity.report(slowest=0)
    runner._update_history(path, first)
    second = dict(first)
    second["files"] = [dict(first["files"][0], seconds=0.4)]
    runner._update_history(path, second)

    history = json.loads(path.read_text(encoding="utf-8"))
    row = history["files"]["tests/test_one.py"]
    assert row["samples"] == 2
    assert row["mean_seconds"] == pytest.approx(0.3)
    test_row = history["tests"][nodeid]
    assert test_row["samples"] == 2
    assert test_row["mean_seconds"] == pytest.approx(0.2)
    assert len(history["runs"]) == 2


def test_duration_history_prioritizes_expensive_tests_for_dynamic_dispatch() -> None:
    nodeids = [
        "tests/test_fast.py::test_unknown_one",
        "tests/test_slow.py::test_slowest",
        "tests/test_medium.py::test_in_file",
        "tests/test_fast.py::test_unknown_two",
    ]
    test_weights = {"tests/test_slow.py::test_slowest": 2.0}
    file_weights = {"tests/test_medium.py": 1.0}

    ordered = sorted(
        nodeids,
        key=lambda nodeid: runner._historical_weight(
            nodeid, test_weights, file_weights
        ),
        reverse=True,
    )

    assert ordered == [
        "tests/test_slow.py::test_slowest",
        "tests/test_medium.py::test_in_file",
        "tests/test_fast.py::test_unknown_one",
        "tests/test_fast.py::test_unknown_two",
    ]
    # Two round-robin dispatch passes give each worker one expensive item,
    # rather than one worker receiving the entire slow prefix.
    assert ordered[0::2] == [
        "tests/test_slow.py::test_slowest",
        "tests/test_fast.py::test_unknown_one",
    ]
    assert ordered[1::2] == [
        "tests/test_medium.py::test_in_file",
        "tests/test_fast.py::test_unknown_two",
    ]


def test_historical_scheduler_refills_a_live_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xdist.scheduler.load import LoadScheduling

    monkeypatch.setattr(LoadScheduling, "__init__", lambda self, config, log: None)
    scheduler = runner.HistoricalSchedulerPlugin({}, {}).pytest_xdist_make_scheduler(
        object(), lambda _message: None
    )
    class Node:
        shutting_down = False

    node = Node()
    scheduler.pending = [1, 2]
    scheduler.node2pending = {node: []}
    sent: list[tuple[object, int]] = []
    scheduler._send_tests = lambda target, count: sent.append((target, count))

    scheduler.check_schedule(node)

    assert sent == [(node, 2)]


def test_an_invalid_mutation_report_is_refused_with_runner_context(tmp_path: Path) -> None:
    class FinishedProcess:
        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

    output = tmp_path / "mutation.json"
    output.write_text("not json", encoding="utf-8")
    mutation = runner._MutationProcess(
        process=FinishedProcess(),
        output_path=output,
        activity_path=tmp_path / "events.jsonl",
        event_state=runner._MutationEventState(total=1),
        baseline_reused=True,
    )
    namespace = SimpleNamespace(mutant="auto", mutant_fail_on_survivor=False)

    with pytest.raises(ValueError, match="mutation confidence returned invalid JSON"):
        runner._finish_mutation_process(namespace, mutation)


def test_a_failed_mutation_process_is_refused_before_reading_its_report(
    tmp_path: Path,
) -> None:
    class FailedProcess:
        def poll(self) -> int:
            return 2

        def wait(self) -> int:
            return 2

    output = tmp_path / "mutation.json"
    output.write_text("{}", encoding="utf-8")
    mutation = runner._MutationProcess(
        process=FailedProcess(),
        output_path=output,
        activity_path=tmp_path / "events.jsonl",
        event_state=runner._MutationEventState(total=1),
        baseline_reused=True,
    )
    namespace = SimpleNamespace(mutant="auto", mutant_fail_on_survivor=False)

    with pytest.raises(ValueError, match="mutation confidence phase failed"):
        runner._finish_mutation_process(namespace, mutation)


def test_a_killed_event_with_a_non_list_killer_field_awards_no_gold(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"event":"finished","ordinal":1,"outcome":"killed",'
        '"killers":"tests/test_policy.py::test_denied"}\n',
        encoding="utf-8",
    )
    state = runner._MutationEventState()

    runner._consume_mutation_events(events, state)

    assert state.verified_files == set()


def test_mutation_sample_cache_round_trips_exact_selection_and_watch_lines(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.json"
    key = {"fingerprint": "abc", "samples": 3}
    selected = frozenset({"guard.remove-raise@shop/gate.py:7"})
    watched = {"/repo/shop/gate.py": frozenset({7, 8})}
    whole_files = frozenset({"/repo/shop/constants.py"})

    runner._write_mutation_sample_cache(
        path, key, selected, watched, whole_files
    )

    assert runner._read_mutation_sample_cache(path, key) == (
        selected,
        watched,
        whole_files,
    )
    assert runner._read_mutation_sample_cache(path, {"fingerprint": "changed"}) is None


def test_test_command_forwards_unknown_pytest_arguments_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[Any] = []

    def fake_execute(namespace: Any) -> int:
        received.append(namespace)
        return 17

    monkeypatch.setattr(runner, "execute", fake_execute)

    result = cli.main([
        "test",
        "--workers",
        "1",
        "-k",
        "auth and not slow",
        "tests/security",
        "--maxfail=2",
    ])

    assert result == 17
    assert received[0].workers == "1"
    assert received[0].mutant_samples == 192
    assert received[0].mutant_budget == 50.0
    assert received[0].pytest_args == [
        "-k",
        "auth and not slow",
        "tests/security",
        "--maxfail=2",
    ]


def test_auto_mutation_workers_reclaim_idle_suite_slots_after_seal() -> None:
    namespace = cli_parser.build_parser().parse_args(["test"])

    arguments = runner._mutation_arguments(namespace)

    assert "--reclaim-workers" in arguments


def test_explicit_mutation_worker_limit_remains_literal_after_seal() -> None:
    namespace = cli_parser.build_parser().parse_args(
        ["test", "--mutant-workers", "1"]
    )

    arguments = runner._mutation_arguments(namespace)

    assert arguments[arguments.index("--jobs") + 1] == "1"
    assert "--reclaim-workers" not in arguments


@pytest.mark.parametrize(
    "argument,message",
    [
        (("--workers", "0"), "--workers must be at least 1"),
        (("--slowest", "-1"), "--slowest must be a non-negative integer"),
        (("--mutant-maxfail", "-1"), "--mutant-maxfail must be a non-negative integer"),
        (("--mutant-workers", "0"), "--mutant-workers must be at least 1"),
    ],
)
def test_test_command_refuses_invalid_runner_limits(
    argument: tuple[str, str], message: str, capsys: Any
) -> None:
    result = cli.main([
        "test",
        *argument,
        "--grid",
        "never",
        "--no-history",
        "tests/test_response_media_type.py",
    ])

    assert result == 2
    assert message in capsys.readouterr().err


def test_real_parallel_pytest_run_reports_pass_fail_and_skip(tmp_path: Path) -> None:
    suite = tmp_path / "test_activity_sample.py"
    report_path = tmp_path / "report.json"
    suite.write_text(
        "import pytest\n"
        "\n"
        "def test_passes():\n"
        "    assert 2 + 2 == 4\n"
        "\n"
        "def test_skips():\n"
        "    pytest.skip('runner classification probe')\n"
        "\n"
        "def test_fails():\n"
        "    assert False, 'deliberate reporting probe'\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "wreath",
            "test",
            "--workers",
            "2",
            "--grid",
            "never",
            "--no-history",
            "--report",
            str(report_path),
            str(suite),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "deliberate reporting probe" in completed.stdout
    assert "no eligible declared controls" in completed.stderr
    assert "\x1b[" not in completed.stdout + completed.stderr
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["kind"] == "wreath-test-run"
    assert "mutation" not in document
    assert document["workers"] == 2
    assert document["counts"] == {
        "collected": 3,
        "errors": 0,
        "failed": 1,
        "files": 1,
        "finished": 3,
        "passed": 1,
        "skipped": 1,
    }
    assert {row["outcome"] for row in document["tests"]} == {
        "failed",
        "passed",
        "skipped",
    }


def test_green_files_still_earn_mutation_confidence_beside_a_red_file(
    tmp_path: Path,
) -> None:
    package = tmp_path / "shop"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    mutant_ran = tmp_path / "mutant-ran"
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "policy.py").write_text(
        "def authorize(value):\n"
        "    if value != 'ok':\n"
        "        raise PermissionError('refused')\n"
        "    return value\n",
        encoding="utf-8",
    )
    (tests / "test_policy.py").write_text(
        "from pathlib import Path\n"
        "import pytest\n"
        "from shop.policy import authorize\n"
        "\n"
        "def test_accepts():\n"
        "    try:\n"
        "        result = authorize('ok')\n"
        "    except PermissionError:\n"
        f"        Path({str(mutant_ran)!r}).write_text('ran')\n"
        "        raise\n"
        "    assert result == 'ok'\n"
        "\n"
        "def test_refuses():\n"
        "    try:\n"
        "        authorize('bad')\n"
        "    except PermissionError as error:\n"
        "        assert str(error) == 'refused'\n"
        "        return\n"
        f"    Path({str(mutant_ran)!r}).write_text('ran')\n"
        "    pytest.fail('the removed guard admitted bad')\n",
        encoding="utf-8",
    )
    (tests / "test_broken.py").write_text(
        "from pathlib import Path\n"
        "import time\n"
        "def test_breaks():\n"
        "    deadline = time.monotonic() + 2.0\n"
        f"    while not Path({str(mutant_ran)!r}).exists():\n"
        "        assert time.monotonic() < deadline, 'live mutant never ran'\n"
        "        time.sleep(0.01)\n"
        "    assert False, 'independent red file'\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "wreath",
            "test",
            "--workers",
            "2",
            "--grid",
            "never",
            "--no-history",
            "--report",
            str(report_path),
            "--mutant-samples",
            "1",
            "--mutant-budget",
            "0.0001",
            "--mutant-path",
            "shop",
            "--mutant-tests",
            "tests",
            str(tests),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 1, completed.stderr
    assert "independent red file" in completed.stdout
    assert "▰" in completed.stderr
    assert "evidence limited to green tests · 1 baseline failure(s) excluded" in (
        completed.stderr
    )
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["mutation"]["counts"]["killed"] == 1
    assert document["mutation"]["live_kills"] == 1
    live = document["mutation"]["live"]
    assert live["probes"] == 1
    assert live["completed"] == 1
    assert live["killed"] == 1
    assert live["cancelled_at_seal"] == 0
    assert 0 < live["first_started_seconds"] < document["mutation"]["baseline"]["seconds"]
    assert document["mutation"]["verified_test_files"] == ["tests/test_policy.py"]
    assert document["mutation"]["baseline"]["failures"] == [
        "tests/test_broken.py::test_breaks"
    ]
    files = {row["path"]: row["outcome"] for row in document["files"]}
    assert files == {
        "tests/test_broken.py": "failed",
        "tests/test_policy.py": "passed",
    }


def test_default_mutation_sample_reuses_baseline_and_attaches_to_json_report(
    tmp_path: Path,
) -> None:
    package = tmp_path / "shop"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "policy.py").write_text(
        "def authorize(value):\n"
        "    if value != 'ok':\n"
        "        raise PermissionError('refused')\n"
        "    return value\n",
        encoding="utf-8",
    )
    (tests / "test_policy.py").write_text(
        "import pytest\n"
        "from shop.policy import authorize\n"
        "\n"
        "def test_accepts():\n"
        "    assert authorize('ok') == 'ok'\n"
        "\n"
        "def test_refuses():\n"
        "    with pytest.raises(PermissionError, match='refused'):\n"
        "        authorize('bad')\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "wreath",
            "test",
            "--workers",
            "2",
            "--grid",
            "never",
            "--no-history",
            "--report",
            str(report_path),
            "--mutant-samples",
            "1",
            "--mutant-path",
            "shop",
            "--mutant-tests",
            "tests",
            str(tests),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Mutation activity   auto" in completed.stderr
    assert "\x1b[" not in completed.stderr
    document = json.loads(report_path.read_text(encoding="utf-8"))
    mutation = document["mutation"]
    assert mutation["baseline"]["tests"] == 2
    assert mutation["baseline_reused"] is True
    assert sum(mutation["counts"].values()) == 1
    assert mutation["rating"]["label"] == "SAMPLE WATCHED"
    assert mutation["verified_test_files"] == ["tests/test_policy.py"]
    assert "score" not in mutation
