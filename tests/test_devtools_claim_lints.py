from __future__ import annotations

from pathlib import Path

from wreath._devtools import build_lint, sql_lint


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


def test_a_stale_free_threaded_artifact_is_reported_beside_a_fresh_one(
    tmp_path: Path,
) -> None:
    root = _extension_tree(tmp_path, sources=("demo.c",), build=True)
    native = root / "src/wreath/_native"
    stale = native / "_demo.cpython-314t-x86_64-linux-gnu.so"
    stale.write_bytes(b"\x7fELF")

    _touch(stale, 1_000_000)
    _touch(native / "demo.c", 2_000_000)
    _touch(native / "demo.h", 2_000_000)
    _touch(native / "_demo.cpython-314-x86_64-linux-gnu.so", 3_000_000)

    findings = build_lint.scan(root)
    assert [f.code for f in findings] == ["BUILD001"]
    assert "cpython-314t" in findings[0].where


def test_an_unbuilt_extension_is_not_stale(tmp_path: Path) -> None:
    root = _extension_tree(tmp_path, sources=("demo.c",), build=False)
    _touch(root / "src/wreath/_native/demo.c", 9_000_000)

    assert build_lint.scan(root) == []


def test_an_extension_whose_sources_are_gone_is_reported(tmp_path: Path) -> None:
    root = _extension_tree(tmp_path, sources=("demo.c",), build=False)
    (root / "src/wreath/_native/demo.c").unlink()

    findings = build_lint.scan(root)
    assert [f.code for f in findings] == ["BUILD002"]


def test_the_real_setup_py_parses_into_every_extension() -> None:
    root = Path(__file__).resolve().parents[1]
    inputs = build_lint.extension_inputs((root / "setup.py").read_text(encoding="utf-8"))
    assert "wreath._native._core" in inputs
    assert "wreath._native._postgres" in inputs
    for extension, listed in inputs.items():
        assert listed, f"{extension} parsed to no build inputs"


REPOSITORY = Path(__file__).resolve().parents[1]
ENCODABLE = sql_lint.encodable_types(REPOSITORY)


def _sql_findings(text: str) -> list[sql_lint.Finding]:
    return sql_lint._scan_text("q.py", 1, text, ENCODABLE)


def test_the_derived_set_is_not_empty_and_holds_the_obvious_types() -> None:
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
    assert _sql_findings("SELECT 1 FROM pg_namespace n WHERE n.nspname = $1::text") == []


def test_a_cast_on_the_column_side_is_clean() -> None:
    assert _sql_findings("SELECT 1 FROM pg_namespace n WHERE n.nspname::text = $1") == []


def test_standard_type_spellings_are_not_flagged() -> None:
    assert _sql_findings("SELECT 1 FROM t WHERE a = $1::integer") == []
    assert _sql_findings("SELECT 1 FROM t WHERE a = $1::double precision") == []
    assert _sql_findings("SELECT 1 FROM t WHERE a = $1::timestamp with time zone") == []


def test_an_ordinary_column_is_not_a_catalog_column() -> None:
    assert _sql_findings("SELECT 1 FROM llamas WHERE paddock_id = $1") == []


def test_owned_table_qualification_distinguishes_all_three_reference_shapes() -> None:
    owned = frozenset({"jobs"})
    scan = sql_lint._scan_qualification

    assert [item.code for item in scan("q.py", 1, "SELECT * FROM jobs", owned)] == ["SQL003"]
    assert scan("q.py", 1, 'SELECT * FROM "wreath"."jobs"', owned) == []
    assert scan("q.py", 1, "SELECT * FROM llamas", owned) == []


def test_docstrings_are_not_sql() -> None:
    source = '"""Explains SELECT ... WHERE oid = $1::regclass and why it fails."""\n'
    assert sql_lint._sql_literals(source) == []


def test_the_repository_scan_parses_each_module_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    package = tmp_path / "src" / "wreath"
    package.mkdir(parents=True)
    (package / "schema.py").write_text(
        'class Component: pass\nJOBS = Component(relations=("jobs",))\n',
        encoding="utf-8",
    )
    (package / "queries.py").write_text(
        'QUERY = "SELECT count(*) FROM jobs"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sql_lint, "encodable_types", lambda _root: ENCODABLE)
    original_parse = sql_lint.ast.parse
    parses = 0

    def recording_parse(*args, **kwargs):
        nonlocal parses
        parses += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(sql_lint.ast, "parse", recording_parse)

    findings = sql_lint.scan(tmp_path)
    assert [finding.code for finding in findings] == ["SQL003"]
    assert parses == 2
