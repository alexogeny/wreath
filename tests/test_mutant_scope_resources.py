import ast
import importlib.util
import sys
from types import CodeType

import pytest

from wreath._mutant import patch, runner


def _fixture(monkeypatch, tmp_path, text):
    source = tmp_path / "fixture.py"
    source.write_text(text)
    spec = importlib.util.spec_from_file_location("_wreath_scope_resources", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(runner, "discover", lambda roots: [source])
    monkeypatch.setattr(runner, "module_name_for", lambda path: module.__name__)
    return source, module


def _prior_compile_scope(tree, qualname, filename):
    owner_name = qualname.partition(".")[0]
    owner = next(
        (
            statement
            for statement in tree.body
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and statement.name == owner_name
        ),
        None,
    )
    if owner is None:
        return compile(tree, filename, "exec", dont_inherit=True, optimize=0)
    futures = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.module == "__future__"
    ]
    return compile(
        ast.Module(body=[*futures, owner], type_ignores=[]),
        filename,
        "exec",
        dont_inherit=True,
        optimize=0,
    )


def _assert_code(actual, expected):
    assert actual == expected
    assert actual.co_flags == expected.co_flags
    assert actual.co_linetable == expected.co_linetable
    assert actual.co_exceptiontable == expected.co_exceptiontable
    for left, right in zip(actual.co_consts, expected.co_consts, strict=True):
        if isinstance(left, CodeType):
            _assert_code(left, right)


def test_plan_bounds_repeated_owner_scans(monkeypatch, tmp_path):
    text = "".join(
        f"def authorize_{index}(value):\n    value = bool(value)\n    return value\n"
        for index in range(64)
    )
    _fixture(monkeypatch, tmp_path, text)
    original = runner.compile_scope
    visits = []

    class Body(list):
        def __iter__(self):
            for item in list.__iter__(self):
                visits.append(None)
                yield item

    def counted(tree, *args, **kwargs):
        before = tree.body
        tree.body = Body(before)
        try:
            result = original(tree, *args, **kwargs)
        finally:
            tree.body = before
        _assert_code(result, _prior_compile_scope(tree, args[0], args[1]))
        return result

    monkeypatch.setattr(runner, "compile_scope", counted)
    plan = runner.build_plan([tmp_path], tmp_path, operators=("predicate.always-true",))
    assert plan.errors == []
    assert tuple(item.site.scope for item in plan.mutations) == tuple(
        f"authorize_{index}" for index in range(64)
    )
    assert len(visits) <= 4 * 64, len(visits)


@pytest.mark.parametrize(("code_count", "value_count"), [(0, 2), (1, 0), (1, 2), (64, 2)])
def test_module_facts_are_built_once_only_when_reused(
    monkeypatch, tmp_path, code_count, value_count
):
    text = ("LIMIT = 10\nOTHER_LIMIT = 20\n" if value_count else "") + "".join(
        f"def authorize_{index}(value):\n    value = bool(value)\n    return value\n"
        for index in range(code_count)
    )
    _fixture(monkeypatch, tmp_path, text)
    original = patch._ScopeFacts.from_tree
    visits = []
    calls = []

    class Body(list):
        def __iter__(self):
            for item in list.__iter__(self):
                visits.append(None)
                yield item

    def counted(tree):
        calls.append(tree)
        before = tree.body
        tree.body = Body(before)
        try:
            return original(tree)
        finally:
            tree.body = before

    monkeypatch.setattr(patch._ScopeFacts, "from_tree", staticmethod(counted))
    plan = runner.build_plan(
        [tmp_path], tmp_path, operators=("predicate.always-true", "value.widen-bound")
    )
    assert plan.errors == []
    assert len(plan.mutations) == code_count + value_count
    assert len(calls) == (1 if code_count > 1 else 0)
    assert len(visits) == (code_count + value_count if code_count > 1 else 0)


DECLARATIONS = """\
from __future__ import annotations
def authorize(value: Missing):
    def nested():
        return bool(value)
    return nested()
async def async_authorize(value: Missing):
    return bool(value)
class Gate:
    class Inner:
        async def authorize(self, value: Missing):
            return bool(value)
    def authorize(self, value):
        return bool(value)
def repeated(value):
    return bool(value)
def repeated(value):
    return not bool(value)
if True:
    def conditional(value):
        return bool(value)
"""


@pytest.mark.parametrize("future", [True, False])
def test_indexed_replacements_match_prior_compiler_and_preserve_ast(future):
    text = DECLARATIONS if future else DECLARATIONS.partition("\n")[2]
    tree = ast.parse(text)
    scopes = runner.tag(tree)
    before = ast.dump(tree, include_attributes=True)
    facts = patch._ScopeFacts.from_tree(tree)
    targets = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "bool"
    ]
    assert len(targets) == 7
    for target in targets:
        node_id = vars(target)["_mutant_id"]
        scope = ".".join(scopes[node_id])
        mutated = patch.transform_module(tree, node_id, lambda node: ast.Constant(True))
        expected = _prior_compile_scope(mutated, scope, "<fixture>")
        actual = patch.compile_scope(mutated, scope, "<fixture>", facts=facts)
        _assert_code(actual, expected)
        _assert_code(patch.compile_scope(mutated, scope, "<fixture>"), expected)
        assert patch.find_code(actual, scope) is not None
        assert ast.dump(tree, include_attributes=True) == before
    _assert_code(
        patch.compile_scope(tree, "missing", "<fixture>", facts=facts),
        _prior_compile_scope(tree, "missing", "<fixture>"),
    )


@pytest.mark.parametrize(
    ("text", "scope"),
    [
        ("def authorize():\n    return 1\nfrom __future__ import unknown\n", "authorize"),
        ("def authorize():\n    break\n", "authorize"),
        ("return 1\n", "missing"),
    ],
)
def test_indexed_compilation_preserves_errors_and_ast(text, scope):
    tree = ast.parse(text)
    before = ast.dump(tree, include_attributes=True)
    facts = patch._ScopeFacts.from_tree(tree)
    with pytest.raises(SyntaxError) as prior:
        _prior_compile_scope(tree, scope, "<fixture>")
    with pytest.raises(SyntaxError) as indexed:
        patch.compile_scope(tree, scope, "<fixture>", facts=facts)
    assert indexed.value.args == prior.value.args
    assert ast.dump(tree, include_attributes=True) == before
