"""The map lint keeps the agent-facing maps honest; these keep the lint honest.

Each test reproduces a drift that actually happened in this repository before
the gate existed, so a regression here means an agent would again be sent to a
path that is not there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wreath._devtools import map_lint
from wreath._devtools.native_lint import repo_root


def _codes(findings: list[map_lint.Finding]) -> set[str]:
    return {finding.code for finding in findings}


def test_repository_maps_are_accurate() -> None:
    """The real thing, which is the whole point of the gate."""
    findings = map_lint.scan(repo_root())
    assert findings == [], "\n".join(finding.render() for finding in findings)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A minimal repository whose maps are clean, ready to be broken one way."""
    (tmp_path / "src" / "wreath").mkdir(parents=True)
    (tmp_path / "src" / "wreath" / "widgets.py").write_text("")
    (tmp_path / "src" / "wreath" / "_private.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_widgets.py").write_text("")
    (tmp_path / "docs" / "guides").mkdir(parents=True)
    (tmp_path / "docs" / "guides" / "widgets.md").write_text("")
    (tmp_path / "docs" / "llms.txt").write_text("- [Widgets](guides/widgets.md)\n")
    for name in map_lint.PROSE_MAPS:
        (tmp_path / name).write_text("See `src/wreath/widgets.py`.\n")
    _write_manifest(tmp_path, _clean_manifest())
    return tmp_path


def _clean_manifest() -> dict:
    return {
        "schema_version": 2,
        "project": "wreath",
        "entrypoints": {"llm_map": "docs/llms.txt"},
        "subsystems": [
            {
                "name": "widgets",
                "guides": ["docs/guides/widgets.md"],
                "sources": ["src/wreath/widgets.py"],
                "tests": ["tests/test_widgets.py"],
            }
        ],
    }


def _write_manifest(root: Path, manifest: dict) -> None:
    path = root / map_lint.MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest))


def test_clean_repository_has_no_findings(fake_repo: Path) -> None:
    assert map_lint.scan(fake_repo) == []


def test_sibling_key_instead_of_array_edit_is_caught(fake_repo: Path) -> None:
    """The corruption that lost three subsystems' test lists.

    A patch meant to add tests to `subsystems[0]` wrote that as a literal
    top-level key. Nothing that walks `subsystems` can see it, and the entry it
    meant to fix still looks incomplete.
    """
    manifest = _clean_manifest()
    manifest["subsystems[0]"] = {"tests": ["tests/test_widgets.py"]}
    _write_manifest(fake_repo, manifest)

    assert "MAP001" in _codes(map_lint.scan(fake_repo))


def test_manifest_path_that_does_not_exist_is_caught(fake_repo: Path) -> None:
    manifest = _clean_manifest()
    manifest["subsystems"][0]["tests"] = ["tests/test_gone.py"]
    _write_manifest(fake_repo, manifest)

    assert "MAP002" in _codes(map_lint.scan(fake_repo))


def test_unmapped_public_module_is_caught(fake_repo: Path) -> None:
    """A subsystem nobody mapped is a subsystem an agent finds by grep."""
    (fake_repo / "src" / "wreath" / "gadgets.py").write_text("")

    findings = map_lint.scan(fake_repo)
    assert "MAP003" in _codes(findings)
    assert any("gadgets" in finding.message for finding in findings)


def test_private_module_needs_no_subsystem(fake_repo: Path) -> None:
    (fake_repo / "src" / "wreath" / "_more_private.py").write_text("")

    assert map_lint.scan(fake_repo) == []


def test_package_counts_as_a_public_surface(fake_repo: Path) -> None:
    package = fake_repo / "src" / "wreath" / "gizmos"
    package.mkdir()
    (package / "__init__.py").write_text("")

    assert "MAP003" in _codes(map_lint.scan(fake_repo))


def test_subsystem_missing_a_required_field_is_caught(fake_repo: Path) -> None:
    manifest = _clean_manifest()
    del manifest["subsystems"][0]["tests"]
    _write_manifest(fake_repo, manifest)

    assert "MAP004" in _codes(map_lint.scan(fake_repo))


def test_prose_map_citing_a_missing_path_is_caught(fake_repo: Path) -> None:
    """`repo-map.md` pointed at `docs/native/` for months. It never existed."""
    (fake_repo / "repo-map.md").write_text("Native details live in `docs/native/`.\n")

    findings = map_lint.scan(fake_repo)
    assert "MAP005" in _codes(findings)
    assert any("docs/native/" in finding.message for finding in findings)


def test_prose_backticks_that_are_not_paths_are_left_alone(fake_repo: Path) -> None:
    """A linter that cries about `dict` gets turned off rather than fixed."""
    (fake_repo / "repo-map.md").write_text(
        "Return a `dict`, run `uv sync --group docs`, see `wreath.router` and\n"
        "the `benchmark-results` trees and `src/wreath/*.py` globs.\n"
    )

    assert map_lint.scan(fake_repo) == []


def test_llms_txt_link_to_a_missing_page_is_caught(fake_repo: Path) -> None:
    (fake_repo / "docs" / "llms.txt").write_text("- [Gone](guides/gone.md)\n")

    assert "MAP006" in _codes(map_lint.scan(fake_repo))


def test_guide_missing_from_llms_txt_is_caught(fake_repo: Path) -> None:
    """Agents read the compact index instead of the nav; an unlisted guide is invisible."""
    (fake_repo / "docs" / "guides" / "gadgets.md").write_text("")

    findings = map_lint.scan(fake_repo)
    assert "MAP007" in _codes(findings)
    assert any("gadgets.md" in finding.message for finding in findings)


def test_main_reports_failure_and_success(fake_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(map_lint, "repo_root", lambda: fake_repo)
    assert map_lint.main([]) == 0

    (fake_repo / "src" / "wreath" / "gadgets.py").write_text("")
    assert map_lint.main([]) == 1
    assert map_lint.main(["--format", "json"]) == 1
