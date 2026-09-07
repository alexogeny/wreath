import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from wreath._mutant.runner import build_plan, changed_lines, sample_identifiers


def stub_git(monkeypatch: pytest.MonkeyPatch, diff: str, untracked: str) -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["git", "-C", "/unused"]
        assert kwargs["timeout"] == 60
        return subprocess.CompletedProcess(
            command, 0, diff if command[3] == "diff" else untracked, ""
        )

    monkeypatch.setattr(subprocess, "run", run)


def test_untracked_lines_use_bounded_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_git(monkeypatch, "", "new.py\nnotes.txt\n")
    result = changed_lines(Path("/unused"), "HEAD")
    assert list(result) == ["new.py"]
    lines = result["new.py"]
    assert [line in lines for line in (-1, 0, 1, 500000, 999999, 1000000)] == [
        False,
        False,
        True,
        True,
        True,
        False,
    ]
    assert len(lines) == 999999
    assert sys.getsizeof(lines) < 1024


def test_tracked_hunks_merge_and_untracked_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_git(
        monkeypatch,
        "+++ b/tracked.py\n@@ -1 +3,2 @@\n@@ -8 +9 @@\n"
        "+++ b/new.py\n@@ -0,0 +5 @@\n+++ /dev/null\n@@ -3 +0,0 @@\n",
        "new.py\n",
    )
    result = changed_lines(Path("/unused"), "HEAD")
    assert list(result) == ["tracked.py", "new.py"]
    assert result["tracked.py"] == {3, 4, 9}
    assert 1 in result["new.py"]
    assert 1000000 not in result["new.py"]


def test_untracked_membership_reaches_planning_and_sampling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = tmp_path / "changed_sample"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    target = package / "checks.py"
    target.write_text(
        "def authorize(value):\n    if value == 1:\n        return value\n    return None\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(tmp_path)
    stub_git(monkeypatch, "", str(target))
    plan = build_plan([package], Path("/unused"), changed="HEAD")
    sample = sample_identifiers([package], Path("/unused"), 2, changed="HEAD")
    assert plan.mutations
    assert sample
    assert set(sample) <= {mutation.identifier for mutation in plan.mutations}
