import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wreath._mutant import runner


def setup_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    selected = tmp_path / "selected.py"
    other = tmp_path / "other.py"
    selected.write_text("value = 1\n", encoding="utf-8")
    other.write_text("value = 2\n", encoding="utf-8")
    monkeypatch.setattr(runner, "discover", lambda roots: [selected, other])
    monkeypatch.setattr(runner, "module_name_for", lambda path: "fixture")
    monkeypatch.setattr(runner, "changed_lines", lambda repo, ref: {"selected.py": {1}})
    monkeypatch.setattr(runner.importlib, "import_module", lambda name: SimpleNamespace())
    return selected, other


def test_changed_plan_parses_only_selected_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    selected, _ = setup_sources(monkeypatch, tmp_path)
    parsed = []
    original = ast.parse

    def parse(source: str, *, filename: str, **kwargs: Any) -> ast.AST:
        parsed.append(filename)
        return original(source, filename=filename, **kwargs)

    monkeypatch.setattr(runner.ast, "parse", parse)
    plan = runner.build_plan([tmp_path], tmp_path, changed="HEAD")
    assert parsed == [str(selected)]
    assert plan.sources == ["selected.py"]
    assert plan.errors == []
    assert plan.mutations == []


@pytest.mark.parametrize("failure", ["syntax", "missing"])
@pytest.mark.parametrize("changed", [None, "HEAD"])
def test_selected_source_errors_are_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str, changed: str | None
) -> None:
    selected, _ = setup_sources(monkeypatch, tmp_path)
    if failure == "syntax":
        selected.write_text("def broken(:\n", encoding="utf-8")
    else:
        selected.unlink()
    plan = runner.build_plan([tmp_path], tmp_path, changed=changed)
    assert len(plan.errors) == 1
    assert plan.errors[0][0] == str(selected)
    assert "unreadable" in plan.errors[0][1]


@pytest.mark.parametrize("failure", ["syntax", "missing"])
def test_changed_selection_excludes_unselected_source_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    _, other = setup_sources(monkeypatch, tmp_path)
    if failure == "syntax":
        other.write_text("def broken(:\n", encoding="utf-8")
    else:
        other.unlink()
    selected_plan = runner.build_plan([tmp_path], tmp_path, changed="HEAD")
    assert selected_plan.errors == []
    full_plan = runner.build_plan([tmp_path], tmp_path)
    assert len(full_plan.errors) == 1
    assert full_plan.errors[0][0] == str(other)
    assert "unreadable" in full_plan.errors[0][1]
