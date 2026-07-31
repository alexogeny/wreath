"""`wreath mutant` -- argument handling and the defaults that make it work
on an application nobody configured.

The defaults matter more than the flags. Somebody with a `Wreath()` app, a
`tests/` directory and no idea this tool existed should be able to type
`wreath mutant` and get an answer, so the sources default to whatever package
the project ships and the tests default to what `pytest` would have collected.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .model import Outcome
from .operators import OPERATORS
from .report import render, render_json
from .runner import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_TIMEOUT,
    ChangedUnavailable,
    execute,
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


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--path", action="append", metavar="PATH", default=[],
        help="file or directory of sources to mutate (repeatable; "
             "default: this project's own package)",
    )
    parser.add_argument(
        "--tests", action="append", metavar="PATH", default=[],
        help="test path passed to pytest (repeatable; default: tests/)",
    )
    parser.add_argument(
        "--operators", action="append", metavar="PREFIX", default=[],
        help=f"only operators starting with PREFIX (repeatable). "
             f"Available: {', '.join(OPERATORS)}",
    )
    parser.add_argument(
        "--only", action="append", metavar="TEXT", default=[],
        help="only mutants whose id contains TEXT (repeatable); "
             "an id is `operator@path:line`",
    )
    parser.add_argument(
        "--pytest-arg", action="append", metavar="ARG", default=[],
        help="extra argument for every pytest invocation (repeatable)",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS",
        help=f"per-mutant deadline; an overrun is reported undecided "
             f"(default {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES, metavar="N",
        help=f"decline a mutant that would run more than N tests "
             f"(default {DEFAULT_MAX_CANDIDATES})",
    )
    parser.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="stop after the first N mutants, in line order (a smoke test of "
             "the setup; to bound a pass onto code you just wrote, reach for "
             "--changed instead)",
    )
    parser.add_argument(
        "--changed", metavar="REF", default=None,
        help="only mutants on lines that differ from REF (e.g. HEAD, main). "
             "Untracked files count entirely. Composes with --limit.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--verbose", action="store_true",
        help="also list the mutants the suite caught, and what caught each",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="no per-mutant progress on stderr"
    )
    parser.add_argument(
        "--fail-on-survivor", action="store_true",
        help="exit 1 when anything survived. Off by default: this is a report, "
             "not a gate, and a survivor is a question rather than a verdict.",
    )


def execute_mutant(namespace: Any) -> int:
    repo = Path.cwd()
    roots = [Path(p).resolve() for p in namespace.path] or default_sources(repo)
    tests = list(namespace.tests) or default_tests(repo)
    missing = [str(root) for root in roots if not root.exists()]
    if missing:
        print(f"wreath: error: no such path: {', '.join(missing)}", file=sys.stderr)
        return 2

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
                changed=namespace.changed,
                progress=not namespace.quiet,
            )
        except ChangedUnavailable as error:
            print(f"wreath: error: --changed needs git: {error}", file=sys.stderr)
            return 2

    # A bound that selects nothing must not read as a clean run: the report
    # says `0 killed, 0 survived` and exits 0, which is a check that passes
    # because it has nothing to check. Each selector gets the advice that fits
    # it -- a wrong hint costs as much as no hint.
    if not report.verdicts:
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
    if namespace.fail_on_survivor and (
        report.by_outcome(Outcome.SURVIVED) or report.by_outcome(Outcome.UNREACHED)
    ):
        return 1
    return 0


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
