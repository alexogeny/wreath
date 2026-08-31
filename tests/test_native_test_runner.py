from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from argparse import Namespace
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from wreath import _native_test_runner as native_runner
from wreath import _pytest_facade as facade
from wreath import _test_runner as test_runner
from wreath._native import _testrunner


def _namespace(*arguments: str) -> Namespace:
    return Namespace(
        collection="auto",
        grid="never",
        history=".wreath/test-history.json",
        mutant="off",
        no_history=True,
        pytest_args=list(arguments),
        report=None,
        slowest=5,
        workers="auto",
    )


def test_fuzz_schedule_is_seeded_reproducible_and_not_collection_order() -> None:
    def contract() -> None:
        return None

    cases = tuple(
        native_runner.Case(
            f"tests/test_policy.py::test_{name}",
            contract,
            (),
            None,
            frozenset(),
        )
        for name in ("alpha", "beta", "gamma", "delta", "epsilon")
    )

    first = native_runner._schedule_fuzz_cases(cases, "seed-a")
    repeated = native_runner._schedule_fuzz_cases(cases, "seed-a")
    other = native_runner._schedule_fuzz_cases(cases, "seed-b")

    assert first == repeated
    assert set(first) == set(cases)
    assert first != cases
    assert first != other


def test_native_core_classifies_pass_skip_and_failure() -> None:
    def passes() -> None:
        return None

    def skips() -> None:
        raise facade.Skipped("not here")

    def fails() -> None:
        raise LookupError("broken")

    results = _testrunner.run(
        (
            ("test_sample.py::test_passes", passes, (), None),
            ("test_sample.py::test_skips", skips, (), None),
            ("test_sample.py::test_fails", fails, (), None),
        ),
        facade.Skipped,
        0,
    )

    assert [(nodeid, outcome) for nodeid, outcome, _, _ in results] == [
        ("test_sample.py::test_passes", "passed"),
        ("test_sample.py::test_skips", "skipped"),
        ("test_sample.py::test_fails", "failed"),
    ]
    assert all(duration >= 0 for _, _, duration, _ in results)
    assert isinstance(results[2][3], LookupError)


def test_native_core_brackets_each_case_for_engine_independent_tracing() -> None:
    events: list[tuple[str, str | None]] = []

    _testrunner.run(
        (("test_sample.py::test_passes", lambda: None, (), None),),
        facade.Skipped,
        0,
        lambda node_id, outcome: events.append((node_id, outcome)),
    )

    assert events == [
        ("test_sample.py::test_passes", None),
        ("test_sample.py::test_passes", "passed"),
    ]


def test_native_trace_observer_streams_green_tests_and_worker_release(
    tmp_path: Path,
) -> None:
    class Tracer:
        def __init__(self) -> None:
            self.hits: dict[str, set[tuple[str, int]]] = {}

        def begin(self, node_id: str) -> None:
            self.hits[node_id] = {("/project/policy.py", 7)}

        def end(self) -> None:
            pass

    output = tmp_path / "live.jsonl"
    observer = native_runner._TraceObserver(Tracer(), output)

    observer("tests/test_policy.py::test_refuses", None)
    observer("tests/test_policy.py::test_refuses", "passed")
    observer.finish()

    events = [json.loads(line) for line in output.read_text().splitlines()]
    assert events == [
        {
            "nodeid": "tests/test_policy.py::test_refuses",
            "hits": [["/project/policy.py", 7]],
        },
        {"event": "worker_finished"},
    ]


def test_native_worker_stream_names_only_the_case_it_is_actually_running() -> None:
    read_descriptor, write_descriptor = os.pipe()
    observer = native_runner._WorkerProgressObserver(write_descriptor, None)

    observer("tests/test_policy.py::test_one", None)
    observer("tests/test_policy.py::test_one", "passed")
    observer.finish()

    encoded = b""
    while chunk := os.read(read_descriptor, 65536):
        encoded += chunk
    os.close(read_descriptor)
    events = [json.loads(line) for line in encoded.splitlines()]
    assert events[0] == ["tests/test_policy.py::test_one", None, 0]
    assert events[1][0:2] == ["tests/test_policy.py::test_one", "passed"]
    assert events[1][2] > 0


def test_native_worker_yields_between_cases_until_the_controller_assigns_cpu() -> None:
    read_progress, write_progress = os.pipe()
    read_control, write_control = os.pipe()
    observer = native_runner._WorkerProgressObserver(
        write_progress,
        None,
        control_descriptor=read_control,
        case_count=2,
    )
    threads: list[threading.Thread] = []

    def call(node_id: str, outcome: str | None, *, should_yield: bool) -> None:
        thread = threading.Thread(target=observer, args=(node_id, outcome))
        threads.append(thread)
        thread.start()
        thread.join(timeout=0.05)
        yielded = thread.is_alive()
        if yielded:
            os.write(write_control, b"1")
            thread.join(timeout=1)
        assert yielded is should_yield
        assert not thread.is_alive()

    try:
        call("tests/test_policy.py::test_one", None, should_yield=False)
        call("tests/test_policy.py::test_one", "passed", should_yield=True)
        call("tests/test_policy.py::test_two", None, should_yield=False)
        call("tests/test_policy.py::test_two", "passed", should_yield=False)
    finally:
        os.close(write_control)
        for thread in threads:
            thread.join(timeout=1)
        observer.finish()
        while os.read(read_progress, 65536):
            pass
        os.close(read_progress)


def test_finished_worker_keeps_ownership_until_buffered_progress_is_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wreath._test_runner import ActivityRenderer, RunActivity

    def passes() -> None:
        return None

    cases = tuple(
        native_runner.Case(
            f"tests/test_buffered.py::test_{ordinal}",
            passes,
            (),
            None,
            frozenset(),
        )
        for ordinal in range(4)
    )
    collection = native_runner.Collection(cases, ())
    activity = RunActivity(workers=1)
    renderer = ActivityRenderer(
        activity,
        stream=StringIO(),
        mode="never",
        slowest=0,
    )
    monkeypatch.setattr(
        native_runner.select,
        "select",
        lambda *_args, **_kwargs: ((), (), ()),
    )

    results, live_fuzz = native_runner._run_parallel(
        collection,
        shards=(cases,),
        max_failures=0,
        renderer=renderer,
        activity=activity,
    )

    assert [result.outcome for result in results] == ["passed"] * 4
    assert live_fuzz is None


def test_worker_reaper_never_waits_for_an_unowned_mutation_or_fuzz_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waited: list[int] = []

    def fake_waitpid(pid: int, options: int) -> tuple[int, int]:
        waited.append(pid)
        assert options == os.WNOHANG
        return (pid, 0) if pid == 22 else (0, 0)

    monkeypatch.setattr(native_runner.os, "waitpid", fake_waitpid)

    assert native_runner._reap_owned_worker((11, 22, 33)) == (22, 0)
    assert waited == [11, 22]


def test_worker_cleanup_refuses_a_process_group_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[int] = []
    monkeypatch.setattr(native_runner.os, "kill", lambda pid, _signal: killed.append(pid))

    with pytest.raises(ValueError, match="positive worker PID"):
        native_runner._terminate_owned_worker(0)

    assert killed == []


def test_native_core_honours_maxfail_without_calling_later_cases() -> None:
    called: list[str] = []

    def fails() -> None:
        raise ValueError("first")

    def later() -> None:
        called.append("later")

    results = _testrunner.run(
        (("first", fails, (), None), ("later", later, (), None)),
        facade.Skipped,
        1,
    )

    assert [result[0] for result in results] == ["first"]
    assert called == []


def test_facade_raises_checks_type_text_and_missing_exception() -> None:
    with facade.raises(ValueError, match="bad value") as captured:
        raise ValueError("a bad value arrived")

    assert str(captured.value) == "a bad value arrived"
    with pytest.raises(AssertionError, match="DID NOT RAISE ValueError"):
        with facade.raises(ValueError):
            pass
    with pytest.raises(AssertionError, match="does not match"):
        with facade.raises(ValueError, match="wanted"):
            raise ValueError("different")


def test_facade_parametrize_records_cases_without_wrapping_function() -> None:
    @facade.mark.parametrize("left,right", [(1, 2), facade.param(3, 4, id="large")])
    def test_add(left: int, right: int) -> None:
        assert left < right

    assert test_add.__name__ == "test_add"
    parameters = test_add.__wreath_parametrize__
    assert parameters[0].names == ("left", "right")
    assert parameters[0].values[1].id == "large"


def test_facade_monkeypatch_restores_items_paths_and_working_directory(
    tmp_path: Path,
) -> None:
    original_directory = Path.cwd()
    original_path = list(sys.path)
    values: dict[str, int] = {}
    patch = facade.MonkeyPatch()

    patch.setitem(values, "answer", 42)
    patch.syspath_prepend(tmp_path)
    patch.chdir(tmp_path)
    patch.undo()

    assert values == {}
    assert sys.path == original_path
    assert Path.cwd() == original_directory


def test_native_collection_and_execution_use_pytest_shaped_nodeids(
    tmp_path: Path, capsys: Any
) -> None:
    test_file = tmp_path / "test_math.py"
    test_file.write_text(
        """\
import pytest

@pytest.mark.parametrize("value", [pytest.param(1, id="one"), 2])
def test_positive(value):
    assert value > 0

@pytest.mark.skip(reason="contract")
def test_skipped():
    raise AssertionError("must not run")
""",
        encoding="utf-8",
    )

    result = native_runner.execute(_namespace(str(test_file)))

    output = capsys.readouterr().out
    assert result == 0
    assert "test_math.py::test_positive[one]" in output
    assert "test_math.py::test_positive[2]" in output
    assert "test_math.py::test_skipped" in output
    assert "2 passed, 1 skipped" in output


def test_native_fixture_dependencies_and_yield_teardown_run_per_case(
    tmp_path: Path, capsys: Any
) -> None:
    events = tmp_path / "events"
    test_file = tmp_path / "test_fixture.py"
    test_file.write_text(
        f"""\
import pytest

@pytest.fixture
def base():
    with open({str(events)!r}, "a") as stream:
        stream.write("setup-base\\n")
    yield 40
    with open({str(events)!r}, "a") as stream:
        stream.write("teardown-base\\n")

@pytest.fixture
def value(base):
    return base + 2

def test_value(value):
    assert value == 42
""",
        encoding="utf-8",
    )

    assert native_runner.execute(_namespace(str(test_file))) == 0

    assert "1 passed" in capsys.readouterr().out
    assert events.read_text(encoding="utf-8").splitlines() == [
        "setup-base",
        "teardown-base",
    ]


def test_native_loads_conftest_autouse_fixtures_before_importing_tests(
    tmp_path: Path, capsys: Any
) -> None:
    marker = tmp_path / "autouse-ran"
    (tmp_path / "conftest.py").write_text(
        f"""\
import pytest

@pytest.fixture(autouse=True)
def audit():
    open({str(marker)!r}, "w").close()
""",
        encoding="utf-8",
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    test_file = nested / "test_native.py"
    test_file.write_text("def test_ok(): pass\n", encoding="utf-8")

    assert native_runner.execute(_namespace(str(test_file))) == 0

    assert marker.exists()
    assert "1 passed" in capsys.readouterr().out


def test_native_fixture_cycle_is_refused_before_any_body_runs(tmp_path: Path) -> None:
    marker = tmp_path / "body-ran"
    test_file = tmp_path / "test_cycle.py"
    test_file.write_text(
        f"""\
import pytest

@pytest.fixture
def left(right): return right

@pytest.fixture
def right(left): return left

def test_cycle(left):
    open({str(marker)!r}, "w").close()
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"fixture cycle.*left.*right.*left"):
        native_runner.execute(_namespace(str(test_file)))

    assert not marker.exists()


def test_native_module_fixture_is_shared_and_torn_down_after_last_case(
    tmp_path: Path, capsys: Any
) -> None:
    events = tmp_path / "module-events"
    test_file = tmp_path / "test_module_scope.py"
    test_file.write_text(
        f"""\
import pytest

@pytest.fixture(scope="module")
def shared():
    with open({str(events)!r}, "a") as stream: stream.write("setup\\n")
    yield object()
    with open({str(events)!r}, "a") as stream: stream.write("teardown\\n")

def test_first(shared):
    assert shared is not None

def test_second(shared):
    assert shared is not None
""",
        encoding="utf-8",
    )

    assert native_runner.execute(_namespace(str(test_file))) == 0

    assert "2 passed" in capsys.readouterr().out
    assert events.read_text(encoding="utf-8").splitlines() == ["setup", "teardown"]


def test_native_runs_an_async_test_body(tmp_path: Path, capsys: Any) -> None:
    test_file = tmp_path / "test_async.py"
    test_file.write_text(
        """\
import asyncio
import pytest

@pytest.mark.asyncio
async def test_async_contract():
    await asyncio.sleep(0)
    assert True
""",
        encoding="utf-8",
    )

    assert native_runner.execute(_namespace(str(test_file))) == 0
    assert "1 passed" in capsys.readouterr().out


def test_native_async_fixture_tears_down_in_the_test_event_loop(
    tmp_path: Path, capsys: Any
) -> None:
    events = tmp_path / "async-events"
    test_file = tmp_path / "test_async_fixture.py"
    test_file.write_text(
        f"""\
import asyncio
import pytest

@pytest.fixture
async def resource():
    loop = asyncio.get_running_loop()
    with open({str(events)!r}, "a") as stream: stream.write("setup\\n")
    yield loop
    assert asyncio.get_running_loop() is loop
    with open({str(events)!r}, "a") as stream: stream.write("teardown\\n")

@pytest.mark.asyncio
async def test_resource(resource):
    assert asyncio.get_running_loop() is resource
""",
        encoding="utf-8",
    )

    assert native_runner.execute(_namespace(str(test_file))) == 0
    assert "1 passed" in capsys.readouterr().out
    assert events.read_text(encoding="utf-8").splitlines() == ["setup", "teardown"]


def test_native_collects_a_fresh_test_class_instance_per_case(tmp_path: Path, capsys: Any) -> None:
    test_file = tmp_path / "test_class.py"
    test_file.write_text(
        """\
import pytest

class TestContract:
    @pytest.mark.parametrize("value", [1, 2])
    def test_value(self, value, tmp_path):
        assert value > 0
        assert tmp_path.is_dir()
        self.seen = value
""",
        encoding="utf-8",
    )

    assert native_runner.execute(_namespace(str(test_file))) == 0
    output = capsys.readouterr().out
    assert "test_class.py::TestContract::test_value[1]" in output
    assert "2 passed" in output


def test_native_capsys_returns_and_resets_captured_streams(tmp_path: Path, capsys: Any) -> None:
    test_file = tmp_path / "test_capture.py"
    test_file.write_text(
        """\
import sys

def test_capture(capsys):
    print("first")
    print("problem", file=sys.stderr)
    first = capsys.readouterr()
    assert first.out == "first\\n"
    assert first.err == "problem\\n"
    assert capsys.readouterr().out == ""
""",
        encoding="utf-8",
    )

    assert native_runner.execute(_namespace(str(test_file))) == 0
    assert "1 passed" in capsys.readouterr().out


def test_native_parametrized_fixture_exposes_request_param_and_ids(
    tmp_path: Path, capsys: Any
) -> None:
    test_file = tmp_path / "test_fixture_params.py"
    test_file.write_text(
        """\
import pytest

@pytest.fixture(params=[1, 2], ids=lambda value: f"value-{value}")
def number(request):
    return request.param

def test_number(number):
    assert number > 0
""",
        encoding="utf-8",
    )

    assert native_runner.execute(_namespace(str(test_file))) == 0
    output = capsys.readouterr().out
    assert "test_number[value-1]" in output
    assert "test_number[value-2]" in output
    assert "2 passed" in output


def test_native_run_restores_an_existing_pytest_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = ModuleType("pytest")
    monkeypatch.setitem(native_runner.sys.modules, "pytest", sentinel)
    test_file = tmp_path / "test_empty.py"
    test_file.write_text("def test_ok(): pass\n", encoding="utf-8")

    assert native_runner.execute(_namespace(str(test_file))) == 0
    assert native_runner.sys.modules["pytest"] is sentinel


def test_native_arguments_refuse_unknown_pytest_options(tmp_path: Path) -> None:
    test_file = tmp_path / "test_empty.py"
    test_file.write_text("def test_ok(): pass\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"--durations.*native options"):
        native_runner.execute(_namespace("--durations=5", str(test_file)))


def test_native_module_mark_participates_in_marker_selection(tmp_path: Path, capsys: Any) -> None:
    test_file = tmp_path / "test_marked.py"
    test_file.write_text(
        """\
import pytest
pytestmark = pytest.mark.contract

def test_selected():
    pass
""",
        encoding="utf-8",
    )

    assert native_runner.execute(_namespace("-m", "contract", str(test_file))) == 0
    assert "test_selected" in capsys.readouterr().out


def test_native_refuses_xfail_instead_of_treating_it_as_an_inert_label(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_xfail.py"
    test_file.write_text(
        """\
import pytest

@pytest.mark.xfail(reason="not native")
def test_expected_failure():
    raise AssertionError("expected")
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"pytest.mark.xfail.*--engine pytest"):
        native_runner.execute(_namespace(str(test_file)))


def test_native_reports_a_syntax_error_as_a_collection_error(tmp_path: Path) -> None:
    test_file = tmp_path / "test_broken.py"
    test_file.write_text("def test_broken(:\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"test_broken.py could not be imported: SyntaxError"):
        native_runner.execute(_namespace(str(test_file)))


def test_native_honours_conftest_collect_ignore_glob(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    ignored = tests / "corpus"
    ignored.mkdir(parents=True)
    (tests / "conftest.py").write_text('collect_ignore_glob = ["corpus/*"]\n', encoding="utf-8")
    (tests / "test_kept.py").write_text("def test_kept(): pass\n", encoding="utf-8")
    (ignored / "test_foreign.py").write_text(
        "import dependency_that_must_not_be_imported\n", encoding="utf-8"
    )

    collection = native_runner.collect(native_runner.Options((tests,)))

    assert len(collection.cases) == 1
    assert collection.cases[0].node_id.endswith("/tests/test_kept.py::test_kept")


def test_native_parses_each_collect_ignore_owner_once_per_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    conftest = tests / "conftest.py"
    conftest.write_text("collect_ignore = []\n", encoding="utf-8")
    (tests / "test_one.py").write_text("def test_one(): pass\n", encoding="utf-8")
    (tests / "test_two.py").write_text("def test_two(): pass\n", encoding="utf-8")
    parsed: list[str] = []
    original_parse = native_runner.ast.parse

    def counting_parse(source: str, *, filename: str) -> Any:
        if filename == str(conftest):
            parsed.append(filename)
        return original_parse(source, filename=filename)

    monkeypatch.setattr(native_runner.ast, "parse", counting_parse)

    collection = native_runner.collect(native_runner.Options((tests,)))

    assert len(collection.cases) == 2
    assert parsed == [str(conftest)]


def test_native_reads_the_default_marker_expression_from_pyproject(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """\
[tool.pytest.ini_options]
addopts = "-q -m 'not network and not performance'"
""",
        encoding="utf-8",
    )

    assert native_runner._configured_markers(tmp_path) == ("not network and not performance")


def test_native_compiles_marker_selection_for_direct_mark_membership() -> None:
    matcher = native_runner._compile_matcher(
        "not network and (contract or unit)",
        exact_atoms=True,
    )

    assert matcher(frozenset({"contract"}))
    assert matcher(frozenset({"unit", "slow"}))
    assert not matcher(frozenset({"contract", "network"}))


def test_exact_native_selection_uses_the_precompiled_collection_index() -> None:
    node_id = "tests/test_index.py::test_index"
    called: list[str] = []

    def test_index() -> None:
        called.append("ran")

    case = native_runner.Case(node_id, test_index, (), None, frozenset())
    collection = native_runner.Collection(
        (),
        (),
        index={node_id: case},
    )

    results = native_runner.run_selected(collection, (node_id,), max_failures=1)

    assert [result.outcome for result in results] == ["passed"]
    assert called == ["ran"]


def test_native_worker_payload_round_trips_compact_results_and_trace_hits() -> None:
    results = (
        native_runner.Result("tests/test_ipc.py::test_pass", "passed", 17, None),
        native_runner.Result(
            "tests/test_ipc.py::test_fail",
            "failed",
            23,
            LookupError("broken"),
        ),
    )
    hits = (("src/wreath/example.py:9", ("tests/test_ipc.py::test_pass",)),)

    encoded = native_runner._encode_worker_payload(results, hits)
    rows, decoded_hits, error = native_runner._decode_worker_payload(encoded)

    assert rows == [
        ["tests/test_ipc.py::test_pass", "passed", 17, None],
        [
            "tests/test_ipc.py::test_fail",
            "failed",
            23,
            "LookupError: broken\n",
        ],
    ]
    assert decoded_hits == [["src/wreath/example.py:9", ["tests/test_ipc.py::test_pass"]]]
    assert error is None


def test_dual_refuses_collection_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = native_runner.Collection(cases=(), modules=())
    monkeypatch.setattr(native_runner, "collect", lambda options: native)
    monkeypatch.setattr(
        native_runner,
        "_pytest_collect",
        lambda arguments: (0, ("tests/test_other.py::test_other",)),
    )

    with pytest.raises(ValueError, match="collection differs"):
        native_runner.execute_dual(_namespace("tests"))


def test_dual_refuses_outcome_drift_after_identical_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id = "tests/test_same.py::test_same"
    case = native_runner.Case(node_id, lambda: None, (), None, frozenset())
    native = native_runner.Collection(
        cases=(case,),
        modules=(),
        index={node_id: case},
    )
    monkeypatch.setattr(native_runner, "collect", lambda options: native)
    monkeypatch.setattr(
        native_runner,
        "_pytest_collect",
        lambda arguments: (0, (node_id,)),
    )
    monkeypatch.setattr(
        native_runner,
        "_run",
        lambda collection, max_failures: [native_runner.Result(node_id, "passed", 1, None)],
    )
    monkeypatch.setattr(
        native_runner,
        "_pytest_execute",
        lambda arguments: (1, ((node_id, "failed"),)),
    )

    with pytest.raises(ValueError, match="outcomes differ"):
        native_runner.execute_dual(_namespace("tests"))


def test_dual_accepts_identical_outcomes_after_identical_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id = "tests/test_same.py::test_same"
    case = native_runner.Case(node_id, lambda: None, (), None, frozenset())
    native = native_runner.Collection(
        cases=(case,),
        modules=(),
        index={node_id: case},
    )
    monkeypatch.setattr(native_runner, "collect", lambda options: native)
    monkeypatch.setattr(
        native_runner,
        "_pytest_collect",
        lambda arguments: (0, (node_id,)),
    )
    monkeypatch.setattr(
        native_runner,
        "_run",
        lambda collection, max_failures: [native_runner.Result(node_id, "passed", 1, None)],
    )
    monkeypatch.setattr(
        native_runner,
        "_pytest_execute",
        lambda arguments: (0, ((node_id, "passed"),)),
    )

    assert native_runner.execute_dual(_namespace("tests")) == 0


def test_non_sampled_mutation_runs_without_a_trace_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id = "tests/test_green.py::test_green"
    case = native_runner.Case(node_id, lambda: None, (), None, frozenset())
    collection = native_runner.Collection(
        cases=(case,),
        modules=(),
        index={node_id: case},
    )
    namespace = _namespace("tests")
    namespace.mutant = "on"
    namespace.mutant_workers = "auto"
    namespace.fuzz = "off"
    namespace.stage_events = None
    namespace.case_selection = None

    monkeypatch.setattr(native_runner, "collect", lambda options: collection)
    monkeypatch.setattr(native_runner, "_native_shards", lambda *_a: ((case,),))
    monkeypatch.setattr(native_runner, "_configured_markers", lambda: "")
    monkeypatch.setattr(test_runner, "_mutation_arguments", lambda _namespace: [])
    monkeypatch.setattr(test_runner, "_resolve_workers", lambda _workers: 1)
    monkeypatch.setattr(test_runner, "_resolve_mutant_workers", lambda _workers: 1)
    monkeypatch.setattr(
        test_runner,
        "_mutation_confidence",
        lambda *_a, **_k: ({"counts": {}}, 0),
    )
    monkeypatch.setattr(
        test_runner,
        "_mutation_activity_from_report",
        lambda mode, report: test_runner.MutationActivity(mode=mode, state="complete"),
    )

    def run_parallel(*_args: object, **options: Any):
        activity = options["activity"]
        activity.start_native_tests((node_id,))
        activity.add_native_result(node_id, "passed", 0.001)
        return [native_runner.Result(node_id, "passed", 1, None)], None

    monkeypatch.setattr(native_runner, "_run_parallel", run_parallel)

    assert native_runner.execute(namespace) == 0


def test_dual_engine_agrees_with_pytest_on_the_supported_contract(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_contract.py"
    test_file.write_text(
        """\
import pytest

@pytest.fixture
def base():
    yield 40

@pytest.mark.parametrize("value", [pytest.param(1, id="one"), 2])
def test_positive(value):
    assert value > 0

@pytest.mark.parametrize("left", [1, 2])
@pytest.mark.parametrize("right", [3, 4])
def test_stacked(left, right):
    assert left < right

def test_raises():
    with pytest.raises(ValueError, match="contract"):
        raise ValueError("native contract")

class TestFixtureContract:
    @pytest.mark.parametrize("increment", [1, 2])
    def test_fixture_value(self, base, increment):
        assert base + increment > 40

@pytest.mark.asyncio
async def test_async_contract(base):
    assert base == 40

@pytest.mark.skip(reason="oracle")
def test_skip():
    raise AssertionError("must not run")
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "wreath._cli",
            "test",
            "--engine",
            "dual",
            "--mutant",
            "off",
            str(test_file),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "10 passed, 1 skipped" in completed.stdout
