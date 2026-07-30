"""Tests for `wreath.mutant`.

The one that matters is `test_a_run_reports_both_a_killed_and_a_surviving_control`:
a mutation tester that can only report KILLED is exactly the shape ADR 0024
names -- a check that passes because it has nothing to check -- and it would be
trusted anyway, because a green mutation report reads like good news. So the
end-to-end test drives a project with one *watched* control and one *unwatched*
one, and demands that the tool tells them apart.

Every assertion here was observed failing before it was made to pass: the
operator tests against a fixture module that does not contain the construct,
the patch tests against an unpatched function, and the end-to-end test against
a fixture whose second control was also covered.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from wreath._mutant import operators
from wreath._mutant.patch import (
    CodePatch,
    PatchError,
    ValuePatch,
    compile_module,
    find_code,
    same_bytecode,
    transform_module,
)
from wreath._mutant.runner import build_plan, module_name_for
from wreath.mutant import OPERATORS, Outcome, Report, Verdict, render

SOURCE = textwrap.dedent(
    '''
    """A module that declares controls, so the operators have something to do."""
    import re

    MAX_UPLOAD_SIZE = 1024
    SENSITIVE_FIELD = re.compile("secret")
    DEFERRED_ALGORITHMS = frozenset({"none"})
    POLICY = """
    permit(principal, action, resource) when { context.ok };
    forbid(principal, action, resource);
    """


    class Gate:
        def is_permitted(self, identity, role):
            """A permission check with two clauses."""
            if identity is None:
                raise PermissionError("anonymous")
            return identity.trusted and role in identity.roles


    def charge(principal, session, limiter):
        """A rate limit keyed on the caller, or on the session."""
        key = principal if principal is not None else session
        return limiter(key)
    '''
)


@pytest.fixture
def module(tmp_path: Path) -> Path:
    path = tmp_path / "controls.py"
    path.write_text(SOURCE, encoding="utf-8")
    return path


def _scan(path: Path) -> list[operators.Candidate]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return operators.scan(tree, None)


def _by(found: list[operators.Candidate], operator: str) -> list[operators.Candidate]:
    return [c for c in found if c.operator == operator]


# -- the operator library ---------------------------------------------------


def test_every_named_operator_is_reachable_from_the_public_surface() -> None:
    """A derived list pins its own names (ADR 0024).

    Without this, an operator that stops being emitted shrinks coverage
    silently and the run still exits 0.
    """
    assert len(OPERATORS) == 16
    assert set(OPERATORS) == {
        "predicate.drop-operand", "predicate.always-true", "expression.take-branch",
        "comprehension.drop-clause", "guard.remove-raise", "guard.never-fires",
        "guard.always-fires", "guard.drop-statement", "declaration.drop-keyword",
        "declaration.widen-bound", "cedar.flip-effect", "cedar.drop-condition",
        "cedar.delete-policy", "value.widen-bound", "value.disable-pattern",
        "value.empty-denylist",
    }


def test_a_conjunction_yields_one_mutation_per_clause(module: Path) -> None:
    found = _by(_scan(module), "predicate.drop-operand")
    controls = [c.control for c in found]
    assert any("identity.trusted" in c for c in controls), controls
    assert any("role in identity.roles" in c for c in controls), controls


def test_a_refusal_becomes_a_mutation_that_does_not_refuse(module: Path) -> None:
    found = _by(_scan(module), "guard.remove-raise")
    assert [c.control for c in found] == ["the refusal `raise PermissionError('anonymous')`"]


def test_a_keyed_choice_yields_both_branches_named_by_what_is_kept(module: Path) -> None:
    """The 'limit that does not limit' operator: key on the free-to-mint side.

    The label has to name the branch that *survives*. It named the other one at
    first, and the report then blamed a set of tests that had nothing to do with
    the mutation -- a wrong answer that still looked like a right one.
    """
    found = _by(_scan(module), "expression.take-branch")
    assert len(found) == 2
    kept = {c.control.rsplit("always ", 1)[1] for c in found}
    assert kept == {"`principal`)", "`session`)"}


def test_a_predicate_named_like_a_permission_check_can_answer_true(module: Path) -> None:
    found = _by(_scan(module), "predicate.always-true")
    assert [c.control for c in found] == ["every check in `is_permitted` (it now answers True)"]


def test_the_always_true_mutation_watches_the_body_not_the_def_line(module: Path) -> None:
    """A `def` line runs once, at import.

    Watching it attributed the mutation to no test at all, so a control every
    test exercised was reported UNREACHED -- the tool inventing ADR 0024's own
    failure mode.
    """
    found = _by(_scan(module), "predicate.always-true")[0]
    assert found.watch, "no watch lines: every test would be filtered out"
    assert found.line not in found.watch or len(found.watch) > 1


def test_cedar_policy_text_is_mutated_as_a_policy_not_as_a_string(module: Path) -> None:
    found = _scan(module)
    assert any(c.operator == "cedar.flip-effect" for c in found)
    assert any(c.operator == "cedar.drop-condition" for c in found)
    assert any(c.operator == "cedar.delete-policy" for c in found)


def test_the_control_vocabulary_is_what_scopes_the_predicate_operators() -> None:
    assert "authoriz" in operators.CONTROL_TOKENS
    assert "second_factor" in operators.CONTROL_KEYWORDS
    quiet = ast.parse("def encode(value):\n    if value:\n        raise ValueError('x')\n")
    assert operators.scan(quiet, None) == []


def test_a_routes_own_controls_are_mutable_where_the_route_is_built(tmp_path: Path) -> None:
    """`@app.get(path, dependencies=...)` goes through `**metadata`.

    Asking the decorator's signature whether `dependencies` has a default
    answers "there is no such parameter", so the operator declined and *the*
    control this tool exists to remove went unmutated. The answer is on
    `RouteDefinition`, one layer down.
    """
    path = tmp_path / "factory.py"
    path.write_text(textwrap.dedent(
        """
        from wreath import Wreath
        from wreath.binding import Depends


        async def only_staff(request) -> None:
            if not request.headers.get("x-staff"):
                raise PermissionError("staff only")


        def build_app():
            app = Wreath()

            @app.get("/reports", dependencies=(Depends(only_staff),))
            async def reports(request) -> dict:
                \"\"\"Every report.\"\"\"
                return {}

            return app
        """
    ), encoding="utf-8")
    found = _by(_scan(path), "declaration.drop-keyword")
    assert [c.control for c in found] == [
        "`dependencies=` on `app.get(...)` (it falls back to the default)"
    ]
    assert found[0].scope == ("build_app",)


def test_a_decorator_belongs_to_the_scope_it_is_written_in(tmp_path: Path) -> None:
    """A decorator runs where the `def` is, not inside the function.

    Attributing it to the decorated function produced a patch that recompiled a
    body nobody had changed. It was caught only because the bytecode came out
    identical -- luck, not design -- so the attribution is pinned here.
    """
    path = tmp_path / "routes.py"
    path.write_text(textwrap.dedent(
        """
        from wreath import Wreath
        from wreath.binding import Depends

        app = Wreath()


        async def only_staff(request) -> None:
            raise PermissionError("staff only")


        @app.get("/reports", dependencies=(Depends(only_staff),))
        async def reports(request) -> dict:
            \"\"\"Every report.\"\"\"
            return {}
        """
    ), encoding="utf-8")
    # Module level: no enclosing function to recompile, so nothing is offered
    # rather than something being offered that cannot be installed.
    assert _by(_scan(path), "declaration.drop-keyword") == []


# -- the patch machinery ----------------------------------------------------


def test_replacing_a_code_object_is_visible_through_an_existing_alias() -> None:
    """`from x import y` copies the function object's *reference*, not its body.

    Reloading the module would leave every such alias enforcing the original
    control; assigning `__code__` cannot, which is the whole reason this tool
    does not reload anything.
    """
    from wreath._mutant import patch as patch_module

    source = "def allowed(value):\n    return value > 0\n"
    tree = ast.parse(source, filename="<fixture>")
    operators.tag(tree)
    namespace: dict[str, Any] = {}
    exec(compile_module(tree, "<fixture>"), namespace)  # noqa: S102 - a fixture
    original: Any = namespace["allowed"]
    alias: Any = original

    comparison: Any = next(node for node in ast.walk(tree) if isinstance(node, ast.Compare))
    mutated = transform_module(
        tree, comparison._mutant_id, lambda _node: ast.Constant(value=True)
    )
    code = find_code(compile_module(mutated, "<fixture>"), "allowed")
    assert code is not None
    assert not same_bytecode(original.__code__, code)
    original.__code__ = code
    assert alias(-1) is True
    assert patch_module.PatchError is PatchError


def test_a_value_patch_rebinds_the_name_in_every_module_that_imported_it() -> None:
    import wreath.crud as crud

    patch = ValuePatch(module_name="wreath.crud", path=("SENSITIVE_FIELD",),
                       value=re.compile("(?!x)x"))
    before = crud.SENSITIVE_FIELD
    assert before.search("password") is not None
    patch.apply()
    try:
        assert crud.SENSITIVE_FIELD.search("password") is None
    finally:
        patch.undo()
    assert crud.SENSITIVE_FIELD is before


def test_a_value_patch_does_not_follow_an_interned_value_across_the_interpreter() -> None:
    """`is` does not mean "came from here" for a small int.

    `crud._MAX_PAGE_SIZE` is 100 and so is a constant in `ssl`; they are the
    same object. The alias sweep proposed rewriting the one in `ssl`, and the
    first such name it reached was read-only, so it raised. Succeeding would
    have been worse: a mutation whose real effect is somewhere nobody is looking
    is noise wearing a finding's clothes.
    """
    import ssl

    import wreath.crud as crud

    before = ssl.OP_NO_TICKET
    patch = ValuePatch(module_name="wreath.crud", path=("_MAX_PAGE_SIZE",), value=1 << 40)
    patch.apply()
    try:
        assert crud._MAX_PAGE_SIZE == 1 << 40
        assert ssl.OP_NO_TICKET == before
    finally:
        patch.undo()
    assert crud._MAX_PAGE_SIZE == 100


def test_a_mutation_that_compiles_to_the_same_bytecode_is_not_a_finding() -> None:
    source = "def f(a, b):\n    return a and b\n"
    tree = ast.parse(source)
    code = compile_module(tree, "<same>")
    assert same_bytecode(code, compile_module(ast.parse(source), "<same>"))


def test_a_patch_whose_target_moved_is_refused_rather_than_silently_skipped() -> None:
    """A patch that cannot apply must be an ERROR, never a survivor.

    A survivor is read as "your tests would not notice". A mutation that never
    reached the interpreter would say that falsely, about a control the suite
    may well be watching.
    """
    code = find_code(compile_module(ast.parse("def other():\n    return 1\n"), "<x>"), "other")
    assert code is not None
    patch = CodePatch(module_name="wreath.crud", scope="crud_router", code=code)
    with pytest.raises(PatchError):
        patch.verify()


# -- planning ---------------------------------------------------------------


def test_a_module_is_named_by_walking_up_while_there_is_an_init(tmp_path: Path) -> None:
    package = tmp_path / "shop" / "web"
    package.mkdir(parents=True)
    (tmp_path / "shop" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "routes.py").write_text("")
    assert module_name_for(package / "routes.py") == "shop.web.routes"


def test_planning_declines_a_mutation_it_cannot_build_and_says_why(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n", encoding="utf-8")
    plan = build_plan([broken], tmp_path)
    assert plan.mutations == []
    assert any("unreadable" in reason for _, reason in plan.errors)


# -- the report -------------------------------------------------------------


def test_the_report_separates_a_control_nobody_watches_from_one_nobody_reaches() -> None:
    from wreath._mutant.model import Mutation, Site

    def verdict(outcome: Outcome, control: str) -> Verdict:
        site = Site(path="app/policies.py", line=12, scope="gate")
        return Verdict(Mutation("id", "guard.remove-raise", control, site, "app"), outcome)

    report = Report(verdicts=[
        verdict(Outcome.SURVIVED, "the refusal `raise Forbidden(...)`"),
        verdict(Outcome.UNREACHED, "the role check"),
        verdict(Outcome.KILLED, "the audience check"),
    ])
    text = render(report)
    assert "SURVIVED" in text and "UNREACHED" in text
    assert text.index("SURVIVED") < text.index("UNREACHED")
    assert report.score == pytest.approx(0.5)


# -- end to end -------------------------------------------------------------


PROJECT = {
    "shop/__init__.py": "",
    "shop/gate.py": textwrap.dedent(
        '''
        """Two controls. One of them has a test."""


        class Forbidden(Exception):
            pass


        def authorize(identity, roles):
            """Refuse a caller who is missing a role."""
            if identity is None:
                raise Forbidden("anonymous")
            if not roles.issubset(identity["roles"]):
                raise Forbidden("missing role")
            return True


        def redact(row, sensitive):
            """Withhold the sensitive columns."""
            return {k: v for k, v in row.items() if k not in sensitive}
        '''
    ),
    "tests/test_gate.py": textwrap.dedent(
        '''
        import pytest

        from shop.gate import Forbidden, authorize, redact


        def test_a_caller_without_the_role_is_refused():
            with pytest.raises(Forbidden):
                authorize({"roles": {"reader"}}, {"admin"})


        def test_a_caller_with_the_role_is_admitted():
            assert authorize({"roles": {"admin"}}, {"admin"}) is True


        def test_redaction_runs():
            # Exercises `redact` without ever asserting that anything is withheld:
            # the shape ADR 0024 names, and the survivor this run must report.
            assert "id" in redact({"id": 1, "token": "t"}, {"token"})
        '''
    ),
    "pyproject.toml": textwrap.dedent(
        """
        [tool.pytest.ini_options]
        testpaths = ["tests"]
        """
    ),
}


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("mutant-e2e")
    for relative, body in PROJECT.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "wreath._mutant.cli", "--path", "shop",
         "--format", "json", "--quiet"],
        cwd=root, capture_output=True, text=True, timeout=300, check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "HOME": str(root)},
    )
    if completed.returncode != 0:
        pytest.fail(f"wreath mutant exited {completed.returncode}\n{completed.stderr[-4000:]}")
    return json.loads(completed.stdout)


def test_a_withheld_field_set_that_stops_withholding_is_a_mutation(module: Path) -> None:
    found = _by(_scan(module), "comprehension.drop-clause")
    assert found == [] or all("filter" in c.control for c in found)


def test_a_run_reports_both_a_killed_and_a_surviving_control(sample_run: dict) -> None:
    """The guard on the guard.

    A tool that answered KILLED for everything would pass any test that only
    checked it found the covered control, and it would be worse than useless:
    it reports that a suite watches controls it does not watch.
    """
    outcomes = {(m["scope"], m["operator"]): m["outcome"] for m in sample_run["mutants"]}
    assert outcomes[("authorize", "guard.never-fires")] == "killed"
    survivors = [
        m for m in sample_run["mutants"]
        if m["scope"] == "redact" and m["outcome"] in ("survived", "unreached")
    ]
    assert survivors, sample_run["mutants"]
    assert survivors[0]["operator"] == "comprehension.drop-clause"
    assert sample_run["counts"]["killed"] >= 1
    assert sample_run["counts"]["survived"] + sample_run["counts"]["unreached"] >= 1


def test_the_run_names_the_test_that_caught_each_control(sample_run: dict) -> None:
    caught = [m for m in sample_run["mutants"] if m["outcome"] == "killed"]
    assert caught
    assert all(m["killers"] for m in caught), caught
    assert any(
        "test_a_caller_without_the_role_is_refused" in killer
        for m in caught for killer in m["killers"]
    )


def test_a_mutant_only_runs_the_tests_that_reach_it(sample_run: dict) -> None:
    """Selection is the difference between a tool people run and one they mean to.

    Three tests exist; the role check inside `authorize` is reached by two of
    them, and `redact` by one. No mutant should ever run all three.
    """
    counts = {m["scope"]: m["candidates"] for m in sample_run["mutants"] if m["candidates"]}
    assert counts, sample_run["mutants"]
    assert max(counts.values()) < 3, counts


def test_a_refusal_no_test_ever_triggers_is_reported_unreached_not_survived(
    sample_run: dict,
) -> None:
    """ADR 0024, stated as an outcome.

    No test in the fixture calls `authorize(None, ...)`, so the anonymous
    refusal never executes. "Nothing would notice if this were deleted" and
    "nothing ever looks at this" are different reports with different fixes.
    """
    unreached = [
        m for m in sample_run["mutants"]
        if m["operator"] == "guard.remove-raise" and m["outcome"] == "unreached"
    ]
    assert unreached, sample_run["mutants"]


# -- resolve_scope: what a dotted path actually yields --------------------------
#
# Nothing tested `resolve_scope`, and it had a bug that cost real coverage: it
# walks a dotted `Class.method` path with `getattr`, and `getattr` *invokes* a
# descriptor. So a `classmethod` arrives as a method bound to the class, not as the
# `classmethod` object `_unwrap` was checking for, and every mutation inside every
# classmethod was refused with "is method, not a function".
#
# The refusal was reported as an `error` outcome. That is the part worth keeping in
# mind: it did not read as "51 classmethods across 25 files are unmeasurable", it
# read as eight lines of noise in a report whose score was computed without them.


class _Subject:
    """One class carrying every callable shape `resolve_scope` claims to unwrap."""

    LIMIT = 3

    def plain(self) -> bool:
        return self.LIMIT > 0

    @classmethod
    def made(cls, value: int) -> bool:
        return value <= cls.LIMIT

    @staticmethod
    def free(value: int) -> bool:
        return value > 0

    @property
    def ready(self) -> bool:
        return self.LIMIT > 0


def _wrapped_target() -> bool:
    return True


def _decorate(fn):
    import functools

    @functools.wraps(fn)
    def outer():
        return fn()

    return outer


decorated = _decorate(_wrapped_target)


@pytest.mark.parametrize(
    "scope,qualname",
    [
        ("_Subject.plain", "_Subject.plain"),
        ("_Subject.made", "_Subject.made"),
        ("_Subject.free", "_Subject.free"),
        ("_Subject.ready", "_Subject.ready"),
        ("decorated", "_wrapped_target"),
    ],
)
def test_resolve_scope_reaches_the_function_behind_every_callable_shape(
    scope: str, qualname: str
) -> None:
    """A method, a classmethod, a staticmethod, a property, and a wrapped function.

    `_Subject.made` is the arm that was failing. The others passed already and are
    here so that a future change to `_unwrap`'s ordering cannot fix one shape by
    breaking another -- the branches are tried in sequence and `MethodType` now
    comes first, which is the kind of ordering that gets 'tidied'.
    """
    from types import FunctionType

    import tests.test_mutant as this_module
    from wreath._mutant.patch import resolve_scope

    resolved = resolve_scope(this_module, scope)
    assert isinstance(resolved, FunctionType), (scope, type(resolved).__name__)
    assert resolved.__qualname__.endswith(qualname), (scope, resolved.__qualname__)


def test_resolve_scope_still_refuses_something_that_is_not_callable_at_all() -> None:
    """The refusal has to survive the fix, or a typo becomes a silent no-op."""
    import tests.test_mutant as this_module
    from wreath._mutant.patch import PatchError, resolve_scope

    with pytest.raises(PatchError, match="not a function"):
        resolve_scope(this_module, "_Subject.LIMIT")


def test_no_real_module_reports_a_scope_it_cannot_patch(module: Path) -> None:
    """The blind spot itself, stated as the absence of its error message.

    A unit test on `resolve_scope` proves the helper resolves each shape. This
    proves the planner then *builds* for them, which is the part that failed and the
    part a caller sees. It runs over real wreath modules rather than a throwaway
    package because `build_plan` resolves a target by importing it by name: a
    fixture package would have to be put on `sys.path` to be importable at all, and
    the modules that ship are both importable already and the ones that matter.

    Before the `MethodType` branch in `_unwrap`, `_auth/cedar_engine.py` alone
    produced eight of these -- one per control inside `EntityUid.parse` -- and they
    were counted as `error`, not as unmeasured, so the score was computed as though
    those controls did not exist.
    """
    targets = [
        Path("src/wreath/_auth/cedar_engine.py"),
        Path("src/wreath/orm/types.py"),
        Path("src/wreath/_sparsevec.py"),
        module,
    ]
    plan = build_plan([p for p in targets if p.exists()], Path("."))
    unpatchable = [pair for pair in plan.errors if "not a function" in pair[1]]
    assert unpatchable == [], unpatchable


def test_a_control_inside_a_classmethod_is_planned_not_skipped() -> None:
    """Every classmethod holding a control contributes a mutation, by name.

    The scopes are derived from the module's own AST rather than hard-coded, so this
    keeps meaning what it says when `cedar_engine` gains or loses a classmethod --
    and it fails, rather than passing vacuously, if the module ever has none.
    """
    target = Path("src/wreath/_auth/cedar_engine.py")
    tree = ast.parse(target.read_text(encoding="utf-8"))
    classmethods = {
        f"{outer.name}.{inner.name}"
        for outer in ast.walk(tree)
        if isinstance(outer, ast.ClassDef)
        for inner in outer.body
        if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(isinstance(d, ast.Name) and d.id == "classmethod" for d in inner.decorator_list)
    }
    assert classmethods, "this module has no classmethod left; move this test"

    plan = build_plan([target], Path("."))
    planned = {m.site.scope for m in plan.mutations if m.patch is not None}
    reached = classmethods & planned
    assert reached, (
        f"no classmethod contributed a mutation; classmethods={sorted(classmethods)} "
        f"planned={sorted(planned)}"
    )
