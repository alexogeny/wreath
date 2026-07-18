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
    return calls


def test_a_group_is_installed_without_removing_the_others(recorded: list[list[str]]) -> None:
    # The entire reason these exist: `uv sync --group docs` uninstalls sanic,
    # and `uv sync --group benchmark` uninstalls mkdocs. --inexact does not.
    tasks.ensure_groups("docs")
    assert recorded[0][:3] == ["/usr/bin/uv", "sync", "--inexact"]
    assert "--group=docs" in recorded[0]


def test_docs_installs_the_docs_group_before_building(recorded: list[list[str]]) -> None:
    tasks.docs([])
    assert "--group=docs" in recorded[0]
    assert recorded[1][1:] == ["-m", "mkdocs", "build", "--strict"]


def test_docs_is_always_strict(recorded: list[list[str]]) -> None:
    # A warning that is not an error is a warning nobody reads.
    tasks.docs([])
    assert "--strict" in recorded[1]


def test_docs_can_serve_instead(recorded: list[list[str]]) -> None:
    tasks.docs(["--serve"])
    assert recorded[1][1:] == ["-m", "mkdocs", "serve"]


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
    ]


def test_bench_matrix_only_skips_the_database_battery(recorded: list[list[str]]) -> None:
    tasks.bench(["--pin", "none", "--matrix-only", "--passes", "1"])
    assert not any("podman" in c for c in recorded)
    assert not any("benchmarks.lifecycle" in c for c in recorded)


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
    groups = [c for c in recorded if c[1] == "sync"]
    assert any("--group=docs" in c for c in groups)
    assert any(c[-3:] == ["mkdocs", "build", "--strict"] for c in recorded)


def test_a_missing_uv_is_reported_rather_than_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit, match="uv"):
        tasks.ensure_groups("docs")


def test_a_failed_sync_stops_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    # Running the tool against a half-installed group produces a confusing
    # ImportError instead of the real problem.
    class _Result:
        returncode = 1

    monkeypatch.setattr(tasks.subprocess, "run", lambda *_a, **_k: _Result())
    monkeypatch.setattr(tasks.shutil, "which", lambda _name: "/usr/bin/uv")
    with pytest.raises(SystemExit, match="not installed"):
        tasks.ensure_groups("docs")
