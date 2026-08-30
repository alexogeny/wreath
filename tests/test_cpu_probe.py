from __future__ import annotations

import subprocess

import pytest

from wreath._devtools import cpu_probe


def test_parse_perf_counters_aggregates_hybrid_pmus() -> None:
    stderr = "\n".join(
        (
            "120,,cpu_core/instructions/u,1,100.00,,",
            "30,,cpu_atom/instructions/u,1,100.00,,",
            "80,,cpu_core/cycles/u,1,100.00,,",
            "20,,cpu_atom/cycles/u,1,100.00,,",
            "4,,cpu_core/cache-misses/u,1,100.00,,",
            "<not counted>,,cpu_atom/cache-misses/u,0,0.00,,",
            "3,,cpu_core/branch-misses/u,1,100.00,,",
            "2,,cpu_atom/branch-misses/u,1,100.00,,",
        )
    )

    assert cpu_probe._parse_perf_counters(stderr) == {
        "instructions": 150.0,
        "cycles": 100.0,
        "cache-misses": 4.0,
        "branch-misses": 5.0,
    }


def test_parse_perf_counters_accepts_standard_userspace_events() -> None:
    stderr = "\n".join(
        (
            "120,,instructions:u,1,100.00,,",
            "80,,cycles:u,1,100.00,,",
            "4,,cache-misses:u,1,100.00,,",
            "3,,branch-misses:u,1,100.00,,",
        )
    )

    assert cpu_probe._parse_perf_counters(stderr) == {
        "instructions": 120.0,
        "cycles": 80.0,
        "cache-misses": 4.0,
        "branch-misses": 3.0,
    }


def test_parse_perf_counters_refuses_incomplete_counts() -> None:
    stderr = "\n".join(
        (
            "120,,cpu_core/instructions/u,1,100.00,,",
            "<not counted>,,cpu_atom/instructions/u,0,0.00,,",
        )
    )

    assert cpu_probe._parse_perf_counters(stderr) is None


def test_perf_counters_runs_userspace_hardware_events(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        stderr = "\n".join(
            f"1,,cpu_core/{name}/u,1,100.00,," for name in cpu_probe.COUNTERS
        )
        return subprocess.CompletedProcess(command, 0, "", stderr)

    monkeypatch.setattr(subprocess, "run", run)

    assert cpu_probe.perf_counters(["python", "workload.py"]) == {
        name: 1.0 for name in cpu_probe.COUNTERS
    }
    assert captured[-3:] == ["--", "python", "workload.py"]
