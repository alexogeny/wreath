"""CLI behaviour: write, --check, atomic replacement, and file ownership."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wreath._cli import main
from wreath.typegen import build_api_model
from wreath.typegen.cli import MANIFEST_NAME, TypegenOptions, check, write
from wreath.typegen.targets.typescript import render_typescript

TARGET = "tests.typegen.app:app"
FACTORY_TARGET = "tests.typegen.app:build_app"
BAD_TARGET = "tests.typegen.badapp:app"


def _run(*args: str) -> int:
    return main(["typegen", *args])


def test_generates_all_files(tmp_path: Path) -> None:
    code = _run(TARGET, "--output", str(tmp_path), "--react-query")
    assert code == 0
    names = {path.name for path in tmp_path.iterdir()}
    assert names == {
        "models.ts",
        "client.ts",
        "index.ts",
        "react-query.ts",
        MANIFEST_NAME,
    }
    manifest = json.loads((tmp_path / MANIFEST_NAME).read_text())
    assert manifest["generator"] == "wreath-typegen"
    assert manifest["renderer"] == "pure"


def test_check_passes_when_current(tmp_path: Path) -> None:
    assert _run(TARGET, "--output", str(tmp_path), "--react-query") == 0
    assert _run(TARGET, "--output", str(tmp_path), "--react-query", "--check") == 0


def test_check_fails_and_writes_nothing_when_stale(tmp_path: Path) -> None:
    _run(TARGET, "--output", str(tmp_path))
    models = tmp_path / "models.ts"
    models.write_text(models.read_text() + "// tampered\n")
    before = models.read_text()
    assert _run(TARGET, "--output", str(tmp_path), "--check") == 1
    assert models.read_text() == before  # --check never writes


def test_check_fails_when_a_file_is_missing(tmp_path: Path) -> None:
    _run(TARGET, "--output", str(tmp_path))
    (tmp_path / "client.ts").unlink()
    assert _run(TARGET, "--output", str(tmp_path), "--check") == 1


def test_factory_target(tmp_path: Path) -> None:
    assert _run(FACTORY_TARGET, "--factory", "--output", str(tmp_path)) == 0
    assert (tmp_path / "models.ts").exists()


def test_strict_failure_leaves_previous_tree_intact(tmp_path: Path) -> None:
    assert _run(TARGET, "--output", str(tmp_path), "--react-query") == 0
    snapshot = {
        path.name: path.read_bytes() for path in tmp_path.iterdir()
    }
    # A strict run against an unsupported annotation fails before any write.
    assert _run(BAD_TARGET, "--output", str(tmp_path)) == 1
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert after == snapshot


def test_allow_unknown_succeeds_where_strict_fails(tmp_path: Path) -> None:
    assert _run(BAD_TARGET, "--output", str(tmp_path)) == 1
    assert _run(BAD_TARGET, "--output", str(tmp_path), "--allow-unknown") == 0
    assert "unknown" in (tmp_path / "models.ts").read_text()


def test_write_removes_only_owned_files(tmp_path: Path) -> None:
    from tests.typegen.app import app

    files = render_typescript(build_api_model(app), react_query=True)
    write(files, tmp_path)
    # A hand-authored file is not owned and must survive regeneration.
    handwritten = tmp_path / "handwritten.ts"
    handwritten.write_text("export const mine = 1;\n")
    # A previously-owned file that is no longer generated must be removed.
    orphan = tmp_path / "react-query.ts"
    assert orphan.exists()
    write({k: v for k, v in files.items() if k != "react-query.ts"}, tmp_path)
    assert handwritten.exists()  # unowned: untouched
    assert not orphan.exists()  # owned + dropped: removed


def test_check_reports_orphaned_owned_file(tmp_path: Path) -> None:
    from tests.typegen.app import app

    files = render_typescript(build_api_model(app), react_query=True)
    write(files, tmp_path)
    # Regeneration without react-query drops react-query.ts; --check must notice
    # the still-listed owned file rather than silently passing.
    without = {k: v for k, v in files.items() if k != "react-query.ts"}
    # rebuild manifest reference: current files list no longer includes it
    problems = check(without, tmp_path)
    assert any("no longer generated" in problem for problem in problems)


def test_options_frozen() -> None:
    options = TypegenOptions(target="typescript", output="x")
    with pytest.raises(AttributeError):
        options.output = "y"  # type: ignore[misc]
