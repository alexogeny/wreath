from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "wreath"


def _runtime_asserts(tree: ast.Module) -> list[int]:
    """Line numbers of assertions that optimized runtime code would discard."""
    return [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)]


def test_no_runtime_assert_guards_an_invariant() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "_devtools" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        # Reading the whole tree costs 22 ms and parsing it 1.9 s, so the cheap
        # half gets to answer first. This is a *sound* filter, not a heuristic:
        # `ast.Assert` is only ever produced by the `assert` keyword, so a file
        # without that substring cannot contain one, and the ones skipped here
        # are provably not offenders. 63% of the tree's bytes never reach the
        # parser. (A substring match is deliberately over-eager the other way --
        # "assertion" in a docstring costs one needless parse and no wrong
        # answer.)
        if "assert" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        offenders += [
            f"{path.relative_to(SRC.parent.parent)}:{line}" for line in _runtime_asserts(tree)
        ]
    assert offenders == [], (
        "runtime `assert` in src/wreath -- `python -O` strips these, so the "
        "invariant disappears in a supported mode. Raise instead:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("module", ["wreath._flight_schema", "wreath.migrations"])
def test_the_layout_modules_import_under_O(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-O", "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
