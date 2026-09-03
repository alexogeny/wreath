"""`wreath mutant` -- argument handling and the defaults that make it work
on an application nobody configured.

The defaults matter more than the flags. Somebody with a `Wreath()` app, a
`tests/` directory and no idea this tool existed should be able to type
`wreath mutant` and get an answer, so the sources default to whatever package
the project ships and the tests default to what `wreath test` collects.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .differential import DifferentialFuzzConfig
from .model import Outcome
from .operators import OPERATORS
from .report import render, render_json
from .runner import (
    DEFAULT_JOBS,
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MAXFAIL,
    DEFAULT_TIMEOUT,
    ChangedUnavailable,
    execute,
    read_baseline,
)

#: Directories that look like a project's own source, in preference order.
_SOURCE_HINTS = ("src", "app", "application")


def default_sources(repo: Path) -> list[Path]:
    """Where a project's own code most likely lives."""
    for hint in _SOURCE_HINTS:
        candidate = repo / hint
        if candidate.is_dir():
            return [candidate]
    packages = [
        child
        for child in sorted(repo.iterdir())
        if child.is_dir() and (child / "__init__.py").exists() and not child.name.startswith(".")
    ]
    return packages or [repo]


def default_tests(repo: Path) -> list[str]:
    for name in ("tests", "test"):
        if (repo / name).is_dir():
            return [name]
    return ["."]


def _exit_status(report: Any, *, fail_on_survivor: bool) -> int:
    """Turn one completed report into CLI status without re-running it."""
    differential = report.differential_fuzz
    if isinstance(differential, dict) and int(differential.get("failures", 0)):
        return 1
    if fail_on_survivor and (
        report.by_outcome(Outcome.SURVIVED) or report.by_outcome(Outcome.UNREACHED)
    ):
        return 1
    return 0


def _read_preselection(path: Path) -> tuple[frozenset[str], dict[str, Any] | None]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return frozenset(str(item) for item in value), None
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("selection must be a version 1 object with identifiers and metadata")
    identifiers = value.get("identifiers")
    metadata = value.get("metadata")
    if not isinstance(identifiers, list) or any(not isinstance(item, str) for item in identifiers):
        raise ValueError("selection identifiers must be a list of mutation id strings")
    if not isinstance(metadata, dict):
        raise ValueError("selection metadata must be an object")
    return (
        frozenset(str(identifier) for identifier in identifiers),
        {str(key): item for key, item in metadata.items()},
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--path",
        action="append",
        metavar="PATH",
        default=[],
        help="file or directory of sources to mutate (repeatable; "
        "default: this project's own package)",
    )
    parser.add_argument(
        "--tests",
        action="append",
        metavar="PATH",
        default=[],
        help="test path passed to the selected engine (repeatable; default: tests/)",
    )
    parser.add_argument(
        "--operators",
        action="append",
        metavar="PREFIX",
        default=[],
        help=f"only operators starting with PREFIX (repeatable). Available: {', '.join(OPERATORS)}",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="TEXT",
        default=[],
        help="only mutants whose id contains TEXT (repeatable); an id is `operator@path:line`",
    )
    parser.add_argument(
        "--pytest-arg",
        action="append",
        metavar="ARG",
        default=[],
        help="extra argument for every pytest invocation (repeatable)",
    )
    parser.add_argument(
        "--test-engine",
        choices=("pytest", "native"),
        default="native",
        help="execute mutant candidate tests with pytest or Wreath's strict native "
        "engine (default: native, including the PEP 669 reachability baseline)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"per-mutant deadline; an overrun is reported undecided (default {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=DEFAULT_MAX_CANDIDATES,
        metavar="N",
        help=f"decline a mutant that would run more than N tests "
        f"(default {DEFAULT_MAX_CANDIDATES})",
    )
    parser.add_argument(
        "--maxfail",
        type=int,
        default=DEFAULT_MAXFAIL,
        metavar="N",
        help=f"stop a mutant's tests after N failures (default {DEFAULT_MAXFAIL}); "
        f"0 runs every candidate test. A mutant is KILLED by the *first* "
        f"baseline-passing test that fails, so the rest decide nothing -- "
        f"measured on wreath.cache_control, 330 test executions become 73 "
        f"for identical verdicts. Raise it only to collect more killers in "
        f"--format json; the text report shows the first either way",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="total mutant-execution ceiling; controls left when it expires are "
        "reported undecided and do not fail the command (default: unlimited)",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        metavar="N",
        help="mutant children to execute concurrently (default: 1). Each child "
        "inherits one warmed interpreter, preserving pytest compatibility",
    )
    parser.add_argument(
        "--reclaim-workers",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--suite-workers",
        type=int,
        default=0,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="stop after the first N mutants, in line order (a smoke test of "
        "the setup; to bound a pass onto code you just wrote, reach for "
        "--changed instead)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        metavar="N",
        help="run a deterministic N-mutant sample drawn across every eligible "
        "source line; unlike --limit, this is a confidence sample rather "
        "than a file-head setup smoke test",
    )
    parser.add_argument(
        "--changed",
        metavar="REF",
        default=None,
        help="only mutants on lines that differ from REF (e.g. HEAD, main). "
        "Untracked files count entirely. Composes with --limit.",
    )
    parser.add_argument("--baseline", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--baseline-wait", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--baseline-stream", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--selection", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--activity-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--background-priority", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="also list the mutants the suite caught, and what caught each",
    )
    parser.add_argument("--quiet", action="store_true", help="no per-mutant progress on stderr")
    parser.add_argument(
        "--fail-on-survivor",
        action="store_true",
        help="exit 1 when anything survived. Off by default: this is a report, "
        "not a gate, and a survivor is a question rather than a verdict.",
    )
    parser.add_argument(
        "--differential-fuzz",
        action="store_true",
        help="probe surviving mutants with matching fuzz targets in isolated children",
    )
    parser.add_argument(
        "--fuzz-corpus-root",
        default=".wreath/fuzz/corpus",
        metavar="PATH",
        help="persistent differential fuzz corpus root",
    )
    parser.add_argument(
        "--fuzz-artifact-root",
        default=".wreath/fuzz/artifacts",
        metavar="PATH",
        help="minimized differential finding artifact root",
    )
    parser.add_argument("--fuzz-seed", type=int, default=None, metavar="N")
    parser.add_argument("--fuzz-cases", type=int, default=1_000, metavar="N")
    parser.add_argument("--fuzz-seconds", type=float, default=10.0, metavar="SECONDS")
    parser.add_argument(
        "--fuzz-replay-only",
        action="store_true",
        help="replay the persisted corpus against survivors without generating inputs",
    )
    parser.add_argument(
        "--fuzz-target",
        action="append",
        default=[],
        metavar="NAME",
        help="restrict differential probing to a named registered target (repeatable)",
    )


def execute_mutant(namespace: Any) -> int:
    repo = Path.cwd()
    roots = [Path(p).resolve() for p in namespace.path] or default_sources(repo)
    tests = list(namespace.tests) or default_tests(repo)
    missing = [str(root) for root in roots if not root.exists()]
    if missing:
        print(f"wreath: error: no such path: {', '.join(missing)}", file=sys.stderr)
        return 2
    if namespace.sample < 0:
        print("wreath: error: --sample must be a non-negative integer", file=sys.stderr)
        return 2
    if namespace.sample and namespace.limit:
        print(
            "wreath: error: --sample and --limit are alternative bounds; choose one",
            file=sys.stderr,
        )
        return 2
    if namespace.budget < 0:
        print("wreath: error: --budget must be non-negative", file=sys.stderr)
        return 2
    if namespace.jobs < 1:
        print("wreath: error: --jobs must be at least 1", file=sys.stderr)
        return 2
    if namespace.fuzz_cases < 1:
        print("wreath: error: --fuzz-cases must be at least 1", file=sys.stderr)
        return 2
    if namespace.fuzz_seconds <= 0:
        print("wreath: error: --fuzz-seconds must be positive", file=sys.stderr)
        return 2
    if namespace.fuzz_seed is not None and not 0 <= namespace.fuzz_seed < 2**64:
        print(
            "wreath: error: --fuzz-seed must be from 0 through 2**64 - 1",
            file=sys.stderr,
        )
        return 2
    if namespace.differential_fuzz and namespace.fuzz_target:
        from wreath._fuzz_targets import TARGETS

        available = {target.name for target in TARGETS}
        unknown = sorted(set(namespace.fuzz_target) - available)
        if unknown:
            print(
                f"wreath: error: unknown --fuzz-target {unknown[0]!r}; choose one of: "
                f"{', '.join(sorted(available))}",
                file=sys.stderr,
            )
            return 2
    preselected: frozenset[str] | None = None
    selection_metadata: dict[str, Any] | None = None
    if namespace.selection is not None:
        try:
            preselected, selection_metadata = _read_preselection(Path(namespace.selection))
        except (OSError, ValueError) as error:
            print(f"wreath: error: invalid mutation selection: {error}", file=sys.stderr)
            return 2
    if namespace.background_priority and hasattr(os, "nice"):
        # ``wreath test`` runs this interpreter beside its ordinary workers.
        # Let the semantic test run own the cores; planning and live probes can
        # consume spare cycles and will regain the machine when pytest exits.
        os.nice(10)

    with tempfile.TemporaryDirectory(prefix="wreath-mutant-") as tmp:
        try:
            report = execute(
                repo=repo,
                roots=roots,
                tests=tests,
                workdir=Path(tmp),
                operators=tuple(namespace.operators),
                only=tuple(namespace.only),
                extra=tuple(namespace.pytest_arg),
                timeout=namespace.timeout,
                max_candidates=namespace.max_candidates,
                limit=namespace.limit,
                sample=namespace.sample,
                changed=namespace.changed,
                progress=not namespace.quiet,
                maxfail=namespace.maxfail,
                baseline=(
                    read_baseline(Path(namespace.baseline))
                    if namespace.baseline is not None
                    else None
                ),
                baseline_wait=(
                    Path(namespace.baseline_wait) if namespace.baseline_wait is not None else None
                ),
                baseline_stream=(
                    Path(namespace.baseline_stream)
                    if namespace.baseline_stream is not None
                    else None
                ),
                budget=namespace.budget,
                jobs=namespace.jobs,
                reclaim_workers=namespace.reclaim_workers,
                suite_workers=namespace.suite_workers,
                preselected=preselected,
                activity_file=(
                    Path(namespace.activity_file) if namespace.activity_file is not None else None
                ),
                test_engine=namespace.test_engine,
                differential_fuzz=(
                    DifferentialFuzzConfig(
                        Path(namespace.fuzz_corpus_root),
                        Path(namespace.fuzz_artifact_root),
                        seed=namespace.fuzz_seed,
                        max_cases=namespace.fuzz_cases,
                        max_seconds=namespace.fuzz_seconds,
                        target_names=tuple(namespace.fuzz_target),
                        generate=not namespace.fuzz_replay_only,
                    )
                    if namespace.differential_fuzz
                    else None
                ),
            )
        except ChangedUnavailable as error:
            print(f"wreath: error: --changed needs git: {error}", file=sys.stderr)
            return 2
    if selection_metadata is not None:
        report.selection = selection_metadata

    # A bound that selects nothing must not read as a clean run: the report
    # says `0 killed, 0 survived` and exits 0, which is a check that passes
    # because it has nothing to check. Each selector gets the advice that fits
    # it -- a wrong hint costs as much as no hint.
    if not report.verdicts:
        if namespace.sample:
            print(
                f"wreath: error: --sample {namespace.sample} found no eligible "
                "mutations under the selected paths and filters",
                file=sys.stderr,
            )
            return 2
        if namespace.only:
            print(
                f"wreath: error: --only {', '.join(repr(s) for s in namespace.only)} "
                f"matched no mutations. The line in `operator@path:line` is where "
                f"the operator anchors -- an operand inside a compound condition, a "
                f"keyword's *value* in a declaration -- which is often not the line "
                f"the control reads on. Run without --only and copy an id from the "
                f"report.",
                file=sys.stderr,
            )
            return 2
        if namespace.changed is not None:
            print(
                f"wreath: error: --changed {namespace.changed!r} matched no "
                f"mutations: nothing under the scanned path differs from that ref, "
                f"or what differs carries no control. Check `git diff --stat "
                f"{namespace.changed}` against the --path you gave. Note that "
                f"`wreath._mutant` is never mutated (it is this tool).",
                file=sys.stderr,
            )
            return 2
    if namespace.format == "json":
        print(render_json(report))
    else:
        print(render(report, verbose=namespace.verbose))
    return _exit_status(report, fail_on_survivor=namespace.fail_on_survivor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wreath-mutant",
        description="Remove one declared control at a time and see whether the "
        "tests notice. A report, not a gate.",
    )
    add_arguments(parser)
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return execute_mutant(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
