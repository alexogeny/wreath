import ast
import importlib.util
import sys
from dataclasses import replace

import pytest

from wreath._mutant import runner
from wreath._mutant.model import Mutation
from wreath._mutant.operators import Candidate
from wreath._mutant.patch import CapturedDefault, PatchError, ValuePatch


def _fixture(monkeypatch, tmp_path, text):
    source = tmp_path / "fixture.py"
    source.write_text(text)
    spec = importlib.util.spec_from_file_location("_wreath_defaults_resources", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(runner, "discover", lambda roots: [source])
    monkeypatch.setattr(runner, "module_name_for", lambda path: module.__name__)
    return source, module


def test_selected_values_share_one_declaration_walk(monkeypatch, tmp_path):
    text = "".join(f"LIMIT_{i} = {i + 1}\n" for i in range(32))
    text += "".join(f"def read_{i}(amount=LIMIT_{i % 32}):\n    return amount\n" for i in range(64))
    _fixture(monkeypatch, tmp_path, text)
    original = ast.NodeVisitor.visit
    walks = []

    def counted(visitor, node):
        if isinstance(node, ast.Module):
            walks.append(type(visitor).__name__)
        return original(visitor, node)

    monkeypatch.setattr(ast.NodeVisitor, "visit", counted)
    plan = runner.build_plan([tmp_path], tmp_path, operators=("value",))
    assert not plan.errors
    assert len(plan.mutations) == 32
    for i, mutation in enumerate(plan.mutations):
        assert isinstance(mutation.patch, ValuePatch)
        assert mutation.patch.captured_defaults == (
            CapturedDefault(f"read_{i}", (0,)),
            CapturedDefault(f"read_{i + 32}", (0,)),
        )
    assert len(walks) == 1


DECLARATIONS = """\
LIMIT = 10
OTHER_LIMIT = 20
def repeated(x=0): pass
async def second(a, /, b=LIMIT, c=OTHER_LIMIT, *, z=LIMIT, a_key=LIMIT): pass
def repeated(x=OTHER_LIMIT, y=LIMIT, *, z=OTHER_LIMIT): pass
def repeated(x=LIMIT, y=OTHER_LIMIT, *, a_key=LIMIT): pass
class Box:
    @staticmethod
    def read(x=LIMIT): pass
    class Inner:
        @classmethod
        async def read(cls, x=OTHER_LIMIT, *, k=LIMIT): pass
    def local(self, x=LIMIT):
        def hidden(x=LIMIT): pass
        class Hidden:
            def read(x=LIMIT): pass
class Box:
    def read(x=OTHER_LIMIT, y=LIMIT): pass
if True:
    def conditional(x=LIMIT + 1, y=(LIMIT,), *, required, k=OTHER_LIMIT): pass
def unrelated(x=10): pass
"""

EXPECTED = {
    "LIMIT": (
        CapturedDefault("repeated", (0, 1), ("a_key",)),
        CapturedDefault("second", (0,), ("a_key", "z")),
        CapturedDefault("Box.read", (0, 1)),
        CapturedDefault("Box.Inner.read", (), ("k",)),
        CapturedDefault("Box.local", (0,)),
    ),
    "OTHER_LIMIT": (
        CapturedDefault("repeated", (0, 1), ("z",)),
        CapturedDefault("second", (1,)),
        CapturedDefault("Box.read", (0,)),
        CapturedDefault("Box.Inner.read", (0,)),
        CapturedDefault("conditional", (), ("k",)),
    ),
}


@pytest.mark.parametrize("names", [("LIMIT", "OTHER_LIMIT"), ("LIMIT",), ("OTHER_LIMIT",)])
def test_selected_plan_matches_standalone_exact_targets(monkeypatch, tmp_path, names):
    source, module = _fixture(monkeypatch, tmp_path, DECLARATIONS)
    tree = ast.parse(DECLARATIONS)
    selected = frozenset(
        f"value.widen-bound@fixture.py:{1 if name == 'LIMIT' else 2}" for name in names
    )
    plan = runner.build_plan([tmp_path], tmp_path, selected_ids=selected)
    assert not plan.errors
    assert {mutation.identifier for mutation in plan.mutations} == selected
    candidates = {
        candidate.value_path[0]: candidate
        for candidate in runner.scan(tree, module.__name__)
        if candidate.kind == "value" and candidate.value_path
    }
    for mutation in plan.mutations:
        assert isinstance(mutation.patch, ValuePatch)
        name = mutation.patch.path[0]
        standalone = runner._build(
            candidates[name], tree, module.__name__, "fixture.py", str(source), mutation.identifier
        )
        assert mutation.patch.captured_defaults == EXPECTED[name]
        assert isinstance(standalone, Mutation)
        assert isinstance(standalone.patch, ValuePatch)
        assert standalone.patch.captured_defaults == EXPECTED[name]
        assert runner._captured_default_targets(tree, (name,)) == EXPECTED[name]


@pytest.mark.parametrize("path", [(), ("Box", "LIMIT"), ("MISSING",)])
def test_helper_has_no_targets_for_non_names(path):
    assert runner._captured_default_targets(ast.parse(DECLARATIONS), path) == ()


def test_no_selected_values_do_not_walk_declarations(monkeypatch, tmp_path):
    _fixture(monkeypatch, tmp_path, "LIMIT = 10\ndef read(x=LIMIT): return x\n")

    def refused(*args, **kwargs):
        raise AssertionError("unselected captured defaults were compiled")

    monkeypatch.setattr(runner, "_captured_default_targets", refused)
    plan = runner.build_plan([tmp_path], tmp_path, only=("does-not-exist",))
    assert plan.mutations == []
    assert plan.errors == []


@pytest.mark.parametrize(
    ("kind", "operator", "path"),
    [
        ("code", "predicate.always-true", ()),
        ("value", "cedar.policy", ("LIMIT",)),
        ("value", "value.widen-bound", ("Box", "LIMIT")),
    ],
)
def test_ineligible_selected_candidates_do_not_walk(monkeypatch, tmp_path, kind, operator, path):
    _fixture(monkeypatch, tmp_path, "LIMIT = 10\nclass Box:\n    LIMIT = 20\n")
    candidate = Candidate(operator, "fixture", 1, (), kind=kind, value_path=path, value=100)
    monkeypatch.setattr(runner, "scan", lambda *args, **kwargs: [candidate])
    monkeypatch.setattr(runner, "unsupported_module_declarations", lambda *args, **kwargs: [])

    def refused(*args, **kwargs):
        raise AssertionError("ineligible captured defaults were compiled")

    monkeypatch.setattr(runner, "_captured_default_targets", refused)
    plan = runner.build_plan([tmp_path], tmp_path)
    if kind == "code":
        assert plan.errors == [(f"{operator}@fixture.py:1", "no transform")]
        assert plan.mutations == []
    else:
        assert plan.errors == []
        assert len(plan.mutations) == 1


def test_batch_records_only_selected_names_and_empty_batch_does_not_walk(monkeypatch):
    tree = ast.parse(DECLARATIONS)
    assert runner._captured_default_targets(tree, (), selected_names=frozenset(("LIMIT",))) == {
        "LIMIT": EXPECTED["LIMIT"]
    }

    def refused(*args, **kwargs):
        raise AssertionError("empty batch walked the source")

    monkeypatch.setattr(ast.NodeVisitor, "visit", refused)
    assert runner._captured_default_targets(tree, (), selected_names=frozenset()) == {}


def test_standalone_build_preserves_noop_errors_and_multicomponent(monkeypatch, tmp_path):
    text = "LIMIT = 10\nclass Box:\n    LIMIT = 20\ndef read(x=LIMIT): return x\n"
    source, module = _fixture(monkeypatch, tmp_path, text)
    tree = ast.parse(text)
    candidate = next(c for c in runner.scan(tree, module.__name__) if c.kind == "value")

    def build(**kwargs):
        return runner._build(
            replace(candidate, **kwargs), tree, module.__name__, "fixture.py", str(source), "id"
        )

    assert build(value=10) == "the replacement value equals the declared one"
    mutation = build(value_path=("Box", "LIMIT"))
    assert isinstance(mutation.patch, ValuePatch)
    assert mutation.patch.captured_defaults == ()
    mutation.patch.apply()
    try:
        assert module.Box.LIMIT == candidate.value
        assert module.read() == 10
    finally:
        mutation.patch.undo()
    assert module.Box.LIMIT == 20

    def refused(self):
        raise PatchError("fixture verification failed")

    monkeypatch.setattr(ValuePatch, "is_noop", refused)
    assert build() == "fixture verification failed"
