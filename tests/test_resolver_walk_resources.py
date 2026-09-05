from graphlib import TopologicalSorter
from types import SimpleNamespace

import pytest

from wreath._graphql.parser import Field
from wreath._graphql.resolvers import ResolverError, order_fields


@pytest.mark.parametrize("size", [32, 64, 128])
@pytest.mark.parametrize("shape", ["chain", "star"])
def test_dependency_walk_has_linear_name_comparison_work(size, shape):
    comparisons = []

    class Name(str):
        __hash__ = str.__hash__

        def __eq__(self, other):
            comparisons.append(None)
            return str.__eq__(self, other)

    names = [Name(f"field_{index}") for index in range(size)]
    specs = {
        name: SimpleNamespace(
            requires=tuple(names[index + 1 : index + 2])
            if shape == "chain"
            else tuple(names[1:])
            if index == 0
            else ()
        )
        for index, name in enumerate(names)
    }
    selected = [Field(names[0], names[0])]
    assert order_fields(selected, specs, type_name="Thing") == selected
    assert len(comparisons) <= 4 * size


@pytest.mark.parametrize("schema_fields", [False, True])
def test_dependency_order_preserves_aliases_and_selection_identity(schema_fields):
    graph = {"last": ("right", "left"), "right": ("base",), "left": ("base",)}
    specs = {name: SimpleNamespace(requires=deps) for name, deps in graph.items()}
    if schema_fields:
        specs = {name: SimpleNamespace(resolver=spec) for name, spec in specs.items()}
        specs["plain"] = SimpleNamespace(resolver=None)
    last, alias = Field("last", "last"), Field("last", "alias")
    right, left, base, plain = [Field(name, name) for name in ("right", "left", "base", "plain")]
    selected = [last, alias, left, right, base, plain, last, alias]
    result = order_fields(selected, specs, type_name="Thing", schema_fields=schema_fields)
    expected = [base, right, left, last, plain, alias, alias]
    assert [id(item) for item in result] == [id(item) for item in expected]
    oracle = tuple(TopologicalSorter(graph).static_order())
    positions = {item.name: index for index, item in enumerate(result[:5])}
    for name, dependencies in graph.items():
        for dependency in dependencies:
            assert positions[dependency] < positions[name]
            assert oracle.index(dependency) < oracle.index(name)


@pytest.mark.parametrize("schema_fields", [False, True])
def test_unselected_and_unknown_dependencies_do_not_widen_selection(schema_fields):
    selected = [Field("last", "last"), Field("first", "first")]
    specs = {
        "last": SimpleNamespace(requires=("hidden", "unknown")),
        "hidden": SimpleNamespace(requires=("first",)),
    }
    if schema_fields:
        specs = {name: SimpleNamespace(resolver=spec) for name, spec in specs.items()}
    assert order_fields(selected, specs, type_name="Thing", schema_fields=schema_fields) == [
        selected[1],
        selected[0],
    ]


@pytest.mark.parametrize(
    "graph, cycle",
    [
        ({"outer": ("a",), "a": ("b",), "b": ("a",)}, "a -> b -> a"),
        ({"outer": ("outer",)}, "outer -> outer"),
    ],
)
@pytest.mark.parametrize("schema_fields", [False, True])
def test_cycle_diagnostic_names_exact_active_path(graph, cycle, schema_fields):
    specs = {name: SimpleNamespace(requires=deps) for name, deps in graph.items()}
    if schema_fields:
        specs = {name: SimpleNamespace(resolver=spec) for name, spec in specs.items()}
    with pytest.raises(ResolverError) as caught:
        order_fields(
            [Field("outer", "outer")], specs, type_name="Thing", schema_fields=schema_fields
        )
    assert str(caught.value) == f"resolver dependency cycle on Thing: {cycle}"
