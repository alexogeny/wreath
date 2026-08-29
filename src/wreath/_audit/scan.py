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

from .model import Finding, Report
from .rules import scan_source

#: Directory names never worth scanning.
SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".ty_cache",
        ".hypothesis",
        "build",
        "dist",
        ".eggs",
        "site-packages",
        ".tox",
        ".nox",
    }
)

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


#: `path -> (mtime_ns, size, surface, findings)`, one entry per file.
#:
#: Keyed on the path alone and *replaced* on a miss, so a file that changes
#: evicts its own stale findings instead of leaving a row behind per revision.
#: That makes this bounded by the tree being scanned rather than by uptime,
#: which matters because a server re-scans on every lifespan startup.
_SCANNED: dict[Path, tuple[int, int, str, list[Finding]]] = {}


def _findings(path: Path, surface: str) -> list[Finding]:
    """`scan_source` for one file, reused while its mtime and size hold.

    The boot audit runs on **every** lifespan startup, and an application's
    modules do not change between two startups of the same process. Measured
    across Wreath's own suite before this existed, the audit ran 674 times for
    65.6 seconds of worker time and 71% of that was re-reading files that had
    not changed -- which is why `tests/conftest.py` turns the audit off
    wholesale for the suite, an escape hatch nobody else's application gets.

    `st_mtime_ns` and `st_size` are the fingerprint the mutation catalog
    already uses for the same job. Neither is a content hash, so a file
    rewritten inside one filesystem timestamp tick *and* to an identical length
    would be served stale. Hashing the bytes would mean reading them, which is
    the cost being removed; `stat` is the cheapest thing that can tell the
    difference at all.

    `Finding` is frozen, so handing the same list to two reports is safe.
    """
    try:
        info = path.stat()
    except OSError:
        return []
    cached = _SCANNED.get(path)
    if cached is not None and cached[:3] == (info.st_mtime_ns, info.st_size, surface):
        return cached[3]
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = scan_source(source, surface=surface)
    _SCANNED[path] = (info.st_mtime_ns, info.st_size, surface, found)
    return found


def scan_paths(
    roots: Iterable[str | Path], *, include_tests: bool = False, base: Path | None = None
) -> Report:
    """Apply every code rule to every module under `roots`."""
    report = Report()
    anchor = base or Path.cwd()
    for path in python_files(roots, include_tests=include_tests):
        try:
            surface = str(path.relative_to(anchor))
        except ValueError:
            surface = str(path)
        report.extend(_findings(path, surface))
    return report
