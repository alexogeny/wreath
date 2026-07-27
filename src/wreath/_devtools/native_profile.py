"""Run repeatable native diagnostics around an arbitrary Wreath workload.

The command is deliberately a small argv-only wrapper around established system
profilers. It records enough metadata to reproduce a run and never invokes a
shell.

    uv run wreath-native-profile cpu -- python benchmarks/example.py
    uv run wreath-native-profile counters -- uv run pytest tests/test_native_perf.py
    uv run wreath-native-profile cache --output .profiles/router -- python bench.py

Build Wreath with `WREATH_NATIVE_PROFILE=1 uv sync --reinstall-package wreath` first
to retain symbols and frame pointers in the optimized native extensions.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final


@dataclass(frozen=True)
class Profile:
    tool: str
    purpose: str
    artifact_name: str
    arguments: tuple[str, ...]
    next_step: str


PROFILES: Final[dict[str, Profile]] = {
    "cpu": Profile(
        "perf",
        "sample CPU stacks to locate hot native and Python call paths",
        "perf.data",
        ("record", "--call-graph", "dwarf", "-o", "{artifact}", "--"),
        "perf report -i {artifact}",
    ),
    "counters": Profile(
        "perf",
        "measure branches, cache misses, faults, switches, and instructions",
        "perf-stat.txt",
        ("stat", "-d", "-d", "-o", "{artifact}", "--"),
        "cat {artifact}",
    ),
    "cache": Profile(
        "valgrind",
        "attribute instruction, data-cache, and branch-prediction misses",
        "cachegrind.out",
        ("--tool=cachegrind", "--branch-sim=yes", "--cachegrind-out-file={artifact}"),
        "cg_annotate {artifact}",
    ),
    "memory": Profile(
        "valgrind",
        "track native heap growth and peak allocation sites",
        "massif.out",
        ("--tool=massif", "--stacks=yes", "--massif-out-file={artifact}"),
        "ms_print {artifact}",
    ),
    "calls": Profile(
        "valgrind",
        "count native calls and their inclusive instruction cost",
        "callgrind.out",
        ("--tool=callgrind", "--collect-jumps=yes", "--callgrind-out-file={artifact}"),
        "callgrind_annotate --inclusive=yes {artifact}",
    ),
    "syscalls": Profile(
        "strace",
        "summarize syscall count, time, and errors across threads and children",
        "strace-summary.txt",
        ("-f", "-c", "-o", "{artifact}", "--"),
        "cat {artifact}",
    ),
}


@dataclass(frozen=True)
class Plan:
    profile: str
    purpose: str
    tool: str
    workload: tuple[str, ...]
    argv: tuple[str, ...]
    artifact: Path
    next_step: str


def build_plan(
    profile: str,
    workload: list[str] | tuple[str, ...],
    output: Path,
    *,
    tool: str | None = None,
) -> Plan:
    """Build a profiler invocation without executing it."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    if not workload:
        raise ValueError("workload is required")

    spec = PROFILES[profile]
    executable = tool or spec.tool
    artifact = output / spec.artifact_name
    substitutions = {"artifact": str(artifact)}
    profiler_args = tuple(argument.format_map(substitutions) for argument in spec.arguments)
    return Plan(
        profile=profile,
        purpose=spec.purpose,
        tool=executable,
        workload=tuple(workload),
        argv=(executable, *profiler_args, *workload),
        artifact=artifact,
        next_step=spec.next_step.format_map(substitutions),
    )


def _default_output(profile: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(".profiles") / "native" / f"{timestamp}-{profile}"


def _metadata(plan: Plan, status: str) -> dict[str, object]:
    return {
        "profile": plan.profile,
        "purpose": plan.purpose,
        "tool": plan.tool,
        "workload": list(plan.workload),
        "argv": list(plan.argv),
        "artifact": str(plan.artifact),
        "next_step": plan.next_step,
        "status": status,
        "recorded_at": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "cwd": os.getcwd(),
    }


def _write_metadata(output: Path, metadata: dict[str, object]) -> None:
    (output / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _tool_exists(tool: str) -> bool:
    candidate = Path(tool)
    if candidate.parent != Path("."):
        return candidate.is_file() and os.access(candidate, os.X_OK)
    return shutil.which(tool) is not None


def _print_profiles() -> None:
    width = max(len(name) for name in PROFILES)
    for name, profile in PROFILES.items():
        print(f"{name:<{width}}  {profile.tool:<8} {profile.purpose}")


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw_args:
        separator = raw_args.index("--")
        parser_args = raw_args[:separator]
        workload = raw_args[separator + 1 :]
    else:
        parser_args = raw_args
        workload = []

    parser = argparse.ArgumentParser(
        prog="wreath-native-profile",
        description="Find CPU, cache, allocation, call, and syscall hotspots in a workload.",
    )
    parser.add_argument("profile", nargs="?", choices=tuple(PROFILES))
    parser.add_argument("--output", type=Path, help="result directory")
    parser.add_argument("--tool", help="profiler executable override")
    parser.add_argument("--dry-run", action="store_true", help="write metadata but do not execute")
    parser.add_argument("--list", action="store_true", help="list available profiles and exit")
    args = parser.parse_args(parser_args)

    if args.list:
        _print_profiles()
        return 0
    if args.profile is None:
        parser.error("a profile is required (or use --list)")

    if not workload:
        print("wreath-native-profile: workload is required after --", file=sys.stderr)
        return 2

    output = args.output or _default_output(args.profile)
    plan = build_plan(args.profile, workload, output, tool=args.tool)
    output.mkdir(parents=True, exist_ok=True)
    command = shlex.join(plan.argv)
    print(f"wreath-native-profile: {plan.purpose}\n$ {command}")

    if args.dry_run:
        _write_metadata(output, _metadata(plan, "dry-run"))
        print(f"metadata: {output / 'metadata.json'}")
        return 0

    if not _tool_exists(plan.tool):
        metadata = _metadata(plan, "tool-not-found")
        _write_metadata(output, metadata)
        expected_tool = PROFILES[plan.profile].tool
        print(
            f"wreath-native-profile: {plan.tool!r} was not found; "
            f"install {expected_tool} or pass --tool",
            file=sys.stderr,
        )
        return 2

    metadata = _metadata(plan, "running")
    _write_metadata(output, metadata)
    started = datetime.now(UTC)
    try:
        completed = subprocess.run(plan.argv, check=False)
    except OSError as exc:
        metadata.update(status="failed-to-start", error=str(exc))
        _write_metadata(output, metadata)
        print(f"wreath-native-profile: could not start profiler: {exc}", file=sys.stderr)
        return 2

    metadata.update(
        status="completed" if completed.returncode == 0 else "workload-failed",
        returncode=completed.returncode,
        elapsed_seconds=(datetime.now(UTC) - started).total_seconds(),
    )
    _write_metadata(output, metadata)
    print(f"artifact: {plan.artifact}\nnext: {plan.next_step}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
