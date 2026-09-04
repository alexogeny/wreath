from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wreath._cli import main
from wreath.typegen import build_api_model
from wreath.typegen.cli import (
    MANIFEST_NAME,
    TypegenCliError,
    TypegenOptions,
    check,
    check_contract,
    run,
    write,
)
from wreath.typegen.targets.python import SPEC_FILE
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
    assert manifest["renderer"] == "python"


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
    snapshot = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
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
    handwritten = tmp_path / "handwritten.ts"
    handwritten.write_text("export const mine = 1;\n")
    orphan = tmp_path / "react-query.ts"
    assert orphan.exists()
    write({k: v for k, v in files.items() if k != "react-query.ts"}, tmp_path)
    assert handwritten.exists()  # unowned: untouched
    assert not orphan.exists()  # owned + dropped: removed


def test_write_validates_every_target_before_replacing_any_file(tmp_path: Path) -> None:
    first = tmp_path / "models.ts"
    first.write_text("old models\n")
    outside = tmp_path.parent / "outside-client.ts"
    outside.write_text("outside\n")
    (tmp_path / "client.ts").symlink_to(outside)

    with pytest.raises(TypegenCliError, match="escapes the output directory"):
        write({"models.ts": "new models\n", "client.ts": "new client\n"}, tmp_path)

    assert first.read_text() == "old models\n"
    assert outside.read_text() == "outside\n"


def test_write_stays_on_the_pinned_output_when_its_path_is_swapped(
    tmp_path: Path, monkeypatch
) -> None:
    import wreath.typegen.cli as cli

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    moved = tmp_path.parent / f"{tmp_path.name}-moved"
    outside.mkdir()
    (outside / "models.ts").write_text("outside\n")
    original_replace = os.replace
    swapped = False

    def replace_after_swap(source, target, *args, **kwargs) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            tmp_path.rename(moved)
            tmp_path.symlink_to(outside, target_is_directory=True)
            if not kwargs:
                (outside / Path(source).name).write_text("attacker temporary\n")
        original_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(cli.os, "replace", replace_after_swap)
    try:
        write({"models.ts": "generated\n"}, tmp_path)
        assert (moved / "models.ts").read_text() == "generated\n"
        assert (outside / "models.ts").read_text() == "outside\n"
    finally:
        if tmp_path.is_symlink():
            tmp_path.unlink()
        if moved.exists():
            moved.rename(tmp_path)


def test_check_refuses_to_read_a_generated_target_outside_the_output(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-models.ts"
    outside.write_text("matching outside content\n")
    (tmp_path / "models.ts").symlink_to(outside)

    with pytest.raises(TypegenCliError, match="escapes the output directory"):
        check({"models.ts": "matching outside content\n"}, tmp_path)


def test_contract_check_refuses_a_pinned_document_outside_the_output(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-spec.json"
    outside.write_text("{}")
    (tmp_path / SPEC_FILE).symlink_to(outside)

    with pytest.raises(TypegenCliError, match="escapes the output directory"):
        check_contract({}, tmp_path)


@pytest.mark.parametrize(
    "name", [None, 123, "", ".", "..", "nested/file", "nested\\file", "/tmp/x"]
)
def test_check_refuses_every_non_filename_target(tmp_path: Path, name) -> None:
    with pytest.raises(TypegenCliError, match="escapes the output directory"):
        check({name: "content"}, tmp_path)


def test_check_refuses_a_non_regular_generated_file(tmp_path: Path) -> None:
    (tmp_path / "models.ts").mkdir()

    with pytest.raises(TypegenCliError, match="not a regular file"):
        check({"models.ts": "content"}, tmp_path)


def test_python_class_name_refusal_reaches_the_cli_error_type(tmp_path: Path) -> None:
    from tests.typegen.app import app

    with pytest.raises(TypegenCliError, match="class_name must be a Python identifier"):
        run(
            app,
            TypegenOptions(
                target="python",
                output=str(tmp_path),
                class_name="not a class",
            ),
        )


def test_check_reports_orphaned_owned_file(tmp_path: Path) -> None:
    from tests.typegen.app import app

    files = render_typescript(build_api_model(app), react_query=True)
    write(files, tmp_path)
    without = {k: v for k, v in files.items() if k != "react-query.ts"}
    problems = check(without, tmp_path)
    assert any("no longer generated" in problem for problem in problems)


def test_options_frozen() -> None:
    options = TypegenOptions(target="typescript", output="x")
    with pytest.raises(AttributeError):
        options.output = "y"  # type: ignore[misc]
