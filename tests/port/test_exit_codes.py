"""What ``wreath port`` tells CI, and what emit mode does with a file it cannot read.

Two follow-ons from the robustness pass. The report renderings learned to say
"n/a" and ``null`` when nothing was recognized, but the process still said "fine"
— and CI reads the process, not the markdown. And the emit path still had the
unguarded read that the analysis path had just lost.

The codes follow the house convention (``wreath docs``, ``wreath inspect``):
``2`` never got started, ``1`` ran with something to report, ``0`` ran clean.
The two tests that carry the convention are the already-ported tree exiting
``0`` and the wrong directory exiting ``2`` — everything else is detail.
"""
from argparse import Namespace
from pathlib import Path

import pytest

port = pytest.importorskip("wreath.port")

from wreath._port.cli import (  # noqa: E402  (after importorskip, by design)
    EXIT_NOT_RUN,
    EXIT_OK,
    EXIT_WORK_REMAINS,
    execute,
)

_APP = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/llamas")
async def list_llamas():
    return []
"""

# boto3 is `ext.boto3`: "not a framework feature; keep the external library" —
# an `unsupported` verdict that does not depend on any ORM fixture.
_UNSUPPORTED_APP = """\
import boto3
from fastapi import FastAPI

app = FastAPI()
s3 = boto3.client("s3")


@app.get("/llamas")
async def list_llamas():
    return []
"""


def _tree(tmp_path: Path, name: str, **files: str) -> Path:
    """Build a tree; each keyword is a module name (``main=...`` -> ``main.py``)."""
    root = tmp_path / name
    root.mkdir()
    for stem, text in files.items():
        (root / f"{stem}.py").write_text(text, encoding="utf-8")
    return root


def _report_args(*roots: Path) -> Namespace:
    return Namespace(source=[str(r) for r in roots], as_json=False)


def _emit_args(*roots: Path, output: Path) -> Namespace:
    return Namespace(source=[str(r) for r in roots], output=str(output),
                     in_place=False, force=False, as_json=False)


# -- report mode: 2 never started, 1 has something to say, 0 is clean ---------


def test_a_clean_port_exits_zero(tmp_path, capsys):
    root = _tree(tmp_path, "app", main=_APP)

    assert execute(_report_args(root)) == EXIT_OK
    capsys.readouterr()


def test_an_already_ported_tree_exits_zero(tmp_path, capsys):
    """The point of the convention. A tree with no FastAPI left in it recognizes
    nothing — and that is a *successful* run with nothing to do, not a failure.
    Anyone re-running `wreath port` as a regression check must see green."""
    root = _tree(tmp_path, "plain", maths="def herd_size(n):\n    return n * 2\n")

    code = execute(_report_args(root))
    capsys.readouterr()

    report = port.analyze(root)
    assert report.files_analyzed > 0, "the file was read"
    assert report.coverage_overall() is None, "and nothing in it was recognized"
    assert not report.skipped
    assert code == EXIT_OK


def test_a_directory_with_nothing_to_analyze_exits_two(tmp_path, capsys):
    """The other half: nothing was analyzed at all, which in practice means the
    path is wrong. `files_analyzed` is what separates this from the case above."""
    root = _tree(tmp_path, "empty")

    code = execute(_report_args(root))
    capsys.readouterr()

    assert port.analyze(root).files_analyzed == 0
    assert code == EXIT_NOT_RUN


def test_unsupported_constructs_exit_one(tmp_path, capsys):
    """The original contract: a pipeline gating on 1 keeps working."""
    root = _tree(tmp_path, "app", main=_UNSUPPORTED_APP)

    code = execute(_report_args(root))
    capsys.readouterr()

    report = port.analyze(root)
    assert report.counts()["unsupported"] > 0, "fixture no longer produces unsupported"
    assert not report.skipped
    assert code == EXIT_WORK_REMAINS


def test_a_skipped_file_is_work_remaining(tmp_path, capsys):
    """A file that could not be read is left for a human, so it is 1 — even
    though everything the run *did* read translated cleanly."""
    root = _tree(tmp_path, "app", main=_APP)
    (root / "dangling.py").symlink_to(root / "no_such_file.py")

    code = execute(_report_args(root))
    capsys.readouterr()

    report = port.analyze(root)
    assert report.recognized_constructs > 0, "the good file was analyzed"
    assert report.skipped, "the bad file was recorded"
    assert not report.counts()["unsupported"], "and nothing in it is unsupported"
    assert code == EXIT_WORK_REMAINS


def test_unsupported_and_skipped_together_are_still_one(tmp_path, capsys):
    """Both conditions collapse into the same code. The report is what tells you
    which you have — and it says so, because a count over a partial tree is a
    lower bound rather than a count."""
    root = _tree(tmp_path, "app", main=_UNSUPPORTED_APP)
    (root / "dangling.py").symlink_to(root / "no_such_file.py")

    code = execute(_report_args(root))
    printed = capsys.readouterr().out

    report = port.analyze(root)
    assert report.counts()["unsupported"] > 0 and report.skipped
    assert code == EXIT_WORK_REMAINS
    assert "skipped" in printed.lower(), "the report names what the code cannot"


def test_the_exit_codes_are_distinct():
    """Three states, three codes — collapsing any two loses the distinction."""
    assert len({EXIT_OK, EXIT_WORK_REMAINS, EXIT_NOT_RUN}) == 3
    assert (EXIT_OK, EXIT_WORK_REMAINS, EXIT_NOT_RUN) == (0, 1, 2)


# -- emit mode: one bad source does not end the run ---------------------------


def test_emit_survives_a_source_it_cannot_read(tmp_path):
    """The defect: `wreath port --output` died on the first unreadable file."""
    root = _tree(tmp_path, "app", main=_APP, other=_APP)
    (root / "dangling.py").symlink_to(root / "no_such_file.py")
    out = tmp_path / "ported"

    result = port.port_tree(root, out)  # must not raise

    assert len(result.written_files) == 2, "both good files were still emitted"
    assert [Path(s.file).name for s in result.failed] == ["dangling.py"]
    assert result.failed[0].reason == "unreadable"
    assert result.failed[0].detail


def test_emit_records_a_source_that_is_not_valid_python(tmp_path):
    root = _tree(tmp_path, "app", main=_APP, broken="def (:\n")
    out = tmp_path / "ported"

    result = port.port_tree(root, out)

    assert len(result.written_files) == 1
    assert [Path(s.file).name for s in result.failed] == ["broken.py"]
    assert result.failed[0].reason == "syntax-error"


def test_emit_records_a_source_that_is_not_utf8(tmp_path):
    root = _tree(tmp_path, "app", main=_APP)
    (root / "latin.py").write_bytes(b'PADDOCK = "caf\xe9"\n')
    out = tmp_path / "ported"

    result = port.port_tree(root, out)

    assert [Path(s.file).name for s in result.failed] == ["latin.py"]
    assert result.failed[0].reason == "undecodable"


def test_a_failure_is_not_a_skip(tmp_path):
    """`skipped` means 'correctly left alone'; `failed` means 'never got there'.

    Folding them together would make an already-ported tree indistinguishable
    from one where every file failed to read.
    """
    root = _tree(tmp_path, "app", main=_APP)
    out = tmp_path / "ported"

    first = port.port_tree(root, out)
    assert first.written_files and not first.skipped and not first.failed

    second = port.port_tree(root, out)  # idempotent re-run
    assert second.skipped, "unchanged source is skipped"
    assert not second.failed, "a skip is a success, not a failure"


def test_an_unwritable_destination_is_fatal(tmp_path):
    """A source that cannot be read costs one file; a destination that cannot be
    written condemns every remaining one, so it must not be swallowed."""
    root = _tree(tmp_path, "app", main=_APP)
    out = tmp_path / "ported"
    out.mkdir()
    out.chmod(0o500)  # readable, not writable
    try:
        with pytest.raises(OSError):
            port.port_tree(root, out)
    finally:
        out.chmod(0o700)


def test_emit_mode_leaves_work_when_a_source_failed(tmp_path, capsys):
    root = _tree(tmp_path, "app", main=_APP)
    (root / "dangling.py").symlink_to(root / "no_such_file.py")
    out = tmp_path / "ported"

    code = execute(_emit_args(root, output=out))
    printed = capsys.readouterr().out

    assert code == EXIT_WORK_REMAINS
    assert "FAILED" in printed and "dangling.py" in printed


def test_emit_mode_exits_zero_when_every_file_was_ported(tmp_path, capsys):
    root = _tree(tmp_path, "app", main=_APP)
    out = tmp_path / "ported"

    assert execute(_emit_args(root, output=out)) == EXIT_OK
    capsys.readouterr()


def test_emit_mode_exits_two_when_there_was_nothing_to_emit(tmp_path, capsys):
    """Same question as report mode's `2`: did you point me at anything?"""
    root = _tree(tmp_path, "empty")
    out = tmp_path / "ported"

    code = execute(_emit_args(root, output=out))
    capsys.readouterr()

    assert code == EXIT_NOT_RUN


def test_emit_mode_re_run_over_an_unchanged_tree_is_clean(tmp_path, capsys):
    """Everything skipped is not the same as nothing found — the second run
    touched every file and decided each needed no work, which is `0`."""
    root = _tree(tmp_path, "app", main=_APP)
    out = tmp_path / "ported"

    assert execute(_emit_args(root, output=out)) == EXIT_OK
    capsys.readouterr()
    assert execute(_emit_args(root, output=out)) == EXIT_OK
    capsys.readouterr()
