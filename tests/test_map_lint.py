"""The map lint keeps the agent-facing maps honest; these keep the lint honest.

Each test reproduces a drift that actually happened in this repository before
the gate existed, so a regression here means an agent would again be sent to a
path that is not there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wreath._devtools import capability_index, map_lint
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
    (tmp_path / map_lint.REFERENCE_DIR).mkdir(parents=True)
    (tmp_path / map_lint.REFERENCE_DIR / "widgets.md").write_text(
        "# `wreath.widgets`\n\n::: wreath.widgets\n")
    (tmp_path / "docs" / "llms.txt").write_text("- [Widgets](guides/widgets.md)\n")
    (tmp_path / map_lint.CAPABILITY_PAGE).write_text(
        "# What you don't have to install\n\n::: capability-map\n")
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
                "capability": "Widgets, and the holding of them",
                "replaces": ["widgetlib", "flask-widgets"],
                "guides": ["docs/guides/widgets.md"],
                "sources": ["src/wreath/widgets.py"],
                "tests": ["tests/test_widgets.py"],
            }
        ],
    }


def _reference_page(root: Path, module: str) -> None:
    """A reference page that actually renders `wreath.<module>`."""
    (root / map_lint.REFERENCE_DIR / f"{module}.md").write_text(
        f"# `wreath.{module}`\n\n::: wreath.{module}\n")


def _write_manifest(root: Path, manifest: dict) -> None:
    """Write the manifest, and the file derived from it.

    `src/wreath/_capability_data.py` is generated from the manifest and `MAP015`
    fails while the two disagree, so a fixture that wrote only one of them would
    add that finding to every test in this file -- none of which is about it.
    Writing both here is what `wreath-map-lint --fix` does for the real tree;
    `tests/test_map_lint_capability_index.py` is where the rule itself is proved.
    """
    path = root / map_lint.MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest))
    capability_index.write_module(root, manifest)


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


def test_public_module_with_no_reference_page_is_caught(fake_repo: Path) -> None:
    """Being named in the manifest is not being documented.

    `wreath.response_cache` was listed in the `cache` subsystem's `sources`, so
    MAP003 passed, while no page anywhere rendered it -- the whole `@cached`
    surface was ungenerated and nothing said so.
    """
    (fake_repo / "src" / "wreath" / "gadgets.py").write_text("")
    manifest = _clean_manifest()
    manifest["subsystems"][0]["sources"].append("src/wreath/gadgets.py")
    _write_manifest(fake_repo, manifest)

    findings = map_lint.scan(fake_repo)
    assert _codes(findings) == {"MAP014"}
    assert any("wreath.gadgets" in finding.message for finding in findings)


def test_public_module_with_a_reference_page_is_not_caught(fake_repo: Path) -> None:
    (fake_repo / "src" / "wreath" / "gadgets.py").write_text("")
    _reference_page(fake_repo, "gadgets")
    manifest = _clean_manifest()
    manifest["subsystems"][0]["sources"].append("src/wreath/gadgets.py")
    _write_manifest(fake_repo, manifest)

    assert map_lint.scan(fake_repo) == []


def test_reference_page_without_the_directive_does_not_count(fake_repo: Path) -> None:
    """A file named after the module is not a rendering of it.

    The `:::` directive is what generates the API; prose about the module beside
    a heading that names it leaves the surface just as ungenerated.
    """
    (fake_repo / "src" / "wreath" / "gadgets.py").write_text("")
    (fake_repo / map_lint.REFERENCE_DIR / "gadgets.md").write_text(
        "# `wreath.gadgets`\n\nGadgets, described by hand and generated nowhere.\n")

    assert "MAP014" in _codes(map_lint.scan(fake_repo))


def test_a_submodule_directive_documents_its_parent(fake_repo: Path) -> None:
    """`docs/reference/orm.md` renders `::: wreath.orm.session` and fourteen more.

    `_public_modules` lists the package as `orm`, so the reduction to a top-level
    name is what keeps the two rules talking about the same thing.
    """
    package = fake_repo / "src" / "wreath" / "gizmos"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (fake_repo / map_lint.REFERENCE_DIR / "gizmos.md").write_text(
        "# `wreath.gizmos`\n\n::: wreath.gizmos.levers\n")

    assert "MAP014" not in _codes(map_lint.scan(fake_repo))


def test_exempt_module_needs_no_reference_page(fake_repo: Path) -> None:
    """`wreath.storage` is a deprecated alias; a page for it would document the
    vocabulary being retired, in parallel with the one that replaced it."""
    (fake_repo / "src" / "wreath" / "storage.py").write_text("")
    manifest = _clean_manifest()
    manifest["subsystems"][0]["sources"].append("src/wreath/storage.py")
    _write_manifest(fake_repo, manifest)

    assert map_lint.scan(fake_repo) == []


def test_missing_reference_section_is_one_finding_not_eighty(fake_repo: Path) -> None:
    """A report with a line per module buries the fact that the section is gone."""
    (fake_repo / map_lint.REFERENCE_DIR / "widgets.md").unlink()
    (fake_repo / map_lint.REFERENCE_DIR).rmdir()

    findings = [f for f in map_lint.scan(fake_repo) if f.code == "MAP014"]
    assert len(findings) == 1
    assert "reference section is gone" in findings[0].message


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


# -- MAP008: sanitizer builds mirror the real extensions ---------------------


def _write_builds(root: Path, sanitizer_sources: str) -> None:
    """A repo whose real `_core` compiles two files, plus one sanitizer build."""
    (root / "setup.py").write_text(
        'Extension(\n'
        '    "wreath._native._core",\n'
        '    sources=["src/wreath/_native/a.c", "src/wreath/_native/b.c"],\n'
        '    depends=["src/wreath/_native/wreathcore.h"],\n'
        ')\n'
    )
    sanitizers = root / "tools" / "sanitizers"
    sanitizers.mkdir(parents=True, exist_ok=True)
    (sanitizers / "setup_core.py").write_text(sanitizer_sources)


def test_sanitizer_check_without_the_real_setup_is_empty(fake_repo: Path) -> None:
    (fake_repo / "tools" / "sanitizers").mkdir(parents=True)

    assert map_lint.check_sanitizer_sources(fake_repo) == []


def test_sanitizer_build_matching_the_extension_is_clean(fake_repo: Path) -> None:
    _write_builds(
        fake_repo,
        'SOURCES = ("a.c", "b.c")\n'
        'Extension("wreath._native._core", sources=[P / n for n in SOURCES])\n',
    )
    assert "MAP008" not in _codes(map_lint.scan(fake_repo))


def test_sanitizer_build_omitting_a_source_is_caught(fake_repo: Path) -> None:
    """The drift that hid cedar.c, jose.c, and scheduler.c from ASan."""
    _write_builds(
        fake_repo,
        'SOURCES = ("a.c",)\n'
        'Extension("wreath._native._core", sources=[P / n for n in SOURCES])\n',
    )
    findings = [f for f in map_lint.scan(fake_repo) if f.code == "MAP008"]
    assert len(findings) == 1
    assert "b.c" in findings[0].message


def test_sanitizer_build_may_add_sources(fake_repo: Path) -> None:
    """Only omissions matter; an extra test shim is legitimate."""
    _write_builds(
        fake_repo,
        'SOURCES = ("a.c", "b.c", "shim.c")\n'
        'Extension("wreath._native._core", sources=[P / n for n in SOURCES])\n',
    )
    assert "MAP008" not in _codes(map_lint.scan(fake_repo))


def test_depends_entries_are_not_treated_as_sources(fake_repo: Path) -> None:
    """`depends` names headers and #included C, which must not be compiled twice.

    The reactor extension lists four `.c` files there; requiring the sanitizer
    to compile them would break its build rather than protect it.
    """
    (fake_repo / "setup.py").write_text(
        'Extension(\n'
        '    "wreath._native._reactor",\n'
        '    sources=["src/wreath/_native/_reactormodule.c"],\n'
        '    depends=["src/wreath/_native/reactor_ring.c"],\n'
        ')\n'
    )
    sanitizers = fake_repo / "tools" / "sanitizers"
    sanitizers.mkdir(parents=True, exist_ok=True)
    (sanitizers / "setup_reactor.py").write_text(
        'Extension("wreath._native._reactor", sources=[P / "_reactormodule.c"])\n'
    )
    assert "MAP008" not in _codes(map_lint.scan(fake_repo))


def test_sanitizer_building_an_unknown_extension_is_caught(fake_repo: Path) -> None:
    _write_builds(
        fake_repo,
        'Extension("wreath._native._ghost", sources=[P / "a.c"])\n',
    )
    findings = [f for f in map_lint.scan(fake_repo) if f.code == "MAP008"]
    assert len(findings) == 1
    assert "does not define" in findings[0].message


# -- `--fix` / `--adopt`: the mechanical repairs -------------------------------
#
# `manifest_patch.py`-shaped one-off scripts kept being written to do exactly
# this by hand, because the lint could report the drift and never repair any of
# it. What is repaired here is only what has one right answer; the tests below
# pin that boundary as hard as they pin the repairs.


def test_fix_attaches_a_conventional_test_that_was_not_listed(fake_repo: Path) -> None:
    """The half that rots: the module is mapped, the later test file is not."""
    manifest = _clean_manifest()
    manifest["subsystems"][0]["tests"] = []
    _write_manifest(fake_repo, manifest)

    changes, refusals = map_lint.repair(fake_repo, [])

    assert refusals == []
    assert changes == ["widgets.tests += tests/test_widgets.py"]
    written = json.loads((fake_repo / map_lint.MANIFEST).read_text())
    assert written["subsystems"][0]["tests"] == ["tests/test_widgets.py"]


def test_fix_is_idempotent(fake_repo: Path) -> None:
    before = (fake_repo / map_lint.MANIFEST).read_text()

    changes, refusals = map_lint.repair(fake_repo, [])

    assert (changes, refusals) == ([], [])
    # Byte-identical: a no-op fix must not reformat the file, or every run of it
    # shows up as a diff and the tool becomes something you avoid.
    assert (fake_repo / map_lint.MANIFEST).read_text() == before


def test_fix_reports_when_the_manifest_is_already_complete(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(map_lint, "repo_root", lambda: fake_repo)

    assert map_lint.main(["--fix"]) == 0

    output = capsys.readouterr().out
    assert "nothing to fix: every source's conventional tests are already listed." in output


@pytest.mark.parametrize(
    ("repair_result", "returncode"),
    [(["attached conventional test"], 0), (["REFUSED manual adoption"], 1)],
)
def test_fix_does_not_report_a_noop_when_it_changed_or_refused_something(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    repair_result: list[str],
    returncode: int,
) -> None:
    monkeypatch.setattr(map_lint, "repo_root", lambda: fake_repo)
    changes = repair_result if returncode == 0 else []
    refusals = repair_result if returncode == 1 else []
    monkeypatch.setattr(map_lint, "repair", lambda *_args: (changes, refusals))

    assert map_lint.main(["--fix"]) == returncode

    output = capsys.readouterr().out
    assert "nothing to fix" not in output


def test_fix_does_not_invent_a_test_that_is_not_on_disk(fake_repo: Path) -> None:
    """A guessed-wrong test path is worse than a missing one: it gets believed."""
    (fake_repo / "src" / "wreath" / "gadgets.py").write_text("")
    manifest = _clean_manifest()
    manifest["subsystems"][0]["sources"].append("src/wreath/gadgets.py")
    _write_manifest(fake_repo, manifest)

    changes, _ = map_lint.repair(fake_repo, [])

    assert changes == []


def test_fix_does_not_sweep_up_a_prefix_match(fake_repo: Path) -> None:
    """`tests/test_widgets_extra.py` is not `widgets`' conventional test path."""
    (fake_repo / "tests" / "test_widgets_extra.py").write_text("")

    changes, _ = map_lint.repair(fake_repo, [])

    assert changes == []


def test_adopt_adds_the_source_and_brings_its_tests(fake_repo: Path) -> None:
    (fake_repo / "src" / "wreath" / "gadgets.py").write_text("")
    (fake_repo / "tests" / "test_gadgets.py").write_text("")
    _reference_page(fake_repo, "gadgets")
    assert "MAP003" in _codes(map_lint.scan(fake_repo))

    changes, refusals = map_lint.repair(fake_repo, [("widgets", "src/wreath/gadgets.py")])

    assert refusals == []
    # The third line is the derived half: adopting a source changes which modules
    # a subsystem exposes, so the shipped capability index moves with it and says
    # so. A silent regeneration would be a file changing under the reader.
    assert changes == [
        "widgets.sources += src/wreath/gadgets.py",
        "widgets.tests += tests/test_gadgets.py",
        f"{capability_index.DATA_MODULE} regenerated from {map_lint.MANIFEST}",
    ]
    assert map_lint.scan(fake_repo) == []


def test_adopt_refuses_an_unknown_subsystem(fake_repo: Path) -> None:
    before = (fake_repo / map_lint.MANIFEST).read_text()

    changes, refusals = map_lint.repair(fake_repo, [("ghosts", "src/wreath/widgets.py")])

    assert changes == []
    assert len(refusals) == 1 and "no subsystem named 'ghosts'" in refusals[0]
    assert (fake_repo / map_lint.MANIFEST).read_text() == before


def test_adopt_refuses_a_path_that_does_not_exist(fake_repo: Path) -> None:
    """Adopting a missing path would add a MAP002 finding, not remove one."""
    changes, refusals = map_lint.repair(fake_repo, [("widgets", "src/wreath/gone.py")])

    assert changes == []
    assert len(refusals) == 1 and "no such path" in refusals[0]


def test_repair_never_resolves_a_finding_that_needs_judgment(fake_repo: Path) -> None:
    """MAP002 and MAP003 are left alone: neither has a derivable answer.

    A repair that guessed at these would produce a manifest that lints clean and
    lies, which is the exact failure the lint exists to prevent.
    """
    (fake_repo / "src" / "wreath" / "gadgets.py").write_text("")   # MAP003
    _reference_page(fake_repo, "gadgets")
    manifest = _clean_manifest()
    manifest["subsystems"][0]["reference"] = ["docs/reference/moved.md"]  # MAP002
    _write_manifest(fake_repo, manifest)

    map_lint.repair(fake_repo, [])

    assert _codes(map_lint.scan(fake_repo)) == {"MAP002", "MAP003"}


# -- MAP010/MAP011/MAP012: the capability map stays describable ---------------


def test_subsystem_with_no_capability_is_caught(fake_repo: Path) -> None:
    """A user-facing subsystem that describes itself nowhere is invisible.

    The capability map is generated from this field, so a subsystem that never
    writes one is simply absent from the page that exists to prove the surface
    is there — silently, which is the failure mode the map already had once.
    """
    manifest = _clean_manifest()
    del manifest["subsystems"][0]["capability"]
    _write_manifest(fake_repo, manifest)

    findings = map_lint.scan(fake_repo)
    assert "MAP011" in _codes(findings)
    assert any("capability" in finding.message for finding in findings)


def test_capability_null_means_deliberately_internal(fake_repo: Path) -> None:
    """`null` is the explicit opt-out: devtools and the example app are not features."""
    manifest = _clean_manifest()
    manifest["subsystems"][0]["capability"] = None
    del manifest["subsystems"][0]["replaces"]
    _write_manifest(fake_repo, manifest)

    assert map_lint.scan(fake_repo) == []


def test_internal_subsystem_claiming_replacements_is_caught(fake_repo: Path) -> None:
    """`capability: null` keeps a row off the map; `replaces` beside it is a claim
    nobody will ever read, which means it is a claim nobody will ever check."""
    manifest = _clean_manifest()
    manifest["subsystems"][0]["capability"] = None
    _write_manifest(fake_repo, manifest)

    assert "MAP011" in _codes(map_lint.scan(fake_repo))


def test_capability_that_is_not_a_sentence_is_caught(fake_repo: Path) -> None:
    manifest = _clean_manifest()
    manifest["subsystems"][0]["capability"] = ["Widgets"]
    _write_manifest(fake_repo, manifest)

    assert "MAP011" in _codes(map_lint.scan(fake_repo))


def test_replaces_must_be_a_list(fake_repo: Path) -> None:
    """A bare string is 24 one-character package names to anything that iterates."""
    manifest = _clean_manifest()
    manifest["subsystems"][0]["replaces"] = "widgetlib"
    _write_manifest(fake_repo, manifest)

    assert "MAP010" in _codes(map_lint.scan(fake_repo))


def test_replaces_entry_that_is_not_a_distribution_name_is_caught(
    fake_repo: Path,
) -> None:
    """Shape, not truth: the lint is offline and cannot ask PyPI anything.

    `pydantic (v2)` and `python-jose[cryptography]` are the two spellings that
    show up, and neither is a name anyone can install.
    """
    manifest = _clean_manifest()
    manifest["subsystems"][0]["replaces"] = ["python-jose[cryptography]", "pydantic (v2)"]
    _write_manifest(fake_repo, manifest)

    findings = [f for f in map_lint.scan(fake_repo) if f.code == "MAP010"]
    assert len(findings) == 2
    assert any("python-jose[cryptography]" in finding.message for finding in findings)


def test_capability_page_that_stopped_rendering_the_map_is_caught(
    fake_repo: Path,
) -> None:
    """The fields are only worth requiring while something still renders them."""
    (fake_repo / map_lint.CAPABILITY_PAGE).write_text(
        "# What you don't have to install\n\nA hand-written table, probably.\n")

    findings = map_lint.scan(fake_repo)
    assert "MAP012" in _codes(findings)
    assert any("capability-map" in finding.message for finding in findings)


def test_capability_page_that_is_gone_is_caught(fake_repo: Path) -> None:
    (fake_repo / map_lint.CAPABILITY_PAGE).unlink()

    assert "MAP012" in _codes(map_lint.scan(fake_repo))
