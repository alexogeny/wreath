import ast

import pytest

from wreath._mutant import operators


def candidates(source: str) -> list[operators.Candidate]:
    tree = ast.parse(source)
    context = operators._Context(module=None, tree=tree, scopes=operators.tag(tree))
    return list(operators._predicate_operators(context))


def test_predicate_return_and_watch_use_existing_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = ast.parse(
        "def authorize(value):\n    if value:\n        return True\n    return False\n"
    )
    function = tree.body[0]
    context = operators._Context(module=None, tree=tree, scopes=operators.tag(tree))
    original = ast.walk
    walks = []

    def counted(node: ast.AST):
        if node is function:
            walks.append(node)
        return original(node)

    monkeypatch.setattr(ast, "walk", counted)
    found = list(operators._predicate_operators(context))
    predicate = [item for item in found if item.operator == "predicate.always-true"]
    assert len(predicate) == 1
    assert predicate[0].watch == (1, 2, 4, 3)
    assert len(walks) <= 2


def test_nested_predicate_watch_includes_already_seen_statements() -> None:
    found = candidates(
        "def authorize_outer(value):\n"
        "    def is_allowed(inner):\n"
        "        if inner:\n"
        "            return True\n"
        "        return False\n"
        "    if value:\n"
        "        return is_allowed(value)\n"
        "    return False\n"
    )
    assert [(item.operator, item.line, item.watch) for item in found] == [
        ("guard.never-fires", 6, (6,)),
        ("guard.always-fires", 6, (6,)),
        ("guard.never-fires", 3, (3,)),
        ("guard.always-fires", 3, (3,)),
        ("predicate.always-true", 1, (1, 2, 6, 8, 3, 5, 7, 4)),
        ("predicate.always-true", 2, (2, 3, 5, 4)),
    ]


@pytest.mark.parametrize("prefix", ["def", "async def"])
def test_only_nested_return_still_qualifies_outer_predicate(prefix: str) -> None:
    found = candidates(
        f"{prefix} authorize(value):\n    def inner():\n        return value\n    local = inner\n"
    )
    predicate = [item for item in found if item.operator == "predicate.always-true"]
    assert len(predicate) == 1
    assert predicate[0].watch == (1, 2, 4, 3)


@pytest.mark.parametrize(
    "source",
    [
        "def authorize(value):\n    local = value\n    other = local\n",
        "def authorize(value):\n    return value\n",
        "def process(principal):\n    local = principal\n    return local\n",
    ],
)
def test_ineligible_functions_do_not_gain_predicate_candidates(source: str) -> None:
    assert candidates(source) == []
