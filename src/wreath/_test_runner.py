"""Pytest-compatible test activity, timing, and heat-map reporting.

``wreath test`` deliberately keeps pytest as the semantic engine.  Fixtures,
plugins, collection, capture, and tracebacks therefore remain pytest's, while
this module owns the inexpensive part around them: a run model, duration
statistics, an interactive file grid, and bounded history for future scheduling.

The activity plugin is activated through :mod:`wreath._pytest_plugin` only when
the command places a controller PID and configuration in the environment.
Ordinary ``pytest`` imports this module never, and nested pytest subprocesses do
not inherit the UI merely because their parent is itself running a test.
"""

from __future__ import annotations

import atexit
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
_DEFAULT_HISTORY = ".wreath/test-history.json"
_MAX_AUTO_WORKERS = 6
_MAX_AUTO_MUTANT_WORKERS = 3

_ENTER_SCREEN = "\x1b[?1049h\x1b[?25l"
_LEAVE_SCREEN = "\x1b[?25h\x1b[?1049l"
_CLEAR_SCREEN = "\x1b[H\x1b[2J"
_RESET = "\x1b[0m"


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """Options consumed by Wreath rather than forwarded to pytest."""

    grid: str = "auto"
    workers: str = "auto"
    slowest: int = 5
    report: str | None = None
    history: str | None = _DEFAULT_HISTORY
    mutation_mode: str = "off"
    mutation_samples: int = 0
    mutation_activity: str | None = None

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
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> RunnerConfig:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("test activity configuration must be an object")
        grid = value.get("grid", "auto")
        workers = value.get("workers", "auto")
        slowest = value.get("slowest", 5)
        report = value.get("report")
        history = value.get("history", _DEFAULT_HISTORY)
        mutation_mode = value.get("mutation_mode", "off")
        mutation_samples = value.get("mutation_samples", 0)
        mutation_activity = value.get("mutation_activity")
        if grid not in {"auto", "always", "never"}:
            raise ValueError(f"unknown test grid mode {grid!r}")
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
        return cls(
            grid=grid,
            workers=workers,
            slowest=slowest,
            report=report,
            history=history,
            mutation_mode=mutation_mode,
            mutation_samples=mutation_samples,
            mutation_activity=mutation_activity,
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
    baseline_failures: int = 0
    live_probes: int = 0
    live_completed: int = 0
    live_cancelled_at_seal: int = 0
    live_first_started_seconds: float | None = None


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
            utilization = float(durations["total_seconds"]) / (
                self.wall_seconds * self.workers
            )
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
            "outliers": [
                {"nodeid": test.nodeid, "seconds": test.duration}
                for test in outliers
            ],
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


def _heat_bucket(duration: float, completed: list[float]) -> int:
    if not completed:
        return 0
    ordered = sorted(completed)
    below = sum(value <= duration for value in ordered)
    return min(4, max(0, math.ceil(below / len(ordered) * 5) - 1))


_COLOURS = {
    "untested": (238, 238, 238, 238, 238),
    "running": (24, 31, 38, 45, 51),
    "passed": (22, 28, 34, 40, 46),
    "mixed": (58, 94, 130, 166, 202),
    "skipped": (58, 94, 130, 166, 202),
    "failed": (52, 88, 124, 160, 196),
    "error": (52, 88, 124, 160, 196),
    "mutating": (53, 89, 125, 161, 201),
    "verified": (136, 178, 214, 220, 226),
}

_SYMBOLS = {
    "untested": "·",
    "running": "◆",
    "passed": "■",
    "mixed": "▲",
    "skipped": "▲",
    "failed": "✕",
    "error": "✕",
    "mutating": "▣",
    "verified": "▰",
}


def _tile(
    file_state: FileState,
    *,
    completed: list[float],
    colour: bool,
    mutating: bool = False,
    verified: bool = False,
) -> str:
    outcome = file_state.outcome
    if outcome == "passed":
        if mutating:
            outcome = "mutating"
        elif verified:
            outcome = "verified"
    symbol = "◆" if outcome == "running" else "■"
    if not colour:
        return _SYMBOLS[outcome]
    if outcome in {"mutating", "verified"}:
        symbol = _SYMBOLS[outcome]
    bucket = _heat_bucket(file_state.duration, completed)
    code = _COLOURS[outcome][bucket]
    return f"\x1b[38;5;{code}m{symbol}{_RESET}"


def _legend_tile(outcome: str, bucket: int, *, colour: bool) -> str:
    if not colour:
        return _SYMBOLS[outcome]
    code = _COLOURS[outcome][bucket]
    symbol = (
        _SYMBOLS[outcome]
        if outcome in {"running", "mutating", "verified"}
        else "■"
    )
    return f"\x1b[38;5;{code}m{symbol}{_RESET}"


def _rating_text(label: str, tone: str, *, colour: bool) -> str:
    symbols = {
        "good": ("■", 46),
        "attention": ("✕", 196),
        "warning": ("▲", 202),
        "incomplete": ("◆", 51),
        "neutral": ("·", 238),
    }
    symbol, code = symbols[tone]
    result = f"{symbol} {label}"
    if colour:
        return f"\x1b[38;5;{code}m{result}{_RESET}"
    return result


def _mutation_lines(mutation: MutationActivity, *, colour: bool) -> list[str]:
    mutation_tile = _legend_tile("mutating", 4, colour=colour)
    if mutation.state == "running":
        scope = f" · {mutation.total} sampled controls" if mutation.total else ""
        action = (
            "testing controls"
            if mutation.mutating_files
            else "preparing controls beside tests"
        )
        return [
            f"  Mutation   {mutation_tile} {mutation.mode}{scope} · {action}"
        ]
    if mutation.state == "unrated":
        return [f"  Mutation   {mutation.mode} · no eligible declared controls"]
    if mutation.state == "error":
        return [f"  Mutation   {mutation.mode} · confidence phase failed"]
    if mutation.state == "no_green":
        return [f"  Mutation   {mutation.mode} · not measured; no tests passed"]
    rating = _rating_text(
        mutation.rating_label,
        mutation.rating_tone,
        colour=colour,
    )
    counts = mutation.counts
    killed = counts.get("killed", 0)
    survived = counts.get("survived", 0)
    unreached = counts.get("unreached", 0)
    undecided = counts.get("timeout", 0) + counts.get("error", 0)
    equivalent = counts.get("equivalent", 0)
    lines = [
        f"  Mutation   {mutation.mode} · {rating} · {mutation.rating_action}",
        (
            f"  {killed} killed · {survived} survived · {unreached} unreached · "
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


def render_activity(
    activity: RunActivity,
    *,
    width: int,
    height: int,
    colour: bool,
    slowest: int,
    mutation: MutationActivity | None = None,
) -> str:
    """Render one stable snapshot of the current run."""
    counts = activity.counts()
    elapsed = activity.wall_seconds or max(0.0, time.perf_counter() - activity.started)
    file_states = sorted(activity.files.values(), key=lambda file_state: file_state.path)
    completed = [
        file_state.duration for file_state in file_states if file_state.finished > 0
    ]
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
        width_columns = max(1, (max(20, width) - 2) // 2)
        mutation_rows = 3 if mutation is not None else 0
        max_rows = max(1, height - 11 - mutation_rows - min(slowest, 5))
        columns = min(53, width_columns)
        columns = min(width_columns, max(columns, math.ceil(len(file_states) / max_rows)))
        for start in range(0, len(file_states), columns):
            row = file_states[start : start + columns]
            lines.append("  " + " ".join(
                _tile(
                    file_state,
                    completed=completed,
                    colour=colour,
                    mutating=(
                        mutation is not None
                        and file_state.path in mutation.mutating_files
                    ),
                    verified=(
                        mutation is not None
                        and file_state.path in mutation.verified_files
                    ),
                )
                for file_state in row
            ))
    else:
        lines.append("  collecting tests …")

    heat_scale = " ".join(
        _legend_tile("passed", bucket, colour=colour) for bucket in range(5)
    )
    outcomes = " · ".join((
        f"{_legend_tile('untested', 2, colour=colour)} queued",
        f"{_legend_tile('running', 4, colour=colour)} running",
        f"{_legend_tile('passed', 4, colour=colour)} pass",
        f"{_legend_tile('mutating', 4, colour=colour)} mutation testing",
        f"{_legend_tile('verified', 4, colour=colour)} pass + killed mutant",
        f"{_legend_tile('mixed', 4, colour=colour)} skip/mixed",
        f"{_legend_tile('failed', 4, colour=colour)} fail/error",
    ))
    lines.extend(("", f"  Duration   Less {heat_scale} More", f"  Outcome    {outcomes}"))
    if mutation is not None:
        lines.extend(_mutation_lines(mutation, colour=colour))
    finished = [test for test in activity.tests.values() if test.finished]
    durations = _duration_summary(finished)
    if finished:
        lines.append(
            "  "
            f"average {_format_duration(float(durations['mean_seconds']))} · "
            f"median {_format_duration(float(durations['median_seconds']))} · "
            f"p95 {_format_duration(float(durations['p95_seconds']))} · "
            f"p99 {_format_duration(float(durations['p99_seconds']))} · "
            f"{durations['outliers']} outliers · "
            f"test time {_format_duration(float(durations['total_seconds']))}"
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
    """Throttled alternate-screen renderer with a static final snapshot."""

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
        self.interactive = mode == "always" or (
            mode == "auto"
            and bool(getattr(stream, "isatty", lambda: False)())
            and os.environ.get("TERM", "") != "dumb"
            and not os.environ.get("CI")
        )
        self.colour = self.interactive and "NO_COLOR" not in os.environ
        self.active = False
        self.disabled = False
        self.last_draw = 0.0
        self._mutation_announced = False
        self.mutation: MutationActivity | None = None

    def start(self) -> None:
        if not self.interactive or self.active or self.disabled:
            return
        if self._write(_ENTER_SCREEN):
            self.active = True
            atexit.register(self.restore)
            self.draw(force=True)

    def draw(self, *, force: bool = False) -> None:
        if not self.active or self.disabled:
            return
        now = time.monotonic()
        if not force and now - self.last_draw < 0.05:
            return
        self.last_draw = now
        size = shutil.get_terminal_size((100, 30))
        snapshot = render_activity(
            self.activity,
            width=size.columns,
            height=size.lines,
            colour=self.colour,
            slowest=self.slowest,
            mutation=self.mutation,
        )
        self._write(_CLEAR_SCREEN + snapshot)

    def finish(self) -> None:
        self.finish_with_mutation(None)

    def defer(self) -> None:
        """Leave the live screen but wait for mutation before printing final state."""
        if self.active:
            self.draw(force=True)
            self.restore()

    def mutation_progress(
        self,
        mode: str,
        total: int,
        *,
        mutating_files: frozenset[str],
        verified_files: frozenset[str],
    ) -> None:
        """Animate live per-file mutation state in the same activity grid."""
        if self.disabled:
            return
        self.mutation = MutationActivity(
            mode=mode,
            state="running",
            total=total,
            mutating_files=mutating_files,
            verified_files=verified_files,
        )
        if not self.interactive:
            if not self._mutation_announced:
                scope = f" · {total} sampled controls" if total else ""
                self._write(f"\nMutation activity   {mode}{scope}\n")
                self._mutation_announced = True
            return
        if not self.active and self._write(_ENTER_SCREEN):
            self.active = True
        self.draw(force=True)

    def finish_mutation_progress(self) -> None:
        return

    def finish_with_mutation(self, mutation: MutationActivity | None) -> None:
        self.mutation = mutation
        if self.active:
            self.draw(force=True)
            self.restore()
        size = shutil.get_terminal_size((100, 30))
        snapshot = render_activity(
            self.activity,
            width=size.columns,
            height=size.lines,
            colour=self.colour,
            slowest=self.slowest,
            mutation=mutation,
        )
        # pytest's progress line deliberately has no newline until its terminal
        # summary. Session-finish hooks run before that summary, so start our
        # static report on a fresh line instead of attaching it to ``[100%]``.
        self._write("\n" + snapshot)

    def restore(self) -> None:
        if not self.active:
            return
        self.active = False
        self._write(_LEAVE_SCREEN)

    def _write(self, value: str) -> bool:
        try:
            self.stream.write(value)
            self.stream.flush()
        except (OSError, ValueError):
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
            "mean_seconds": old_mean + (seconds - old_mean) / samples,
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
            "mean_seconds": old_mean + (seconds - old_mean) / samples,
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
    except (OSError, ValueError):
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
            if isinstance(seconds, int | float) and seconds >= 0.0:
                result[name] = float(seconds)
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


class HistoricalSchedulerPlugin:
    """Dispatch longest-known tests first without letting a worker hoard them."""

    def __init__(self, test_weights: dict[str, float], file_weights: dict[str, float]) -> None:
        self.test_weights = test_weights
        self.file_weights = file_weights

    def pytest_xdist_make_scheduler(self, config: Any, log: Any) -> Any:
        from xdist.scheduler.load import LoadScheduling

        test_weights = self.test_weights
        file_weights = self.file_weights

        class HistoricalLoadScheduling(LoadScheduling):
            """Two queued items per worker, refilled from an LPT priority list."""

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
                self.pending[:] = sorted(
                    range(len(collection)),
                    key=lambda index: _historical_weight(
                        collection[index], test_weights, file_weights
                    ),
                    reverse=True,
                )
                if not self.pending:
                    return
                # One item per worker per pass spreads the heaviest controls
                # across workers. A second queued item is required by xdist's
                # worker protocol so it can send the next item before teardown.
                for _pass in range(2):
                    for node in self.nodes:
                        self._send_tests(node, 1)
                        if not self.pending:
                            break
                    if not self.pending:
                        break

            def check_schedule(self, node: Any, duration: float = 0) -> None:
                if node.shutting_down:
                    return
                node_pending = self.node2pending[node]
                if self.pending and len(node_pending) < 2:
                    self._send_tests(node, 2 - len(node_pending))
                elif not self.pending:
                    node.shutdown()

        return HistoricalLoadScheduling(config, log)


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
            [f"{path}:{line}", list(nodes)]
            for (path, line), nodes in self.tracer.index().items()
        ]
        _atomic_json(self.output, {"hits": hits})


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
        self.mutation_event_state = _MutationEventState(
            total=config.mutation_samples
        )

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
        )

    def pytest_sessionstart(self, session: Any) -> None:
        self.renderer.start()

    def pytest_collection_finish(self, session: Any) -> None:
        self.activity.collect(tuple(item.nodeid for item in session.items))
        self.renderer.draw(force=True)

    def pytest_xdist_node_collection_finished(self, node: Any, ids: Any) -> None:
        self.activity.collect(tuple(str(nodeid) for nodeid in ids))
        self.renderer.draw(force=True)

    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        self.activity.start_test(nodeid)
        self._sync_mutation()
        self.renderer.draw()

    def pytest_runtest_logreport(self, report: Any) -> None:
        self.activity.add_report(report)
        self._sync_mutation()
        self.renderer.draw()

    def pytest_runtest_logfinish(self, nodeid: str, location: Any) -> None:
        self.activity.finish_test(nodeid)
        self._sync_mutation()
        self.renderer.draw()

    def pytest_collectreport(self, report: Any) -> None:
        if getattr(report, "failed", False):
            self.activity.collection_error(
                str(report.nodeid), float(getattr(report, "duration", 0.0))
            )
            self.renderer.draw()

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
    if hasattr(config, "workerinput"):
        return
    if not _controller_process() or config.pluginmanager.hasplugin(_PLUGIN_NAME):
        return
    workers = _workers_from_pytest_config(config, runner_config.workers)

    if runner_config.history and workers > 1:
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
    return any(
        argument == "--dist" or argument.startswith("--dist=")
        for argument in arguments
    )


def _resolve_workers(raw: str) -> int:
    if raw == "auto":
        return min(_MAX_AUTO_WORKERS, os.cpu_count() or 1)
    try:
        workers = int(raw)
    except ValueError:
        raise ValueError(f"--workers expects 'auto' or a positive integer, got {raw!r}") from None
    if workers < 1:
        raise ValueError("--workers must be at least 1")
    return workers


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
    arguments = ["--format", "json"]
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
    if mode in {"auto", "sample"}:
        arguments.extend(("--budget", str(namespace.mutant_budget)))
    return arguments


def _resolve_mutant_workers(raw: str) -> int:
    if raw == "auto":
        return min(_MAX_AUTO_MUTANT_WORKERS, os.cpu_count() or 1)
    try:
        workers = int(raw)
    except ValueError:
        raise ValueError(
            f"--mutant-workers expects 'auto' or a positive integer, got {raw!r}"
        ) from None
    if workers < 1:
        raise ValueError("--mutant-workers must be at least 1")
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
        _write_mutation_sample_cache(
            history_path, cache_key, selected, watched, whole_files
        )
    output_dir = directory / "mutation-trace"
    output_dir.mkdir()
    return MutationTraceSpec(selected, watched, whole_files, output_dir)


def _read_mutation_sample_cache(
    path: Path, key: dict[str, Any]
) -> tuple[frozenset[str], dict[str, frozenset[int]], frozenset[str]] | None:
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
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
    except (OSError, ValueError):
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
            "watched": {
                path: sorted(lines) for path, lines in spec.watched.items()
            },
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
    except (OSError, ValueError):
        return False
    documents: list[dict[str, Any]] = []
    for path in spec.output_dir.glob("trace-*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
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


def _format_mutation_rating(label: str, tone: str, stream: TextIO) -> str:
    colour = (
        bool(getattr(stream, "isatty", lambda: False)())
        and "NO_COLOR" not in os.environ
    )
    return _rating_text(label, tone, colour=colour)


@dataclass(slots=True)
class _MutationEventState:
    processed: int = 0
    total: int = 0
    mutating_files: set[str] = field(default_factory=set)
    verified_files: set[str] = field(default_factory=set)
    active: dict[int, set[str]] = field(default_factory=dict)


def _consume_mutation_events(path: Path, state: _MutationEventState) -> None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    lines = raw.splitlines()
    if raw and not raw.endswith("\n"):
        lines = lines[:-1]
    for line in lines[state.processed:]:
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("mutation activity event must be an object")
        event = value.get("event")
        if event == "planned":
            state.total = int(value.get("total", 0))
        elif event == "started":
            tests = value.get("tests")
            if isinstance(tests, list):
                state.active[int(value.get("ordinal", 0))] = {
                    str(path) for path in tests
                }
                state.mutating_files = set().union(*state.active.values())
        elif event == "finished":
            state.active.pop(int(value.get("ordinal", 0)), None)
            state.mutating_files = (
                set().union(*state.active.values()) if state.active else set()
            )
            if value.get("outcome") == "killed":
                killers = value.get("killers")
                if isinstance(killers, list):
                    state.verified_files.update(
                        _path_from_nodeid(str(nodeid)) for nodeid in killers
                    )
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
            verified.update(
                _path_from_nodeid(str(nodeid))
                for nodeid in killers
            )
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
    raw_report = mutation.output_path.read_text(encoding="utf-8")
    if returncode != 0:
        raise ValueError(
            "mutation confidence phase failed; its diagnostic is printed above"
        )
    try:
        report = json.loads(raw_report)
    except (TypeError, ValueError) as error:
        raise ValueError("mutation confidence returned invalid JSON") from error
    if not isinstance(report, dict):
        raise ValueError("mutation confidence report must be an object")
    report["baseline_reused"] = mutation.baseline_reused
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


def execute(namespace: Any) -> int:
    """Run pytest with Wreath's activity controller and return its exit code."""
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
            selection_path.write_text(
                json.dumps(sorted(trace_spec.selected)), encoding="utf-8"
            )
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
                int(namespace.mutant_samples)
                if namespace.mutant in {"auto", "sample"}
                else 0
            ),
            mutation_activity=(
                str(prepared_mutation.activity_path)
                if prepared_mutation is not None
                else None
            ),
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
            activity_plugin.activity.counts()["passed"]
            if activity_plugin is not None
            else 0
        )
        if not passed_tests:
            if prepared_mutation is not None:
                _stop_mutation_process(prepared_mutation)
            if activity_plugin is None:
                print(
                    "Mutation confidence   not measured because no tests passed",
                    file=sys.stderr,
                )
            return pytest_status
        if namespace.mutant == "auto" and trace_spec is None:
            if activity_plugin is not None:
                activity_plugin.finish_mutation(
                    MutationActivity(mode="auto", state="unrated")
                )
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
        except (OSError, ValueError):
            if activity_plugin is not None:
                activity_plugin.finish_mutation(
                    MutationActivity(mode=str(namespace.mutant), state="error")
                )
            raise
        mutation_activity = _mutation_activity_from_report(
            str(namespace.mutant), mutation
        )
        if user_report is not None:
            _attach_mutation_report(user_report, mutation)
        if activity_plugin is not None:
            activity_plugin.finish_mutation(mutation_activity)
        return pytest_status if pytest_status != 0 else mutation_status
