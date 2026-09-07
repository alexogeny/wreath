import ast
import importlib.util
import sys

import pytest

from wreath._mutant import operators, runner


@pytest.fixture
def source_module(monkeypatch, tmp_path):
    source = tmp_path / "fixture.py"
    source.write_text("def authorize(value):\n    value = bool(value)\n    return value\n")
    spec = importlib.util.spec_from_file_location("tag_fixture", source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "tag_fixture", module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(runner, "discover", lambda roots: [source])
    monkeypatch.setattr(runner, "module_name_for", lambda path: "tag_fixture")
    return source


@pytest.mark.parametrize("operation", ["plan", "sample", "watch"])
def test_one_source_is_tagged_once(monkeypatch, source_module, operation):
    original = operators.tag
    visits = []

    def counted(tree):
        visits.append(tree)
        return original(tree)

    monkeypatch.setattr(operators, "tag", counted)
    monkeypatch.setattr(runner, "tag", counted)
    root = source_module.parent
    identifier = "predicate.always-true@fixture.py:1"
    if operation == "plan":
        plan = runner.build_plan([root], root)
        assert plan.sources == ["fixture.py"]
        assert plan.errors == []
        assert [mutation.identifier for mutation in plan.mutations] == [identifier]
        assert plan.mutations[0].site.scope == "authorize"
    elif operation == "sample":
        selection = runner.select_sample([root], root, 1)
        assert selection.identifiers == (identifier,)
        assert selection.errors == ()
    else:
        watched, whole = runner.watch_selected_identifiers([root], root, frozenset([identifier]))
        assert watched == {str(source_module): frozenset([1, 2, 3])}
        assert whole == frozenset()
    assert len(visits) == 1


def test_standalone_scanner_tags_fresh_ast():
    tree = ast.parse("def authorize(value):\n    value = bool(value)\n    return value\n")
    candidates = operators.scan(tree, None)
    assert [(candidate.operator, candidate.line, candidate.scope) for candidate in candidates] == [
        ("predicate.always-true", 1, ("authorize",))
    ]


def test_standalone_diagnostics_tags_fresh_ast():
    tree = ast.parse("value = 1\n")
    assert operators.unsupported_module_declarations(tree, None) == []
    assert hasattr(tree, "_mutant_id")


def test_shared_scope_map_preserves_nested_and_decorator_ownership(monkeypatch):
    tree = ast.parse(
        "@decorate(flag=True)\n"
        "def authorize(value):\n"
        "    @decorate(flag=False)\n"
        "    def nested():\n"
        "        return value\n"
        "    return nested()\n"
    )
    scopes = operators.tag(tree)
    before = dict(scopes)
    outer = tree.body[0]
    nested = outer.body[0]
    assert scopes[outer.decorator_list[0]._mutant_id] == ()
    assert scopes[nested.decorator_list[0]._mutant_id] == ("authorize",)
    assert scopes[nested.body[0]._mutant_id] == ("authorize",)

    def reject_retag(tree):
        raise AssertionError("shared scope map must not trigger tagging")

    monkeypatch.setattr(operators, "tag", reject_retag)
    candidates = operators.scan(tree, None, scopes=scopes)
    assert [(item.operator, item.scope) for item in candidates] == [
        ("predicate.always-true", ("authorize",))
    ]
    assert operators.unsupported_module_declarations(tree, None, scopes=scopes) == []
    assert scopes == before
