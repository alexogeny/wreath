from __future__ import annotations

from pathlib import Path

import pytest

from wreath._audit import scan as scan_module
from wreath._audit.scan import scan_paths

CLEAN = "def handler(request):\n    return {'ok': True}\n"
UNSAFE = "def handler(request):\n    return eval(request.query['q'])\n"


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every path whose bytes `scan_paths` actually reads.

    Nothing is cleared between tests and nothing needs to be: every test scans
    its own `tmp_path`, so one test's entries can never answer another's.
    """
    seen: list[Path] = []
    original = Path.read_text

    def counting(self: Path, *args: object, **kwargs: object) -> str:
        seen.append(self)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", counting)
    return seen


def _rules(root: Path) -> set[str]:
    return {finding.rule_id for finding in scan_paths([root], include_tests=True).findings}


def test_an_unchanged_tree_is_not_read_a_second_time(tmp_path: Path, counted: list[Path]) -> None:
    module = tmp_path / "handlers.py"
    module.write_text(UNSAFE, encoding="utf-8")

    first = _rules(tmp_path)
    assert "dynamic-import" in first, first
    assert counted == [module]

    second = _rules(tmp_path)
    assert second == first
    assert counted == [module], "the second scan re-read a file nothing had touched"


def test_a_changed_file_is_read_again_and_its_new_findings_win(
    tmp_path: Path, counted: list[Path]
) -> None:
    module = tmp_path / "handlers.py"
    module.write_text(UNSAFE, encoding="utf-8")
    assert "dynamic-import" in _rules(tmp_path)

    # A different length as well as a different mtime, so this passes whatever
    # the filesystem's timestamp granularity turns out to be.
    module.write_text(CLEAN, encoding="utf-8")
    assert "dynamic-import" not in _rules(tmp_path)
    assert counted == [module, module]


def test_a_rewrite_of_identical_length_is_still_noticed(
    tmp_path: Path, counted: list[Path]
) -> None:
    module = tmp_path / "handlers.py"
    module.write_text(UNSAFE, encoding="utf-8")
    assert "dynamic-import" in _rules(tmp_path)

    same_length = UNSAFE.replace("eval(", "int (")
    assert len(same_length) == len(UNSAFE)
    module.write_text(same_length, encoding="utf-8")
    assert "dynamic-import" not in _rules(tmp_path)


def test_the_cache_holds_one_entry_per_path(tmp_path: Path, counted: list[Path]) -> None:
    module = tmp_path / "handlers.py"
    for index in range(5):
        module.write_text(f"{UNSAFE}# revision {index}\n", encoding="utf-8")
        _rules(tmp_path)
    mine = [path for path in scan_module._SCANNED if path.is_relative_to(tmp_path)]
    assert mine == [module]
