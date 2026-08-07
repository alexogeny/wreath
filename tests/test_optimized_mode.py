"""`python -O` is a supported deployment mode, so no invariant may use `assert`.

`-O` strips every `assert` statement. An `assert` guarding a wire format or a
struct layout therefore vanishes in exactly the interpreter mode nothing else
here exercises -- the module imports with a wrong layout and says nothing, which
is a check that silently has nothing to check (ADR 0024).

Eight module-level `assert`s guarded struct layouts in `wreath._flight_schema`
and `wreath.migrations` until this was measured: under `-O`, a completion cell
packed to 60 bytes where the format requires 64 imported without complaint.
They are `raise` statements now, and this module keeps them that way.

`assert` remains correct in `tests/` -- it is pytest's idiom, and `-O` is never
used to run a test suite.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "wreath"


def _module_level_asserts(tree: ast.Module) -> list[int]:
    """Line numbers of `assert` statements at module scope.

    Only module scope: an `assert` inside a function is a developer check whose
    disappearance under `-O` costs a diagnostic, not an invariant. A
    module-level one runs at import and is the shape that guards a layout.
    """
    return [node.lineno for node in tree.body if isinstance(node, ast.Assert)]


def test_no_module_level_assert_guards_an_invariant() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
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
            f"{path.relative_to(SRC.parent.parent)}:{line}"
            for line in _module_level_asserts(tree)
        ]
    assert offenders == [], (
        "module-level `assert` in src/wreath -- `python -O` strips these, so the "
        "invariant disappears in a supported mode. Raise instead:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("module", ["wreath._flight_schema", "wreath.migrations"])
def test_the_layout_modules_import_under_O(module: str) -> None:
    """The converted modules must still import cleanly with asserts stripped.

    A raise that fires on a *correct* layout would be worse than the assert it
    replaced, so this runs the real interpreter rather than trusting the source.
    """
    result = subprocess.run(
        [sys.executable, "-O", "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
