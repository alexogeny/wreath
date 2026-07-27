"""Every native lint must survive a C source it cannot read.

These are gates: `wreath-check` runs four of them and trusts the exit code. A
lint that crashes on one unreadable file ends the whole run, and a lint that
skipped the file silently and still printed "0 finding(s)" would be worse -- it
would report a clean bill of health over files it never opened. The house answer
is to name the file on stderr and exit non-zero, which three of the five already
did; `native-error-lint` and `native-gil-lint` raised `FileNotFoundError` out of
`main` instead.

The unreadable file here is a dangling symlink rather than a `chmod 000` one, so
the test behaves the same for an unprivileged user and for root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wreath._devtools import (
    native_boundary_lint,
    native_error_lint,
    native_gil_lint,
    native_lint,
    native_memory_lint,
)

LINTS = pytest.mark.parametrize(
    "module",
    (native_lint, native_boundary_lint, native_error_lint, native_gil_lint, native_memory_lint),
    ids=lambda m: m.__name__.rsplit(".", 1)[-1],
)


@pytest.fixture
def tree_with_a_dangling_source(tmp_path: Path) -> Path:
    (tmp_path / "good.c").write_text("int ok(void) { return 0; }\n")
    (tmp_path / "dangling.c").symlink_to(tmp_path / "nowhere" / "missing.c")
    return tmp_path


@LINTS
def test_an_unreadable_source_is_named_and_fails_rather_than_raising(
    module: object, tree_with_a_dangling_source: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = module.main([str(tree_with_a_dangling_source)])  # type: ignore[attr-defined]

    assert status == 1, "an unreadable source must fail the gate, not pass it"
    err = capsys.readouterr().err
    assert "dangling.c" in err, "the unreadable file must be named"
    assert "cannot read" in err


@LINTS
def test_a_readable_tree_still_reports_cleanly(
    module: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard must not turn an ordinary clean run into a failure."""
    (tmp_path / "good.c").write_text("int ok(void) { return 0; }\n")

    assert module.main([str(tmp_path)]) == 0  # type: ignore[attr-defined]
    assert "cannot read" not in capsys.readouterr().err
