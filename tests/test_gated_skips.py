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
import pathlib

import _gated_skips as gated

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
