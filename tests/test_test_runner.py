from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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


def test_run_activity_ingests_terminal_native_results_without_report_objects() -> None:
    activity = runner.RunActivity(workers=1)
    passed = "tests/test_native.py::test_passes"
    skipped = "tests/test_native.py::test_skips"
    failed = "tests/test_native.py::test_fails"
    activity.collect((passed, skipped, failed))
    activity.start_native_tests((passed, skipped, failed))

    assert activity.files["tests/test_native.py"].outcome == "running"

    activity.add_native_result(passed, "passed", 0.1)
    activity.add_native_result(skipped, "skipped", 0.2)
    activity.add_native_result(failed, "failed", 0.3)

    assert activity.counts() == {
        "collected": 3,
        "finished": 3,
        "passed": 1,
        "failed": 1,
        "errors": 0,
        "skipped": 1,
    }
    assert activity.tests[failed].duration == pytest.approx(0.3)
    assert activity.files["tests/test_native.py"].outcome == "failed"


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


def test_render_is_a_stable_file_state_map_with_duration_statistics() -> None:
    activity = runner.RunActivity(workers=1)
    activity.collect(
        (
            "tests/a/test_alpha.py::test_a",
            "tests/b/test_beta.py::test_b",
        )
    )
    activity.start_test("tests/a/test_alpha.py::test_a")
    activity.add_report(_report("tests/a/test_alpha.py::test_a", "passed", "call", 0.25))
    activity.finish_test("tests/a/test_alpha.py::test_a")

    text = runner.render_activity(
        activity,
        width=80,
        height=24,
        slowest=1,
    )

    assert "Test activity   current run" in text
    assert "2 tests · 2 files" in text
    assert "Test pass     ■" in text
    assert "Queued        ■" in text
    assert "Duration   Less" not in text
    assert "State      ■ queued · ■ running · ■ pass" in text
    for state in (
        "■ mutation testing",
        "■ mutation passed",
        "× mutation failed",
        "■ fuzzing",
        "× fuzz failed",
        "★ all stages passed",
        "■ skip/mixed",
        "× fail/error",
    ):
        assert state in text
    assert "average 250.0ms" in text
    assert "slow tail   1 >=100ms · 1 >=250ms · 0 >=1s · Tukey 0 >250.0ms" in text
    assert "Slowest tests" in text

    assert "\x1b[" not in text


def test_final_render_names_failed_tests() -> None:
    activity = runner.RunActivity(workers=1)
    nodeid = "tests/test_policy.py::test_refuses_an_untrusted_caller"
    activity.collect((nodeid,))
    activity.add_native_result(nodeid, "failed", 0.01)
    activity.finish(1)

    text = runner.render_activity(
        activity,
        width=100,
        height=24,
        slowest=0,
    )

    assert "Failures" in text
    assert f"× {nodeid}" in text


def test_static_renderer_does_no_work_until_finish_and_never_emits_ansi() -> None:
    activity = runner.RunActivity(workers=1)
    nodeid = "tests/test_passed.py::test_contract"
    activity.collect((nodeid,))
    activity.add_native_result(nodeid, "passed", 0.01)
    activity.finish(0)
    stream = io.StringIO()
    renderer = runner.ActivityRenderer(
        activity,
        stream=stream,
        mode="never",
        slowest=0,
    )

    assert stream.getvalue() == ""
    renderer.finish()

    output = stream.getvalue()
    assert output.count("Test activity   current run") == 1
    assert "\x1b[" not in output


def test_state_legend_never_soft_wraps_terminal_rows() -> None:
    for width in (40, 80, 120, 190):
        lines = runner._state_legend_lines(width=width)
        assert all(len(line) < width for line in lines)


def test_mutation_tiles_show_active_verified_and_failed_states() -> None:
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
    failed = runner.render_activity(
        activity,
        width=100,
        height=30,
        slowest=0,
        mutation=runner.MutationActivity(
            mode="auto",
            state="complete",
            failed_mutation_files=frozenset({"tests/test_policy.py"}),
        ),
    )

    assert "Mutating      ■" in active
    assert "Test pass     ■" in active
    assert "Mutation   ■ auto · 1 sampled controls · testing controls" in active
    assert "Mutant pass   ■" in verified
    assert "Mutation   auto · ■ SAMPLE WATCHED" in verified
    assert "Mutation miss ×" in verified
    assert "Test pass" not in verified
    assert "■ 1 gold test file · 1 without mutation evidence · 1 killed" in verified
    assert "live overlap · 2 started · 1 completed before seal · first at 0.24s" in verified
    assert "1 stopped at seal" in verified
    assert "Mutation miss ×" in failed

    plural = runner.render_activity(
        activity,
        width=100,
        height=30,
        slowest=0,
        mutation=runner.MutationActivity(
            mode="sample",
            state="complete",
            counts={"killed": 2},
            verified_files=frozenset({"tests/test_policy.py", "tests/test_other.py"}),
        ),
    )
    assert "■ 2 gold test files · 2 killed" in plural


def test_fuzz_tiles_move_between_named_groups_and_finish_as_stars() -> None:
    activity = runner.RunActivity(workers=1)
    paths = ("tests/test_hero.py", "tests/test_active.py", "tests/test_failed.py")
    nodeids = tuple(f"{path}::test_contract" for path in paths)
    activity.collect(nodeids)
    for nodeid in nodeids:
        activity.add_native_result(nodeid, "passed", 0.01)
    activity.finish(0)
    mutation = runner.MutationActivity(
        mode="auto",
        state="complete",
        verified_files=frozenset(paths),
    )
    fuzz = runner.FuzzActivity(
        state="running",
        selected_files=frozenset(paths),
        active_files=frozenset({"tests/test_active.py"}),
        passed_files=frozenset({"tests/test_hero.py"}),
        failed_files=frozenset({"tests/test_failed.py"}),
    )

    text = runner.render_activity(
        activity,
        width=100,
        height=30,
        slowest=0,
        mutation=mutation,
        fuzz=fuzz,
    )

    assert "Complete      ★" in text
    assert "Fuzzing       ■" in text
    assert "Mutant pass   ×" in text
    assert "Fuzz       ■ 1 active · 1 complete" in text


def test_fuzz_pulses_only_files_reported_running_by_a_worker(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    file_state = runner.FileState("tests/test_fuzz.py")
    state = runner._FuzzEventState()

    runner._append_stage_event(events_path, file_state, outcome="running")
    runner._consume_fuzz_events(events_path, state)
    running = runner._fuzz_activity(
        "running", ("tests/test_fuzz.py", "tests/test_waiting.py"), state
    )

    assert running.active_files == frozenset({"tests/test_fuzz.py"})

    runner._append_stage_event(events_path, file_state, outcome="passed")
    runner._consume_fuzz_events(events_path, state)
    finished = runner._fuzz_activity(
        "running", ("tests/test_fuzz.py", "tests/test_waiting.py"), state
    )
    assert finished.active_files == frozenset()
    assert finished.passed_files == frozenset({"tests/test_fuzz.py"})


def test_mutation_report_requires_a_complete_rating() -> None:
    report = {
        "counts": {"killed": 1},
        "rating": {"label": "SAMPLE WATCHED", "action": "Keep it."},
    }

    with pytest.raises(ValueError, match="rating is incomplete"):
        runner._mutation_activity_from_report("auto", report)


def test_mutation_report_does_not_blame_candidate_files_for_a_survivor() -> None:
    report = {
        "counts": {"survived": 1},
        "rating": {
            "label": "REVIEW ASSERTIONS",
            "action": "Add an objection.",
            "tone": "attention",
        },
        "failed_mutation_test_files": ["tests/test_policy.py"],
    }

    activity = runner._mutation_activity_from_report("auto", report)

    assert activity.failed_mutation_files == frozenset()


def test_mutation_event_stream_moves_only_killers_to_verified(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"event":"planned","total":2}\n{"event":"started","tests":["tests/test_policy.py"]}\n',
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
    assert state.killer_tests == {"tests/test_policy.py::test_refuses"}


def test_mutation_event_stream_keeps_every_parallel_mutant_purple(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"event":"capacity","test_workers":5,"mutant_workers":3}\n'
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
    assert (state.test_workers, state.mutant_workers) == (5, 3)

    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"event":"finished","ordinal":1,"outcome":"survived","killers":[]}\n')
    runner._consume_mutation_events(path, state)

    assert state.mutating_files == set()
    assert state.survivor_candidate_files == {"tests/test_b.py"}


def test_static_reporter_retains_mutation_progress_without_writing() -> None:
    activity = runner.RunActivity(workers=1)
    stream = io.StringIO()
    renderer = runner.ActivityRenderer(
        activity,
        stream=stream,
        mode="never",
        slowest=0,
    )

    for active in ({"tests/test_a.py"}, {"tests/test_b.py"}):
        renderer.mutation_progress(
            "auto",
            2,
            mutating_files=frozenset(active),
            verified_files=frozenset(),
            failed_mutation_files=frozenset(),
        )

    assert stream.getvalue() == ""
    assert renderer.mutation is not None
    assert renderer.mutation.mutating_files == frozenset({"tests/test_b.py"})


def test_a_passing_session_defers_its_final_render_for_mutation() -> None:
    plugin = runner.ActivityPlugin(
        runner.RunnerConfig(
            grid="never",
            history=None,
            mutation_mode="auto",
        ),
        workers=1,
    )
    node_id = "tests/test_green.py::test_green"
    plugin.activity.collect((node_id,))
    plugin.activity.start_native_tests((node_id,))
    plugin.activity.add_native_result(node_id, "passed", 0.001)
    calls: list[str] = []
    plugin.renderer.defer = lambda: calls.append("defer")
    plugin.renderer.finish = lambda: calls.append("finish")
    plugin.renderer.finish_with_mutation = lambda mutation: calls.append("mutation")

    plugin.pytest_sessionfinish(None, 0)

    assert plugin.deferred is True
    assert calls == ["defer"]


def test_session_finish_distinguishes_no_green_from_mutation_disabled() -> None:
    no_green = runner.ActivityPlugin(
        runner.RunnerConfig(
            grid="never",
            history=None,
            mutation_mode="auto",
        ),
        workers=1,
    )
    no_green_calls: list[str] = []
    no_green.renderer.defer = lambda: no_green_calls.append("defer")
    no_green.renderer.finish = lambda: no_green_calls.append("finish")
    no_green.renderer.finish_with_mutation = lambda mutation: no_green_calls.append(
        f"mutation:{mutation.state}"
    )

    no_green.pytest_sessionfinish(None, 1)

    assert no_green.deferred is False
    assert no_green_calls == ["mutation:no_green"]

    disabled = runner.ActivityPlugin(
        runner.RunnerConfig(
            grid="never",
            history=None,
            mutation_mode="off",
        ),
        workers=1,
    )
    node_id = "tests/test_green.py::test_green"
    disabled.activity.collect((node_id,))
    disabled.activity.start_native_tests((node_id,))
    disabled.activity.add_native_result(node_id, "passed", 0.001)
    disabled_calls: list[str] = []
    disabled.renderer.defer = lambda: disabled_calls.append("defer")
    disabled.renderer.finish = lambda: disabled_calls.append("finish")
    disabled.renderer.finish_with_mutation = lambda mutation: disabled_calls.append("mutation")

    disabled.pytest_sessionfinish(None, 0)

    assert disabled.deferred is False
    assert disabled_calls == ["finish"]


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


def _history_after(path: Path, samples: list[tuple[str, float]]) -> None:
    """Drive `_update_history` once per (outcome, seconds) pair, in order."""
    nodeid = "tests/test_one.py::test_timing"
    for outcome, seconds in samples:
        activity = runner.RunActivity(workers=1)
        activity.collect((nodeid,))
        activity.start_test(nodeid)
        activity.add_report(_report(nodeid, outcome, "call", seconds))
        activity.finish_test(nodeid)
        activity.finish(0)
        runner._update_history(path, activity.report(slowest=0))


def test_a_test_that_stops_running_stops_carrying_its_old_weight(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    _history_after(path, [("passed", 3.3)] * 30 + [("skipped", 0.0004)])

    test_weights, _ = runner._history_weights(path)
    assert test_weights["tests/test_one.py::test_timing"] == 0.0, (
        "a test that skipped last run is still being scheduled as expensive"
    )


def test_a_weight_follows_a_lasting_change_in_what_a_test_costs(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    old, new = 0.1, 2.0
    _history_after(path, [("passed", old)] * 200 + [("passed", new)] * runner._MEAN_WINDOW)

    test_weights, _ = runner._history_weights(path)
    weight = test_weights["tests/test_one.py::test_timing"]
    assert weight > (old + new) / 2, f"weight {weight:.3f} still anchored to {old}"


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
        key=lambda nodeid: runner._historical_weight(nodeid, test_weights, file_weights),
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


class _Node:
    """Enough of an xdist worker to be dispatched to."""

    shutting_down = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[list[str]] = []

    def shutdown(self) -> None:
        self.shutting_down = True

    def __repr__(self) -> str:
        return f"<node {self.name}>"


def _scheduler(
    monkeypatch: pytest.MonkeyPatch,
    collection: list[str],
    *,
    workers: int = 2,
    test_weights: dict[str, float] | None = None,
    file_weights: dict[str, float] | None = None,
):
    """The real scheduler over a fake collection, with `_send_tests` recorded."""
    from xdist.scheduler.load import LoadScheduling

    monkeypatch.setattr(LoadScheduling, "__init__", lambda self, config, log: None)
    scheduler = runner.HistoricalSchedulerPlugin(
        test_weights or {}, file_weights or {}
    ).pytest_xdist_make_scheduler(object(), lambda *_args: None)

    nodes = [_Node(f"gw{index}") for index in range(workers)]
    # `collection_is_completed` and `nodes` are properties over the two dicts
    # below rather than attributes, so they are set by populating those.
    scheduler.numnodes = workers
    scheduler.log = lambda *_args: None
    scheduler.collection = None
    scheduler.maxschedchunk = None
    scheduler.pending = []
    scheduler.node2pending = {node: [] for node in nodes}
    scheduler.node2collection = {node: collection for node in nodes}
    scheduler._check_nodes_have_same_collection = lambda: True

    def send(node, count):
        taken = scheduler.pending[:count]
        del scheduler.pending[:count]
        scheduler.node2pending[node].extend(taken)
        node.sent.append([scheduler.collection[index] for index in taken])

    scheduler._send_tests = send
    return scheduler, nodes


def test_the_heavy_head_goes_out_longest_first_and_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = [
        "tests/test_a.py::quick_one",
        "tests/test_a.py::slowest",
        "tests/test_b.py::middling",
        "tests/test_a.py::quick_two",
    ]
    scheduler, nodes = _scheduler(
        monkeypatch,
        collection,
        test_weights={"tests/test_a.py::slowest": 2.0, "tests/test_b.py::middling": 1.0},
    )
    scheduler.schedule()

    # The first thing each worker is given is one of the two expensive tests,
    # heaviest first, so they run side by side instead of queueing behind each
    # other on one worker.
    assert nodes[0].sent[0] == ["tests/test_a.py::slowest"]
    assert nodes[1].sent[0] == ["tests/test_b.py::middling"]


def test_the_cheap_tail_keeps_collection_order_and_travels_in_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = [f"tests/test_{index // 10}.py::test_{index}" for index in range(200)]
    scheduler, nodes = _scheduler(monkeypatch, collection, workers=2)
    scheduler.schedule()

    # Seeding is deliberately thin; the chunking shows up on the first refill,
    # which is what a worker actually spends the run doing.
    node = nodes[0]
    scheduler.node2pending[node].clear()
    scheduler.check_schedule(node)

    refill = node.sent[-1]
    assert len(refill) > 2, (
        "the tail was handed out in ones and twos, which is the cost this avoids"
    )
    positions = [collection.index(nodeid) for nodeid in refill]
    assert positions == list(range(positions[0], positions[0] + len(positions))), (
        "a refill must be a consecutive run, or neighbouring tests stop sharing fixtures"
    )


def test_an_xdist_group_is_handed_to_one_worker_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = [
        "tests/test_mutant.py::test_one@mutant_crud",
        "tests/test_fast.py::test_unrelated",
        "tests/test_mutant.py::test_two@mutant_crud",
        "tests/test_mutant.py::test_three@mutant_cedar",
    ]
    scheduler, nodes = _scheduler(monkeypatch, collection, workers=2)
    scheduler.schedule()

    for name in ("mutant_crud", "mutant_cedar"):
        owners = {
            node.name
            for node in nodes
            for batch in node.sent
            for nodeid in batch
            if nodeid.endswith(f"@{name}")
        }
        assert len(owners) == 1, f"{name} was split across {owners}"
    assert nodes[0].sent[0] == [
        "tests/test_mutant.py::test_one@mutant_crud",
        "tests/test_mutant.py::test_two@mutant_crud",
    ], "a group must go out in one send, or it is not on one worker"


def test_collection_shards_keep_whole_modules_and_balance_recorded_costs(
    tmp_path: Path,
) -> None:
    modules = tuple(tmp_path / f"test_{index}.py" for index in range(8))
    for module in modules:
        module.write_text("def test_one(): pass\n", encoding="utf-8")
    weights = {module: float(index + 1) for index, module in enumerate(modules)}

    shards = runner._collection_shards(modules, workers=2, weights=weights)

    assert {Path(path) for path, _owner in shards} == set(modules)
    loads = [0.0, 0.0]
    for path, owner in shards:
        loads[owner] += weights[Path(path)]
    assert loads == [18.0, 18.0]


def test_auto_collection_sharding_requires_a_broad_history_backed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    modules = []
    for index in range(8):
        module = tests / f"test_{index}.py"
        module.write_text("def test_one(): pass\n", encoding="utf-8")
        modules.append(module)
    stamp = "2026-08-23T00:00:00+00:00"
    history = tmp_path / "history.json"
    history.write_text(
        json.dumps(
            {
                "version": 1,
                "runs": [
                    {
                        "finished_at": stamp,
                        "counts": {"collected": len(modules)},
                    }
                ],
                "files": {
                    str(module.relative_to(tmp_path)): {
                        "last_seen": stamp,
                        "last_seconds": 1.0,
                        "last_outcome": "passed",
                    }
                    for module in modules
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    shards = runner._prepare_collection_shards("auto", [], workers=2, history=history)

    assert len(shards) == len(modules)
    assert (
        runner._prepare_collection_shards("auto", [str(modules[0])], workers=2, history=history)
        == ()
    )
    for focused in (["-m", "fuzz"], ["--markexpr=fuzz"], ["-kneedle"]):
        assert runner._prepare_collection_shards("auto", focused, workers=2, history=history) == ()
    assert runner._prepare_collection_shards("auto", ["-m", ""], workers=2, history=history)
    assert runner._prepare_collection_shards("auto", [], workers=2, history=None) == ()


def test_forced_collection_sharding_needs_no_timing_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    for index in range(8):
        (tests / f"test_{index}.py").write_text("def test_one(): pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    shards = runner._prepare_collection_shards("sharded", [], workers=2, history=None)

    assert len(shards) == 8
    assert {owner for _path, owner in shards} == {0, 1}

    with pytest.raises(ValueError, match="collection replicated.*max-worker-restart"):
        runner._prepare_collection_shards(
            "sharded",
            ["--max-worker-restart=1"],
            workers=2,
            history=None,
        )


def test_collection_sharding_refuses_a_group_that_spans_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    grouped = (
        "import pytest\npytestmark = pytest.mark.xdist_group(name='shared')\ndef test_one(): pass\n"
    )
    for index in range(8):
        (tests / f"test_{index}.py").write_text(
            grouped if index < 2 else "def test_one(): pass\n",
            encoding="utf-8",
        )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(
        ValueError,
        match="xdist_group 'shared' spans 2 modules.*collection replicated",
    ):
        runner._prepare_collection_shards("sharded", [], workers=2, history=None)


def test_a_parametrised_id_containing_an_at_sign_is_not_a_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert runner._xdist_group("tests/test_mail.py::test_to[a@b.com]") is None
    assert runner._xdist_group("tests/test_mutant.py::test_x@group") == "group"
    assert runner._xdist_group("tests/test_plain.py::test_y") is None


def test_historical_scheduler_refills_a_live_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = ["tests/test_a.py::one", "tests/test_a.py::two", "tests/test_a.py::three"]
    scheduler, nodes = _scheduler(
        monkeypatch,
        collection,
        workers=1,
        file_weights={"tests/test_a.py": 1.0},
    )
    scheduler.schedule()
    node = nodes[0]
    assert node.sent == [["tests/test_a.py::one"], ["tests/test_a.py::two"]]

    scheduler.node2pending[node].clear()
    scheduler.check_schedule(node)

    assert node.sent[-1] == ["tests/test_a.py::three"]


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

    runner._write_mutation_sample_cache(path, key, selected, watched, whole_files)

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

    result = cli.main(
        [
            "test",
            "--workers",
            "1",
            "-k",
            "auth and not slow",
            "tests/security",
            "--maxfail=2",
        ]
    )

    assert result == 17
    assert received[0].workers == "1"
    assert received[0].collection == "auto"
    assert received[0].mutant_samples == cli_parser._DEFAULT_MUTANT_SAMPLES == 192
    assert received[0].mutant_budget == 50.0
    assert received[0].pytest_args == [
        "-k",
        "auth and not slow",
        "tests/security",
        "--maxfail=2",
    ]


def test_test_command_selects_the_native_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[Any] = []

    def fake_execute(namespace: Any) -> int:
        received.append(namespace)
        return 0

    monkeypatch.setattr(runner, "execute", fake_execute)

    assert cli.main(["test", "--engine", "native", "--mutant", "off"]) == 0
    assert received[0].engine == "native"


def test_test_command_defaults_to_native_execution() -> None:
    namespace = cli_parser.build_parser().parse_args(["test"])

    assert namespace.engine == "native"
    assert namespace.grid == "never"
    assert namespace.mutant_engine == "native"
    assert namespace.fuzz == "auto"


def test_test_command_refuses_removed_animated_grid_modes() -> None:
    with pytest.raises(SystemExit):
        cli_parser.build_parser().parse_args(["test", "--grid", "auto"])


def test_test_command_accepts_independent_mutation_and_fuzz_switches() -> None:
    namespace = cli_parser.build_parser().parse_args(["test", "--mutant", "on", "--fuzz", "on"])

    assert namespace.mutant == "on"
    assert namespace.fuzz == "on"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [([], "on"), (["--mutant", "off"], "off")],
)
def test_auto_fuzz_follows_the_mutation_switch(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    expected: str,
) -> None:
    from wreath import _native_test_runner as native_runner

    received: list[str] = []

    def fake_execute(namespace: Any) -> int:
        received.append(str(namespace.fuzz))
        return 0

    monkeypatch.setattr(native_runner, "execute", fake_execute)
    namespace = cli_parser.build_parser().parse_args(["test", *arguments])

    assert runner.execute(namespace) == 0
    assert received == [expected]


def test_fuzz_refuses_to_run_without_mutation_evidence() -> None:
    namespace = cli_parser.build_parser().parse_args(["test", "--mutant", "off", "--fuzz", "on"])

    with pytest.raises(ValueError, match="--fuzz on requires mutation evidence"):
        runner.execute(namespace)


def test_fuzz_without_gold_has_an_explicit_empty_stage() -> None:
    report, activity = runner._no_gold_fuzz()

    assert report["selected_files"] == []
    assert activity.state == "no_gold"
    assert activity.selected_files == frozenset()


def test_fuzz_command_runs_the_fresh_native_evidence_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[Any] = []

    def fake_execute(namespace: Any) -> int:
        received.append(namespace)
        return 0

    monkeypatch.setattr(runner, "execute", fake_execute)

    assert cli.main(["fuzz", "--workers", "1"]) == 0
    assert received[0].engine == "native"
    assert received[0].mutant == "on"
    assert received[0].fuzz == "on"
    assert received[0].workers == "1"


def test_fuzz_selects_every_file_with_positive_mutation_evidence() -> None:
    mutation = {
        "verified_test_files": ["tests/test_gold.py", "tests/test_survivor.py"],
        "failed_mutation_test_files": ["tests/test_survivor.py"],
    }

    assert runner._mutation_gold_files(mutation) == (
        "tests/test_gold.py",
        "tests/test_survivor.py",
    )


def test_fuzz_runs_exact_killers_from_each_gold_file() -> None:
    mutation = {
        "mutants": [
            {
                "outcome": "killed",
                "killers": [
                    "tests/test_first.py::test_guard",
                    "tests/test_failed.py::test_guard",
                ],
            },
            {
                "outcome": "survived",
                "killers": ["tests/test_first.py::test_not_a_kill"],
            },
        ]
    }

    assert runner._mutation_gold_tests(mutation, ("tests/test_first.py",)) == (
        "tests/test_first.py::test_guard",
    )


def test_live_fuzz_unlocks_when_five_percent_of_passed_files_are_gold() -> None:
    assert not runner._live_fuzz_ready(0, 100)
    assert not runner._live_fuzz_ready(4, 100)
    assert runner._live_fuzz_ready(5, 100)
    assert runner._live_fuzz_ready(1, 1)


def test_live_and_sealed_fuzz_batches_merge_without_repeating_files() -> None:
    first = runner.FuzzActivity(
        state="complete",
        selected_files=frozenset({"tests/test_early.py"}),
        passed_files=frozenset({"tests/test_early.py"}),
        counts={"collected": 1, "passed": 1},
    )
    second = runner.FuzzActivity(
        state="complete",
        selected_files=frozenset({"tests/test_late.py"}),
        failed_files=frozenset({"tests/test_late.py"}),
        counts={"collected": 1, "failed": 1},
    )

    report, activity, status = runner._merge_fuzz_batches(
        [({}, first, 0), ({}, second, 1)],
        ("tests/test_early.py", "tests/test_late.py"),
    )

    assert report["batches"] == 2
    assert report["counts"] == {"collected": 2, "passed": 1, "failed": 1}
    assert activity.passed_files == frozenset({"tests/test_early.py"})
    assert activity.failed_files == frozenset({"tests/test_late.py"})
    assert status == 1


def test_rerunning_a_killer_is_the_generic_fuzz_contract(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    event_path = tmp_path / "events.jsonl"
    log_path = tmp_path / "runner.log"
    report_path.write_text(
        json.dumps(
            {
                "counts": {"collected": 1, "passed": 1},
                "fuzz_case_ids": [],
            }
        ),
        encoding="utf-8",
    )
    event_path.write_text(
        '{"path":"tests/test_policy.py","outcome":"passed"}\n',
        encoding="utf-8",
    )
    log_path.write_text("", encoding="utf-8")

    class FinishedProcess:
        @staticmethod
        def poll() -> int:
            return 0

        @staticmethod
        def wait() -> int:
            return 0

    process = runner._FuzzProcess(
        process=cast(Any, FinishedProcess()),
        report_path=report_path,
        event_path=event_path,
        log_path=log_path,
        events=runner._FuzzEventState(),
        selected=("tests/test_policy.py",),
    )
    mutation = runner.MutationActivity(mode="sample", state="complete")

    report, activity, status = runner._finish_fuzz_process(process, mutation, renderer=None)

    assert status == 0
    assert activity.passed_files == frozenset({"tests/test_policy.py"})
    assert activity.schedule_only_files == frozenset({"tests/test_policy.py"})
    assert report["schedule_only_files"] == ["tests/test_policy.py"]


def test_only_an_explicit_passing_fuzz_contract_earns_a_star(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    event_path = tmp_path / "events.jsonl"
    log_path = tmp_path / "runner.log"
    report_path.write_text(
        json.dumps(
            {
                "counts": {"collected": 2, "passed": 2},
                "fuzz_case_ids": ["tests/test_policy.py::test_fuzz_boundaries"],
            }
        ),
        encoding="utf-8",
    )
    event_path.write_text(
        '{"path":"tests/test_policy.py","outcome":"passed"}\n',
        encoding="utf-8",
    )
    log_path.write_text("", encoding="utf-8")

    class FinishedProcess:
        @staticmethod
        def poll() -> int:
            return 0

        @staticmethod
        def wait() -> int:
            return 0

    process = runner._FuzzProcess(
        process=cast(Any, FinishedProcess()),
        report_path=report_path,
        event_path=event_path,
        log_path=log_path,
        events=runner._FuzzEventState(),
        selected=("tests/test_policy.py",),
    )
    mutation = runner.MutationActivity(mode="sample", state="complete")

    report, activity, status = runner._finish_fuzz_process(process, mutation, renderer=None)

    assert status == 0
    assert activity.passed_files == frozenset({"tests/test_policy.py"})
    assert activity.schedule_only_files == frozenset()
    assert report["fuzzed_files"] == ["tests/test_policy.py"]


def test_a_skipped_fuzz_contract_is_incomplete_not_passing_or_failed(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    event_path = tmp_path / "events.jsonl"
    log_path = tmp_path / "runner.log"
    report_path.write_text(
        json.dumps(
            {
                "counts": {"collected": 1, "skipped": 1},
                "fuzz_case_ids": ["tests/test_policy.py::test_fuzz_boundaries"],
            }
        ),
        encoding="utf-8",
    )
    event_path.write_text(
        '{"path":"tests/test_policy.py","outcome":"skipped"}\n',
        encoding="utf-8",
    )
    log_path.write_text("", encoding="utf-8")

    class FinishedProcess:
        @staticmethod
        def poll() -> int:
            return 0

        @staticmethod
        def wait() -> int:
            return 0

    process = runner._FuzzProcess(
        process=cast(Any, FinishedProcess()),
        report_path=report_path,
        event_path=event_path,
        log_path=log_path,
        events=runner._FuzzEventState(),
        selected=("tests/test_policy.py",),
    )

    report, activity, status = runner._finish_fuzz_process(
        process,
        runner.MutationActivity(mode="sample", state="complete"),
        renderer=None,
    )

    assert status == 0
    assert activity.passed_files == frozenset()
    assert activity.failed_files == frozenset()
    assert activity.incomplete_files == frozenset({"tests/test_policy.py"})
    assert report["incomplete_files"] == ["tests/test_policy.py"]


def test_fuzz_command_executes_killers_and_explicit_cases_in_each_gold_file(
    tmp_path: Path,
) -> None:
    package = tmp_path / "shop"
    tests = tmp_path / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "policy.py").write_text(
        "def authorize(value):\n"
        "    if value < 0:\n"
        "        raise ValueError('negative')\n"
        "    return value\n",
        encoding="utf-8",
    )
    fuzz_ran = tmp_path / "fuzz-ran"
    ordinary_fuzz_ran = tmp_path / "ordinary-fuzz-ran"
    unrelated_fuzz_ran = tmp_path / "unrelated-fuzz-ran"
    (tests / "test_policy.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "import pytest\n"
        "from shop.policy import authorize\n\n"
        "def test_refuses_negative():\n"
        "    if os.environ.get('WREATH_FUZZ_STAGE'):\n"
        f"        Path({str(ordinary_fuzz_ran)!r}).write_text('ran')\n"
        "    with pytest.raises(ValueError):\n"
        "        authorize(-1)\n\n"
        "@pytest.mark.fuzz\n"
        "def test_fuzz_contract():\n"
        f"    Path({str(fuzz_ran)!r}).write_text('ran')\n\n"
        "def test_unrelated_regression():\n"
        "    if os.environ.get('WREATH_FUZZ_STAGE'):\n"
        f"        Path({str(unrelated_fuzz_ran)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    (tests / "test_slow.py").write_text(
        "import time\ndef test_keeps_the_ordinary_pool_busy():\n    time.sleep(2.0)\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "wreath._cli",
            "fuzz",
            "--workers",
            "2",
            "--mutant-samples",
            "1",
            "--mutant-budget",
            "10",
            "--mutant-path",
            "shop",
            "--mutant-tests",
            "tests",
            "--mutant-operator",
            "guard.remove-raise",
            "--grid",
            "never",
            "--no-history",
            "--report",
            str(report),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert fuzz_ran.read_text(encoding="utf-8") == "ran"
    assert ordinary_fuzz_ran.read_text(encoding="utf-8") == "ran"
    assert not unrelated_fuzz_ran.exists()
    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["mutation"]["counts"]["killed"] == 1, document["mutation"]
    assert document["fuzz"]["counts"]["passed"] == 2
    assert document["fuzz"]["passed_files"] == ["tests/test_policy.py"]
    assert document["fuzz"]["schedule_seeds"] == ["wreath-fuzz-v1"]
    assert document["fuzz"]["fuzzed_files"] == ["tests/test_policy.py"]
    assert document["fuzz"]["live_started"] is True


def test_auto_mutation_workers_reclaim_idle_suite_slots_after_seal() -> None:
    namespace = cli_parser.build_parser().parse_args(["test"])

    arguments = runner._mutation_arguments(namespace)

    assert "--reclaim-workers" in arguments
    assert arguments[arguments.index("--jobs") + 1] == "2"
    assert arguments[arguments.index("--suite-workers") + 1] == str(runner._resolve_workers("auto"))


def test_native_mutation_engine_is_forwarded_to_wreath_mutant() -> None:
    namespace = cli_parser.build_parser().parse_args(
        ["test", "--mutant", "sample", "--mutant-engine", "native"]
    )

    arguments = runner._mutation_arguments(namespace)

    assert arguments[arguments.index("--test-engine") + 1] == "native"


def test_explicit_mutation_worker_limit_remains_literal_after_seal() -> None:
    namespace = cli_parser.build_parser().parse_args(["test", "--mutant-workers", "1"])

    arguments = runner._mutation_arguments(namespace)

    assert arguments[arguments.index("--jobs") + 1] == "1"
    assert "--reclaim-workers" not in arguments


def test_auto_test_workers_use_the_measured_eight_worker_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 12)

    assert runner._resolve_workers("auto") == 8


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
    result = cli.main(
        [
            "test",
            *argument,
            "--grid",
            "never",
            "--no-history",
            "tests/test_response_media_type.py",
        ]
    )

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
            "--engine",
            "pytest",
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


def test_sharded_collection_imports_each_module_once_and_runs_every_test(
    tmp_path: Path,
) -> None:
    tests = tmp_path / "tests"
    imports = tmp_path / "imports"
    tests.mkdir()
    imports.mkdir()
    report_path = tmp_path / "report.json"
    for index in range(8):
        (tests / f"test_shard_{index}.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "\n"
            f"LOG = Path({str(imports / f'{index}.log')!r})\n"
            "with LOG.open('a', encoding='utf-8') as stream:\n"
            "    stream.write(os.environ['PYTEST_XDIST_WORKER'] + '\\n')\n"
            "COMMON = tuple((value, str(value)) for value in range(100))\n"
            "\n"
            "def test_first():\n"
            "    assert COMMON[1] == (1, '1')\n"
            "\n"
            "def test_second():\n"
            "    assert COMMON[99] == (99, '99')\n",
            encoding="utf-8",
        )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "wreath",
            "test",
            "--engine",
            "pytest",
            "--workers",
            "2",
            "--collection",
            "sharded",
            "--grid",
            "never",
            "--no-history",
            "--mutant",
            "off",
            "--report",
            str(report_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["counts"]["collected"] == 16
    assert document["counts"]["passed"] == 16
    import_counts = [
        len(path.read_text(encoding="utf-8").splitlines()) for path in imports.iterdir()
    ]
    assert import_counts == [1] * 8


@pytest.mark.parametrize("engine", ["pytest", "native"])
def test_green_files_still_earn_mutation_confidence_beside_a_red_file(
    tmp_path: Path,
    engine: str,
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
    live_wait = (
        "    deadline = time.monotonic() + 28.0\n"
        f"    while not Path({str(mutant_ran)!r}).exists():\n"
        "        assert time.monotonic() < deadline, 'live mutant never ran'\n"
        "        time.sleep(0.01)\n"
        if engine == "native"
        else ""
    )
    (tests / "test_zbroken.py").write_text(
        "from pathlib import Path\n"
        "import time\n"
        "def test_breaks():\n"
        f"{live_wait}"
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
            "--engine",
            engine,
            "--workers",
            "1",
            "--grid",
            "never",
            "--no-history",
            "--report",
            str(report_path),
            "--mutant-samples",
            "1",
            "--mutant-budget",
            "10",
            "--mutant-path",
            "shop",
            "--mutant-tests",
            "tests",
            "--mutant-operator",
            "guard.remove-raise",
            str(tests),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 1, completed.stderr
    assert "■" in completed.stderr, completed.stderr
    assert "evidence limited to green tests · 1 baseline failure(s) excluded" in (completed.stderr)
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["mutation"]["counts"]["killed"] == 1, document["mutation"]
    live = document["mutation"]["live"]
    if engine == "native":
        assert document["mutation"]["live_kills"] == 1
        assert live["probes"] == 1
        assert live["completed"] == 1
        assert live["killed"] == 1
        assert live["cancelled_at_seal"] == 0
        assert live["first_started_seconds"] > 0
    assert document["mutation"]["verified_test_files"] == ["tests/test_policy.py"]
    assert document["mutation"]["baseline"]["failures"] == ["tests/test_zbroken.py::test_breaks"]
    files = {row["path"]: row["outcome"] for row in document["files"]}
    assert files == {
        "tests/test_zbroken.py": "failed",
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
    assert "Mutation   auto" in completed.stderr
    assert completed.stderr.count("Test activity   current run") == 1
    assert "\x1b[" not in completed.stderr
    document = json.loads(report_path.read_text(encoding="utf-8"))
    mutation = document["mutation"]
    assert mutation["baseline"]["tests"] == 2
    assert mutation["baseline_reused"] is True
    assert sum(mutation["counts"].values()) == 1
    assert mutation["rating"]["label"] == "SAMPLE WATCHED"
    assert mutation["verified_test_files"] == ["tests/test_policy.py"]
    assert "score" not in mutation
