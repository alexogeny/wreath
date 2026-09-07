import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from wreath._mutant import runner


@pytest.fixture
def watch_source(monkeypatch, tmp_path):
    source = tmp_path / "fixture.py"
    source.write_text("LIMIT_A = 1\nLIMIT_B = 2\nLIMIT_C = 3\n")
    module = ModuleType("watch_fixture")
    module.__dict__.update(LIMIT_A=1, LIMIT_B=2, LIMIT_C=3)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(runner, "discover", lambda roots: [source])
    monkeypatch.setattr(runner, "module_name_for", lambda path: "watch_fixture")
    return source


def test_selected_values_read_and_split_source_once(monkeypatch, watch_source):
    counts = {"read": 0, "split": 0}
    original = Path.read_text

    class SourceText(str):
        def splitlines(self, *args, **kwargs):
            counts["split"] += 1
            return super().splitlines(*args, **kwargs)

    def read(path, *args, **kwargs):
        counts["read"] += 1
        return SourceText(original(path, *args, **kwargs))

    monkeypatch.setattr(Path, "read_text", read)
    selected = frozenset(f"value.widen-bound@fixture.py:{line}" for line in (1, 2, 3))
    root = watch_source.parent
    result = runner.watch_selected_identifiers([root], root, selected)
    assert result == ({str(watch_source): frozenset((1, 2, 3))}, frozenset((str(watch_source),)))
    assert counts == {"read": 1, "split": 1}


@pytest.mark.parametrize("ending", ["", "\n", "\n\n", "\r\n", "\r"])
def test_value_watch_retains_splitlines_extent(watch_source, ending):
    text = "LIMIT_A = 1\n# separator\vstill a comment" + ending
    watch_source.write_text(text)
    root = watch_source.parent
    watched, whole = runner.watch_selected_identifiers(
        [root], root, frozenset(("value.widen-bound@fixture.py:1",))
    )
    assert watched == {str(watch_source): frozenset(range(1, len(text.splitlines()) + 1))}
    assert whole == frozenset((str(watch_source),))


@pytest.mark.parametrize("value_first", [True, False])
def test_duplicate_selection_and_mixed_watches(monkeypatch, watch_source, value_first):
    candidates = [
        SimpleNamespace(operator="test", line=1, kind="code", watch=(2, 7)),
        SimpleNamespace(operator="test", line=1, kind="code", watch=(3, 9)),
    ]
    value = SimpleNamespace(operator="value", line=2, kind="value", watch=())
    candidates.insert(0 if value_first else 2, value)
    monkeypatch.setattr(runner, "scan", lambda tree, name: candidates)
    root = watch_source.parent
    watched, whole = runner.watch_selected_identifiers(
        [root], root, frozenset(("test@fixture.py:1#1", "value@fixture.py:2"))
    )
    assert watched == {str(watch_source): frozenset((1, 2, 3, 9))}
    assert whole == frozenset((str(watch_source),))


@pytest.mark.parametrize("selected", [frozenset(), frozenset(("value@other.py:1",))])
def test_unselected_sources_are_not_read(monkeypatch, watch_source, selected):
    def refuse(*args, **kwargs):
        raise AssertionError("unselected source was read")

    monkeypatch.setattr(Path, "read_text", refuse)
    root = watch_source.parent
    assert runner.watch_selected_identifiers([root], root, selected) == ({}, frozenset())


@pytest.mark.parametrize(
    "error", [OSError("unreadable"), SyntaxError("invalid"), ValueError("null")]
)
def test_selected_source_errors_remain_ignored(monkeypatch, watch_source, error):
    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(Path, "read_text", refuse)
    root = watch_source.parent
    selected = frozenset(("value.widen-bound@fixture.py:1",))
    assert runner.watch_selected_identifiers([root], root, selected) == ({}, frozenset())
