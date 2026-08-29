from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from wreath._devtools import dup_scan


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "wreath").mkdir(parents=True)
    return tmp_path


def _write(tree: Path, name: str, source: str) -> None:
    target = tree / "src" / "wreath" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)


PARTIAL_PYTHON_COPY = """
def first(source):
    if source:
        initialize_alpha(source)
    shared = load(source)
    parsed = parse(shared)
    checked = validate(parsed)
    mapped = transform(checked)
    encoded = encode(mapped)
    stored = save(encoded)
    emitted = publish(stored)
    audited = audit(emitted)
    finish_alpha()
    return audited


def second(source):
    for entry in source:
        initialize_beta(entry)
    shared = load(source)
    parsed = parse(shared)
    checked = validate(parsed)
    mapped = transform(checked)
    encoded = encode(mapped)
    stored = save(encoded)
    emitted = publish(stored)
    audited = audit(emitted)
    finish_beta()
    if audited:
        return audited
    raise RuntimeError("missing audit")
"""


PARTIAL_NATIVE_COPY = r"""
static int
first(Buffer *buffer, Item *item)
{
    if (item == NULL) return -1;
    size_t need = item->size;
    char *fresh = PyMem_Realloc(buffer->data, need);
    if (fresh == NULL) return -1;
    buffer->data = fresh;
    buffer->size = need;
    buffer->ready = 1;
    record_success(buffer);
    return 0;
}

static int
second(Buffer *buffer, Item *item)
{
    while (item->pending) advance(item);
    size_t need = item->size;
    char *fresh = PyMem_Realloc(buffer->data, need);
    if (fresh == NULL) return -1;
    buffer->data = fresh;
    buffer->size = need;
    buffer->ready = 1;
    record_success(buffer);
    return item->pending ? -1 : 0;
}
"""


RENAMED_RELATIONSHIPS = """
def first(left, right):
    chosen = prepare(left)
    joined = combine(chosen, chosen)
    checked = validate(joined)
    encoded = encode(checked)
    stored = save(encoded)
    notify(stored)
    audit(stored)
    return stored


def second(alpha, beta):
    selected = prepare(alpha)
    merged = combine(selected, beta)
    verified = validate(merged)
    rendered = encode(verified)
    persisted = save(rendered)
    notify(persisted)
    audit(persisted)
    return persisted


def third(source, unused):
    value = prepare(source)
    result = combine(value, value)
    safe = validate(result)
    payload = encode(safe)
    written = save(payload)
    notify(written)
    audit(written)
    return written
"""


EXACT_TWINS = """
def first(source):
    loaded = load(source)
    parsed = parse(loaded)
    checked = validate(parsed)
    mapped = transform(checked)
    encoded = encode(mapped)
    stored = save(encoded)
    audit(stored)
    return stored


def second(item):
    value = load(item)
    syntax = parse(value)
    safe = validate(syntax)
    projected = transform(safe)
    payload = encode(projected)
    written = save(payload)
    audit(written)
    return written
"""


def test_native_fragment_scan_finds_a_python_interior_copy(tree: Path) -> None:
    _write(tree, "partial.py", PARTIAL_PYTHON_COPY)

    assert dup_scan.scan(tree, ("src/wreath",), 8, ("python",))[0] == []
    assert dup_scan.near_clones(tree, ("src/wreath",), 8, ("python",)) == []

    fragments = dup_scan.fragment_clones(
        tree,
        ("src/wreath",),
        min_lines=6,
        min_tokens=24,
        langs=("python",),
    )

    assert len(fragments) == 1
    fragment = fragments[0]
    assert (fragment.left.name, fragment.right.name) == ("first", "second")
    assert fragment.lines >= 6
    assert fragment.tokens >= 24


def test_native_fragment_scan_handles_c_and_uses_a_dual_floor(tree: Path) -> None:
    _write(tree, "partial.c", PARTIAL_NATIVE_COPY)

    fragments = dup_scan.fragment_clones(
        tree,
        ("src/wreath",),
        min_lines=5,
        min_tokens=20,
        langs=("native",),
    )

    assert len(fragments) == 1
    assert (fragments[0].left.name, fragments[0].right.name) == ("first", "second")
    assert (
        dup_scan.fragment_clones(
            tree,
            ("src/wreath",),
            min_lines=5,
            min_tokens=10_000,
            langs=("native",),
        )
        == []
    )


def test_fragment_matcher_is_a_native_builtin() -> None:
    assert inspect.isbuiltin(dup_scan._fragment_scan)


def test_repetitive_fragment_work_is_near_linear_beside_a_same_size_control() -> None:
    def work(size: int, *, repetitive: bool) -> int:
        if repetitive:
            source = (
                "def subject(value, item):\n"
                + "    value = value + item\n" * size
                + "    return value\n"
            )
        else:
            source = (
                "def subject(value):\n"
                + "".join(f"    value = operation_{index}(value)\n" for index in range(size))
                + "    return value\n"
            )
        _matches, measured = dup_scan._fragment_scan(
            [(source.encode(), 1, 0)],
            1,
            8,
            1,
            True,
        )
        return measured

    small_repeat = work(320, repetitive=True)
    large_repeat = work(640, repetitive=True)
    large_control = work(640, repetitive=False)

    assert large_repeat <= small_repeat * 2.1
    assert large_repeat <= large_control * 7


def test_fragment_only_scan_does_not_build_the_whole_body_shape(
    tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tree, "partial.py", PARTIAL_PYTHON_COPY)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("fragment-only scan built a whole-body shape")

    monkeypatch.setattr(dup_scan, "_structure", unexpected)
    monkeypatch.setattr(dup_scan, "_alpha_structure", unexpected)

    assert dup_scan.fragment_clones(
        tree,
        ("src/wreath",),
        min_lines=6,
        min_tokens=24,
        langs=("python",),
    )


def test_alpha_normalization_preserves_identifier_relationships(tree: Path) -> None:
    _write(tree, "relationships.py", RENAMED_RELATIONSHIPS)

    shape, _ = dup_scan.scan(tree, ("src/wreath",), 8, ("python",))
    alpha, _ = dup_scan.scan(
        tree,
        ("src/wreath",),
        8,
        ("python",),
        normalization="alpha",
    )

    assert [site.name for site in shape[0].sites] == ["first", "second", "third"]
    assert len(alpha) == 1
    assert [site.name for site in alpha[0].sites] == ["first", "third"]


def test_context_json_contains_exact_source_ranges_and_snippets(
    tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(tree, "twins.py", EXACT_TWINS)
    monkeypatch.setattr(dup_scan, "repo_root", lambda: tree)

    assert (
        dup_scan.main(
            [
                "--path",
                "src/wreath/twins.py",
                "--format",
                "json",
                "--context",
                "1",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)

    sites = report["groups"][0]["sites"]
    assert sites[0]["start_line"] == 3
    assert sites[0]["end_line"] == 10
    assert "loaded = load(source)" in sites[0]["source"]
    assert "value = load(item)" in sites[1]["source"]


def test_cli_reports_skipped_sources_without_turning_them_into_a_failure(
    tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(tree, "twins.py", EXACT_TWINS)
    _write(tree, "broken.py", "def (:\n")
    monkeypatch.setattr(dup_scan, "repo_root", lambda: tree)

    assert dup_scan.main(["--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["coverage"] == {
        "discovered_files": 2,
        "scanned_files": 1,
        "skipped_files": [
            {
                "path": "src/wreath/broken.py",
                "reason": "invalid Python syntax at line 1",
            }
        ],
    }


def test_cli_reports_an_undecodable_native_source_as_skipped(
    tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tree / "src" / "wreath" / "broken.c"
    target.write_bytes(b"static int broken(void) { return \xff; }\n")
    monkeypatch.setattr(dup_scan, "repo_root", lambda: tree)

    assert dup_scan.main(["--format", "json", "--lang", "native"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["coverage"] == {
        "discovered_files": 1,
        "scanned_files": 0,
        "skipped_files": [
            {
                "path": "src/wreath/broken.c",
                "reason": "native source is not valid UTF-8",
            }
        ],
    }


def test_cli_refuses_a_missing_path_with_the_correct_form(
    tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(dup_scan, "repo_root", lambda: tree)

    with pytest.raises(SystemExit, match="2"):
        dup_scan.main(["--path", "src/wreath/missing"])

    error = capsys.readouterr().err
    assert "src/wreath/missing does not exist" in error
    assert "use a repository-relative file or directory" in error


def test_summary_aggregates_duplicate_lines_by_file_and_directory(
    tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(tree, "left/twins.py", EXACT_TWINS)
    _write(
        tree, "right/twins.py", EXACT_TWINS.replace("first", "third").replace("second", "fourth")
    )
    monkeypatch.setattr(dup_scan, "repo_root", lambda: tree)

    assert dup_scan.main(["--format", "json", "--summary"]) == 0
    summary = json.loads(capsys.readouterr().out)["summary"]

    assert summary["files"][:2] == [
        {
            "path": "src/wreath/left/twins.py",
            "duplicated_lines": 16,
            "groups": 1,
        },
        {
            "path": "src/wreath/right/twins.py",
            "duplicated_lines": 16,
            "groups": 1,
        },
    ]
    assert summary["directories"][:2] == [
        {
            "path": "src/wreath/left",
            "duplicated_lines": 16,
            "groups": 1,
        },
        {
            "path": "src/wreath/right",
            "duplicated_lines": 16,
            "groups": 1,
        },
    ]
