from __future__ import annotations

import json
from pathlib import Path

import pytest

from wreath._devtools import native_profile


def test_profiles_cover_cpu_counters_cache_memory_calls_and_syscalls() -> None:
    assert set(native_profile.PROFILES) == {
        "cpu",
        "counters",
        "cache",
        "memory",
        "calls",
        "syscalls",
    }


def test_cpu_plan_uses_perf_and_keeps_workload_argv(tmp_path: Path) -> None:
    plan = native_profile.build_plan(
        "cpu", ["python", "bench.py", "--count", "10"], tmp_path, tool="/usr/bin/perf"
    )

    assert plan.argv == (
        "/usr/bin/perf",
        "record",
        "--call-graph",
        "dwarf",
        "-o",
        str(tmp_path / "perf.data"),
        "--",
        "python",
        "bench.py",
        "--count",
        "10",
    )
    assert plan.artifact == tmp_path / "perf.data"


@pytest.mark.parametrize(
    ("profile", "tool", "expected"),
    [
        ("counters", "perf", "stat"),
        ("cache", "valgrind", "--tool=cachegrind"),
        ("memory", "valgrind", "--tool=massif"),
        ("calls", "valgrind", "--tool=callgrind"),
        ("syscalls", "strace", "-c"),
    ],
)
def test_each_plan_selects_the_expected_profiler_option(
    profile: str, tool: str, expected: str, tmp_path: Path
) -> None:
    plan = native_profile.build_plan(profile, ["workload"], tmp_path, tool=tool)
    assert expected in plan.argv


def test_dry_run_writes_reproducible_metadata_without_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "profile"
    result = native_profile.main(
        [
            "cpu", "--output", str(output), "--tool", "/usr/bin/perf", "--dry-run",
            "--", "python", "bench.py",
        ]
    )

    assert result == 0
    metadata = json.loads((output / "metadata.json").read_text())
    assert metadata["profile"] == "cpu"
    assert metadata["workload"] == ["python", "bench.py"]
    assert metadata["status"] == "dry-run"
    assert "perf report" in metadata["next_step"]
    assert "/usr/bin/perf record" in capsys.readouterr().out


def test_main_rejects_an_empty_workload(capsys: pytest.CaptureFixture[str]) -> None:
    assert native_profile.main(["cpu", "--dry-run"]) == 2
    assert "workload is required" in capsys.readouterr().err
