"""Collector for the source tier: walk a tree and apply the code rules.

Separate from `sources.py` because the input is different in kind -- that tier
reconstructs the HTML an application *emits*, this one reads the application's
own modules. Sharing a collector would have meant one function that takes an
app object or a path and does something unrelated with each.

## What is skipped, and why it is skipped by default

A security scan that reports its own fixtures is a scan nobody reads. Virtual
environments, caches, build output and version-control metadata are never the
code under audit. `tests/` is skipped too, and that one is a judgement call:
test code legitimately hardcodes secrets, seeds PRNGs deterministically and
compares tokens with `==`, so scanning it produces findings that are all
correct and all useless. `--tests` includes it for the rare case you want it.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .model import Report
from .rules import scan_source

#: Directory names never worth scanning.
SKIP_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", ".ty_cache", ".hypothesis",
    "build", "dist", ".eggs", "site-packages", ".tox", ".nox",
})

#: Skipped unless `--tests`. See the module docstring.
TEST_DIRECTORIES = frozenset({"tests", "test"})


def python_files(roots: Iterable[str | Path], *, include_tests: bool = False) -> list[Path]:
    """Every `.py` file under `roots`, in a stable order."""
    skip = set(SKIP_DIRECTORIES) if include_tests else SKIP_DIRECTORIES | TEST_DIRECTORIES
    found: list[Path] = []
    for root in roots:
        base = Path(root)
        if base.is_file():
            found.append(base)
            continue
        for path in base.rglob("*.py"):
            if any(part in skip for part in path.parts):
                continue
            found.append(path)
    return sorted(dict.fromkeys(found))


def scan_paths(
    roots: Iterable[str | Path], *, include_tests: bool = False, base: Path | None = None
) -> Report:
    """Apply every code rule to every module under `roots`."""
    report = Report()
    anchor = base or Path.cwd()
    for path in python_files(roots, include_tests=include_tests):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            surface = str(path.relative_to(anchor))
        except ValueError:
            surface = str(path)
        report.extend(scan_source(source, surface=surface))
    return report
