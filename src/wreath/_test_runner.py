"""Pytest-compatible test activity, timing, and static state reporting.

``wreath test`` uses the native semantic engine by default and retains pytest
as an explicit oracle. This module owns the shared run model, duration
statistics, one static final report, bounded history and mutation orchestration.

The activity plugin is activated through :mod:`wreath._pytest_plugin` only when
the command places a controller PID and configuration in the environment.
Ordinary ``pytest`` imports this module never, and nested pytest subprocesses do
not inherit runner reporting merely because their parent is itself running a
test.
"""

from __future__ import annotations

import ast
import collections
import hashlib
import importlib.util
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

_CONFIG_ENV = "WREATH_TEST_ACTIVITY_CONFIG"
_CONTROLLER_PID_ENV = "WREATH_TEST_ACTIVITY_CONTROLLER_PID"
_MUTATION_TRACE_ENV = "WREATH_TEST_MUTATION_TRACE"
_PLUGIN_NAME = "wreath-test-activity"
_HISTORY_RUNS = 20
_MAX_HISTORY_FILES = 50_000
_MAX_HISTORY_TESTS = 200_000

#: Weighting window for the next-run duration estimate; the sample count remains
#: unbounded for reporting.
_MEAN_WINDOW = 20
_DEFAULT_HISTORY = ".wreath/test-history.json"
_MAX_AUTO_WORKERS = 8
_MAX_AUTO_MUTANT_WORKERS = 3
_MIN_SHARD_MODULES_PER_WORKER = 4
_SHARD_HISTORY_COVERAGE = 0.8
_LIVE_FUZZ_GOLD_RATIO = 0.05
_FUZZ_SCHEDULE_SEED = "wreath-fuzz-v1"


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """Options consumed by Wreath rather than forwarded to pytest."""

    grid: str = "never"
    workers: str = "auto"
    slowest: int = 5
    report: str | None = None
    history: str | None = _DEFAULT_HISTORY
    mutation_mode: str = "off"
    mutation_samples: int = 0
    mutation_activity: str | None = None
    stage_events: str | None = None
    collection_shards: tuple[tuple[str, int], ...] = ()

    def as_json(self) -> str:
        return json.dumps(
            {
                "grid": self.grid,
                "workers": self.workers,
                "slowest": self.slowest,
                "report": self.report,
                "history": self.history,
                "mutation_mode": self.mutation_mode,
                "mutation_samples": self.mutation_samples,
                "mutation_activity": self.mutation_activity,
                "stage_events": self.stage_events,
                "collection_shards": self.collection_shards,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> RunnerConfig:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("test activity configuration must be an object")
        grid = value.get("grid", "never")
        workers = value.get("workers", "auto")
        slowest = value.get("slowest", 5)
        report = value.get("report")
        history = value.get("history", _DEFAULT_HISTORY)
        mutation_mode = value.get("mutation_mode", "off")
        mutation_samples = value.get("mutation_samples", 0)
        mutation_activity = value.get("mutation_activity")
        stage_events = value.get("stage_events")
        collection_shards = value.get("collection_shards", ())
        if grid != "never":
            raise ValueError(f"unknown test grid mode {grid!r}; the static form is 'never'")
        if not isinstance(workers, str):
            raise ValueError("test worker count must be text")
        if not isinstance(slowest, int) or slowest < 0:
            raise ValueError("slowest count must be a non-negative integer")
        if report is not None and not isinstance(report, str):
            raise ValueError("test report path must be text")
        if history is not None and not isinstance(history, str):
            raise ValueError("test history path must be text")
        if mutation_mode not in {"auto", "off", "sample", "changed", "full"}:
            raise ValueError(f"unknown mutation confidence mode {mutation_mode!r}")
        if not isinstance(mutation_samples, int) or mutation_samples < 0:
            raise ValueError("mutation sample count must be a non-negative integer")
        if mutation_activity is not None and not isinstance(mutation_activity, str):
            raise ValueError("mutation activity path must be text")
        if stage_events is not None and not isinstance(stage_events, str):
            raise ValueError("test stage event path must be text")
        if not isinstance(collection_shards, list | tuple):
            raise ValueError("test collection shards must be a sequence")
        parsed_shards: list[tuple[str, int]] = []
        for shard in collection_shards:
            if (
                not isinstance(shard, list | tuple)
                or len(shard) != 2
                or not isinstance(shard[0], str)
                or not isinstance(shard[1], int)
                or shard[1] < 0
            ):
                raise ValueError(
                    "each test collection shard must be a path and non-negative worker"
                )
            parsed_shards.append((shard[0], shard[1]))
        return cls(
            grid=grid,
            workers=workers,
            slowest=slowest,
            report=report,
            history=history,
            mutation_mode=mutation_mode,
            mutation_samples=mutation_samples,
            mutation_activity=mutation_activity,
            stage_events=stage_events,
            collection_shards=tuple(parsed_shards),
        )


@dataclass(slots=True)
class TestState:
    nodeid: str
    path: str
    duration: float = 0.0
    started: bool = False
    finished: bool = False
    call_passed: bool = False
    skipped: bool = False
    failed: bool = False
    error: bool = False

    @property
    def outcome(self) -> str:
        if self.error:
            return "error"
        if self.failed:
            return "failed"
        if self.skipped:
            return "skipped"
        if self.finished and self.call_passed:
            return "passed"
        if self.started:
            return "running"
        return "untested"


@dataclass(slots=True)
class FileState:
    path: str
    nodeids: set[str] = field(default_factory=set)
    duration: float = 0.0
    started: int = 0
    finished: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0

    @property
    def outcome(self) -> str:
        if self.errors:
            return "error"
        if self.failed:
            return "failed"
        if self.started > self.finished:
            return "running"
        if self.finished == 0:
            return "untested"
        if self.skipped and (self.passed or self.finished < len(self.nodeids)):
            return "mixed"
        if self.skipped:
            return "skipped"
        return "passed"


@dataclass(frozen=True, slots=True)
class MutationActivity:
    """Mutation state rendered as part of the test activity report."""

    mode: str
    state: str
    total: int = 0
    rating_label: str = ""
    rating_action: str = ""
    rating_tone: str = "neutral"
    counts: dict[str, int] = field(default_factory=dict)
    mutating_files: frozenset[str] = frozenset()
    verified_files: frozenset[str] = frozenset()
    failed_mutation_files: frozenset[str] = frozenset()
    baseline_failures: int = 0
    live_probes: int = 0
    live_completed: int = 0
    live_cancelled_at_seal: int = 0
    live_first_started_seconds: float | None = None
    test_workers: int = 0
    mutant_workers: int = 0


@dataclass(frozen=True, slots=True)
class FuzzActivity:
    """Per-file evidence from the fuzz pass after mutation verification."""

    state: str
    selected_files: frozenset[str] = frozenset()
    active_files: frozenset[str] = frozenset()
    passed_files: frozenset[str] = frozenset()
    failed_files: frozenset[str] = frozenset()
    incomplete_files: frozenset[str] = frozenset()
    schedule_only_files: frozenset[str] = frozenset()
    counts: dict[str, int] = field(default_factory=dict)


def _path_from_nodeid(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _duration_summary(tests: list[TestState]) -> dict[str, float | int]:
    durations = [test.duration for test in tests if test.finished]
    if not durations:
        return {
            "total_seconds": 0.0,
            "mean_seconds": 0.0,
            "median_seconds": 0.0,
            "p95_seconds": 0.0,
            "p99_seconds": 0.0,
            "outlier_threshold_seconds": 0.0,
            "outliers": 0,
            "over_100ms": 0,
            "over_250ms": 0,
            "over_1s": 0,
        }
    q1 = _percentile(durations, 0.25)
    q3 = _percentile(durations, 0.75)
    threshold = q3 + 1.5 * (q3 - q1)
    return {
        "total_seconds": sum(durations),
        "mean_seconds": statistics.fmean(durations),
        "median_seconds": statistics.median(durations),
        "p95_seconds": _percentile(durations, 0.95),
        "p99_seconds": _percentile(durations, 0.99),
        "outlier_threshold_seconds": threshold,
        "outliers": sum(duration > threshold for duration in durations),
        "over_100ms": sum(duration >= 0.1 for duration in durations),
        "over_250ms": sum(duration >= 0.25 for duration in durations),
        "over_1s": sum(duration >= 1.0 for duration in durations),
    }


def _format_duration(seconds: float) -> str:
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f}us"
    if seconds < 1.0:
        return f"{seconds * 1000:.1f}ms"
    return f"{seconds:.2f}s"


class RunActivity:
    """Controller-owned aggregation of pytest lifecycle reports."""

    def __init__(self, *, workers: int) -> None:
        self.workers = workers
        self.files: dict[str, FileState] = {}
        self.tests: dict[str, TestState] = {}
        self.started_at = datetime.now(UTC)
        self.started = time.perf_counter()
        self.finished_at: datetime | None = None
        self.wall_seconds = 0.0
        self.exitstatus = 0

    def collect(self, nodeids: list[str] | tuple[str, ...]) -> None:
        for nodeid in nodeids:
            path = _path_from_nodeid(nodeid)
            file_state = self.files.setdefault(path, FileState(path))
            file_state.nodeids.add(nodeid)
            self.tests.setdefault(nodeid, TestState(nodeid, path))

    def start_test(self, nodeid: str) -> None:
        test = self._test(nodeid)
        if test.started:
            return
        test.started = True
        self.files[test.path].started += 1

    def start_native_tests(self, nodeids: Iterable[str]) -> None:
        """Mark one native worker shard running without per-item method dispatch."""
        for nodeid in nodeids:
            test = self._test(nodeid)
            if test.started:
                continue
            test.started = True
            file_state = self.files[test.path]
            file_state.started = file_state.started + 1

    def add_report(self, report: Any) -> None:
        nodeid = str(report.nodeid)
        test = self._test(nodeid)
        test.duration += max(0.0, float(getattr(report, "duration", 0.0)))
        outcome = getattr(report, "outcome", "")
        when = getattr(report, "when", "")
        if outcome == "failed":
            if when == "call":
                test.failed = True
            else:
                test.error = True
        elif outcome == "skipped":
            test.skipped = True
        elif outcome == "passed" and when == "call":
            test.call_passed = True

    def add_native_result(self, nodeid: str, outcome: str, duration: float) -> None:
        """Ingest one terminal native result without emulating pytest phases."""
        if outcome not in {"passed", "failed", "skipped"}:
            raise ValueError(
                f"native result {nodeid!r} has outcome {outcome!r}; "
                "expected 'passed', 'failed', or 'skipped'"
            )
        test = self._test(nodeid)
        test.duration += max(0.0, duration)
        if outcome == "failed":
            test.failed = True
        elif outcome == "skipped":
            test.skipped = True
        else:
            test.call_passed = True
        if test.finished:
            return
        if not test.started:
            test.started = True
            self.files[test.path].started += 1
        test.finished = True
        file_state = self.files[test.path]
        file_state.finished += 1
        file_state.duration += test.duration
        if test.failed:
            file_state.failed += 1
        elif test.skipped:
            file_state.skipped += 1
        else:
            file_state.passed += 1

    def reconcile_native_duration(self, nodeid: str, duration: float) -> None:
        """Replace a streamed UI duration with the C clock's final duration."""
        test = self._test(nodeid)
        exact = max(0.0, duration)
        if not test.finished:
            return
        file_state = self.files[test.path]
        file_state.duration += exact - test.duration
        test.duration = exact

    def finish_test(self, nodeid: str) -> None:
        test = self._test(nodeid)
        if test.finished:
            return
        if not test.started:
            self.start_test(nodeid)
        test.finished = True
        file_state = self.files[test.path]
        file_state.finished += 1
        file_state.duration += test.duration
        if test.error:
            file_state.errors += 1
        elif test.failed:
            file_state.failed += 1
        elif test.skipped:
            file_state.skipped += 1
        else:
            # A successful item normally has a passing call report. Some custom
            # collectors do not use pytest's setup/call/teardown protocol, and a
            # clean logfinish is still their successful terminal event.
            test.call_passed = True
            file_state.passed += 1

    def collection_error(self, nodeid: str, duration: float = 0.0) -> None:
        test = self._test(nodeid)
        test.started = True
        test.error = True
        test.duration += max(0.0, duration)
        self.finish_test(nodeid)

    def finish(self, exitstatus: int) -> None:
        self.exitstatus = exitstatus
        self.finished_at = datetime.now(UTC)
        self.wall_seconds = max(0.0, time.perf_counter() - self.started)

    def _test(self, nodeid: str) -> TestState:
        test = self.tests.get(nodeid)
        if test is not None:
            return test
        path = _path_from_nodeid(nodeid)
        self.files.setdefault(path, FileState(path)).nodeids.add(nodeid)
        test = TestState(nodeid, path)
        self.tests[nodeid] = test
        return test

    def counts(self) -> dict[str, int]:
        counts = {
            "collected": len(self.tests),
            "finished": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        }
        for test in self.tests.values():
            if test.finished:
                counts["finished"] += 1
            outcome = test.outcome
            if outcome == "passed":
                counts["passed"] += 1
            elif outcome == "failed":
                counts["failed"] += 1
            elif outcome == "error":
                counts["errors"] += 1
            elif outcome == "skipped":
                counts["skipped"] += 1
        return counts

    def report(self, *, slowest: int) -> dict[str, Any]:
        tests = sorted(self.tests.values(), key=lambda test: (-test.duration, test.nodeid))
        files = sorted(self.files.values(), key=lambda file_state: file_state.path)
        durations = _duration_summary(tests)
        threshold = float(durations["outlier_threshold_seconds"])
        outliers = [test for test in tests if test.finished and test.duration > threshold]
        utilization = 0.0
        if self.wall_seconds > 0.0 and self.workers > 0:
            utilization = float(durations["total_seconds"]) / (self.wall_seconds * self.workers)
        return {
            "version": 1,
            "kind": "wreath-test-run",
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "exitstatus": self.exitstatus,
            "workers": self.workers,
            "wall_seconds": self.wall_seconds,
            "worker_utilization": utilization,
            "counts": {**self.counts(), "files": len(files)},
            "durations": durations,
            "outliers": [{"nodeid": test.nodeid, "seconds": test.duration} for test in outliers],
            "slowest": [
                {"nodeid": test.nodeid, "seconds": test.duration, "outcome": test.outcome}
                for test in tests[:slowest]
                if test.finished
            ],
            "files": [
                {
                    "path": file_state.path,
                    "outcome": file_state.outcome,
                    "seconds": file_state.duration,
                    "tests": len(file_state.nodeids),
                    "finished": file_state.finished,
                    "passed": file_state.passed,
                    "failed": file_state.failed,
                    "errors": file_state.errors,
                    "skipped": file_state.skipped,
                }
                for file_state in files
            ],
            "tests": [
                {
                    "nodeid": test.nodeid,
                    "path": test.path,
                    "outcome": test.outcome,
                    "seconds": test.duration,
                }
                for test in sorted(self.tests.values(), key=lambda test: test.nodeid)
            ],
        }


_SYMBOLS = {
    "untested": "■",
    "running": "■",
    "passed": "■",
    "mixed": "■",
    "skipped": "■",
    "failed": "×",
    "error": "×",
    "mutating": "■",
    "verified": "■",
    "mutation_failed": "×",
    "fuzzing": "■",
    "fuzz_failed": "×",
    "complete": "★",
}

_GROUP_LABELS = {
    "complete": "Complete",
    "fuzzing": "Fuzzing",
    "mutant_pass": "Mutant pass",
    "mutating": "Mutating",
    "mutation_miss": "Mutation miss",
    "test_pass": "Test pass",
    "testing": "Testing",
    "queued": "Queued",
    "skipped": "Skipped/mixed",
    "failed": "Failed",
}

_GROUP_BUCKET = {
    "complete": "complete",
    "fuzzing": "fuzzing",
    "fuzz_failed": "mutant_pass",
    "verified": "mutant_pass",
    "mutating": "mutating",
    "mutation_failed": "mutation_miss",
    "passed": "test_pass",
    "running": "testing",
    "untested": "queued",
    "mixed": "skipped",
    "skipped": "skipped",
    "failed": "failed",
    "error": "failed",
}
_GROUP_ORDER = {name: index for index, name in enumerate(_GROUP_LABELS)}


def _tile(
    file_state: FileState,
    *,
    mutation: MutationActivity | None = None,
    fuzz: FuzzActivity | None = None,
) -> str:
    outcome = _tile_outcome(file_state, mutation=mutation, fuzz=fuzz)
    return _SYMBOLS[outcome]


def _tile_outcome(
    file_state: FileState,
    *,
    mutation: MutationActivity | None,
    fuzz: FuzzActivity | None,
) -> str:
    outcome = file_state.outcome
    if outcome != "passed":
        return outcome
    path = file_state.path
    if fuzz is not None:
        if path in fuzz.failed_files:
            return "fuzz_failed"
        if path in fuzz.passed_files:
            return "complete"
        if path in fuzz.active_files:
            return "fuzzing"
    if mutation is not None:
        if path in mutation.failed_mutation_files:
            return "mutation_failed"
        if path in mutation.mutating_files:
            return "mutating"
        if path in mutation.verified_files:
            return "verified"
        if mutation.state in {"complete", "error", "unrated"}:
            # Green is an intermediate test result once mutation is enabled.
            # A terminal file either supplied positive mutation evidence or it
            # visibly missed the stage; leaving it green claims unfinished work
            # is a final outcome.
            return "mutation_failed"
    return outcome


def _legend_tile(outcome: str) -> str:
    return _SYMBOLS[outcome]


def _state_legend_lines(*, width: int) -> list[str]:
    states = (
        ("untested", "queued"),
        ("running", "running"),
        ("passed", "pass"),
        ("mutating", "mutation testing"),
        ("verified", "mutation passed"),
        ("mutation_failed", "mutation failed"),
        ("fuzzing", "fuzzing"),
        ("fuzz_failed", "fuzz failed"),
        ("complete", "all stages passed"),
        ("mixed", "skip/mixed"),
        ("failed", "fail/error"),
    )
    prefix = "  State      "
    continuation = "             "
    maximum = max(29, width - 1)
    lines: list[str] = []
    current = prefix
    visible = len(prefix)
    populated = False
    for outcome, label in states:
        rendered = f"{_legend_tile(outcome)} {label}"
        item_width = 2 + len(label)
        separator = " · " if populated else ""
        separator_width = 3 if populated else 0
        if populated and visible + separator_width + item_width > maximum:
            lines.append(current)
            current = continuation + rendered
            visible = len(continuation) + item_width
            continue
        current += separator + rendered
        visible += separator_width + item_width
        populated = True
    lines.append(current)
    return lines


def _rating_text(label: str, tone: str) -> str:
    symbols = {
        "good": "■",
        "attention": "×",
        "warning": "■",
        "incomplete": "■",
        "neutral": "■",
    }
    return f"{symbols[tone]} {label}"


def _mutation_lines(
    mutation: MutationActivity,
    *,
    passing_files: int = 0,
) -> list[str]:
    mutation_tile = _legend_tile("mutating")
    if mutation.state == "running":
        scope = f" · {mutation.total} sampled controls" if mutation.total else ""
        allocation = (
            f" · workers {mutation.test_workers} test / {mutation.mutant_workers} mutant"
            if mutation.test_workers or mutation.mutant_workers
            else ""
        )
        action = (
            "testing controls" if mutation.mutating_files else "preparing controls beside tests"
        )
        return [f"  Mutation   {mutation_tile} {mutation.mode}{scope} · {action}{allocation}"]
    if mutation.state == "unrated":
        return [f"  Mutation   {mutation.mode} · no eligible declared controls"]
    if mutation.state == "error":
        return [f"  Mutation   {mutation.mode} · confidence phase failed"]
    if mutation.state == "no_green":
        return [f"  Mutation   {mutation.mode} · not measured; no tests passed"]
    rating = _rating_text(
        mutation.rating_label,
        mutation.rating_tone,
    )
    counts = mutation.counts
    killed = counts.get("killed", 0)
    survived = counts.get("survived", 0)
    unreached = counts.get("unreached", 0)
    undecided = counts.get("timeout", 0) + counts.get("error", 0)
    equivalent = counts.get("equivalent", 0)
    verified_tile = _legend_tile("verified")
    verified_count = len(mutation.verified_files)
    verified_label = "file" if verified_count == 1 else "files"
    without_evidence = max(0, passing_files - verified_count)
    without_evidence_text = (
        f" · {without_evidence} without mutation evidence" if without_evidence else ""
    )
    lines = [
        f"  Mutation   {mutation.mode} · {rating} · {mutation.rating_action}",
        (
            f"  {verified_tile} {verified_count} gold test {verified_label}"
            f"{without_evidence_text} · "
            f"{killed} killed · {survived} survived · {unreached} unreached · "
            f"{undecided} undecided/declined · {equivalent} equivalent"
        ),
    ]
    if mutation.live_probes:
        first = (
            f" · first at {mutation.live_first_started_seconds:.2f}s"
            if mutation.live_first_started_seconds is not None
            else ""
        )
        cancelled = (
            f" · {mutation.live_cancelled_at_seal} stopped at seal"
            if mutation.live_cancelled_at_seal
            else ""
        )
        lines.append(
            f"  live overlap · {mutation.live_probes} started · "
            f"{mutation.live_completed} completed before seal{first}{cancelled}"
        )
    if mutation.baseline_failures:
        lines.append(
            f"  evidence limited to green tests · {mutation.baseline_failures} "
            "baseline failure(s) excluded"
        )
    return lines


def _fuzz_lines(fuzz: FuzzActivity) -> list[str]:
    if fuzz.state == "running":
        return [
            f"  Fuzz       {_legend_tile('fuzzing')} "
            f"{len(fuzz.active_files)} active · "
            f"{len(fuzz.passed_files)} complete"
        ]
    if fuzz.state == "no_gold":
        return ["  Fuzz       not run · no mutation-passing test files"]
    if fuzz.state == "no_tests":
        return ["  Fuzz       no runnable tests in mutation-passing files"]
    passed = len(fuzz.passed_files)
    failed = len(fuzz.failed_files)
    incomplete = len(fuzz.incomplete_files)
    schedule_only = len(fuzz.schedule_only_files)
    incomplete_text = f" · {incomplete} incomplete" if incomplete else ""
    schedule_text = f" · {schedule_only} schedule-fuzzed" if schedule_only else ""
    return [
        f"  Fuzz       {_legend_tile('complete')} "
        f"{passed} files passed · {failed} failed"
        f"{incomplete_text}{schedule_text}"
    ]


def render_activity(
    activity: RunActivity,
    *,
    width: int,
    height: int,
    slowest: int,
    mutation: MutationActivity | None = None,
    fuzz: FuzzActivity | None = None,
) -> str:
    """Render one stable snapshot of the current run."""
    counts = activity.counts()
    elapsed = activity.wall_seconds or max(0.0, time.perf_counter() - activity.started)
    file_states = sorted(activity.files.values(), key=lambda file_state: file_state.path)
    lines = [
        "Test activity   current run",
        (
            f"{counts['collected']:,} tests · {len(file_states):,} files · "
            f"{_format_duration(elapsed)} · {counts['passed']:,} pass · "
            f"{counts['failed'] + counts['errors']:,} fail/error · "
            f"{counts['skipped']:,} skipped"
        ),
        "",
    ]
    if file_states:
        grouped: dict[str, list[FileState]] = {}
        for file_state in file_states:
            outcome = _tile_outcome(file_state, mutation=mutation, fuzz=fuzz)
            grouped.setdefault(_GROUP_BUCKET[outcome], []).append(file_state)
        columns = max(1, min(53, (max(30, width) - 18) // 2))
        ordered = sorted(grouped, key=lambda name: _GROUP_ORDER[name])
        for group_name in ordered:
            group = grouped[group_name]
            for start in range(0, len(group), columns):
                label = _GROUP_LABELS[group_name] if start == 0 else ""
                row = group[start : start + columns]
                lines.append(
                    f"  {label:<13} "
                    + " ".join(
                        _tile(
                            file_state,
                            mutation=mutation,
                            fuzz=fuzz,
                        )
                        for file_state in row
                    )
                )
    else:
        lines.append("  collecting tests …")

    lines.append("")
    lines.extend(_state_legend_lines(width=width))
    if mutation is not None:
        passing_files = sum(file_state.outcome == "passed" for file_state in file_states)
        lines.extend(
            _mutation_lines(
                mutation,
                passing_files=passing_files,
            )
        )
    if fuzz is not None:
        lines.extend(_fuzz_lines(fuzz))
    finished = [test for test in activity.tests.values() if test.finished]
    failures = sorted(
        (test for test in finished if test.outcome in {"failed", "error"}),
        key=lambda test: test.nodeid,
    )
    if failures:
        lines.append("")
        lines.append("  Failures")
        visible_failures = failures[:20]
        for test in visible_failures:
            available = max(10, width - 6)
            nodeid = test.nodeid
            if len(nodeid) > available:
                nodeid = "…" + nodeid[-(available - 1) :]
            lines.append(f"  × {nodeid}")
        hidden = len(failures) - len(visible_failures)
        if hidden:
            lines.append(f"  … {hidden} more failure(s) in the JSON report")
    durations = _duration_summary(finished)
    if finished:
        lines.append(
            "  "
            f"average {_format_duration(float(durations['mean_seconds']))} · "
            f"median {_format_duration(float(durations['median_seconds']))} · "
            f"p95 {_format_duration(float(durations['p95_seconds']))} · "
            f"p99 {_format_duration(float(durations['p99_seconds']))} · "
            f"test time {_format_duration(float(durations['total_seconds']))}"
        )
        lines.append(
            "  slow tail   "
            f"{durations['over_100ms']} >=100ms · "
            f"{durations['over_250ms']} >=250ms · "
            f"{durations['over_1s']} >=1s · "
            f"Tukey {durations['outliers']} "
            f">{_format_duration(float(durations['outlier_threshold_seconds']))}"
        )
        if activity.wall_seconds and activity.workers:
            utilization = float(durations["total_seconds"]) / (
                activity.wall_seconds * activity.workers
            )
            lines.append(f"  worker utilization {utilization * 100:.1f}%")
        if slowest:
            lines.append("")
            lines.append("  Slowest tests")
            ordered = sorted(finished, key=lambda test: (-test.duration, test.nodeid))
            for test in ordered[:slowest]:
                available = max(10, width - 16)
                nodeid = test.nodeid
                if len(nodeid) > available:
                    nodeid = "…" + nodeid[-(available - 1) :]
                lines.append(f"  {_format_duration(test.duration):>9}  {nodeid}")
    return "\n".join(lines) + "\n"


class ActivityRenderer:
    """Zero-work progress sink that prints one uncoloured final snapshot."""

    def __init__(
        self,
        activity: RunActivity,
        *,
        stream: TextIO,
        mode: str,
        slowest: int,
    ) -> None:
        self.activity = activity
        self.stream = stream
        self.slowest = slowest
        if mode != "never":
            raise ValueError(f"unknown test grid mode {mode!r}; the static form is 'never'")
        self.disabled = False
        self.mutation: MutationActivity | None = None
        self.fuzz: FuzzActivity | None = None

    def finish(self) -> None:
        self.finish_with_mutation(None)

    def defer(self) -> None:
        """Wait for mutation before printing the single final state."""
        return

    def mutation_progress(
        self,
        mode: str,
        total: int,
        *,
        mutating_files: frozenset[str],
        verified_files: frozenset[str],
        failed_mutation_files: frozenset[str],
        test_workers: int = 0,
        mutant_workers: int = 0,
    ) -> None:
        """Retain mutation state for the final report without repainting."""
        if self.disabled:
            return
        self.mutation = MutationActivity(
            mode=mode,
            state="running",
            total=total,
            mutating_files=mutating_files,
            verified_files=verified_files,
            failed_mutation_files=failed_mutation_files,
            test_workers=test_workers,
            mutant_workers=mutant_workers,
        )

    def finish_mutation_progress(self) -> None:
        return

    def finish_with_mutation(self, mutation: MutationActivity | None) -> None:
        self.finish_pipeline(mutation, None)

    def fuzz_progress(
        self,
        mutation: MutationActivity,
        fuzz: FuzzActivity,
    ) -> None:
        self.mutation = mutation
        self.fuzz = fuzz

    def finish_pipeline(
        self,
        mutation: MutationActivity | None,
        fuzz: FuzzActivity | None,
    ) -> None:
        self.mutation = mutation
        self.fuzz = fuzz
        size = shutil.get_terminal_size((100, 30))
        snapshot = render_activity(
            self.activity,
            width=size.columns,
            height=size.lines,
            slowest=self.slowest,
            mutation=mutation,
            fuzz=fuzz,
        )
        # pytest's progress line deliberately has no newline until its terminal
        # summary. Session-finish hooks run before that summary, so start our
        # static report on a fresh line instead of attaching it to ``[100%]``.
        self._write("\n" + snapshot)

    def restore(self) -> None:
        return

    def _write(self, value: str) -> bool:
        try:
            self.stream.write(value)
            self.stream.flush()
        except OSError, ValueError:
            self.disabled = True
            return False
        return True


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _append_stage_event(
    path: Path,
    file_state: FileState,
    *,
    outcome: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {"path": file_state.path, "outcome": outcome or file_state.outcome},
                separators=(",", ":"),
            )
            + "\n"
        )


def _update_history(path: Path, report: dict[str, Any]) -> None:
    history: dict[str, Any] = {"version": 1, "runs": [], "files": {}, "tests": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and loaded.get("version") == 1:
            history = loaded
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        print(f"wreath test: ignoring unreadable history {path}: {error}", file=sys.stderr)

    runs = history.get("runs")
    if not isinstance(runs, list):
        runs = []
    runs.append(
        {
            "finished_at": report["finished_at"],
            "exitstatus": report["exitstatus"],
            "wall_seconds": report["wall_seconds"],
            "workers": report["workers"],
            "counts": report["counts"],
        }
    )
    history["runs"] = runs[-_HISTORY_RUNS:]

    old_files = history.get("files")
    files: dict[str, Any] = old_files if isinstance(old_files, dict) else {}
    for row in report["files"]:
        old = files.get(row["path"])
        old = old if isinstance(old, dict) else {}
        samples = int(old.get("samples", 0)) + 1
        old_mean = float(old.get("mean_seconds", 0.0))
        seconds = float(row["seconds"])
        files[row["path"]] = {
            "samples": samples,
            "mean_seconds": old_mean + (seconds - old_mean) / min(samples, _MEAN_WINDOW),
            "last_seconds": seconds,
            "last_outcome": row["outcome"],
            "last_seen": report["finished_at"],
        }
    if len(files) > _MAX_HISTORY_FILES:
        newest = sorted(
            files.items(),
            key=lambda pair: str(pair[1].get("last_seen", "")),
            reverse=True,
        )[:_MAX_HISTORY_FILES]
        files = dict(newest)
    history["files"] = files

    old_tests = history.get("tests")
    tests: dict[str, Any] = old_tests if isinstance(old_tests, dict) else {}
    for row in report["tests"]:
        old = tests.get(row["nodeid"])
        old = old if isinstance(old, dict) else {}
        samples = int(old.get("samples", 0)) + 1
        old_mean = float(old.get("mean_seconds", 0.0))
        seconds = float(row["seconds"])
        tests[row["nodeid"]] = {
            "samples": samples,
            "mean_seconds": old_mean + (seconds - old_mean) / min(samples, _MEAN_WINDOW),
            "last_seconds": seconds,
            "last_outcome": row["outcome"],
            "last_seen": report["finished_at"],
        }
    if len(tests) > _MAX_HISTORY_TESTS:
        newest = sorted(
            tests.items(),
            key=lambda pair: str(pair[1].get("last_seen", "")),
            reverse=True,
        )[:_MAX_HISTORY_TESTS]
        tests = dict(newest)
    history["tests"] = tests
    _atomic_json(path, history)


def _history_weights(path: Path) -> tuple[dict[str, float], dict[str, float]]:
    """Return per-test and fallback per-file means from a history document."""
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}, {}
    if not isinstance(history, dict) or history.get("version") != 1:
        return {}, {}

    def means(value: Any) -> dict[str, float]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, float] = {}
        for name, row in value.items():
            if not isinstance(name, str) or not isinstance(row, dict):
                continue
            seconds = row.get("mean_seconds")
            if not isinstance(seconds, int | float) or seconds < 0.0:
                continue
            # A skip is a step change, not a slow drift, and even a bounded mean
            # spends its whole window catching up to one. What decides it is
            # environmental and stable across a run -- a DSN, a built extension,
            # a free-threaded interpreter -- so last run's answer is the best
            # prediction available and a rolling mean is strictly worse than it.
            # Without this, 446 skipped tests carried 160.6s of weight against
            # 0.383s of real cost: 44% of everything the scheduler balanced on.
            result[name] = 0.0 if row.get("last_outcome") == "skipped" else float(seconds)
        return result

    return means(history.get("tests")), means(history.get("files"))


def _historical_weight(
    nodeid: str,
    test_weights: dict[str, float],
    file_weights: dict[str, float],
) -> float:
    exact = test_weights.get(nodeid)
    if exact is not None:
        return exact
    return file_weights.get(_path_from_nodeid(nodeid), 0.0)


#: The weight above which a test is worth placing by hand rather than by chunk.
#:
#: Longest-processing-time ordering earns its keep on a heavy tail, and whether
#: this suite has one depends on the run: with `WREATH_TEST_POSTGRES_DSN` set
#: there are 116-160 tests over a second, and without it those same tests skip
#: in microseconds and 91% of the suite finishes inside 10ms. Ordering all
#: fifteen thousand and handing them out two at a time therefore bought a real
#: makespan improvement in one regime and, in the other, sorted a flat list at
#: the price of a controller round trip per test.
#:
#: 50ms is roughly three hundred round trips, so above it the placement decision
#: dominates and below it the dispatch does. Measured on this tree's history the
#: cut takes 786 tests -- 5.1% of them, carrying 93.7% of the work -- and leaves
#: the other 14,493 to travel in `LoadScheduling`'s own consecutive chunks,
#: which is also what keeps a worker's fixtures alive across neighbouring tests.
_LPT_SECONDS = 0.05


def _xdist_group(nodeid: str) -> str | None:
    """The `xdist_group` name xdist appended to `nodeid`, if any.

    xdist encodes the mark as an `@name` suffix on the nodeid, and checks for a
    `]` after the last `@` so a parametrised value containing one is not
    mistaken for a group. Copied rather than imported because it lives on
    `LoadGroupScheduling`, which is not the class being subclassed here.
    """
    at = nodeid.rfind("@")
    return nodeid[at + 1 :] if at > nodeid.rfind("]") else None


class HistoricalSchedulerPlugin:
    """Place the known-slow tests by weight; let the rest ride in chunks."""

    def __init__(self, test_weights: dict[str, float], file_weights: dict[str, float]) -> None:
        self.test_weights = test_weights
        self.file_weights = file_weights

    def pytest_xdist_make_scheduler(self, config: Any, log: Any) -> Any:
        from xdist.scheduler.load import LoadScheduling

        test_weights = self.test_weights
        file_weights = self.file_weights

        class HistoricalLoadScheduling(LoadScheduling):
            """LPT for the heavy head, `LoadScheduling` verbatim for the tail.

            The head is dispatched a whole *unit* at a time, where a unit is one
            `xdist_group` or one ungrouped test. Sending a group in a single
            `_send_tests` call is what keeps its members on one worker, and it
            is why every group goes in the head whatever it weighs: the tail is
            then all single tests, so the inherited chunking cannot split one.

            This hook overrides `--dist` outright -- it is `firstresult` and
            xdist's own implementation is `trylast` -- so honouring the mark is
            this class's job rather than `LoadGroupScheduling`'s.
            """

            #: Units still queued at the front of `pending`, longest first. While
            #: this is non-empty each `check_schedule` hands over exactly one
            #: unit; once it drains, every decision belongs to `LoadScheduling`.
            _head: collections.deque[int]

            def schedule(self) -> None:
                if not self.collection_is_completed:
                    raise RuntimeError("xdist scheduled before worker collection completed")
                if self.collection is not None:
                    for node in self.nodes:
                        self.check_schedule(node)
                    return
                if not self._check_nodes_have_same_collection():
                    self.log("**Different tests collected, aborting run**")
                    return
                collection = next(iter(self.node2collection.values()))
                self.collection = collection
                if not collection:
                    return
                if self.maxschedchunk is None:
                    self.maxschedchunk = len(collection)

                weights = [
                    _historical_weight(nodeid, test_weights, file_weights) for nodeid in collection
                ]
                groups: dict[str, list[int]] = {}
                singles: list[int] = []
                for index, nodeid in enumerate(collection):
                    name = _xdist_group(nodeid)
                    if name is None:
                        singles.append(index)
                    else:
                        groups.setdefault(name, []).append(index)

                units = [(sum(weights[i] for i in members), members) for members in groups.values()]
                units += [(weights[i], [i]) for i in singles if weights[i] >= _LPT_SECONDS]
                units.sort(key=lambda unit: unit[0], reverse=True)
                tail = [i for i in singles if weights[i] < _LPT_SECONDS]

                self.pending[:] = [i for _weight, members in units for i in members]
                self.pending.extend(tail)
                self._head = collections.deque(len(members) for _weight, members in units)

                # One unit per worker per pass spreads the heaviest work across
                # workers. A second queued item is required by xdist's worker
                # protocol so it can send the next item before teardown.
                for _pass in range(2):
                    for node in self.nodes:
                        if not self.pending:
                            break
                        self._send_unit(node)
                    if not self.pending:
                        break
                if not self.pending:
                    for node in self.nodes:
                        node.shutdown()

            def _send_unit(self, node: Any) -> None:
                """Hand over one whole unit, or one test once the head is gone."""
                self._send_tests(node, self._head.popleft() if self._head else 1)

            def check_schedule(self, node: Any, duration: float = 0) -> None:
                if node.shutting_down:
                    return
                if self._head and self.pending:
                    if len(self.node2pending[node]) < 2:
                        self._send_unit(node)
                    return
                super().check_schedule(node, duration)

        return HistoricalLoadScheduling(config, log)


class CollectionShardPlugin:
    """Keep each collected test module in exactly one fresh xdist worker."""

    def __init__(self, shards: tuple[tuple[str, int], ...]) -> None:
        self.shards = {Path(path): worker for path, worker in shards}
        self.root = Path(os.path.commonpath(tuple(self.shards)))
        self.workers = max(self.shards.values()) + 1

    def pytest_ignore_collect(self, collection_path: Path, config: Any) -> bool | None:
        worker = getattr(config, "workerinput", None)
        if not isinstance(worker, dict) or not collection_path.is_file():
            return None
        path = collection_path.resolve()
        owner = self.shards.get(path)
        if owner is None and path.name != "conftest.py" and path.is_relative_to(self.root):
            digest = hashlib.sha256(os.fsencode(path)).digest()
            owner = int.from_bytes(digest[:8], "little") % self.workers
        if owner is None:
            return None
        worker_id = str(worker.get("workerid", ""))
        try:
            worker_index = int(worker_id.removeprefix("gw"))
        except ValueError as error:
            raise ValueError(f"xdist worker id {worker_id!r} must have the form 'gwN'") from error
        return owner != worker_index

    def pytest_sessionfinish(self, session: Any) -> None:
        output = getattr(session.config, "workeroutput", None)
        if output is not None:
            output["wreath_collection_shard"] = True


class CollectionShardSchedulerPlugin:
    """Run each worker's disjoint collection without replicating another shard."""

    def pytest_xdist_make_scheduler(self, config: Any, log: Any) -> Any:
        from xdist.scheduler.each import EachScheduling

        return EachScheduling(config, log)


class MutationTracePlugin:
    """Capture selected mutation-line hits during the ordinary pytest run."""

    def __init__(self, watched: dict[str, frozenset[int]], output: Path) -> None:
        from ._mutant.trace import LineTracer

        self.tracer = LineTracer(watched)
        self.output = output
        self.live_output = output.with_name(f"live-{os.getpid()}.jsonl")
        self.outcomes: dict[str, str] = {}

    def pytest_sessionstart(self, session: Any) -> None:
        self.tracer.start()

    def pytest_runtest_setup(self, item: Any) -> None:
        self.tracer.pytest_runtest_setup(item)

    def pytest_runtest_teardown(self, item: Any, nextitem: Any) -> None:
        self.tracer.pytest_runtest_teardown(item, nextitem)

    def pytest_runtest_logreport(self, report: Any) -> None:
        nodeid = str(report.nodeid)
        if report.outcome == "failed":
            self.outcomes[nodeid] = "failed"
        elif report.when == "call" and report.outcome == "passed":
            self.outcomes.setdefault(nodeid, "passed")
        if report.when != "teardown":
            return
        outcome = self.outcomes.pop(nodeid, "")
        if outcome != "passed":
            return
        hits = self.tracer.hits.get(nodeid, set())
        if not hits:
            return
        # Each pytest worker owns one append-only file, so complete-test trace
        # evidence becomes visible without a cross-process lock. The final
        # atomic trace remains the source of truth for the sealed baseline.
        with self.live_output.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "nodeid": nodeid,
                        "hits": [[path, line] for path, line in sorted(hits)],
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        self.tracer.stop()
        hits = [
            [f"{path}:{line}", list(nodes)] for (path, line), nodes in self.tracer.index().items()
        ]
        _atomic_json(self.output, {"hits": hits})
        with self.live_output.open("a", encoding="utf-8") as stream:
            stream.write('{"event":"worker_finished"}\n')


class ActivityPlugin:
    """Pytest hook implementation installed in the controller process only."""

    def __init__(self, config: RunnerConfig, *, workers: int) -> None:
        self.config = config
        self.activity = RunActivity(workers=workers)
        self.renderer = ActivityRenderer(
            self.activity,
            stream=sys.stderr,
            mode=config.grid,
            slowest=config.slowest,
        )
        self.deferred = False
        self.mutation_activity_path = (
            Path(config.mutation_activity) if config.mutation_activity else None
        )
        self.mutation_event_state = _MutationEventState(total=config.mutation_samples)
        self.stage_events = Path(config.stage_events) if config.stage_events else None
        self._emitted_stage_files: set[str] = set()

    def _sync_mutation(self) -> None:
        path = self.mutation_activity_path
        if path is None:
            return
        before = self.mutation_event_state.processed
        _consume_mutation_events(path, self.mutation_event_state)
        if self.mutation_event_state.processed == before:
            return
        self.renderer.mutation = MutationActivity(
            mode=self.config.mutation_mode,
            state="running",
            total=self.mutation_event_state.total,
            mutating_files=frozenset(self.mutation_event_state.mutating_files),
            verified_files=frozenset(self.mutation_event_state.verified_files),
            failed_mutation_files=frozenset(),
            test_workers=self.mutation_event_state.test_workers,
            mutant_workers=self.mutation_event_state.mutant_workers,
        )

    def pytest_collection_finish(self, session: Any) -> None:
        self.activity.collect(tuple(item.nodeid for item in session.items))

    def pytest_xdist_node_collection_finished(self, node: Any, ids: Any) -> None:
        self.activity.collect(tuple(str(nodeid) for nodeid in ids))

    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        self.activity.start_test(nodeid)
        test = self.activity.tests.get(nodeid)
        if test is not None and self.stage_events is not None:
            _append_stage_event(
                self.stage_events,
                self.activity.files[test.path],
                outcome="running",
            )
        self._sync_mutation()

    def pytest_runtest_logreport(self, report: Any) -> None:
        self.activity.add_report(report)
        self._sync_mutation()

    def pytest_runtest_logfinish(self, nodeid: str, location: Any) -> None:
        self.activity.finish_test(nodeid)
        test = self.activity.tests.get(nodeid)
        if test is not None:
            file_state = self.activity.files[test.path]
            if (
                self.stage_events is not None
                and test.path not in self._emitted_stage_files
                and file_state.finished == len(file_state.nodeids)
            ):
                _append_stage_event(self.stage_events, file_state)
                self._emitted_stage_files.add(test.path)
            elif self.stage_events is not None:
                _append_stage_event(
                    self.stage_events,
                    file_state,
                    outcome="idle",
                )
        self._sync_mutation()

    def pytest_collectreport(self, report: Any) -> None:
        if getattr(report, "failed", False):
            self.activity.collection_error(
                str(report.nodeid), float(getattr(report, "duration", 0.0))
            )

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        self._sync_mutation()
        self.activity.finish(int(exitstatus))
        report = self.activity.report(slowest=self.config.slowest)
        if self.config.report:
            _atomic_json(Path(self.config.report), report)
        if self.config.history:
            try:
                _update_history(Path(self.config.history), report)
            except OSError as error:
                # History is an optimization input, never part of whether the
                # tests passed. An explicit report path above remains strict.
                print(f"wreath test: could not update history: {error}", file=sys.stderr)
        passing = int(report["counts"]["passed"])
        if self.config.mutation_mode != "off" and passing:
            self.renderer.defer()
            self.deferred = True
        elif self.config.mutation_mode != "off":
            self.renderer.finish_with_mutation(
                MutationActivity(
                    mode=self.config.mutation_mode,
                    state="no_green",
                )
            )
        else:
            self.renderer.finish()

    def finish_mutation(self, mutation: MutationActivity) -> None:
        if not self.deferred:
            return
        self.deferred = False
        self.renderer.finish_mutation_progress()
        self.renderer.finish_with_mutation(mutation)

    def finish_pipeline(
        self,
        mutation: MutationActivity,
        fuzz: FuzzActivity,
    ) -> None:
        if not self.deferred:
            return
        self.deferred = False
        self.renderer.finish_mutation_progress()
        self.renderer.finish_pipeline(mutation, fuzz)


_ACTIVE_ACTIVITY_PLUGIN: ActivityPlugin | None = None


def _controller_process() -> bool:
    raw = os.environ.get(_CONTROLLER_PID_ENV)
    if raw is None:
        return False
    try:
        controller = int(raw)
    except ValueError:
        return False
    return os.getpid() == controller


def install_activity_plugin(config: Any) -> None:
    """Install the controller plugin when ``wreath test`` activated this run."""
    raw = os.environ.get(_CONFIG_ENV)
    if raw is None:
        return
    runner_config = RunnerConfig.from_json(raw)
    _install_mutation_trace_plugin(config)
    if runner_config.collection_shards:
        config.pluginmanager.register(
            CollectionShardPlugin(runner_config.collection_shards),
            f"{_PLUGIN_NAME}-collection-shard",
        )
    if hasattr(config, "workerinput"):
        return
    if not _controller_process() or config.pluginmanager.hasplugin(_PLUGIN_NAME):
        return
    workers = _workers_from_pytest_config(config, runner_config.workers)

    if runner_config.collection_shards:
        config.pluginmanager.register(
            CollectionShardSchedulerPlugin(),
            f"{_PLUGIN_NAME}-collection-shard-scheduler",
        )
    elif runner_config.history and workers > 1:
        test_weights, file_weights = _history_weights(Path(runner_config.history))
        if test_weights:
            config.pluginmanager.register(
                HistoricalSchedulerPlugin(test_weights, file_weights),
                f"{_PLUGIN_NAME}-historical-scheduler",
            )

    # xdist is optional, so its hook is optional too. Marking it here keeps an
    # ordinary serial pytest installation from rejecting the plugin at startup.
    import pytest

    method = ActivityPlugin.pytest_xdist_node_collection_finished
    if not hasattr(method, "pytest_impl"):
        pytest.hookimpl(optionalhook=True)(method)
    plugin = ActivityPlugin(runner_config, workers=workers)
    config.pluginmanager.register(plugin, _PLUGIN_NAME)
    global _ACTIVE_ACTIVITY_PLUGIN
    _ACTIVE_ACTIVITY_PLUGIN = plugin


def _install_mutation_trace_plugin(config: Any) -> None:
    raw = os.environ.get(_MUTATION_TRACE_ENV)
    if raw is None:
        return
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("mutation trace configuration must be an object")
    root_pid = int(value["root_pid"])
    worker = hasattr(config, "workerinput")
    if worker:
        if os.getppid() != root_pid:
            return
    elif os.getpid() != root_pid:
        return
    else:
        numprocesses = getattr(config.option, "numprocesses", None)
        if isinstance(numprocesses, int) and numprocesses > 0:
            return
    watched_value = value.get("watched")
    output_dir = value.get("output_dir")
    if not isinstance(watched_value, dict) or not isinstance(output_dir, str):
        raise ValueError("mutation trace configuration is incomplete")
    watched = {
        str(path): frozenset(int(line) for line in lines)
        for path, lines in watched_value.items()
        if isinstance(lines, list)
    }
    output = Path(output_dir) / f"trace-{os.getpid()}.json"
    config.pluginmanager.register(
        MutationTracePlugin(watched, output),
        f"{_PLUGIN_NAME}-mutation-trace",
    )


def _workers_from_pytest_config(config: Any, requested: str) -> int:
    value = getattr(config.option, "numprocesses", None)
    if isinstance(value, int) and value > 0:
        return value
    if value == 0:
        return 1
    try:
        parsed = int(requested)
    except ValueError:
        return 1
    return max(1, parsed)


def _has_xdist_argument(arguments: list[str]) -> bool:
    for index, argument in enumerate(arguments):
        if argument == "-n" or argument == "--numprocesses":
            return index + 1 < len(arguments)
        if argument.startswith("-n") and argument != "-n":
            return True
        if argument.startswith("--numprocesses="):
            return True
    return False


def _has_xdist_distribution(arguments: list[str]) -> bool:
    return any(argument == "--dist" or argument.startswith("--dist=") for argument in arguments)


def _has_xdist_restart(arguments: list[str]) -> bool:
    return any(
        argument == "--max-worker-restart" or argument.startswith("--max-worker-restart=")
        for argument in arguments
    )


def _explicit_test_paths(arguments: list[str]) -> tuple[Path, ...]:
    """Return existing positional-looking paths; auto mode treats any as focused."""
    paths: list[Path] = []
    for argument in arguments:
        if argument.startswith("-"):
            continue
        raw_path = argument.split("::", 1)[0]
        if not raw_path:
            continue
        path = Path(raw_path)
        if path.exists():
            paths.append(path.resolve())
    return tuple(dict.fromkeys(paths))


def _has_focused_expression(arguments: list[str]) -> bool:
    """Whether pytest will deselect tests after modules have been assigned.

    Collection sharding assigns whole files before pytest evaluates `-m` and
    `-k`. A focused expression can leave one or more workers with an empty
    shard; xdist then contributes NO_TESTS_COLLECTED (5) even while other
    workers run green tests. An explicitly empty expression is broad and keeps
    sharding eligible.
    """
    names = {"-m", "--markexpr", "-k", "--keyword"}
    for index, argument in enumerate(arguments):
        if argument in names:
            return index + 1 >= len(arguments) or bool(arguments[index + 1])
        for prefix in ("--markexpr=", "--keyword="):
            if argument.startswith(prefix):
                return bool(argument.removeprefix(prefix))
        if argument.startswith(("-m", "-k")) and argument not in names:
            return bool(argument[2:])
    return False


def _ignored_test_path(path: Path) -> bool:
    return any(part.startswith(".") or part == "__pycache__" for part in path.parts)


def _collection_modules(arguments: list[str], *, forced: bool) -> tuple[Path, ...]:
    explicit = _explicit_test_paths(arguments)
    if explicit and not forced:
        return ()
    roots = explicit
    if not roots:
        default = Path("tests")
        roots = ((default if default.is_dir() else Path.cwd()).resolve(),)

    modules: set[Path] = set()
    for root in roots:
        candidates = (root,) if root.is_file() else root.rglob("*.py")
        for candidate in candidates:
            if candidate.name == "conftest.py":
                continue
            if not (candidate.name.startswith("test_") or candidate.name.endswith("_test.py")):
                continue
            relative = candidate.resolve().relative_to(root.parent if root.is_file() else root)
            if _ignored_test_path(relative):
                continue
            modules.add(candidate.resolve())
    return tuple(sorted(modules))


def _declared_xdist_group(node: ast.Call) -> str | None:
    values = [keyword.value for keyword in node.keywords if keyword.arg == "name"]
    if not values and node.args:
        values.append(node.args[0])
    if len(values) != 1 or not isinstance(values[0], ast.Constant):
        return None
    name = values[0].value
    return name if isinstance(name, str) else None


def _cross_module_xdist_group(modules: tuple[Path, ...]) -> str | None:
    """Name a group sharding cannot preserve, including a dynamic declaration."""
    owners: dict[str, set[Path]] = {}
    for module in modules:
        source = module.read_bytes()
        if b"xdist_group" not in source:
            continue
        tree = ast.parse(source, filename=str(module))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "xdist_group"
            ):
                continue
            name = _declared_xdist_group(node)
            if name is None:
                return f"dynamic xdist_group in {module}"
            owners.setdefault(name, set()).add(module)
    for name, paths in owners.items():
        if len(paths) > 1:
            return f"xdist_group {name!r} spans {len(paths)} modules"
    return None


def _recent_file_weights(path: Path) -> dict[Path, float]:
    """Read file costs from the newest broad run, excluding stale gated files."""
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    if not isinstance(history, dict) or history.get("version") != 1:
        return {}
    runs = history.get("runs")
    if not isinstance(runs, list):
        return {}
    valid_runs = [
        run
        for run in runs
        if isinstance(run, dict)
        and isinstance(run.get("counts"), dict)
        and isinstance(run["counts"].get("collected"), int)
        and isinstance(run.get("finished_at"), str)
    ]
    if not valid_runs:
        return {}
    largest = max(int(run["counts"]["collected"]) for run in valid_runs)
    broad = [run for run in valid_runs if int(run["counts"]["collected"]) >= largest * 0.9]
    stamp = str(broad[-1]["finished_at"])
    files = history.get("files")
    if not isinstance(files, dict):
        return {}
    result: dict[Path, float] = {}
    for name, row in files.items():
        if not isinstance(name, str) or not isinstance(row, dict):
            continue
        seconds = row.get("last_seconds")
        if (
            row.get("last_seen") != stamp
            or row.get("last_outcome") == "skipped"
            or not isinstance(seconds, int | float)
            or seconds < 0.0
        ):
            continue
        result[Path(name).resolve()] = float(seconds)
    return result


def _collection_shards(
    modules: tuple[Path, ...],
    *,
    workers: int,
    weights: dict[Path, float],
) -> tuple[tuple[str, int], ...]:
    """Greedily balance whole modules, preserving module fixture locality."""
    loads = [0.0] * workers
    costs: list[tuple[float, Path]] = []
    for module in modules:
        collection_cost = 0.001 + module.stat().st_size / 10_000_000
        costs.append((max(weights.get(module, 0.0), collection_cost), module))
    shards: list[tuple[str, int]] = []
    ordered = sorted(costs, key=lambda row: (-row[0], str(row[1])))
    for cost, module in ordered:
        owner = min(range(workers), key=loads.__getitem__)
        loads[owner] += cost
        shards.append((str(module), owner))
    return tuple(sorted(shards))


def _prepare_collection_shards(
    mode: str,
    arguments: list[str],
    *,
    workers: int,
    history: Path | None,
) -> tuple[tuple[str, int], ...]:
    if mode == "replicated" or workers < 2:
        return ()
    if mode == "auto" and _has_focused_expression(arguments):
        return ()
    if _has_xdist_distribution(arguments):
        if mode == "sharded":
            raise ValueError(
                "--collection sharded cannot be combined with pytest --dist; "
                "use --collection replicated with --dist"
            )
        return ()
    if _has_xdist_argument(arguments):
        if mode == "sharded":
            raise ValueError(
                "--collection sharded cannot be combined with pytest -n; "
                "use the wreath test --workers option"
            )
        return ()
    if _has_xdist_restart(arguments):
        if mode == "sharded":
            raise ValueError(
                "--collection sharded cannot restart a worker with a different "
                "shard id; use --collection replicated with --max-worker-restart"
            )
        return ()
    modules = _collection_modules(arguments, forced=mode == "sharded")
    if len(modules) < workers * _MIN_SHARD_MODULES_PER_WORKER:
        return ()
    incompatible_group = _cross_module_xdist_group(modules)
    if incompatible_group is not None:
        if mode == "sharded":
            raise ValueError(
                f"--collection sharded cannot preserve {incompatible_group}; "
                "use --collection replicated"
            )
        return ()
    weights = _recent_file_weights(history) if history is not None else {}
    unknown_paths = weights.keys() - set(modules)
    if mode == "auto" and any(path.exists() for path in unknown_paths):
        return ()
    coverage = len(set(modules) & weights.keys()) / len(modules)
    if mode == "auto" and coverage < _SHARD_HISTORY_COVERAGE:
        return ()
    return _collection_shards(modules, workers=workers, weights=weights)


def _resolve_workers(raw: str) -> int:
    return _resolve_worker_count(raw, option="--workers", auto_cap=_MAX_AUTO_WORKERS)


def _mutation_arguments(namespace: Any) -> list[str]:
    mode = str(namespace.mutant)
    if namespace.mutant_samples < 1:
        raise ValueError("--mutant-samples must be at least 1")
    if namespace.mutant_timeout <= 0:
        raise ValueError("--mutant-timeout must be greater than zero")
    if namespace.mutant_max_candidates < 1:
        raise ValueError("--mutant-max-candidates must be at least 1")
    if namespace.mutant_maxfail < 0:
        raise ValueError("--mutant-maxfail must be a non-negative integer")
    if namespace.mutant_budget < 0:
        raise ValueError("--mutant-budget must be non-negative")
    raw_mutant_workers = str(namespace.mutant_workers)
    mutant_workers = _resolve_mutant_workers(raw_mutant_workers)
    if raw_mutant_workers == "auto" and namespace.fuzz in {"auto", "on"}:
        # Mutation and live fuzz share the measured three-slot background
        # envelope. Five ordinary workers remain until baseline seal; the
        # sealed mutation scheduler can still reclaim all suite workers.
        mutant_workers = max(1, mutant_workers - 1)
    arguments = ["--format", "json"]
    arguments.extend(("--test-engine", str(namespace.mutant_engine)))
    if mode in {"auto", "sample"}:
        arguments.extend(("--sample", str(namespace.mutant_samples)))
    elif mode == "changed":
        arguments.extend(("--changed", str(namespace.mutant_changed)))
    elif mode != "full":
        raise ValueError(f"unknown mutation confidence mode {mode!r}")
    for path in namespace.mutant_path:
        arguments.extend(("--path", str(path)))
    for path in namespace.mutant_tests:
        arguments.extend(("--tests", str(path)))
    for operator in namespace.mutant_operator:
        arguments.extend(("--operators", str(operator)))
    for selector in namespace.mutant_only:
        arguments.extend(("--only", str(selector)))
    for argument in namespace.mutant_pytest_arg:
        arguments.extend(("--pytest-arg", str(argument)))
    arguments.extend(("--timeout", str(namespace.mutant_timeout)))
    arguments.extend(("--max-candidates", str(namespace.mutant_max_candidates)))
    arguments.extend(("--maxfail", str(namespace.mutant_maxfail)))
    arguments.extend(("--jobs", str(mutant_workers)))
    if raw_mutant_workers == "auto":
        arguments.append("--reclaim-workers")
        arguments.extend(("--suite-workers", str(_resolve_workers(str(namespace.workers)))))
    if mode in {"auto", "sample"}:
        arguments.extend(("--budget", str(namespace.mutant_budget)))
    return arguments


def _resolve_mutant_workers(raw: str) -> int:
    return _resolve_worker_count(raw, option="--mutant-workers", auto_cap=_MAX_AUTO_MUTANT_WORKERS)


def _resolve_worker_count(raw: str, *, option: str, auto_cap: int) -> int:
    """Resolve one worker option through the shared positive/auto contract."""
    if raw == "auto":
        return min(auto_cap, os.cpu_count() or 1)
    try:
        workers = int(raw)
    except ValueError:
        raise ValueError(f"{option} expects 'auto' or a positive integer, got {raw!r}") from None
    if workers < 1:
        raise ValueError(f"{option} must be at least 1")
    return workers


@dataclass(frozen=True, slots=True)
class MutationTraceSpec:
    selected: frozenset[str]
    watched: dict[str, frozenset[int]]
    whole_files: frozenset[str]
    output_dir: Path


def _prepare_mutation_trace(namespace: Any, directory: Path) -> MutationTraceSpec | None:
    if namespace.mutant not in {"auto", "sample"}:
        return None
    from ._mutant.cli import default_sources
    from ._mutant.runner import discover, sample_identifiers, watch_selected_identifiers

    repo = Path.cwd()
    roots = [Path(value).resolve() for value in namespace.mutant_path]
    roots = roots or default_sources(repo)
    missing = [str(root) for root in roots if not root.exists()]
    if missing:
        raise ValueError(f"no such mutation source path: {', '.join(missing)}")
    fingerprint = hashlib.blake2b(digest_size=20)
    for path in discover(roots):
        try:
            stat = path.stat()
        except OSError:
            continue
        fingerprint.update(str(path).encode())
        fingerprint.update(f"\0{stat.st_mtime_ns}\0{stat.st_size}\0".encode())
    cache_key = {
        "fingerprint": fingerprint.hexdigest(),
        "roots": [str(root) for root in roots],
        "samples": namespace.mutant_samples,
        "operators": list(namespace.mutant_operator),
        "only": list(namespace.mutant_only),
    }
    history_path = None if namespace.no_history else Path(namespace.history)
    if history_path is not None:
        cached = _read_mutation_sample_cache(history_path, cache_key)
        if cached is not None:
            selected, watched, whole_files = cached
            if not selected:
                return None
            output_dir = directory / "mutation-trace"
            output_dir.mkdir()
            return MutationTraceSpec(selected, watched, whole_files, output_dir)
    selected = frozenset(
        sample_identifiers(
            roots,
            repo,
            namespace.mutant_samples,
            operators=tuple(namespace.mutant_operator),
            only=tuple(namespace.mutant_only),
        )
    )
    if not selected:
        if history_path is not None:
            _write_mutation_sample_cache(history_path, cache_key, selected, {}, frozenset())
        return None
    watched, whole_files = watch_selected_identifiers(roots, repo, selected)
    if history_path is not None:
        _write_mutation_sample_cache(history_path, cache_key, selected, watched, whole_files)
    output_dir = directory / "mutation-trace"
    output_dir.mkdir()
    return MutationTraceSpec(selected, watched, whole_files, output_dir)


def _read_mutation_sample_cache(
    path: Path, key: dict[str, Any]
) -> tuple[frozenset[str], dict[str, frozenset[int]], frozenset[str]] | None:
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(history, dict) or history.get("version") != 1:
        return None
    cached = history.get("mutation_sample")
    if not isinstance(cached, dict) or cached.get("key") != key:
        return None
    selected = cached.get("selected")
    watched = cached.get("watched")
    whole_files = cached.get("whole_files")
    if not isinstance(selected, list | tuple) or not isinstance(watched, dict):
        return None
    if not isinstance(whole_files, list | tuple):
        return None
    return (
        frozenset(str(value) for value in selected),
        {
            str(source): frozenset(int(line) for line in lines)
            for source, lines in watched.items()
            if isinstance(lines, list | tuple)
        },
        frozenset(str(value) for value in whole_files),
    )


def _write_mutation_sample_cache(
    path: Path,
    key: dict[str, Any],
    selected: frozenset[str],
    watched: dict[str, frozenset[int]],
    whole_files: frozenset[str],
) -> None:
    history: dict[str, Any] = {
        "version": 1,
        "runs": [],
        "files": {},
        "tests": {},
    }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and loaded.get("version") == 1:
            history = loaded
    except OSError, ValueError:
        pass
    history["mutation_sample"] = {
        "key": key,
        "selected": sorted(selected),
        "watched": {source: sorted(lines) for source, lines in watched.items()},
        "whole_files": sorted(whole_files),
    }
    try:
        _atomic_json(path, history)
    except OSError as error:
        print(f"wreath test: could not cache mutation sample: {error}", file=sys.stderr)


def _trace_environment(spec: MutationTraceSpec) -> str:
    return json.dumps(
        {
            "root_pid": os.getpid(),
            "output_dir": str(spec.output_dir),
            "watched": {path: sorted(lines) for path, lines in spec.watched.items()},
        },
        separators=(",", ":"),
    )


def _write_reused_baseline(
    spec: MutationTraceSpec,
    activity_path: Path,
    target: Path,
) -> bool:
    try:
        activity = json.loads(activity_path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return False
    documents: list[dict[str, Any]] = []
    for path in spec.output_dir.glob("trace-*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        if isinstance(value, dict):
            documents.append(value)
    expected = activity.get("workers") if isinstance(activity, dict) else None
    if not isinstance(expected, int) or len(documents) < expected:
        return False
    merged: dict[str, set[str]] = {}
    for document in documents:
        rows = document.get("hits")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                continue
            key, nodes = row
            if not isinstance(key, str) or not isinstance(nodes, list):
                continue
            merged.setdefault(key, set()).update(str(node) for node in nodes)
    tests = activity.get("tests")
    if not isinstance(tests, list):
        return False
    passed = sorted(
        str(row["nodeid"])
        for row in tests
        if isinstance(row, dict) and row.get("outcome") == "passed"
    )
    failed = sorted(
        str(row["nodeid"])
        for row in tests
        if isinstance(row, dict) and row.get("outcome") in {"failed", "error"}
    )
    per_file: dict[str, set[str]] = {path: set() for path in spec.whole_files}
    for key, nodes in merged.items():
        path, separator, _line = key.rpartition(":")
        if separator and path in per_file:
            per_file[path].update(nodes)
    _atomic_json(
        target,
        {
            "passed": passed,
            "failed": failed,
            "hits": [[key, sorted(nodes)] for key, nodes in sorted(merged.items())],
            "files": {path: sorted(nodes) for path, nodes in per_file.items()},
            "seconds": float(activity.get("wall_seconds", 0.0)),
        },
    )
    return True


@dataclass(slots=True)
class _MutationEventState:
    processed: int = 0
    total: int = 0
    mutating_files: set[str] = field(default_factory=set)
    verified_files: set[str] = field(default_factory=set)
    killer_tests: set[str] = field(default_factory=set)
    survivor_candidate_files: set[str] = field(default_factory=set)
    active: dict[int, set[str]] = field(default_factory=dict)
    test_workers: int = 0
    mutant_workers: int = 0


def _consume_mutation_events(path: Path, state: _MutationEventState) -> None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    lines = raw.splitlines()
    if raw and not raw.endswith("\n"):
        lines = lines[:-1]
    for position, line in enumerate(lines):
        if position < state.processed:
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("mutation activity event must be an object")
        event = value.get("event")
        if event == "planned":
            state.total = int(value.get("total", 0))
        elif event == "capacity":
            state.test_workers = int(value.get("test_workers", 0))
            state.mutant_workers = int(value.get("mutant_workers", 0))
        elif event == "started":
            tests = value.get("tests")
            if isinstance(tests, list):
                state.active[int(value.get("ordinal", 0))] = {
                    _path_from_nodeid(str(path)) for path in tests
                }
                state.mutating_files = set().union(*state.active.values())
        elif event == "finished":
            tested = state.active.pop(int(value.get("ordinal", 0)), set())
            state.mutating_files = set().union(*state.active.values()) if state.active else set()
            if value.get("outcome") == "killed":
                killers = value.get("killers")
                if isinstance(killers, list):
                    state.killer_tests.update(str(nodeid) for nodeid in killers)
                    state.verified_files.update(
                        _path_from_nodeid(str(nodeid)) for nodeid in killers
                    )
            elif value.get("outcome") == "survived":
                # A survivor belongs to the mutated source control, not to any
                # one test file in the candidate set. Retain the candidates to
                # keep fuzz selection conservative without immediately blaming
                # every one. At terminal render, every file lacking positive
                # mutation evidence becomes a generic mutation-miss ×.
                state.survivor_candidate_files.update(tested)
    state.processed = len(lines)


def _mutation_activity_from_report(mode: str, report: dict[str, Any]) -> MutationActivity:
    counts_value = report.get("counts")
    if not isinstance(counts_value, dict):
        raise ValueError("mutation confidence report has no outcome counts")
    counts = {str(key): int(value) for key, value in counts_value.items()}
    rating_value = report.get("rating")
    if not isinstance(rating_value, dict):
        raise ValueError("mutation confidence report has no rating")
    label = rating_value.get("label")
    action = rating_value.get("action")
    tone = rating_value.get("tone")
    if not all(isinstance(value, str) for value in (label, action, tone)):
        raise ValueError("mutation confidence rating is incomplete")
    verified: set[str] = set()
    mutants = report.get("mutants")
    if isinstance(mutants, list):
        for mutant in mutants:
            if not isinstance(mutant, dict) or mutant.get("outcome") != "killed":
                continue
            killers = mutant.get("killers")
            if not isinstance(killers, list):
                continue
            verified.update(_path_from_nodeid(str(nodeid)) for nodeid in killers)
    verified_files = frozenset(verified)
    report["verified_test_files"] = sorted(verified_files)
    baseline_failures = 0
    baseline_value = report.get("baseline")
    if isinstance(baseline_value, dict):
        failures = baseline_value.get("failures")
        if isinstance(failures, list):
            baseline_failures = len(failures)
    live_probes = 0
    live_completed = 0
    live_cancelled = 0
    live_first: float | None = None
    live_value = report.get("live")
    if isinstance(live_value, dict):
        live_probes = int(live_value.get("probes", 0))
        live_completed = int(live_value.get("completed", 0))
        live_cancelled = int(live_value.get("cancelled_at_seal", 0))
        first_value = live_value.get("first_started_seconds")
        if isinstance(first_value, int | float):
            live_first = float(first_value)
    return MutationActivity(
        mode=mode,
        state="complete",
        total=sum(counts.values()),
        rating_label=label,
        rating_action=action,
        rating_tone=tone,
        counts=counts,
        verified_files=verified_files,
        baseline_failures=baseline_failures,
        live_probes=live_probes,
        live_completed=live_completed,
        live_cancelled_at_seal=live_cancelled,
        live_first_started_seconds=live_first,
    )


def _mutation_gold_files(report: dict[str, Any]) -> tuple[str, ...]:
    """Files with positive mutation evidence from at least one exact killer."""
    verified_value = report.get("verified_test_files")
    verified = {str(path) for path in verified_value} if isinstance(verified_value, list) else set()
    return tuple(sorted(verified))


def _mutation_gold_tests(report: dict[str, Any], selected_files: Iterable[str]) -> tuple[str, ...]:
    """Exact killing tests that provide the minimum fuzz surface per gold file."""
    selected_lookup = dict.fromkeys(selected_files, True)
    killers: list[str] = []
    mutants = report.get("mutants")
    if not isinstance(mutants, list):
        return ()
    for mutant in mutants:
        if not isinstance(mutant, dict) or mutant.get("outcome") != "killed":
            continue
        values = mutant.get("killers")
        if not isinstance(values, list):
            continue
        for nodeid in values:
            rendered = str(nodeid)
            if selected_lookup.get(_path_from_nodeid(rendered), False):
                killers.append(rendered)
    return tuple(sorted(set(killers)))


def _live_fuzz_ready(gold_files: int, passed_files: int) -> bool:
    """Whether positive mutation evidence has unlocked the live fuzz stage."""
    if gold_files <= 0 or passed_files <= 0:
        return False
    return gold_files >= math.ceil(passed_files * _LIVE_FUZZ_GOLD_RATIO)


@dataclass(slots=True)
class _FuzzEventState:
    processed: int = 0
    active_counts: dict[str, int] = field(default_factory=dict)
    passed_files: set[str] = field(default_factory=set)
    failed_files: set[str] = field(default_factory=set)
    incomplete_files: set[str] = field(default_factory=set)
    finished_files: set[str] = field(default_factory=set)


def _consume_fuzz_events(path: Path, state: _FuzzEventState) -> None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    lines = raw.splitlines()
    if raw and not raw.endswith("\n"):
        lines = lines[:-1]
    for position, line in enumerate(lines):
        if position < state.processed:
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("fuzz activity event must be an object")
        path_value = value.get("path")
        outcome = value.get("outcome")
        if not isinstance(path_value, str) or not isinstance(outcome, str):
            raise ValueError("fuzz activity event must name its path and outcome")
        if outcome == "running":
            state.active_counts[path_value] = state.active_counts.get(path_value, 0) + 1
            continue
        active = state.active_counts.get(path_value, 0)
        if active > 1:
            state.active_counts[path_value] = active - 1
        else:
            state.active_counts.pop(path_value, None)
        if outcome == "idle":
            continue
        state.finished_files.add(path_value)
        if outcome == "passed":
            state.passed_files.add(path_value)
        elif outcome in {"skipped", "mixed"}:
            state.incomplete_files.add(path_value)
        else:
            state.failed_files.add(path_value)
    state.processed = len(lines)


def _fuzz_activity(
    state: str,
    selected: Sequence[str],
    events: _FuzzEventState,
    *,
    counts: dict[str, int] | None = None,
    schedule_only: Iterable[str] = (),
) -> FuzzActivity:
    selected_files = frozenset(selected)
    return FuzzActivity(
        state=state,
        selected_files=selected_files,
        active_files=(frozenset(events.active_counts) if state == "running" else frozenset()),
        passed_files=frozenset(events.passed_files),
        failed_files=frozenset(events.failed_files),
        incomplete_files=frozenset(events.incomplete_files),
        schedule_only_files=frozenset(schedule_only),
        counts=counts or {},
    )


@dataclass(slots=True)
class _FuzzProcess:
    process: subprocess.Popen[str]
    report_path: Path
    event_path: Path
    log_path: Path
    events: _FuzzEventState
    selected: tuple[str, ...]


def _start_fuzz_process(
    namespace: Any,
    selected: Sequence[str],
    *,
    directory: Path,
    workers: str | None = None,
    case_ids: Sequence[str] = (),
) -> _FuzzProcess:
    """Start one fixed gold-file batch without waiting for it."""
    chosen = tuple(sorted(set(selected)))
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.json"
    event_path = directory / "events.jsonl"
    log_path = directory / "runner.log"
    case_path = directory / "cases.json"
    engine = "native" if namespace.engine == "dual" else str(namespace.engine)
    command = [
        sys.executable,
        "-m",
        "wreath._cli",
        "test",
        "--engine",
        engine,
        "--mutant",
        "off",
        "--fuzz",
        "off",
        "--grid",
        "never",
        "--workers",
        workers or str(namespace.workers),
        "--slowest",
        "0",
        "--no-history",
        "--report",
        str(report_path),
        "--stage-events",
        str(event_path),
        "-m",
        "",
        *chosen,
    ]
    exact_cases = tuple(sorted(set(case_ids)))
    if exact_cases:
        case_path.write_text(json.dumps(exact_cases), encoding="utf-8")
        command[6:6] = ["--case-selection", str(case_path)]
    environment = os.environ.copy()
    environment["WREATH_FUZZ_STAGE"] = "1"
    environment["WREATH_FUZZ_SCHEDULE_SEED"] = _FUZZ_SCHEDULE_SEED
    environment["PYTHONHASHSEED"] = "424242"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )
    return _FuzzProcess(
        process=process,
        report_path=report_path,
        event_path=event_path,
        log_path=log_path,
        events=_FuzzEventState(),
        selected=chosen,
    )


def _sync_fuzz_process(
    fuzz: _FuzzProcess,
    mutation_activity: MutationActivity,
    *,
    renderer: ActivityRenderer | None,
) -> None:
    _consume_fuzz_events(fuzz.event_path, fuzz.events)
    if renderer is not None:
        renderer.fuzz_progress(
            mutation_activity,
            _fuzz_activity("running", fuzz.selected, fuzz.events),
        )


def _finish_fuzz_process(
    fuzz: _FuzzProcess,
    mutation_activity: MutationActivity,
    *,
    renderer: ActivityRenderer | None,
) -> tuple[dict[str, Any], FuzzActivity, int]:
    while fuzz.process.poll() is None:
        _sync_fuzz_process(fuzz, mutation_activity, renderer=renderer)
        time.sleep(0.08)
    returncode = fuzz.process.wait()
    _consume_fuzz_events(fuzz.event_path, fuzz.events)
    try:
        raw_report = json.loads(fuzz.report_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as error:
        diagnostic = fuzz.log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        raise ValueError(f"fuzz phase returned no valid report:\n{diagnostic}") from error
    if not isinstance(raw_report, dict):
        raise ValueError("fuzz phase report must be an object")
    counts_value = raw_report.get("counts")
    counts = (
        {str(key): int(value) for key, value in counts_value.items()}
        if isinstance(counts_value, dict)
        else {}
    )
    fuzz_case_ids = raw_report.get("fuzz_case_ids")
    if not isinstance(fuzz_case_ids, list) or not all(
        isinstance(nodeid, str) for nodeid in fuzz_case_ids
    ):
        fuzz_case_ids = []
    fuzzed_files = {_path_from_nodeid(nodeid) for nodeid in fuzz_case_ids}
    # Selection is a per-file contract. A file that produced no terminal event
    # did not complete the stage, whether collection found nothing or the
    # worker stopped before it. Never leave a selected gold tile purple.
    missing_files = set(fuzz.selected).difference(
        fuzz.events.passed_files,
        fuzz.events.failed_files,
        fuzz.events.incomplete_files,
    )
    fuzz.events.failed_files.update(missing_files)
    # Every selected file reruns its exact mutation killers in a fresh process;
    # explicit deterministic fuzz cases in that file expand the same stage.
    # A clean replay is the generic fuzz contract and earns the final star.
    # Keep schedule-only files visible in the report so callers can distinguish
    # that generic evidence from a purpose-built input corpus.
    fuzz.events.passed_files.difference_update(
        fuzz.events.failed_files | fuzz.events.incomplete_files
    )
    schedule_only_files = set(fuzz.selected).difference(
        fuzzed_files,
        fuzz.events.failed_files,
        fuzz.events.incomplete_files,
    )
    raw_report["selected_files"] = list(fuzz.selected)
    raw_report["fuzz_case_ids"] = fuzz_case_ids
    raw_report["fuzzed_files"] = sorted(fuzzed_files)
    raw_report["schedule_only_files"] = sorted(schedule_only_files)
    raw_report["passed_files"] = sorted(fuzz.events.passed_files)
    raw_report["failed_files"] = sorted(fuzz.events.failed_files)
    raw_report["incomplete_files"] = sorted(fuzz.events.incomplete_files)
    state = "no_tests" if counts.get("collected", 0) == 0 else "complete"
    activity = _fuzz_activity(
        state,
        fuzz.selected,
        fuzz.events,
        counts=counts,
        schedule_only=schedule_only_files,
    )
    status = 0 if returncode in {0, 5} else returncode
    if missing_files and status == 0:
        status = 1
    return raw_report, activity, status


def _stop_fuzz_process(fuzz: _FuzzProcess) -> None:
    if fuzz.process.poll() is not None:
        return
    fuzz.process.terminate()
    try:
        fuzz.process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        fuzz.process.kill()
        fuzz.process.wait()


def _merge_fuzz_batches(
    batches: Sequence[tuple[dict[str, Any], FuzzActivity, int]],
    selected: Sequence[str],
) -> tuple[dict[str, Any], FuzzActivity, int]:
    """Combine disjoint early and sealed fuzz batches into one report."""
    selected_files = frozenset(selected)
    activities = tuple(activity for _report, activity, _status in batches)
    counts = dict(
        sum(
            (collections.Counter(activity.counts) for activity in activities),
            collections.Counter(),
        )
    )
    passed_files = set().union(*(activity.passed_files & selected_files for activity in activities))
    failed_files = set().union(*(activity.failed_files & selected_files for activity in activities))
    incomplete_files = set().union(
        *(activity.incomplete_files & selected_files for activity in activities)
    )
    schedule_only_files = set().union(
        *(activity.schedule_only_files & selected_files for activity in activities)
    )
    status = next(
        (batch_status for _report, _activity, batch_status in batches if batch_status),
        0,
    )
    passed_files.difference_update(failed_files | incomplete_files)
    incomplete_files.difference_update(failed_files)
    schedule_only_files.difference_update(failed_files | incomplete_files)
    fuzz_case_id_set: set[str] = set()
    schedule_seed_set: set[str] = set()
    for report, _activity, _status in batches:
        case_ids = report.get("fuzz_case_ids")
        if isinstance(case_ids, list):
            for nodeid in case_ids:
                if isinstance(nodeid, str):
                    fuzz_case_id_set.add(nodeid)
        schedule_seed = report.get("fuzz_schedule_seed")
        if isinstance(schedule_seed, str):
            schedule_seed_set.add(schedule_seed)
    fuzz_case_ids = sorted(fuzz_case_id_set)
    schedule_seeds = sorted(schedule_seed_set)
    state = "no_tests" if counts.get("collected", 0) == 0 else "complete"
    activity = FuzzActivity(
        state=state,
        selected_files=selected_files,
        passed_files=frozenset(passed_files),
        failed_files=frozenset(failed_files),
        incomplete_files=frozenset(incomplete_files),
        schedule_only_files=frozenset(schedule_only_files),
        counts=counts,
    )
    report = {
        "version": 1,
        "kind": "wreath-fuzz-run",
        "counts": counts,
        "selected_files": sorted(selected_files),
        "passed_files": sorted(passed_files),
        "failed_files": sorted(failed_files),
        "incomplete_files": sorted(incomplete_files),
        "schedule_only_files": sorted(schedule_only_files),
        "fuzz_case_ids": fuzz_case_ids,
        "fuzzed_files": sorted({_path_from_nodeid(nodeid) for nodeid in fuzz_case_ids}),
        "schedule_seeds": schedule_seeds,
        "batches": len(batches),
    }
    return report, activity, status


def _fuzz_confidence(
    namespace: Any,
    mutation_report: dict[str, Any],
    mutation_activity: MutationActivity,
    *,
    renderer: ActivityRenderer | None,
    selected: Sequence[str] | None = None,
) -> tuple[dict[str, Any], FuzzActivity, int]:
    """Run each gold file's killers and explicit fuzz cases in a fresh pass."""
    chosen = tuple(selected) if selected is not None else _mutation_gold_files(mutation_report)
    if not chosen:
        report, activity = _no_gold_fuzz()
        return report, activity, 0
    with tempfile.TemporaryDirectory(prefix="wreath-fuzz-") as directory:
        fuzz = _start_fuzz_process(
            namespace,
            chosen,
            directory=Path(directory),
            case_ids=_mutation_gold_tests(mutation_report, chosen),
        )
        return _finish_fuzz_process(
            fuzz,
            mutation_activity,
            renderer=renderer,
        )


def _no_gold_fuzz() -> tuple[dict[str, Any], FuzzActivity]:
    selected: tuple[str, ...] = ()
    report = {"version": 1, "kind": "wreath-fuzz-run", "selected_files": []}
    return report, _fuzz_activity("no_gold", selected, _FuzzEventState())


@dataclass(slots=True)
class _MutationProcess:
    process: subprocess.Popen[str]
    output_path: Path
    activity_path: Path
    event_state: _MutationEventState
    baseline_reused: bool


def _start_mutation_process(
    namespace: Any,
    *,
    directory: Path,
    baseline: Path | None = None,
    baseline_wait: Path | None = None,
    baseline_stream: Path | None = None,
    selection: Path | None = None,
) -> _MutationProcess:
    """Start mutation preparation in a fresh interpreter, possibly before pytest."""
    arguments = _mutation_arguments(namespace)
    if baseline is not None:
        arguments.extend(("--baseline", str(baseline)))
    if baseline_wait is not None:
        arguments.extend(("--baseline-wait", str(baseline_wait)))
    if baseline_stream is not None:
        arguments.extend(("--baseline-stream", str(baseline_stream)))
    if selection is not None:
        arguments.extend(("--selection", str(selection)))
    if baseline_wait is not None:
        arguments.append("--background-priority")
    arguments.append("--quiet")
    total_hint = namespace.mutant_samples if namespace.mutant in {"auto", "sample"} else 0
    event_state = _MutationEventState(total=total_hint)
    activity_path = directory / "mutation-events.jsonl"
    output_path = directory / "mutation-report.json"
    arguments.extend(("--activity-file", str(activity_path)))
    command = [sys.executable, "-m", "wreath._mutant.cli", *arguments]
    with output_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(command, stdout=output, text=True)
    return _MutationProcess(
        process=process,
        output_path=output_path,
        activity_path=activity_path,
        event_state=event_state,
        baseline_reused=baseline is not None or baseline_wait is not None,
    )


def _stop_mutation_process(mutation: _MutationProcess) -> None:
    if mutation.process.poll() is not None:
        return
    mutation.process.terminate()
    try:
        mutation.process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        mutation.process.kill()
        mutation.process.wait()


def _finish_mutation_process(
    namespace: Any,
    mutation: _MutationProcess,
    *,
    renderer: ActivityRenderer | None = None,
    live_fuzz: _FuzzProcess | None = None,
) -> tuple[dict[str, Any], int]:
    announced = False
    try:
        while mutation.process.poll() is None:
            if renderer is not None:
                _consume_mutation_events(mutation.activity_path, mutation.event_state)
                renderer.mutation_progress(
                    str(namespace.mutant),
                    mutation.event_state.total,
                    mutating_files=frozenset(mutation.event_state.mutating_files),
                    verified_files=frozenset(mutation.event_state.verified_files),
                    failed_mutation_files=frozenset(),
                    test_workers=mutation.event_state.test_workers,
                    mutant_workers=mutation.event_state.mutant_workers,
                )
                if live_fuzz is not None and renderer.mutation is not None:
                    _sync_fuzz_process(
                        live_fuzz,
                        renderer.mutation,
                        renderer=renderer,
                    )
            elif not announced:
                print(f"\nMutation activity   {namespace.mutant}", file=sys.stderr)
                announced = True
            time.sleep(0.08)
    except KeyboardInterrupt:
        _stop_mutation_process(mutation)
        raise
    returncode = mutation.process.wait()
    if renderer is not None:
        _consume_mutation_events(mutation.activity_path, mutation.event_state)
        if live_fuzz is not None and renderer.mutation is not None:
            _sync_fuzz_process(
                live_fuzz,
                renderer.mutation,
                renderer=renderer,
            )
    raw_report = mutation.output_path.read_text(encoding="utf-8")
    if returncode != 0:
        raise ValueError("mutation confidence phase failed; its diagnostic is printed above")
    try:
        report = json.loads(raw_report)
    except (TypeError, ValueError) as error:
        raise ValueError("mutation confidence returned invalid JSON") from error
    if not isinstance(report, dict):
        raise ValueError("mutation confidence report must be an object")
    report["baseline_reused"] = mutation.baseline_reused
    report["failed_mutation_test_files"] = sorted(mutation.event_state.survivor_candidate_files)
    activity = _mutation_activity_from_report(str(namespace.mutant), report)
    survived = activity.counts.get("survived", 0)
    unreached = activity.counts.get("unreached", 0)
    status = 1 if namespace.mutant_fail_on_survivor and (survived or unreached) else 0
    return report, status


def _mutation_confidence(
    namespace: Any,
    *,
    baseline: Path | None = None,
    selection: Path | None = None,
    renderer: ActivityRenderer | None = None,
) -> tuple[dict[str, Any], int]:
    """Run the explicit mutation phase in a fresh, fork-safe interpreter."""
    with tempfile.TemporaryDirectory(prefix="wreath-mutation-activity-") as directory:
        mutation = _start_mutation_process(
            namespace,
            directory=Path(directory),
            baseline=baseline,
            selection=selection,
        )
        return _finish_mutation_process(namespace, mutation, renderer=renderer)


def _attach_mutation_report(path: Path, mutation: dict[str, Any]) -> None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"could not extend test report {path}: {error}") from error
    if not isinstance(report, dict):
        raise ValueError(f"could not extend test report {path}: root must be an object")
    report["mutation"] = mutation
    _atomic_json(path, report)


def _attach_fuzz_report(path: Path, fuzz: dict[str, Any]) -> None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"could not extend test report {path}: {error}") from error
    if not isinstance(report, dict):
        raise ValueError(f"could not extend test report {path}: root must be an object")
    report["fuzz"] = fuzz
    _atomic_json(path, report)


def execute(namespace: Any) -> int:
    """Run the selected engine with Wreath's activity controller."""
    if not hasattr(namespace, "fuzz"):
        namespace.fuzz = "auto"
    if not hasattr(namespace, "stage_events"):
        namespace.stage_events = None
    if namespace.mutant == "on":
        namespace.mutant = "auto"
    if namespace.fuzz == "auto":
        namespace.fuzz = "off" if namespace.mutant == "off" else "on"
    if namespace.fuzz == "on" and namespace.mutant == "off":
        raise ValueError("--fuzz on requires mutation evidence; use --mutant on")
    engine = str(getattr(namespace, "engine", "pytest"))
    if engine == "native":
        from ._native_test_runner import execute as execute_native

        return execute_native(namespace)
    if engine == "dual":
        from ._native_test_runner import execute_dual

        return execute_dual(namespace)
    if engine != "pytest":
        raise ValueError(f"unknown test engine {engine!r}; expected pytest, native, or dual")
    global _ACTIVE_ACTIVITY_PLUGIN
    _ACTIVE_ACTIVITY_PLUGIN = None
    if namespace.slowest < 0:
        raise ValueError("--slowest must be a non-negative integer")
    if namespace.mutant != "off":
        # Validate before spending the ordinary suite's time. The arguments
        # are rebuilt after the ordinary run to keep subprocess construction local.
        _mutation_arguments(namespace)
    try:
        import pytest
    except ModuleNotFoundError as error:
        if error.name != "pytest":
            raise
        raise ValueError(
            "wreath test needs pytest; install Wreath's dev group or pytest>=8.4"
        ) from error

    pytest_arguments: list[str] = [
        str(argument) for argument in getattr(namespace, "pytest_args", ())
    ]
    if pytest_arguments[:1] == ["--"]:
        pytest_arguments.pop(0)
    selection_arguments = list(pytest_arguments)
    requested_workers = str(namespace.workers)
    workers = _resolve_workers(requested_workers)
    if not _has_xdist_argument(pytest_arguments) and workers > 1:
        if importlib.util.find_spec("xdist") is None:
            print(
                "wreath test: pytest-xdist is unavailable; running serially",
                file=sys.stderr,
            )
            workers = 1
        else:
            pytest_arguments.extend(("-n", str(workers)))
            if not _has_xdist_distribution(pytest_arguments):
                # Unmarked tests retain load balancing, while an expensive
                # shared fixture can opt its consumers into one worker instead
                # of being rebuilt independently on every worker that gets one.
                pytest_arguments.extend(("--dist", "loadgroup"))

    history_path = None if namespace.no_history else Path(namespace.history)
    collection_shards = _prepare_collection_shards(
        str(namespace.collection),
        selection_arguments,
        workers=workers,
        history=history_path,
    )
    if collection_shards:
        pytest_arguments.append("--max-worker-restart=0")

    with tempfile.TemporaryDirectory(prefix="wreath-test-") as temporary:
        temporary_path = Path(temporary)
        trace_spec = _prepare_mutation_trace(namespace, temporary_path)
        if namespace.mutant == "sample" and trace_spec is None:
            raise ValueError("--mutant sample found no eligible controls")
        user_report = Path(namespace.report) if namespace.report else None
        activity_path = user_report
        if activity_path is None and trace_spec is not None:
            activity_path = temporary_path / "activity.json"
        selection_path = None
        baseline_wait_path = None
        prepared_mutation = None
        if trace_spec is not None:
            selection_path = temporary_path / "mutation-selection.json"
            selection_path.write_text(json.dumps(sorted(trace_spec.selected)), encoding="utf-8")
            baseline_wait_path = temporary_path / "mutation-baseline.json"
            prepared_mutation = _start_mutation_process(
                namespace,
                directory=temporary_path,
                baseline_wait=baseline_wait_path,
                baseline_stream=trace_spec.output_dir,
                selection=selection_path,
            )
        runner_config = RunnerConfig(
            grid=namespace.grid,
            workers=str(workers),
            slowest=namespace.slowest,
            report=str(activity_path) if activity_path is not None else None,
            history=None if namespace.no_history else namespace.history,
            mutation_mode=str(namespace.mutant),
            mutation_samples=(
                int(namespace.mutant_samples) if namespace.mutant in {"auto", "sample"} else 0
            ),
            mutation_activity=(
                str(prepared_mutation.activity_path) if prepared_mutation is not None else None
            ),
            stage_events=namespace.stage_events,
            collection_shards=collection_shards,
        )
        previous_config = os.environ.get(_CONFIG_ENV)
        previous_pid = os.environ.get(_CONTROLLER_PID_ENV)
        previous_trace = os.environ.get(_MUTATION_TRACE_ENV)
        os.environ[_CONFIG_ENV] = runner_config.as_json()
        os.environ[_CONTROLLER_PID_ENV] = str(os.getpid())
        if trace_spec is not None:
            os.environ[_MUTATION_TRACE_ENV] = _trace_environment(trace_spec)
        try:
            pytest_status = int(pytest.main(pytest_arguments))
        except BaseException:
            if prepared_mutation is not None:
                _stop_mutation_process(prepared_mutation)
            raise
        finally:
            if previous_config is None:
                os.environ.pop(_CONFIG_ENV, None)
            else:
                os.environ[_CONFIG_ENV] = previous_config
            if previous_pid is None:
                os.environ.pop(_CONTROLLER_PID_ENV, None)
            else:
                os.environ[_CONTROLLER_PID_ENV] = previous_pid
            if previous_trace is None:
                os.environ.pop(_MUTATION_TRACE_ENV, None)
            else:
                os.environ[_MUTATION_TRACE_ENV] = previous_trace
        activity_plugin = _ACTIVE_ACTIVITY_PLUGIN
        if namespace.mutant == "off":
            return pytest_status
        passed_tests = (
            activity_plugin.activity.counts()["passed"] if activity_plugin is not None else 0
        )
        if not passed_tests:
            if prepared_mutation is not None:
                _stop_mutation_process(prepared_mutation)
            if activity_plugin is None:
                print(
                    "Mutation confidence   not measured because no tests passed",
                    file=sys.stderr,
                )
            elif namespace.fuzz == "on":
                fuzz, fuzz_activity = _no_gold_fuzz()
                if user_report is not None:
                    _attach_fuzz_report(user_report, fuzz)
                activity_plugin.finish_pipeline(
                    MutationActivity(mode=str(namespace.mutant), state="no_green"),
                    fuzz_activity,
                )
            return pytest_status
        if namespace.mutant == "auto" and trace_spec is None:
            if activity_plugin is not None:
                mutation_activity = MutationActivity(mode="auto", state="unrated")
                if namespace.fuzz == "on":
                    fuzz, fuzz_activity = _no_gold_fuzz()
                    if user_report is not None:
                        _attach_fuzz_report(user_report, fuzz)
                    activity_plugin.finish_pipeline(mutation_activity, fuzz_activity)
                else:
                    activity_plugin.finish_mutation(mutation_activity)
            else:
                print(
                    "Mutation confidence   unrated: no eligible declared controls",
                    file=sys.stderr,
                )
            return pytest_status
        baseline_path = None
        if trace_spec is not None and activity_path is not None:
            if baseline_wait_path is None:
                raise RuntimeError("mutation baseline path was not prepared")
            if _write_reused_baseline(trace_spec, activity_path, baseline_wait_path):
                baseline_path = baseline_wait_path
            elif prepared_mutation is not None:
                _stop_mutation_process(prepared_mutation)
                prepared_mutation = None
        try:
            renderer = activity_plugin.renderer if activity_plugin is not None else None
            if prepared_mutation is not None:
                mutation, mutation_status = _finish_mutation_process(
                    namespace, prepared_mutation, renderer=renderer
                )
            else:
                mutation, mutation_status = _mutation_confidence(
                    namespace,
                    baseline=baseline_path,
                    selection=selection_path,
                    renderer=renderer,
                )
        except OSError, ValueError:
            if activity_plugin is not None:
                activity_plugin.finish_mutation(
                    MutationActivity(mode=str(namespace.mutant), state="error")
                )
            raise
        mutation_activity = _mutation_activity_from_report(str(namespace.mutant), mutation)
        if user_report is not None:
            _attach_mutation_report(user_report, mutation)
        if namespace.fuzz == "off":
            if activity_plugin is not None:
                activity_plugin.finish_mutation(mutation_activity)
            return pytest_status if pytest_status != 0 else mutation_status
        renderer = activity_plugin.renderer if activity_plugin is not None else None
        fuzz, fuzz_activity, fuzz_status = _fuzz_confidence(
            namespace,
            mutation,
            mutation_activity,
            renderer=renderer,
        )
        if user_report is not None:
            _attach_fuzz_report(user_report, fuzz)
        if activity_plugin is not None:
            activity_plugin.finish_pipeline(mutation_activity, fuzz_activity)
        if pytest_status != 0:
            return pytest_status
        if mutation_status != 0:
            return mutation_status
        return fuzz_status
