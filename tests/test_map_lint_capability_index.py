"""MAP015: the shipped capability index still says what the manifest says."""

from __future__ import annotations

import json
from pathlib import Path

from wreath._devtools.capability_index import DATA_MODULE, render_module, write_module
from wreath._devtools.map_lint import check_capability_index, repair, scan
from wreath._devtools.native_lint import repo_root


def _tree(root: Path, manifest: dict) -> Path:
    """A miniature checkout: a manifest and whatever index we want beside it."""
    (root / "docs" / "agents").mkdir(parents=True)
    (root / "src" / "wreath").mkdir(parents=True)
    (root / "docs" / "agents" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    return root


_MANIFEST = {
    "subsystems": [
        {
            "name": "jobs",
            "capability": "Durable jobs",
            "sources": ["src/wreath/jobs.py"],
            "guides": ["docs/guides/jobs.md"],
            "replaces": ["celery"],
        }
    ]
}


def test_a_current_index_is_clean(tmp_path: Path) -> None:
    root = _tree(tmp_path, _MANIFEST)
    (root / DATA_MODULE).write_text(render_module(_MANIFEST), encoding="utf-8")
    assert check_capability_index(root) == []


def test_a_stale_index_is_reported_as_stale_and_names_the_fix(tmp_path: Path) -> None:
    """The message has to carry the command, because the file says do not edit
    it -- a finding that only says "differs" leaves the reader nowhere to go."""
    root = _tree(tmp_path, _MANIFEST)
    (root / DATA_MODULE).write_text(
        render_module({"subsystems": []}), encoding="utf-8")
    findings = check_capability_index(root)
    assert [finding.code for finding in findings] == ["MAP015"]
    assert "wreath-map-lint --fix" in findings[0].message


def test_a_missing_index_is_reported_rather_than_passing_vacuously(
    tmp_path: Path,
) -> None:
    """A check that reads no file and finds no difference must not report clean.

    This is the shape `AGENTS.md` spends its length on: absence and agreement
    are indistinguishable unless the check separates them.
    """
    root = _tree(tmp_path, _MANIFEST)
    findings = check_capability_index(root)
    assert [finding.code for finding in findings] == ["MAP015"]
    assert "is missing" in findings[0].message


def test_fix_regenerates_the_index(tmp_path: Path) -> None:
    root = _tree(tmp_path, _MANIFEST)
    (root / DATA_MODULE).write_text("# stale\n", encoding="utf-8")
    changes, refusals = repair(root, [])
    assert refusals == []
    assert any(DATA_MODULE in change for change in changes)
    assert check_capability_index(root) == []


def test_fix_is_idempotent(tmp_path: Path) -> None:
    """A second `--fix` must report nothing, or the gate cries wolf forever."""
    root = _tree(tmp_path, _MANIFEST)
    write_module(root, _MANIFEST)
    changes, _ = repair(root, [])
    assert not any(DATA_MODULE in change for change in changes)


def test_this_repository_has_no_map015_finding() -> None:
    codes = [finding.render() for finding in scan(repo_root())
             if finding.code == "MAP015"]
    assert codes == []
