"""The database-skip banner, and the convention that keeps its count honest.

The banner counts skips whose *reason* names ``WREATH_TEST_POSTGRES_DSN``. That
is what makes the count derived rather than a hardcoded 47 that rots the moment
someone adds a suite -- but it only holds while every gated test says so in its
reason. `test_every_gated_module_names_the_variable_in_its_reason` is the pin:
add a gated test that skips with a vaguer reason and it fails, here, loudly,
instead of the count quietly drifting below reality.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import re
import tomllib
from types import SimpleNamespace

import _gated_skips as gated

_SUITE_CONFIG_SPEC = importlib.util.spec_from_file_location(
    "_wreath_suite_config", pathlib.Path(__file__).with_name("conftest.py")
)
if _SUITE_CONFIG_SPEC is None or _SUITE_CONFIG_SPEC.loader is None:
    raise RuntimeError("could not load the root test-suite conftest")
suite_config = importlib.util.module_from_spec(_SUITE_CONFIG_SPEC)
_SUITE_CONFIG_SPEC.loader.exec_module(suite_config)

DSN_ENV = gated.DSN_ENV


class _Report:
    """The two attributes `gated_skip_count` reads off a real report."""

    def __init__(self, skipped: bool, longrepr: object) -> None:
        self.skipped = skipped
        self.longrepr = longrepr


def _skip(reason: str) -> _Report:
    return _Report(True, ("/repo/tests/test_x.py", 12, reason))


def test_the_count_is_derived_from_the_reason_text() -> None:
    reports = [
        _skip(f"Skipped: set {DSN_ENV} to run live jobs integration tests"),
        _skip(f"Skipped: set {DSN_ENV} for real PostgreSQL webhook tests"),
        _skip("Skipped: io_uring unavailable"),
        _skip("Skipped: node>=18 toolchain not available"),
        _Report(False, None),
    ]
    assert gated.gated_skip_count(reports) == 2


def test_a_report_with_no_longrepr_is_not_counted() -> None:
    """xdist and some plugins hand back skips whose longrepr is a bare string."""
    assert gated.gated_skip_count([_Report(True, None)]) == 0
    assert gated.gated_skip_count([_Report(True, "Skipped: whatever")]) == 0


def _skip_calls(tree: ast.AST) -> list[ast.Call]:
    """Every `pytest.skip(...)` and `pytest.mark.skipif(...)` call in a module."""
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in ("skip", "skipif"):
            calls.append(node)
    return calls


def _mentions_dsn(call: ast.Call) -> bool:
    """Does any string literal in this call name the variable?"""
    for node in ast.walk(call):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if DSN_ENV in node.value:
                return True
    return False


def test_every_gated_module_names_the_variable_in_its_reason() -> None:
    """Every module that gates on the DSN must say so where a skip can see it.

    Scans the tree rather than listing files, so a suite that does not exist yet
    is covered the day it is written. A module may mention the variable in prose
    only -- what matters is that a module which *gates* on it puts the name in
    the reason, because the reason is what the banner counts.

    Parsed rather than grepped: this file is full of the variable inside string
    literals, and a text scan flags itself.
    """
    tests = pathlib.Path(__file__).parent
    offenders = []
    for path in sorted(tests.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        if DSN_ENV not in source:
            continue
        calls = _skip_calls(ast.parse(source))
        if not calls:
            continue  # mentions it in prose, gates on nothing
        gates_on_dsn = [call for call in calls if _mentions_dsn(call)]
        if not gates_on_dsn:
            offenders.append(path.relative_to(tests).as_posix())
    assert not offenders, (
        f"these gate on {DSN_ENV} but never name it in a skip reason, so the "
        f"banner will undercount them: {offenders}"
    )


def test_the_banner_reads_differently_when_no_runtime_is_installed() -> None:
    """The two situations are different and must not read the same."""
    with_runtime = "\n".join(gated.banner_lines(9, "podman"))
    without = "\n".join(gated.banner_lines(9, None))

    assert "you can run them" in with_runtime
    assert "podman" in with_runtime
    assert "unverified" not in with_runtime

    # Asserted word by word rather than as a phrase: the banner is hand-wrapped,
    # and a reflow should not be able to fail a test about what it *says*.
    assert "unverified" in without
    assert "no container runtime" in without
    assert with_runtime != without
    for text in (with_runtime, without):
        assert "9 " in text, "the count has to appear in both"
        assert DSN_ENV in text


def test_the_runtime_probe_prefers_the_first_installed() -> None:
    """Driven through a fake `which` so both branches run on any machine."""
    assert gated.container_runtime(lambda name: None) is None
    assert gated.container_runtime(lambda name: "/usr/bin/" + name) == "docker"
    only_podman = gated.container_runtime(
        lambda name: "/usr/bin/podman" if name == "podman" else None
    )
    assert only_podman == "podman"
    only_nerdctl = gated.container_runtime(
        lambda name: "/usr/bin/nerdctl" if name == "nerdctl" else None
    )
    assert only_nerdctl == "nerdctl"


def test_the_banner_pluralises_a_single_skip() -> None:
    single = "\n".join(gated.banner_lines(1, None))
    assert "1 database-backed test did not run" in single
    assert "1 assertion about" in single


class _Mark:
    def __init__(self, name: str) -> None:
        self.name = name


class _Item:
    """The one method `deselected_by_mark` reads off a real item."""

    def __init__(self, *marks: str) -> None:
        self._marks = [_Mark(name) for name in marks]

    def iter_markers(self) -> list[_Mark]:
        return self._marks


def test_the_excluded_marks_come_from_the_expression_not_the_registry() -> None:
    """Registered and *excluded* are different sets.

    The first cut read `config.getini("markers")`, which includes `asyncio`
    because pytest-asyncio registers it -- so three deselected async tests
    reported "3 asyncio, 3 network". Only the expression explains a deselection.
    """
    expr = "not fuzz and not performance and not network and not thesis"
    assert gated.marks_in_expression(expr) == ("fuzz", "performance", "network", "thesis")
    assert gated.marks_in_expression("") == ()
    assert gated.marks_in_expression(None) == ()
    for keyword in ("not", "and", "or"):
        assert keyword not in gated.marks_in_expression(expr)


def test_the_total_counts_tests_while_the_breakdown_counts_marks() -> None:
    """A test carrying two excluded marks is one test, not two.

    Summing the per-mark tallies reported six tests for three.
    """
    items = [_Item("network"), _Item("network", "fuzz"), _Item("asyncio")]
    counts = gated.deselected_by_mark(items, ("network", "fuzz"))

    assert counts[gated.TOTAL] == 2, "the asyncio-only item is not excluded by these marks"
    assert counts["network"] == 2
    assert counts["fuzz"] == 1
    assert "asyncio" not in counts
    assert sum(n for name, n in counts.items() if name != gated.TOTAL) != counts[gated.TOTAL]


def test_worker_counts_merge_by_max_because_every_worker_sees_them_all() -> None:
    """Under xdist each worker collects the whole suite and deselects the same
    items, so all six report the same number. Summing would claim 6x reality."""
    one = {gated.TOTAL: 104, "network": 60}
    merged = gated.merge_worker_counts({}, one)
    for _ in range(5):
        merged = gated.merge_worker_counts(merged, one)
    assert merged[gated.TOTAL] == 104
    assert merged["network"] == 60


def test_a_worker_that_collected_more_wins() -> None:
    """Max, not first-wins: a worker seeing more is the truthful one."""
    merged = gated.merge_worker_counts({gated.TOTAL: 3, "network": 3}, {gated.TOTAL: 9, "fuzz": 6})
    assert merged[gated.TOTAL] == 9
    assert merged["network"] == 3
    assert merged["fuzz"] == 6


def test_disjoint_collection_shards_sum_their_deselections(monkeypatch) -> None:
    monkeypatch.setattr(suite_config, "_DESELECTED", {})
    first = SimpleNamespace(workeroutput={
        "wreath_collection_shard": True,
        "gated_deselected": {gated.TOTAL: 3, "network": 3},
    })
    second = SimpleNamespace(workeroutput={
        "wreath_collection_shard": True,
        "gated_deselected": {gated.TOTAL: 2, "fuzz": 2},
    })

    suite_config.pytest_testnodedown(first, None)
    suite_config.pytest_testnodedown(second, None)

    assert suite_config._DESELECTED == {
        gated.TOTAL: 5,
        "network": 3,
        "fuzz": 2,
    }


def test_the_deselect_banner_asks_for_a_flag_not_a_database() -> None:
    """The two halves must not read alike.

    A skip wants a DSN; a deselection wants `-m ''`, and no database in the
    world changes that. Sending someone to start a container is worse than
    silence.
    """
    text = "\n".join(gated.deselect_lines({gated.TOTAL: 104, "network": 60, "fuzz": 44}))
    assert "104 tests were not collected" in text
    assert "60 network" in text and "44 fuzz" in text
    assert "-m ''" in text
    assert "exits 0" in text
    assert DSN_ENV not in text, "a deselection is not fixed by setting the DSN"
    assert "docker run" not in text

    single = "\n".join(gated.deselect_lines({gated.TOTAL: 1, "network": 1}))
    assert "1 test was not collected" in single


def test_every_mark_the_default_run_excludes_is_a_declared_marker() -> None:
    """The pin that keeps the deselection count honest.

    The banner counts items carrying a mark named in the active ``-m``
    expression. If that expression names a mark nobody declares -- a typo, or a
    marker later renamed -- pytest deselects nothing, the banner says nothing,
    and the tests it was meant to surface go quiet again. Reading both out of
    `pyproject.toml` makes the drift a failure here instead.
    """
    config = tomllib.loads(
        pathlib.Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text()
    )
    pytest_ini = config["tool"]["pytest"]["ini_options"]
    declared = {entry.split(":", 1)[0].strip() for entry in pytest_ini["markers"]}

    # `addopts` is a whole command line ("-q -m '...'"), and `marks_in_expression`
    # takes only the expression -- handing it the lot harvests the flag letters
    # `q` and `m` as mark names. At runtime it reads `config.option.markexpr`,
    # which is already just the expression; here that has to be extracted.
    match = re.search(r"-m\s+'([^']*)'", pytest_ini["addopts"])
    assert match, f"could not find a -m expression in addopts: {pytest_ini['addopts']!r}"
    excluded = set(gated.marks_in_expression(match.group(1)))

    assert excluded, "the default run excludes nothing; the banner has nothing to report"
    assert excluded <= declared, (
        f"the default -m expression excludes marks nobody declares: "
        f"{sorted(excluded - declared)} -- pytest will deselect nothing for these "
        f"and the banner will stay silent about them"
    )
