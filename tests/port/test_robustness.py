"""What ``wreath port`` does with a tree that is not a tidy corpus.

These pin the three ways a run over a real 3000-file checkout used to go wrong:
a coverage number that reads 100% precisely when nothing was understood, a single
unreadable file ending the whole run, and a checked-out virtualenv being walked
and counted as application code.
"""
from pathlib import Path

import pytest

port = pytest.importorskip("wreath.port")

_APP = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/llamas")
async def list_llamas():
    return []
"""


def _app_tree(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    root.mkdir()
    (root / "main.py").write_text(_APP, encoding="utf-8")
    return root


# -- 1. an empty denominator is not 100% --------------------------------------


def test_empty_tree_reports_no_coverage_rather_than_perfect_coverage(tmp_path):
    """A tree the analyzer recognized nothing in must never render as 100%."""
    root = tmp_path / "empty"
    root.mkdir()

    report = port.analyze(root)

    assert report.recognized_constructs == 0
    assert report.coverage_overall() is None
    assert report.coverage("routing") is None

    doc = report.to_json()
    assert doc["coverage_overall"] is None
    assert doc["files_analyzed"] == 0

    markdown = report.to_markdown()
    assert "100%" not in markdown
    assert "n/a" in markdown


def test_a_tree_of_unrecognized_python_is_not_100_percent(tmp_path):
    """Files were read and understood to contain *nothing portable*: still n/a."""
    root = tmp_path / "plain"
    root.mkdir()
    (root / "maths.py").write_text("def herd_size(n):\n    return n * 2\n", encoding="utf-8")

    report = port.analyze(root)

    assert report.files_analyzed == 1
    assert report.recognized_constructs == 0
    assert report.coverage_overall() is None
    assert "100%" not in report.to_markdown()


def test_a_category_with_no_findings_has_no_coverage(tmp_path):
    """Per-category coverage has the same empty-denominator rule as the overall."""
    report = port.analyze(_app_tree(tmp_path))

    assert report.coverage("routing") is not None
    assert report.coverage("a_category_that_does_not_exist") is None


# -- 2. a bad file is recorded and skipped, not fatal -------------------------


def test_an_unreadable_file_is_recorded_and_skipped(tmp_path):
    """A broken symlink is the reliable form of "cannot be read" (root included)."""
    root = _app_tree(tmp_path)
    (root / "dangling.py").symlink_to(root / "no_such_file.py")

    report = port.analyze(root)  # must not raise

    assert report.recognized_constructs > 0, "the good file was still analyzed"
    assert report.files_analyzed == 1
    skipped = {Path(s.file).name: s for s in report.skipped}
    assert set(skipped) == {"dangling.py"}
    assert skipped["dangling.py"].reason == "unreadable"
    assert skipped["dangling.py"].detail


def test_a_file_that_is_not_utf8_is_recorded_and_skipped(tmp_path):
    root = _app_tree(tmp_path)
    (root / "latin.py").write_bytes(b'PADDOCK = "caf\xe9"\n')

    report = port.analyze(root)

    reasons = {Path(s.file).name: s.reason for s in report.skipped}
    assert reasons == {"latin.py": "undecodable"}
    assert report.recognized_constructs > 0


def test_a_file_with_nul_bytes_is_recorded_and_skipped(tmp_path):
    """A binary blob named ``.py``. CPython 3.14 reports NULs as a SyntaxError.

    Older CPythons raised ``ValueError`` here, which is why the analyzer catches
    that too (``invalid-source``); what this pins is that the file is skipped and
    named rather than ending the run, under whichever of the two it raises.
    """
    root = _app_tree(tmp_path)
    (root / "binary.py").write_bytes(b"TREK = 1\x00\n")

    report = port.analyze(root)

    reasons = {Path(s.file).name: s.reason for s in report.skipped}
    assert reasons in ({"binary.py": "syntax-error"}, {"binary.py": "invalid-source"})
    assert report.recognized_constructs > 0


def test_a_value_error_is_classified_as_invalid_source():
    from wreath._port.analyzer import _skip_reason

    assert _skip_reason(ValueError("bad")) == "invalid-source"
    assert _skip_reason(UnicodeDecodeError("utf-8", b"", 0, 1, "x")) == "undecodable"


def test_a_module_nested_past_the_recursion_limit_is_recorded_and_skipped(tmp_path):
    """A generated module can exceed the parser's stack budget; that is one file."""
    root = _app_tree(tmp_path)
    (root / "generated.py").write_text(
        "PADDOCKS = " + "+".join(["1"] * 100_000) + "\n", encoding="utf-8"
    )

    report = port.analyze(root)

    reasons = {Path(s.file).name: s.reason for s in report.skipped}
    assert reasons == {"generated.py": "too-deep"}
    assert report.recognized_constructs > 0


def test_a_syntax_error_is_now_reported_rather_than_silently_dropped(tmp_path):
    root = _app_tree(tmp_path)
    (root / "python2.py").write_text("print 'llama'\n", encoding="utf-8")

    report = port.analyze(root)

    reasons = {Path(s.file).name: s.reason for s in report.skipped}
    assert reasons == {"python2.py": "syntax-error"}


def test_a_skipped_file_is_named_once_not_once_per_pass(tmp_path):
    """The tree is read twice (index, then analysis); the skip is reported once."""
    root = _app_tree(tmp_path)
    (root / "python2.py").write_text("print 'llama'\n", encoding="utf-8")

    report = port.analyze(root)

    assert len(report.skipped) == 1


def test_skips_are_visible_in_both_renderings(tmp_path):
    root = _app_tree(tmp_path)
    (root / "python2.py").write_text("print 'llama'\n", encoding="utf-8")

    report = port.analyze(root)

    assert [s["reason"] for s in report.to_json()["skipped"]] == ["syntax-error"]
    markdown = report.to_markdown()
    assert "could not be analyzed" in markdown
    assert "python2.py" in markdown


def test_skipped_files_are_outside_the_coverage_fraction(tmp_path):
    """Documented semantics: coverage describes the files that *were* read.

    A skipped file has no classified constructs, so it moves neither numerator
    nor denominator — which is exactly why the skip list has to be printed next
    to the number.
    """
    root = _app_tree(tmp_path)
    clean = port.analyze(root)

    (root / "python2.py").write_text("print 'llama'\n", encoding="utf-8")
    with_skip = port.analyze(root)

    assert with_skip.coverage_overall() == clean.coverage_overall()
    assert with_skip.recognized_constructs == clean.recognized_constructs
    assert with_skip.files_analyzed == clean.files_analyzed == 1
    assert len(with_skip.skipped) == 1


def test_an_unlistable_directory_is_recorded_not_silently_dropped(tmp_path):
    root = _app_tree(tmp_path)
    locked = root / "locked"
    locked.mkdir()
    (locked / "hidden.py").write_text(_APP, encoding="utf-8")
    locked.chmod(0o000)
    try:
        report = port.analyze(root)
    finally:
        locked.chmod(0o755)

    if report.files_analyzed == 2:  # running as root: the mode bit does not apply
        pytest.skip("running with privileges that ignore directory permissions")
    assert report.files_analyzed == 1
    assert [Path(s.file).name for s in report.skipped] == ["locked"]
    assert report.skipped[0].reason == "unreadable"


def test_merge_carries_skips_and_file_counts(tmp_path):
    first = _app_tree(tmp_path)
    (first / "python2.py").write_text("print 'llama'\n", encoding="utf-8")
    second = tmp_path / "other"
    second.mkdir()
    (second / "main.py").write_text(_APP, encoding="utf-8")

    merged = port.analyze_all([first, second])

    assert merged.files_analyzed == 2
    assert len(merged.skipped) == 1


# -- 3. infrastructure directories are not application code -------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_virtualenv_inside_the_tree_is_not_walked(tmp_path):
    """Detected by its ``pyvenv.cfg`` marker, so an unconventional name is caught."""
    root = _app_tree(tmp_path)
    for venv_name in (".venv", "herd-env"):  # dotted convention, and neither
        venv = root / venv_name
        _write(venv / "pyvenv.cfg", "home = /usr/bin\n")
        _write(venv / "lib" / "python3.14" / "site-packages" / "dep" / "api.py", _APP)

    report = port.analyze(root)

    assert report.files_analyzed == 1
    assert {f.file for f in report.findings} == {"main.py"}


def test_conventional_infrastructure_directories_are_pruned(tmp_path):
    root = _app_tree(tmp_path)
    for rel in (
        "__pycache__/main.py",
        "node_modules/paddock/api.py",
        ".git/hooks/api.py",
        ".tox/py314/api.py",
        ".mypy_cache/api.py",
        "build/lib/app/main.py",
        "dist/app/main.py",
        "app.egg-info/api.py",
        "vendored/site-packages/dep/api.py",
    ):
        _write(root / rel, _APP)

    report = port.analyze(root)

    assert report.files_analyzed == 1
    assert {f.file for f in report.findings} == {"main.py"}


def test_pruning_is_not_over_eager(tmp_path):
    """Names that merely *look* like infrastructure are still application code."""
    root = _app_tree(tmp_path)
    for rel in ("env/settings.py", "builders/api.py", "distribution/api.py",
                "venvironment/api.py", "tests/test_api.py"):
        _write(root / rel, _APP)

    report = port.analyze(root)

    assert report.files_analyzed == 6


def test_a_root_that_is_itself_a_virtualenv_is_still_analyzed(tmp_path):
    """The marker prunes *nested* environments; an explicitly named root is honored."""
    root = _app_tree(tmp_path)
    (root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    report = port.analyze(root)

    assert report.files_analyzed == 1


def test_a_symlinked_directory_is_not_followed_out_of_the_tree(tmp_path):
    """A link out of the named tree must not widen the walk."""
    outside = tmp_path / "outside"
    _write(outside / "api.py", _APP)
    root = _app_tree(tmp_path)
    (root / "linked").symlink_to(outside, target_is_directory=True)

    report = port.analyze(root)

    assert report.files_analyzed == 1
