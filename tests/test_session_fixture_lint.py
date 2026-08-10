"""Whoever builds an ORM `Session` must close it before the pool stops.

`Session.close()` is what returns the leased connection to its pool. Code that
builds a Session and then stops the `Database` without closing it leaves a
connection borrowed, so `Pool.stop()` waits out the whole `shutdown_timeout`
(10.0s, `wreath.postgres.Database.__init__`) before closing it underneath the
holder anyway.

That is a correctness defect wearing a performance costume. The grace period
exists so a caller can finish and hand the connection back; a caller that never
will turns it into a fixed 10-second stall, and any transaction still open is
force-closed rather than rolled back -- which is precisely the work
`Session.close()` documents itself as doing.

Measured before this check existed: `db.stop()` took **10.003s** with a leased
connection and **0.000s** after `await session.close()`. Twelve tests in
`tests/tracking/test_place.py` each paid 10.01s of teardown against calls of
0.01-0.22s, making a 16-test file the slowest in the suite at 133.9s; four
fixtures and one test body were leaking, and fixing them took the full run from
133.0s to 99.8s.

**Test bodies are checked, not just fixtures.** The first version of this lint
only walked fixtures, and
`tests/example/test_analysis_views.py::test_a_card_pulled_late_records_a_correction`
built its Session inline and paid the same 10s inside `call` rather than
teardown -- invisible to a fixture-only check while costing exactly as much.

The check is structural rather than timed: a stall this large is easy to measure
but a timing assertion in the suite would be flaky, and the invariant worth
holding is "the connection was returned", not "teardown was fast".
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent

#: Only the real ORM Session leases a pooled connection. The suite is full of
#: doubles -- `FakeSession`, `RecordingSession` -- that hold no connection and
#: have no `close()`; matching any name ending in "Session" flagged 200+ of them.
_REAL_SESSION = "Session"


def _is_fixture(node: ast.AST) -> bool:
    """Whether a function carries a `pytest.fixture` decorator, called or bare."""
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "fixture":
            return True
        if isinstance(target, ast.Name) and target.id == "fixture":
            return True
    return False


#: Substrings naming a test double. A real `Session` over one of these holds no
#: pooled connection, so it has nothing to give back and `close()` would be
#: noise -- `tests/postgres/test_advisory_locks.py` drives the real class over a
#: `_FakeRegistry` in the same file that elsewhere starts a real pool, which is
#: why this cannot be decided per *file*.
_DOUBLES = ("Fake", "Stub", "Dummy", "Recording")


def _mentions(node: ast.AST, names: set[str]) -> bool:
    """Whether any identifier in this subtree is a double, by name or binding."""
    for part in ast.walk(node):
        identifier = None
        if isinstance(part, ast.Name):
            identifier = part.id
        elif isinstance(part, ast.Attribute):
            identifier = part.attr
        if identifier is None:
            continue
        if identifier in names:
            return True
        if any(double in identifier for double in _DOUBLES):
            return True
    return False


def _double_names(node: ast.AST) -> set[str]:
    """Local names standing in for a database, transitively.

    One hop is not enough: `database = _FakeDatabase()` then `registry =
    _IsolatedRegistry(database)` leaves the registry's *own* name innocent, and
    the session is built from that.
    """
    names: set[str] = set()
    for _pass in range(4):
        grew = False
        for child in ast.walk(node):
            if not isinstance(child, ast.Assign) or not isinstance(child.value, ast.Call):
                continue
            if not _mentions(child.value, names):
                continue
            for target in child.targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    grew = True
        if not grew:
            break
    return names


def _real_session_calls(node: ast.AST) -> list[ast.Call]:
    """Every `Session(...)` in this function that is built over a real pool."""
    doubles = _double_names(node)
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == _REAL_SESSION
        and not _mentions(child, doubles)
    ]


def _session_names(node: ast.AST) -> set[str]:
    """Names bound to a `Session(...)` built inside this function."""
    real = {id(call) for call in _real_session_calls(node)}
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Assign) and isinstance(child.value, ast.Call):
            if id(child.value) in real:
                names.update(
                    target.id for target in child.targets if isinstance(target, ast.Name)
                )
    return names


def _builds_anonymous_session(node: ast.AST) -> bool:
    """A `Session(...)` built without being bound to a name.

    There is no handle to close, so the only correct forms are handing it
    straight out (`yield`/`return`) or not building it at all.
    """
    bindings = {
        id(other.value) for other in ast.walk(node) if isinstance(other, ast.Assign)
    }
    return any(id(call) not in bindings for call in _real_session_calls(node))


def _handed_out(node: ast.AST, names: set[str]) -> bool:
    """Whether a session leaves this function, so closing it is the caller's job.

    Only meaningful for `return`: a fixture's `yield` resumes for teardown, so a
    fixture still owns the close.
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or child.value is None:
            continue
        for part in ast.walk(child.value):
            if isinstance(part, ast.Name) and part.id in names:
                return True
            if isinstance(part, ast.Call):
                function = part.func
                if isinstance(function, ast.Name) and function.id == _REAL_SESSION:
                    return True
    return False


def _closes_the_session(node: ast.AST, names: set[str]) -> bool:
    """Whether `close()` is called on the session itself.

    Deliberately not "calls `close()` somewhere": these tests routinely close a
    *raw connection* they opened for DDL, and accepting that as proof let a
    fixture leaking a 10-second session pass this check.
    """
    if not names:
        return False
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "close"
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id in names
        for child in ast.walk(node)
    )


def _stops_a_real_pool(tree: ast.Module) -> bool:
    """Whether this module builds a real `Database` and stops it."""
    builds = False
    stops = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "Database":
            builds = True
        if isinstance(node.func, ast.Attribute) and node.func.attr == "stop":
            stops = True
    return builds and stops


def test_whoever_builds_a_session_closes_it() -> None:
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("test_*.py")):
        source = path.read_text()
        # Reading is cheap, parsing is not, and only a handful of these ~640
        # files mention a Session at all.
        if "Session(" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a broken test file fails elsewhere
            continue
        # The grace period is spent in exactly one place -- `Pool.stop()` -- so a
        # file that never stops a real pool cannot pay it however many sessions
        # it builds. That is what separates the true leaks from the many tests
        # driving a real `Session` over a stub: `tests/orm/test_complexity.py`
        # builds six and finishes in 2.5s because nothing there is pooled.
        #
        # Matched on the tree rather than the text, because `_FakeDatabase(` and
        # `_FakeRegistry(` contain the substring and are exactly the doubles this
        # needs to ignore.
        if not _stops_a_real_pool(tree):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            names = _session_names(node)
            anonymous = _builds_anonymous_session(node)
            if not names and not anonymous:
                continue
            if _handed_out(node, names) and not _is_fixture(node):
                continue
            if anonymous or not _closes_the_session(node, names):
                relative = path.relative_to(TESTS.parent)
                offenders.append(f"{relative}:{node.lineno} {node.name}()")

    assert not offenders, (
        "these build an ORM Session and never close it, so the pool spends its "
        "full 10s shutdown_timeout draining a connection that is never coming "
        "back:\n  " + "\n  ".join(offenders)
    )
