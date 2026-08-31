from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from wreath._devtools import complexity_probe as complexity
from wreath._devtools import dup_scan


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "wreath").mkdir(parents=True)
    return tmp_path


def _site(name: str, line: int = 1, *, path: str = "src/wreath/sample.py") -> dup_scan.Site:
    return dup_scan.Site(path, name, line, 3, name, line, line + 2)


def _group(digest: str, line: int = 1) -> dup_scan.Group:
    return dup_scan.Group(
        digest,
        (_site(f"{digest}_left", line), _site(f"{digest}_right", line + 4)),
    )


def _probe(*, metric: str | None = None, todo: complexity.Todo | None = None) -> complexity.Probe:
    return complexity.Probe(
        name="mutation-control",
        fn=lambda size: float(size),
        expect=todo.degree if todo else 1.0,
        sizes=(1, 2, 4),
        repeats=1,
        noise_floor=0.0,
        metric=metric,
        axis="rows",
        assumption="work grows with rows",
        stage="test",
        group="test",
        todo=todo,
    )


def _result(
    *,
    metric: str | None = None,
    todo: complexity.Todo | None = None,
    times: list[float] | None = None,
) -> complexity.Result:
    samples = times or [1.0, 2.0, 4.0]
    counters = [{metric: value} for value in (1, 2, 4)] if metric else [{}, {}, {}]
    return complexity.Result(_probe(metric=metric, todo=todo), samples, counters)


def test_site_projection_uses_qualified_name_ranges_and_requested_context(
    source_tree: Path,
) -> None:
    target = source_tree / "src" / "wreath" / "sample.py"
    target.write_text("zero\none\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    site = dup_scan.Site("src/wreath/sample.py", "leaf", 3, 2, "Owner.leaf", 3, 4)

    assert site.identity_name == "Owner.leaf"
    assert site.as_dict(source_tree, 1) == {
        "path": "src/wreath/sample.py",
        "name": "Owner.leaf",
        "line": 3,
        "lines": 2,
        "start_line": 3,
        "end_line": 4,
        "source": "one\ntwo\nthree\nfour",
    }


def test_site_projection_falls_back_to_unqualified_name_and_derived_range(
    source_tree: Path,
) -> None:
    site = dup_scan.Site("src/wreath/sample.py", "leaf", 7, 3)

    assert site.identity_name == "leaf"
    assert site.as_dict(source_tree, -1) == {
        "path": "src/wreath/sample.py",
        "name": "leaf",
        "line": 7,
        "lines": 3,
        "start_line": 7,
        "end_line": 9,
    }


def test_source_selection_filters_files_and_sorts_directory_entries(source_tree: Path) -> None:
    root = source_tree / "src" / "wreath"
    (root / "nested").mkdir()
    (root / "nested" / "z.c").write_text("", encoding="utf-8")
    (root / "nested" / "a.py").write_text("", encoding="utf-8")
    (root / "nested" / "ignored.txt").write_text("", encoding="utf-8")
    (root / "nested" / "fake.py").mkdir()
    (root / "single.py").write_text("", encoding="utf-8")
    (root / "single.txt").write_text("", encoding="utf-8")

    selected = dup_scan._sources(
        source_tree,
        ("src/wreath/nested", "src/wreath/single.py", "src/wreath/single.txt"),
        ("python", "native"),
    )

    assert [(path.name, language) for path, language in selected] == [
        ("a.py", "python"),
        ("z.c", "native"),
        ("single.py", "python"),
    ]


def test_scope_catalog_records_owners_and_qualified_nested_functions() -> None:
    tree = ast.parse(
        "class Owner:\n"
        "    def method(self):\n"
        "        def nested():\n"
        "            return 1\n"
        "        return nested\n"
    )

    definitions, children = dup_scan._scope_catalog(tree)

    owner = tree.body[0]
    method = owner.body[0]
    nested = method.body[0]
    assert [name for _, name in definitions] == [
        "Owner.method",
        "Owner.method.<locals>.nested",
    ]
    assert children[id(owner)] == [method]
    assert children[id(method)] == [nested]
    assert id(None) not in children


def test_python_body_collection_distinguishes_empty_stub_short_and_real_bodies(
    source_tree: Path,
) -> None:
    target = source_tree / "src" / "wreath" / "bodies.py"
    target.write_text(
        "def empty():\n"
        "    pass\n\n"
        "def stub():\n"
        "    raise NotImplementedError\n\n"
        "def short(value):\n"
        "    return value\n\n"
        "def real(value):\n"
        "    prepared = value + 1\n"
        "    checked = prepared * 2\n"
        "    return checked\n",
        encoding="utf-8",
    )

    bodies = dup_scan._python_bodies(target, "src/wreath/bodies.py", 2)
    shape_free = dup_scan._python_bodies(
        target,
        "src/wreath/bodies.py",
        2,
        build_structure=False,
    )

    assert [body.site.name for body in bodies] == ["real"]
    assert bodies[0].site.body_start == 11
    assert bodies[0].site.body_end == 13
    assert bodies[0].shape
    assert shape_free[0].shape == b""


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ((), (), 0.0),
        ((1, 2), (), 0.0),
        ((), (1, 2), 0.0),
        ((1, 2), (1, 2), 1.0),
        ((1, 3), (2, 3), pytest.approx(1 / 3)),
    ],
)
def test_similarity_handles_empty_exhausted_and_shared_sketches(
    left: tuple[int, ...],
    right: tuple[int, ...],
    expected: object,
) -> None:
    assert dup_scan._similarity(left, right) == expected


def test_similarity_counts_a_shared_value_before_either_side_is_exhausted() -> None:
    assert dup_scan._similarity((1,), (1, 2)) == 0.5
    assert dup_scan._similarity((1, 2), (1,)) == 0.5


def test_short_shape_has_no_similarity_sketch() -> None:
    assert dup_scan._sketch(b"x" * dup_scan._GRAM) == ()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--min-lines", "0"), "--min-lines must be at least 1"),
        (("--min-tokens", "0"), "--min-tokens must be at least 1"),
        (("--context", "-1"), "--context must be non-negative"),
        (("--similarity", "1.1"), "--similarity must be between 0 and 1"),
    ],
)
def test_dup_scan_cli_refuses_invalid_bounds(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    message: str,
) -> None:
    monkeypatch.setattr(dup_scan, "repo_root", lambda: source_tree)

    with pytest.raises(SystemExit, match="2"):
        dup_scan.main(list(arguments))

    assert message in capsys.readouterr().err


def test_dup_scan_json_dispatches_every_detailed_report(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = source_tree / "src" / "wreath" / "sample.py"
    source.write_text("one\ntwo\nthree\nfour\nfive\nsix\n", encoding="utf-8")
    groups = [_group("exact")]
    pair = dup_scan.Pair(_site("near_left"), _site("near_right", 4), 0.875)
    fragment = dup_scan.Fragment(_site("fragment_left"), _site("fragment_right", 4), 12)
    bodies = [dup_scan.Body(_site("body"), "digest", b"shape")]
    collect_calls: list[tuple[tuple[str, ...], tuple[str, ...], str]] = []

    def collect(
        _root: Path,
        relatives: tuple[str, ...],
        _min_lines: int,
        langs: tuple[str, ...],
        *,
        normalization: str,
        coverage: dup_scan.Coverage | None,
    ) -> list[dup_scan.Body]:
        collect_calls.append((relatives, langs, normalization))
        assert coverage is not None
        coverage.discovered_files = 1
        coverage.scanned_files = 1
        return bodies

    monkeypatch.setattr(dup_scan, "repo_root", lambda: source_tree)
    monkeypatch.setattr(dup_scan, "collect", collect)
    monkeypatch.setattr(dup_scan, "_scan_bodies", lambda _bodies: (groups, 2))
    monkeypatch.setattr(dup_scan, "_near_bodies", lambda _bodies, _threshold: [pair])
    monkeypatch.setattr(
        dup_scan,
        "_fragment_bodies",
        lambda _bodies, _lines, _tokens, _normalization: [fragment],
    )

    assert (
        dup_scan.main(
            [
                "--path",
                "src/wreath/sample.py",
                "--lang",
                "python",
                "--near",
                "--fragments",
                "--summary",
                "--normalization",
                "alpha",
                "--context",
                "1",
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)

    assert collect_calls == [(("src/wreath/sample.py",), ("python",), "alpha")]
    assert report["scanned_functions"] == 2
    assert report["langs"] == ["python"]
    assert report["normalization"] == "alpha"
    assert report["coverage"] == {
        "discovered_files": 1,
        "scanned_files": 1,
        "skipped_files": [],
    }
    assert report["groups"][0]["sites"][0]["source"] == "one\ntwo\nthree\nfour"
    assert report["near"][0]["similarity"] == 0.875
    assert report["fragments"][0]["tokens"] == 12
    assert report["summary"]["files"][0]["groups"] == 1


def test_dup_scan_text_reports_exact_near_fragment_and_summary_limits(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = source_tree / "src" / "wreath" / "sample.py"
    source.write_text("one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\n", encoding="utf-8")
    groups = [_group("first"), _group("second", 2)]
    pairs = [
        dup_scan.Pair(_site("near_a"), _site("near_b", 4), 0.9),
        dup_scan.Pair(_site("near_c", 2), _site("near_d", 5), 0.8),
    ]
    fragments = [
        dup_scan.Fragment(_site("fragment_a"), _site("fragment_b", 4), 12),
        dup_scan.Fragment(_site("fragment_c", 2), _site("fragment_d", 5), 10),
    ]
    bodies = [dup_scan.Body(_site("body"), "digest", b"shape")]

    monkeypatch.setattr(dup_scan, "repo_root", lambda: source_tree)
    monkeypatch.setattr(dup_scan, "collect", lambda *_args, **_kwargs: bodies)
    monkeypatch.setattr(dup_scan, "_scan_bodies", lambda _bodies: (groups, 4))
    monkeypatch.setattr(dup_scan, "_near_bodies", lambda _bodies, _threshold: pairs)
    monkeypatch.setattr(
        dup_scan,
        "_fragment_bodies",
        lambda _bodies, _lines, _tokens, _normalization: fragments,
    )

    assert (
        dup_scan.main(
            [
                "--path",
                "src/wreath/sample.py",
                "--near",
                "--fragments",
                "--summary",
                "--context",
                "1",
                "--top",
                "1",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out

    assert "4 function(s)" in output
    assert "... and 1 more group(s)" in output
    assert "... and 1 more pair(s)" in output
    assert "... and 1 more fragment(s)" in output
    assert "duplicate hotspots by file" in output
    assert "| one" in output


def test_dup_scan_text_reports_an_empty_exact_scan(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(dup_scan, "repo_root", lambda: source_tree)
    monkeypatch.setattr(dup_scan, "scan", lambda *_args, **_kwargs: ([], 0))
    monkeypatch.setattr(
        dup_scan,
        "collect",
        lambda *_args, **_kwargs: pytest.fail("plain exact scan collected detailed bodies"),
    )

    assert dup_scan.main([]) == 0
    output = capsys.readouterr().out
    assert "no shared structure found" in output
    assert "intentional groups" not in output
    assert "near copies" not in output
    assert "fragments (" not in output
    assert "duplicate hotspots" not in output
    assert "| " not in output


@pytest.mark.parametrize(
    "argument",
    ["--near", "--fragments", "--summary", "--context=1", "--normalization=alpha"],
)
def test_each_dup_scan_option_independently_selects_the_detailed_path(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    argument: str,
) -> None:
    calls: list[dup_scan.Coverage | None] = []
    monkeypatch.setattr(dup_scan, "repo_root", lambda: source_tree)

    def collect(*_args: object, **kwargs: object) -> list[dup_scan.Body]:
        calls.append(kwargs.get("coverage"))
        return []

    monkeypatch.setattr(dup_scan, "collect", collect)
    monkeypatch.setattr(dup_scan, "_scan_bodies", lambda _bodies: ([], 0))
    monkeypatch.setattr(dup_scan, "_near_bodies", lambda _bodies, _threshold: [])
    monkeypatch.setattr(
        dup_scan,
        "_fragment_bodies",
        lambda _bodies, _lines, _tokens, _normalization: [],
    )

    assert dup_scan.main([argument]) == 0
    assert calls == [None]


def test_dup_scan_json_without_context_or_summary_omits_both_payloads(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(dup_scan, "repo_root", lambda: source_tree)
    monkeypatch.setattr(dup_scan, "collect", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(dup_scan, "_scan_bodies", lambda _bodies: ([_group("only")], 2))

    assert dup_scan.main(["--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert "summary" not in report
    assert "source" not in report["groups"][0]["sites"][0]


def test_dup_scan_zero_top_limit_keeps_every_text_row(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    groups = [_group("first"), _group("second", 2)]
    monkeypatch.setattr(dup_scan, "repo_root", lambda: source_tree)
    monkeypatch.setattr(dup_scan, "scan", lambda *_args, **_kwargs: (groups, 4))

    assert dup_scan.main(["--top", "0"]) == 0
    output = capsys.readouterr().out

    assert "first_left" in output
    assert "second_left" in output
    assert "more group(s)" not in output


def test_dup_scan_show_excluded_partitions_intentional_groups(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    included = _group("included")
    excluded = _group("excluded", 2)
    monkeypatch.setattr(dup_scan, "repo_root", lambda: source_tree)
    monkeypatch.setattr(
        dup_scan,
        "scan",
        lambda *_args, **_kwargs: ([included, excluded], 4),
    )
    monkeypatch.setattr(
        dup_scan,
        "intentional_reason",
        lambda group: "separate native kernels" if group.digest == "excluded" else None,
    )

    assert dup_scan.main(["--show-excluded"]) == 0
    output = capsys.readouterr().out

    assert "included_left" in output
    assert "excluded_left" in output
    assert "separate native kernels" in output
    assert "1 intentional group(s) filtered" in output


def test_complexity_result_table_includes_metric_ratios_and_contract_kind(
    capsys: pytest.CaptureFixture[str],
) -> None:
    complexity._print_result(_result(metric="visits"))
    output = capsys.readouterr().out

    assert "at most O(n)" in output
    assert "on visits" in output
    assert "2.00x" in output
    assert "visits" in output
    assert "rows" in output


def test_complexity_stale_mark_explains_why_the_contract_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    todo = complexity.Todo(2.0, 1.0, "nested scan", "issue-1")
    complexity._print_result(_result(todo=todo, times=[1.0, 2.0, 4.0]))
    output = capsys.readouterr().out

    assert "pinned at O(n^2)" in output
    assert "FIX LATER" in output
    assert "STALE MARK" in output
    assert "issue-1" in output


def test_complexity_result_without_metric_or_stale_mark_prints_neither_detail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _result()
    result.status = "STALE"

    complexity._print_result(result)
    output = capsys.readouterr().out

    assert "on None" not in output
    assert "STALE MARK" not in output


def test_complexity_probe_declaration_requires_exactly_one_contract_kind() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        complexity.probe("missing", sizes=(1, 2))
    with pytest.raises(ValueError, match="exactly one"):
        complexity.probe(
            "double",
            expect=1.0,
            todo=complexity.Todo(2.0, 1.0, "reason", "owner"),
            sizes=(1, 2),
        )


def test_complexity_probe_declaration_accepts_one_contract_kind() -> None:
    decorator = complexity.probe("valid-control", expect=1.0, sizes=(1, 2))

    def operation(size: int) -> float:
        return float(size)

    assert decorator(operation) is operation
    assert complexity._REGISTRY.pop("valid-control").expect == 1.0


def test_complexity_non_stale_mark_does_not_print_stale_guidance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    todo = complexity.Todo(2.0, 1.0, "nested scan", "issue-1")

    complexity._print_result(_result(todo=todo, times=[1.0, 4.0, 16.0]))

    assert "STALE MARK" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "existing",
    [
        [],
        {"version": complexity.BASELINE_VERSION + 1, "probes": {}},
        {"version": complexity.BASELINE_VERSION, "probes": []},
    ],
)
def test_selected_complexity_baseline_update_refuses_invalid_existing_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    existing: object,
) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(existing), encoding="utf-8")
    monkeypatch.setattr(complexity, "_baseline_path", lambda: path)
    monkeypatch.setattr(complexity, "run_probe", lambda _probe: _result())

    assert complexity._write_baseline(["css-no-media-control"]) == 1
    assert "invalid or version-mismatched baseline" in capsys.readouterr().err


def test_complexity_baseline_update_refuses_failed_measurements(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failed = _result(times=[1.0, 4.0, 16.0])
    monkeypatch.setattr(complexity, "run_probe", lambda _probe: failed)

    assert complexity._write_baseline(["css-no-media-control"]) == 1
    assert "refusing to record" in capsys.readouterr().err


def test_full_complexity_baseline_update_ignores_an_existing_invalid_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "baseline.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(complexity, "_baseline_path", lambda: path)
    monkeypatch.setattr(complexity, "run_probe", lambda _probe: _result())

    assert complexity._write_baseline(list(complexity._REGISTRY)) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == complexity.BASELINE_VERSION


@pytest.mark.parametrize(
    "arguments",
    [
        ["--check", "--update-baseline"],
        ["--discover", "--discover-check"],
        ["css-no-media-control", "--group", "web"],
        ["--group", "missing-group"],
        ["missing-probe"],
    ],
)
def test_complexity_cli_refuses_conflicting_or_unknown_selection(arguments: list[str]) -> None:
    original = complexity.run_probe
    complexity.run_probe = lambda _probe: pytest.fail("invalid selection ran a probe")
    try:
        with pytest.raises(SystemExit, match="2"):
            complexity.main(arguments)
    finally:
        complexity.run_probe = original


def test_complexity_cli_refuses_check_update_before_either_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        complexity,
        "_write_baseline",
        lambda _names: pytest.fail("conflicting mode updated the baseline"),
    )
    monkeypatch.setattr(
        complexity,
        "_check_baseline",
        lambda _names: pytest.fail("conflicting mode checked the baseline"),
    )

    with pytest.raises(SystemExit, match="2"):
        complexity.main(["css-no-media-control", "--check", "--update-baseline"])


def test_complexity_cli_refuses_multiple_discovery_modes_before_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        complexity,
        "_run_discovery",
        lambda: pytest.fail("conflicting discovery modes scanned the tree"),
    )

    with pytest.raises(SystemExit, match="2"):
        complexity.main(["--discover", "--discover-check"])


def test_complexity_cli_refuses_probe_and_group_before_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        complexity,
        "run_probe",
        lambda _probe: pytest.fail("conflicting selection ran a probe"),
    )

    with pytest.raises(SystemExit, match="2"):
        complexity.main(["css-no-media-control", "--group", "web"])


def test_complexity_cli_refuses_an_empty_group_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="2"):
        complexity.main(["--group", "missing-group"])

    assert "unknown or empty group" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argument", "dispatched"),
    [
        ("--discover", "print"),
        ("--discover-check", "check"),
        ("--update-discovery", "write"),
    ],
)
def test_complexity_cli_dispatches_each_discovery_mode(
    monkeypatch: pytest.MonkeyPatch,
    argument: str,
    dispatched: str,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(complexity, "_run_discovery", lambda: [])
    monkeypatch.setattr(complexity, "_print_discovery", lambda _findings: calls.append("print"))
    monkeypatch.setattr(
        complexity,
        "_check_discovery",
        lambda _findings: calls.append("check") or 7,
    )
    monkeypatch.setattr(
        complexity,
        "_write_discovery",
        lambda _findings: calls.append("write") or 8,
    )
    monkeypatch.setattr(
        complexity,
        "run_probe",
        lambda _probe: pytest.fail("discovery mode ran a timing probe"),
    )

    expected = {"print": 0, "check": 7, "write": 8}[dispatched]
    assert complexity.main([argument]) == expected
    assert calls == [dispatched]


def test_complexity_cli_lists_probes_and_recorded_defects(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    summaries: list[list[str]] = []
    monkeypatch.setattr(
        complexity,
        "_print_todo_summary",
        lambda names: summaries.append(list(names)),
    )
    monkeypatch.setattr(
        complexity,
        "run_probe",
        lambda _probe: pytest.fail("listing ran a timing probe"),
    )

    assert complexity.main(["--list"]) == 0
    listed = capsys.readouterr().out
    assert "css-no-media-control" in listed
    assert "at most" in listed or "PINNED" in listed
    assert complexity.main(["--todos"]) == 0
    assert summaries == [list(complexity._REGISTRY)]


def test_complexity_cli_applies_overrides_and_emits_json_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = complexity._REGISTRY["css-no-media-control"]
    selected = complexity.Probe(**vars(original))
    monkeypatch.setitem(complexity._REGISTRY, "css-no-media-control", selected)
    monkeypatch.setattr(complexity, "run_probe", lambda probe: _result())

    assert (
        complexity.main(
            [
                "css-no-media-control",
                "--sizes",
                "3,6,12",
                "--repeats",
                "2",
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)

    assert selected.sizes == (3, 6, 12)
    assert selected.repeats == 2
    assert report[0]["probe"] == "mutation-control"


def test_complexity_cli_returns_failure_and_prints_table_for_failed_probe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failed = _result(times=[1.0, 4.0, 16.0])
    monkeypatch.setattr(complexity, "run_probe", lambda _probe: failed)

    assert complexity.main(["css-no-media-control"]) == 1
    output = capsys.readouterr().out
    assert "[FAIL]" in output
    assert '"probe"' not in output


def test_complexity_cli_without_repeat_override_preserves_registered_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = complexity._REGISTRY["css-no-media-control"]
    selected = complexity.Probe(**vars(original))
    monkeypatch.setitem(complexity._REGISTRY, "css-no-media-control", selected)
    monkeypatch.setattr(complexity, "run_probe", lambda _probe: _result())

    assert complexity.main(["css-no-media-control", "--format", "json"]) == 0
    assert selected.repeats == original.repeats


def test_complexity_cli_filters_a_named_group_before_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: list[str] = []

    def run_probe(probe: complexity.Probe) -> complexity.Result:
        selected.append(probe.name)
        return _result()

    monkeypatch.setattr(complexity, "run_probe", run_probe)

    assert complexity.main(["--group", "web", "--format", "json"]) == 0
    assert selected
    assert selected == [
        name for name, probe in complexity._REGISTRY.items() if probe.group == "web"
    ]


def test_complexity_cli_dispatches_check_and_update_without_running_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        complexity,
        "_check_baseline",
        lambda names: calls.append(("check", list(names))) or 6,
    )
    monkeypatch.setattr(
        complexity,
        "_write_baseline",
        lambda names: calls.append(("update", list(names))) or 7,
    )

    assert complexity.main(["css-no-media-control", "--check"]) == 6
    assert complexity.main(["css-no-media-control", "--update-baseline"]) == 7
    assert calls == [
        ("check", ["css-no-media-control"]),
        ("update", ["css-no-media-control"]),
    ]
