"""Task entry points, and the group-eviction they exist to prevent.

`uv sync` removes anything outside the groups it is given. These assert the one
property that makes the tasks worth having: a task installs what it needs
*additively*, so running one never uninstalls another's dependencies.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath._devtools import tasks


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture the commands a task would run, without running them."""
    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        calls.append(command)
        return _Result()

    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    monkeypatch.setattr(tasks.shutil, "which", lambda _name: "/usr/bin/uv")
    # `wreath-bench` refuses to start beside a competing workload, because a
    # number taken next to four other processes is worthless. Nothing here takes
    # a number -- `subprocess.run` is stubbed above, so no benchmark ever runs --
    # and under `pytest -n` the sibling xdist workers are themselves competing
    # workloads, so the guard failed every bench task test in this file whenever
    # the suite was run in parallel. Neutralise it for the fixture that cannot
    # measure anything; `tests/test_bench_quiet.py` covers the guard itself.
    from wreath._devtools import quiet

    monkeypatch.setattr(quiet, "competing_workloads", lambda: [])
    return calls


def test_a_group_is_installed_without_removing_the_others(recorded: list[list[str]]) -> None:
    # The entire reason these exist: `uv sync --group dev` uninstalls sanic,
    # and `uv sync --group benchmark` uninstalls the dev toolchain. --inexact
    # does not.
    tasks.ensure_groups("benchmark")
    assert recorded[0][:3] == ["/usr/bin/uv", "sync", "--inexact"]
    assert "--group=benchmark" in recorded[0]


def test_docs_builds_with_wreaths_own_generator(recorded: list[list[str]]) -> None:
    """No group is installed: the docs toolchain is the framework itself."""
    tasks.docs([])
    assert not [c for c in recorded if c[1] == "sync"]
    assert recorded[0][1:] == ["-m", "wreath", "docs", "check"]


def test_docs_is_always_strict(recorded: list[list[str]]) -> None:
    # A warning that is not an error is a warning nobody reads, so the task
    # runs `check` -- which fails on an orphan page or a dead link -- and never
    # the plain `build`.
    tasks.docs([])
    assert "check" in recorded[0] and "build" not in recorded[0]


def test_docs_can_serve_instead(recorded: list[list[str]]) -> None:
    tasks.docs(["--serve"])
    assert recorded[0][1:] == ["-m", "wreath", "docs", "serve"]


def test_bench_installs_the_benchmark_group(recorded: list[list[str]]) -> None:
    # --pin none so the test never re-pins the CPU affinity of the test runner.
    tasks.bench(["--pin", "none", "--matrix-only"])
    assert "--group=benchmark" in recorded[0]
    assert any(c[1:3] == ["-m", "benchmarks.run"] for c in recorded)


def test_bench_runs_the_requested_number_of_matrix_passes(recorded: list[list[str]]) -> None:
    tasks.bench(["--pin", "none", "--matrix-only", "--passes", "2"])
    matrix = [c for c in recorded if c[1:3] == ["-m", "benchmarks.run"]]
    assert len(matrix) == 2


def test_bench_forwards_unknown_arguments_to_the_matrix(recorded: list[list[str]]) -> None:
    # Owned flags are consumed; everything else still narrows benchmarks.run.
    # (wreath-native is auto-added next to wreath; see the native-arm test.)
    tasks.bench(["--pin", "none", "--matrix-only", "--passes", "1",
                 "--framework", "wreath", "starlette", "--protocol", "h2", "h3"])
    matrix = next(c for c in recorded if c[1:3] == ["-m", "benchmarks.run"])
    assert matrix[3:] == [
        "--framework", "wreath", "starlette", "wreath-native", "--protocol", "h2", "h3",
        # Always stated, never inferred: an arm left on its own defaults takes
        # whatever parallelism its runtime prefers, and the row then compares
        # deployments instead of frameworks.
        "--workers", "1",
    ]


def test_bench_holds_every_arm_to_one_worker_by_default(recorded: list[list[str]]) -> None:
    tasks.bench(["--pin", "none", "--matrix-only", "--passes", "1"])
    matrix = next(c for c in recorded if c[1:3] == ["-m", "benchmarks.run"])
    assert matrix[-2:] == ["--workers", "1"]


def test_bench_multi_gives_every_arm_the_same_worker_count(
    recorded: list[list[str]],
) -> None:
    tasks.bench(["--pin", "none", "--matrix-only", "--passes", "1", "--multi", "4"])
    matrix = next(c for c in recorded if c[1:3] == ["-m", "benchmarks.run"])
    assert matrix[-2:] == ["--workers", "4"]


def test_bench_multi_does_not_overrule_an_explicit_worker_count(
    recorded: list[list[str]],
) -> None:
    # `--multi 4 --workers 2` is a legitimate ask -- four server cores, two
    # workers -- and the harness must not quietly rewrite it.
    tasks.bench(["--pin", "none", "--matrix-only", "--passes", "1",
                 "--multi", "4", "--workers", "2"])
    matrix = next(c for c in recorded if c[1:3] == ["-m", "benchmarks.run"])
    assert "--workers" in matrix
    assert matrix[matrix.index("--workers") + 1] == "2"
    assert matrix.count("--workers") == 1


def test_bench_multi_auto_leaves_the_generator_more_cores_than_the_server(
    recorded: list[list[str]],
) -> None:
    """The generator must outrun what it measures, so it gets two cores per one.

    Measured: one h2load thread saturates near 133k req/s and one metal worker
    serves near 120k, so an even split stands the generator up at parity with
    the server -- the case where a plateau reads as the server's ceiling when it
    is really the client's.
    """
    from wreath._devtools.quiet import physical_cores

    cores = len(physical_cores())
    if cores < 6:
        pytest.skip(f"needs >= 6 physical cores to have headroom, found {cores}")
    server = tasks._resolve_multi("auto")
    assert cores - server >= 2 * server


def test_bench_multi_rejects_a_nonsense_core_count(
    recorded: list[list[str]],
) -> None:
    for bad in ("0", "-1", "many"):
        with pytest.raises(SystemExit):
            tasks.bench(["--pin", "none", "--matrix-only", "--multi", bad])


def test_bench_matrix_only_skips_the_database_battery(
    recorded: list[list[str]], capsys: pytest.CaptureFixture[str],
) -> None:
    tasks.bench(["--pin", "none", "--matrix-only", "--passes", "1"])
    assert not any("podman" in c for c in recorded)
    assert not any("benchmarks.lifecycle" in c for c in recorded)
    assert "combined report written" not in capsys.readouterr().out


def test_bench_combines_a_matrix_result(
    recorded: list[list[str]], monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    results = tmp_path / "benchmark-results"
    results.mkdir()
    matrix = results / "2026-08-03T000000Z.json"

    def run(command: list[str]) -> int:
        if command[1:3] == ["-m", "benchmarks.run"]:
            matrix.write_text("{}", encoding="utf-8")
        return 0

    report_args: list[str] = []
    from wreath._devtools import bench_report

    monkeypatch.setattr(tasks, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(tasks, "_run", run)
    monkeypatch.setattr(bench_report, "main", report_args.extend)

    tasks.bench(["--pin", "none", "--matrix-only", "--passes", "1"])

    assert str(matrix) in report_args
    assert report_args[-2:] == ["-o", str(results / "full-battery.html")]


def test_bench_adds_the_native_arm_when_wreath_is_requested(recorded: list[list[str]]) -> None:
    # wreath's own HTTP is the wreath-native arm; asking for `wreath` must not
    # silently measure only the uvicorn/httptools arm.
    tasks.bench(["--pin", "none", "--matrix-only", "--passes", "1",
                 "--framework", "wreath", "starlette"])
    matrix = next(c for c in recorded if c[1:3] == ["-m", "benchmarks.run"])
    assert "wreath-native" in matrix
    assert "wreath" in matrix and "starlette" in matrix


def test_bench_leaves_an_explicit_native_only_selection_alone(recorded: list[list[str]]) -> None:
    tasks.bench(["--pin", "none", "--matrix-only", "--passes", "1",
                 "--framework", "wreath-native"])
    matrix = next(c for c in recorded if c[1:3] == ["-m", "benchmarks.run"])
    assert matrix.count("wreath-native") == 1
    assert "wreath" not in matrix[matrix.index("--framework"):]


def test_ensure_native_arm_is_a_noop_without_a_framework_flag() -> None:
    # No --framework means benchmarks.run's default (which already runs both).
    assert tasks._ensure_native_arm(["--protocol", "h2"]) == ["--protocol", "h2"]


def test_check_runs_every_gate_even_after_one_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Knowing three things broke is worth more than the seconds saved by
    # stopping at the first.
    seen: list[str] = []

    class _Result:
        def __init__(self, code: int) -> None:
            self.returncode = code

    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        if command[1] == "sync":
            return _Result(0)
        seen.append(command[-1])
        return _Result(1)  # everything fails

    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    monkeypatch.setattr(tasks.shutil, "which", lambda _name: "/usr/bin/uv")
    assert tasks.check([]) == 1
    assert len(seen) == len(tasks._CHECKS)


def test_check_passes_when_every_gate_passes(recorded: list[list[str]]) -> None:
    assert tasks.check([]) == 0


def test_check_can_add_the_docs_build(recorded: list[list[str]]) -> None:
    tasks.check(["--docs"])
    assert any(c[-3:] == ["wreath", "docs", "check"] for c in recorded)


def test_a_missing_uv_is_reported_rather_than_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit, match="uv"):
        tasks.ensure_groups("benchmark")


def test_a_failed_sync_stops_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    # Running the tool against a half-installed group produces a confusing
    # ImportError instead of the real problem.
    class _Result:
        returncode = 1

    monkeypatch.setattr(tasks.subprocess, "run", lambda *_a, **_k: _Result())
    monkeypatch.setattr(tasks.shutil, "which", lambda _name: "/usr/bin/uv")
    with pytest.raises(SystemExit, match="not installed"):
        tasks.ensure_groups("docs")


def test_the_check_suite_goes_through_the_runner_not_bare_pytest() -> None:
    """The gate and `wreath test` schedule the suite the same way.

    `HistoricalSchedulerPlugin` is installed by the runner, not by the `pytest11`
    entry point, so a gate that shelled `pytest -n 6` ran with no historical
    scheduling *and* a second, lower worker cap. Measured at equal workers it
    was 1.10x slower with a fifteenfold wider spread.

    Pinned because the failure is invisible: a raw-pytest gate is perfectly
    green, just slower and noisier than the command it is meant to mirror.
    """
    command = tasks._pytest_command()

    assert command[1:4] == ["-m", "wreath.cli", "test"], command
    assert "--mutant" in command and command[command.index("--mutant") + 1] == "off"
    assert "--grid" in command and command[command.index("--grid") + 1] == "never"
    # No `-n`: the runner owns the worker count, so the curve lives in one place.
    assert "-n" not in command and "--numprocesses" not in command
    assert not hasattr(tasks, "_PYTEST_MAX_WORKERS"), (
        "a second worker cap here is how the two paths drifted apart before"
    )


def test_the_check_suite_is_the_pytest_gate() -> None:
    """The command above is the one `wreath-check` actually runs."""
    gates = dict(tasks._CHECKS)

    assert gates["pytest"] == tasks._pytest_command()
