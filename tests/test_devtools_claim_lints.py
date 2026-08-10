"""The three gates that check claims nothing else could.

Each of these lints exists because a specific class of statement about this
codebase had no verifier: an artifact's claim to be built from its sources, a
page's claim that a feature is absent, and a query's implicit claim that its
parameter has a type the driver can send.

Every test here builds a synthetic tree rather than reading the real one, so a
gate reaching zero in the repository does not make the suite vacuous -- which is
the failure mode AGENTS.md names, and would be an unusually poor one to ship in
the tests *for* these gates.
"""

from __future__ import annotations

from pathlib import Path

from wreath._devtools import build_lint, roadmap_lint, sql_lint

# --- build_lint: a compiled artifact older than what it was built from -------


def _extension_tree(tmp_path: Path, *, sources: tuple[str, ...], build: bool) -> Path:
    native = tmp_path / "src" / "wreath" / "_native"
    native.mkdir(parents=True)
    listed = "".join(f'                "src/wreath/_native/{name}",\n' for name in sources)
    (tmp_path / "setup.py").write_text(
        "        Extension(\n"
        '            "wreath._native._demo",\n'
        "            sources=[\n" + listed + "            ],\n"
        '            depends=["src/wreath/_native/demo.h"],\n'
        "        ),\n",
        encoding="utf-8",
    )
    for name in (*sources, "demo.h"):
        (native / name).write_text("/* c */\n", encoding="utf-8")
    if build:
        (native / "_demo.cpython-314-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
    return tmp_path


def _touch(path: Path, when: float) -> None:
    import os

    os.utime(path, (when, when))


def test_an_artifact_older_than_its_source_is_stale(tmp_path: Path) -> None:
    root = _extension_tree(tmp_path, sources=("demo.c",), build=True)
    artifact = root / "src/wreath/_native/_demo.cpython-314-x86_64-linux-gnu.so"
    _touch(root / "src/wreath/_native/demo.h", 500_000)
    _touch(artifact, 1_000_000)
    _touch(root / "src/wreath/_native/demo.c", 2_000_000)

    findings = build_lint.scan(root)
    assert [f.code for f in findings] == ["BUILD001"]
    # The message names the newest offending source, so a rebuild has a target.
    assert "demo.c" in findings[0].message


def test_a_header_change_makes_the_artifact_stale(tmp_path: Path) -> None:
    """`depends` counts. A header change invalidates every object including it."""
    root = _extension_tree(tmp_path, sources=("demo.c",), build=True)
    artifact = root / "src/wreath/_native/_demo.cpython-314-x86_64-linux-gnu.so"
    _touch(root / "src/wreath/_native/demo.c", 1_000_000)
    _touch(artifact, 2_000_000)
    _touch(root / "src/wreath/_native/demo.h", 3_000_000)

    assert [f.code for f in build_lint.scan(root)] == ["BUILD001"]


def test_an_artifact_newer_than_its_sources_is_clean(tmp_path: Path) -> None:
    root = _extension_tree(tmp_path, sources=("demo.c",), build=True)
    _touch(root / "src/wreath/_native/demo.c", 1_000_000)
    _touch(root / "src/wreath/_native/demo.h", 1_000_000)
    _touch(root / "src/wreath/_native/_demo.cpython-314-x86_64-linux-gnu.so", 2_000_000)

    assert build_lint.scan(root) == []


def test_an_unbuilt_extension_is_not_stale(tmp_path: Path) -> None:
    """Absent is not stale.

    Conflating them would fire on every optional extension of a default install,
    which is most of them, and the rule would be turned off rather than fixed.
    """
    root = _extension_tree(tmp_path, sources=("demo.c",), build=False)
    _touch(root / "src/wreath/_native/demo.c", 9_000_000)

    assert build_lint.scan(root) == []


def test_an_extension_whose_sources_are_gone_is_reported(tmp_path: Path) -> None:
    root = _extension_tree(tmp_path, sources=("demo.c",), build=False)
    (root / "src/wreath/_native/demo.c").unlink()

    findings = build_lint.scan(root)
    assert [f.code for f in findings] == ["BUILD002"]


def test_the_real_setup_py_parses_into_every_extension() -> None:
    """The source list is derived, so its derivation is the thing to pin.

    If `_BUILD_INPUT_RE` or the block scanner stopped matching, every extension
    would report zero inputs and the lint would pass by having nothing to check.
    """
    root = Path(__file__).resolve().parents[1]
    inputs = build_lint.extension_inputs((root / "setup.py").read_text(encoding="utf-8"))
    assert "wreath._native._core" in inputs
    assert "wreath._native._postgres" in inputs
    for extension, listed in inputs.items():
        assert listed, f"{extension} parsed to no build inputs"


# --- roadmap_lint: a claim of absence must have nothing to point at ----------


def _roadmap(tmp_path: Path, rows: str) -> Path:
    page = tmp_path / "docs" / "reference"
    page.mkdir(parents=True)
    (page / "roadmap.md").write_text(
        "# Reserved and in-progress surfaces\n\n"
        "| Surface | Status |\n|---|---|\n" + rows,
        encoding="utf-8",
    )
    return tmp_path


def test_a_claim_of_absence_that_resolves_is_reported(tmp_path: Path) -> None:
    """The exact shape that shipped: capture was live while the page denied it."""
    root = _roadmap(
        tmp_path,
        "| Recording capture engine | Not shipped. Policy types only."
        " <!-- absent: wreath._native._flight.Recorder --> |\n",
    )
    findings = roadmap_lint.scan(root)
    assert [f.code for f in findings] == ["ROAD002"]
    assert "resolves" in findings[0].message


def test_a_claim_of_absence_that_does_not_resolve_is_clean(tmp_path: Path) -> None:
    root = _roadmap(
        tmp_path,
        # Names a genuinely absent symbol. It used to name `apply_fleet`, which
        # then shipped -- and the lint reported this fixture, which is the lint
        # working: a page claiming absence for something that resolves
        # understates what the tree does.
        "| Tenant-fleet DDL execution | Not shipped."
        " <!-- absent: wreath.infra.Stack --> |\n",
    )
    assert roadmap_lint.scan(root) == []


def test_an_unmarked_claim_of_absence_is_reported(tmp_path: Path) -> None:
    root = _roadmap(tmp_path, "| Something | Not shipped. Trust me. |\n")
    assert [f.code for f in roadmap_lint.scan(root)] == ["ROAD001"]


def test_a_marker_naming_an_unimportable_module_is_reported(tmp_path: Path) -> None:
    """Guard the guard: a typo would otherwise verify nothing, forever."""
    root = _roadmap(
        tmp_path,
        "| Typo | Not shipped. <!-- absent: wreath.migrationz.apply_fleet --> |\n",
    )
    findings = roadmap_lint.scan(root)
    assert [f.code for f in findings] == ["ROAD003"]
    assert "verifies nothing" in findings[0].message


def test_partial_coverage_prose_is_out_of_scope(tmp_path: Path) -> None:
    """A partial claim is not a claim of absence, and no symbol would refute it.

    Demanding a marker here would produce one nobody could write honestly, which
    is how a rule acquires a rubber-stamp value.
    """
    root = _roadmap(
        tmp_path,
        "| Broader object coverage | Expression indexes are still being"
        " implemented (emitted as `MANUAL`). |\n",
    )
    assert roadmap_lint.scan(root) == []


def _module(tmp_path: Path, dotted: str, source: str) -> Path:
    """A source tree the prose rule can read, since it never imports anything."""
    parts = dotted.split(".")
    directory = tmp_path / "src"
    for part in parts[:-1]:
        directory = directory / part
        directory.mkdir(exist_ok=True, parents=True)
        (directory / "__init__.py").touch()
    (directory / f"{parts[-1]}.py").write_text(source, encoding="utf-8")
    return tmp_path


def test_a_prose_marker_whose_surface_now_exists_is_reported(tmp_path: Path) -> None:
    """The exact shape that shipped: `scim_router` landed, the paragraph did not."""
    root = _roadmap(
        tmp_path,
        "\nOne surface is unbuilt, and is listed by absence rather than by a row.\n"
        "<!-- absent: wreath.organizations.scim_router -->\n",
    )
    _module(root, "wreath.organizations", "def scim_router(app):\n    return app\n")

    findings = roadmap_lint.scan(root)
    assert [f.code for f in findings] == ["ROAD002"]
    assert "resolves" in findings[0].message
    assert "wreath.organizations.scim_router" in findings[0].message


def test_a_prose_marker_for_a_surface_still_absent_is_clean(tmp_path: Path) -> None:
    root = _roadmap(
        tmp_path,
        "\nA reconnect is a fresh snapshot.\n<!-- absent: wreath.sync.resume -->\n",
    )
    _module(root, "wreath.sync", "def subscribe(shape):\n    return shape\n")

    assert roadmap_lint.scan(root) == []


def test_a_prose_marker_naming_a_whole_absent_module_is_clean(tmp_path: Path) -> None:
    """`wreath.oauth` is the honest marker for a module nobody has written."""
    root = _roadmap(tmp_path, "\nIssuance is not built.\n<!-- absent: wreath.oauth -->\n")
    _module(root, "wreath.sync", "")

    assert roadmap_lint.scan(root) == []


def test_a_prose_marker_naming_a_module_that_ships_is_reported(tmp_path: Path) -> None:
    root = _roadmap(tmp_path, "\nIssuance is not built.\n<!-- absent: wreath.oauth -->\n")
    _module(root, "wreath.oauth", "def issue(request):\n    return request\n")

    assert [f.code for f in roadmap_lint.scan(root)] == ["ROAD002"]


def test_a_prose_marker_anchored_on_no_module_is_reported(tmp_path: Path) -> None:
    """Guard the guard: a typo would otherwise verify nothing, forever."""
    root = _roadmap(tmp_path, "\nTypo.\n<!-- absent: wreath.migrationz.apply_fleet -->\n")
    _module(root, "wreath.migrations", "def apply(plan):\n    return plan\n")

    findings = roadmap_lint.scan(root)
    assert [f.code for f in findings] == ["ROAD003"]
    assert "verifies nothing" in findings[0].message


def test_a_conditionally_imported_re_export_still_counts_as_shipped(tmp_path: Path) -> None:
    """A facade binds its name inside `try:`; that is an export, not a local."""
    root = _roadmap(tmp_path, "\nAbsent.\n<!-- absent: wreath.organizations.scim_router -->\n")
    _module(
        tmp_path,
        "wreath.organizations",
        "try:\n    from ._scim import scim_router\nexcept ImportError:\n    scim_router = None\n",
    )

    assert [f.code for f in roadmap_lint.scan(root)] == ["ROAD002"]


def test_a_name_local_to_a_function_is_not_a_surface(tmp_path: Path) -> None:
    root = _roadmap(tmp_path, "\nAbsent.\n<!-- absent: wreath.organizations.scim_router -->\n")
    _module(
        tmp_path,
        "wreath.organizations",
        "def build():\n    scim_router = 1\n    return scim_router\n",
    )

    assert roadmap_lint.scan(root) == []


def test_prose_beneath_the_table_is_not_a_row(tmp_path: Path) -> None:
    root = _roadmap(
        tmp_path,
        "| Tenant-fleet DDL execution | Not shipped."
        " <!-- absent: wreath.infra.Stack --> |\n"
        "The Native Flight Recorder is not shipped on this list, prose says.\n",
    )
    assert roadmap_lint.scan(root) == []


# --- sql_lint: a parameter typed by inference the driver cannot encode -------

REPOSITORY = Path(__file__).resolve().parents[1]
ENCODABLE = sql_lint.encodable_types(REPOSITORY)

def _sql_findings(text: str) -> list[sql_lint.Finding]:
    return sql_lint._scan_text("q.py", 1, text, ENCODABLE)


def test_the_derived_set_is_not_empty_and_holds_the_obvious_types() -> None:
    """If the derivation broke, every cast would look encodable and pass."""
    assert {"text", "int4", "int8", "bool", "uuid", "jsonb"} <= ENCODABLE
    assert "name" not in ENCODABLE
    assert "regclass" not in ENCODABLE


def test_a_bare_comparison_against_a_name_column_is_reported() -> None:
    findings = _sql_findings("SELECT 1 FROM pg_namespace n WHERE n.nspname = $1")
    assert [f.code for f in findings] == ["SQL002"]


def test_a_cast_to_an_unencodable_type_is_reported() -> None:
    findings = _sql_findings("SELECT relname FROM pg_class WHERE oid = $1::regclass")
    assert [f.code for f in findings] == ["SQL001"]


def test_the_shipped_fix_is_clean() -> None:
    """`n.nspname = $1::text` is what the six live instances were changed to."""
    assert _sql_findings("SELECT 1 FROM pg_namespace n WHERE n.nspname = $1::text") == []


def test_a_cast_on_the_column_side_is_clean() -> None:
    assert _sql_findings("SELECT 1 FROM pg_namespace n WHERE n.nspname::text = $1") == []


def test_standard_type_spellings_are_not_flagged() -> None:
    """`::integer` is `int4`. Flagging it would be an over-refusal."""
    assert _sql_findings("SELECT 1 FROM t WHERE a = $1::integer") == []
    assert _sql_findings("SELECT 1 FROM t WHERE a = $1::double precision") == []
    assert _sql_findings("SELECT 1 FROM t WHERE a = $1::timestamp with time zone") == []


def test_an_ordinary_column_is_not_a_catalog_column() -> None:
    assert _sql_findings("SELECT 1 FROM llamas WHERE paddock_id = $1") == []


def test_docstrings_are_not_sql() -> None:
    """Prose about the defect is not the defect -- this module's own docstring
    describes `$1::regclass`, and the first run of the lint flagged it."""
    source = '"""Explains SELECT ... WHERE oid = $1::regclass and why it fails."""\n'
    assert sql_lint._sql_literals(source) == []
