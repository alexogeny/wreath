"""The golden regeneration tool, and the drift it exists to have caught.

`tests/port/golden/README.md` carried the regeneration procedure as a snippet
with a hardcoded list of four `tumbleweed_api` modules. A fifth golden was added
later under a different app, and the snippet said nothing about skipping it — so
"regenerate the goldens" quietly regenerated four fifths of them. The first test
here is that specific failure, expressed as a property: whatever is in the golden
tree is what gets checked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wreath._devtools import port_golden
from wreath._devtools.native_lint import repo_root

SOURCE = """\
from fastapi import APIRouter

router = APIRouter()


@router.get("/llamas")
async def list_llamas():
    return {"ok": True}
"""


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Two goldens under two different apps — the shape the snippet got wrong."""
    corpus = tmp_path / port_golden.CORPUS_DIR
    golden = tmp_path / port_golden.GOLDEN_DIR
    for app in ("tumbleweed_api", "summit_ops"):
        (corpus / app).mkdir(parents=True)
        (corpus / app / "routes.py").write_text(SOURCE)
        # An empty `.expected` is how a module is nominated for pinning: the
        # pinned set is the golden tree, so `--update` fills it in rather than
        # deciding for itself which corpus modules deserve a golden.
        (golden / app).mkdir(parents=True)
        (golden / app / "routes.py.expected").touch()
    port_golden.check(tmp_path, update=True)
    return tmp_path


def test_the_repositorys_own_goldens_are_current() -> None:
    """The real thing: `wreath-port-golden` is clean on this repository."""
    findings, seen = port_golden.check(repo_root())
    assert seen > 0, "no goldens found; the tool would report a false clean"
    assert findings == [], "\n".join(f.render() for f in findings)


def test_every_app_in_the_golden_tree_is_checked(fake_repo: Path) -> None:
    """The drift the hardcoded snippet allowed: a whole app going unnoticed."""
    _, seen = port_golden.check(fake_repo)
    assert seen == 2

    stale = fake_repo / port_golden.GOLDEN_DIR / "summit_ops" / "routes.py.expected"
    stale.write_text("# stale\n")

    findings, _ = port_golden.check(fake_repo)
    assert [f.reason for f in findings] == [port_golden.DRIFT]
    assert "summit_ops" in findings[0].golden


def test_update_rewrites_drift_and_then_runs_clean(fake_repo: Path) -> None:
    stale = fake_repo / port_golden.GOLDEN_DIR / "summit_ops" / "routes.py.expected"
    stale.write_text("# stale\n")

    findings, _ = port_golden.check(fake_repo, update=True)
    assert [f.updated for f in findings] == [True]
    assert port_golden.check(fake_repo) == ([], 2)


def test_a_golden_whose_source_is_gone_is_reported_not_rewritten(fake_repo: Path) -> None:
    """An orphan still passes its own test, because no case is generated for it."""
    (fake_repo / port_golden.CORPUS_DIR / "summit_ops" / "routes.py").unlink()

    findings, _ = port_golden.check(fake_repo, update=True)

    assert [f.reason for f in findings] == [port_golden.MISSING_SOURCE]
    assert findings[0].updated is False


def test_a_source_that_does_not_emit_is_reported_not_rewritten(fake_repo: Path) -> None:
    """A failed emit must never overwrite the last known-good golden."""
    broken = fake_repo / port_golden.CORPUS_DIR / "summit_ops" / "routes.py"
    broken.write_text("def (:\n")
    golden = fake_repo / port_golden.GOLDEN_DIR / "summit_ops" / "routes.py.expected"
    before = golden.read_text()

    findings, _ = port_golden.check(fake_repo, update=True)

    assert [f.reason for f in findings] == [port_golden.EMIT_FAILED]
    assert golden.read_text() == before


def test_main_exits_2_when_there_are_no_goldens(tmp_path: Path, monkeypatch) -> None:
    """Nothing found is about the run, not the code — and must not read clean."""
    (tmp_path / port_golden.GOLDEN_DIR).mkdir(parents=True)
    monkeypatch.setattr(port_golden, "repo_root", lambda: tmp_path)

    assert port_golden.main([]) == port_golden.EXIT_NOT_RUN


def test_main_exits_1_on_drift_and_0_after_update(fake_repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(port_golden, "repo_root", lambda: fake_repo)
    (fake_repo / port_golden.GOLDEN_DIR / "summit_ops" / "routes.py.expected").write_text("x\n")

    assert port_golden.main([]) == port_golden.EXIT_WORK_REMAINS
    assert port_golden.main(["--update"]) == port_golden.EXIT_OK
    assert port_golden.main([]) == port_golden.EXIT_OK
