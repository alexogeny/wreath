import json

import pytest

from wreath import _test_runner as runner


def test_machine_history_does_not_store_pretty_print_whitespace(tmp_path):
    path = tmp_path / "history.json"
    selected = frozenset("fixture-" + str(index) for index in range(100))
    runner._write_mutation_sample_cache(path, {}, selected, {}, frozenset(), {})
    raw = path.read_bytes()
    document = json.loads(raw)
    assert frozenset(document["mutation_sample"]["selected"]) == selected
    compact = json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    assert raw == compact


@pytest.mark.parametrize("compact", [False, True])
def test_atomic_json_preserves_values_sorting_unicode_and_newline(tmp_path, compact):
    path = tmp_path / "history.json"
    value = {"z": [True, None, 'é\nquote"'], "a": {"b": 1.25, "a": -2}}
    runner._atomic_json(path, value, compact=compact)
    expected = (
        json.dumps(
            value,
            sort_keys=True,
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
        )
        + "\n"
    )
    assert path.read_text() == expected
    assert json.loads(path.read_text()) == value
    assert list(tmp_path.iterdir()) == [path]


def test_human_report_remains_pretty_by_default(tmp_path):
    path = tmp_path / "report.json"
    value = {"z": 2, "a": [1, 3]}
    runner._atomic_json(path, value)
    assert path.read_text() == json.dumps(value, indent=2, sort_keys=True) + "\n"


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("failure", ["serialize", "write", "replace"])
def test_atomic_failure_preserves_existing_file_and_removes_temp(
    tmp_path, monkeypatch, compact, failure
):
    path = tmp_path / "history.json"
    path.write_text("original")

    def fail(*args, **kwargs):
        raise OSError("synthetic atomic failure")

    if failure == "serialize":
        monkeypatch.setattr(runner.json, "dumps", fail)
    elif failure == "write":
        original_write = type(path).write_text

        def partial_write(target, *args, **kwargs):
            original_write(target, "partial")
            raise OSError("synthetic atomic failure")

        monkeypatch.setattr(type(path), "write_text", partial_write)
    else:
        monkeypatch.setattr(runner.os, "replace", fail)
    with pytest.raises(OSError, match="synthetic atomic failure"):
        runner._atomic_json(path, {"key": "value"}, compact=compact)
    assert path.read_text() == "original"
    assert list(tmp_path.iterdir()) == [path]


def test_timing_history_remains_compact_and_preserves_sample_cache(tmp_path):
    path = tmp_path / "history.json"
    runner._write_mutation_sample_cache(path, {}, frozenset({"test-é"}), {}, frozenset(), {})
    before = json.loads(path.read_text())["mutation_sample"]
    report = {
        "finished_at": "2026-09-05T00:00:00Z",
        "exitstatus": 0,
        "wall_seconds": 1.0,
        "workers": 2,
        "counts": {"passed": 1},
        "files": [{"path": "test_a.py", "seconds": 0.25, "outcome": "passed"}],
        "tests": [{"nodeid": "test_a.py::test_a", "seconds": 0.25, "outcome": "passed"}],
    }
    runner._update_history(path, report)
    runner._update_history(path, report)
    raw = path.read_text()
    document = json.loads(raw)
    assert raw == json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    assert document["mutation_sample"] == before
    assert document["tests"]["test_a.py::test_a"]["samples"] == 2
    assert len(document["runs"]) == 2
