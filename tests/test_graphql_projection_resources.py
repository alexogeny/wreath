from types import SimpleNamespace

import pytest

from wreath._graphql.execute import _Run
from wreath._graphql.parser import Field, parse
from wreath._graphql.resolvers import ResolverError, ResolverSpec, order_fields
from wreath._graphql.schema import ObjectType, Schema, SchemaField


def _run(object_type):
    return _Run(
        Schema(None, {object_type.name: object_type}, {}),
        parse("{ wanted }"),
        SimpleNamespace(),
        variables={},
        authorizer=None,
        request=None,
        max_page_size=10,
        on_denied="error",
        action="read",
        policy_schema=None,
    )


def _field(name, fn, requires=()):
    return SchemaField(
        name,
        "Int",
        True,
        False,
        resolver=ResolverSpec("Thing", name, fn, requires=requires),
    )


async def test_projection_does_not_scan_unselected_schema_fields():
    scanned = []

    class Fields(dict):
        def items(self):
            scanned.append(len(self))
            return super().items()

    fields = Fields(
        {
            f"field{index}": SchemaField(
                f"field{index}", "Int", True, False, attribute=f"field{index}"
            )
            for index in range(1000)
        }
    )
    fields["wanted"] = _field("wanted", lambda values, info: [7] * len(values))
    object_type = ObjectType("Thing", None, fields)
    result = await _run(object_type)._project(
        [object()], object_type, [Field("wanted", "wanted")], ()
    )
    assert result == [{"wanted": 7}]
    assert scanned == []


async def test_hidden_dependencies_and_aliases_keep_resolver_order():
    calls = []

    def resolver(name, value):
        def resolve(values, info):
            calls.append(name)
            return [value] * len(values)

        return resolve

    fields = {
        "wanted": _field("wanted", resolver("wanted", 3), ("middle",)),
        "middle": _field("middle", resolver("middle", 2), ("first",)),
        "first": _field("first", resolver("first", 1)),
    }
    object_type = ObjectType("Thing", None, fields)
    result = await _run(object_type)._project(
        [object()],
        object_type,
        [Field("wanted", "a"), Field("wanted", "b")],
        (),
    )
    assert result == [{"a": 3, "b": 3}]
    assert calls == ["first", "middle", "wanted", "wanted"]


async def test_unselected_transitive_cycle_is_still_refused():
    fields = {
        "wanted": _field("wanted", lambda values, info: [3], ("middle",)),
        "middle": _field("middle", lambda values, info: [2], ("first",)),
        "first": _field("first", lambda values, info: [1], ("middle",)),
    }
    object_type = ObjectType("Thing", None, fields)
    with pytest.raises(ResolverError, match="middle -> first -> middle"):
        await _run(object_type)._project([object()], object_type, [Field("wanted", "wanted")], ())


async def test_mutated_field_map_is_seen_on_next_projection():
    fields = {"wanted": _field("wanted", lambda values, info: [1])}
    object_type = ObjectType("Thing", None, fields)
    run = _run(object_type)
    selected = [Field("wanted", "wanted")]
    assert await run._project([object()], object_type, selected, ()) == [{"wanted": 1}]
    fields["wanted"] = _field("wanted", lambda values, info: [2])
    assert await run._project([object()], object_type, selected, ()) == [{"wanted": 2}]


def test_original_resolver_mapping_order_contract():
    first, last = Field("first", "first"), Field("last", "last")
    specs = {
        "last": ResolverSpec("Thing", "last", lambda values, info: values, requires=("first",))
    }
    assert order_fields([last, first], specs, type_name="Thing") == [first, last]


async def test_field_replacement_during_resolver_await_is_visible():
    async def first(values, info):
        fields["wanted"] = _field("wanted", lambda values, info: [9])
        return [1]

    fields = {
        "first": _field("first", first),
        "wanted": _field("wanted", lambda values, info: [2]),
    }
    object_type = ObjectType("Thing", None, fields)
    selected = [Field("first", "first"), Field("wanted", "wanted")]
    assert await _run(object_type)._project([object()], object_type, selected, ()) == [
        {"first": 1, "wanted": 9}
    ]


def test_all_selected_schema_fields_match_spec_mapping_order():
    fields = {
        "last": _field("last", lambda values, info: values, ("middle",)),
        "middle": _field("middle", lambda values, info: values, ("first",)),
        "first": _field("first", lambda values, info: values),
        "plain": SchemaField("plain", "Int", True, False, attribute="plain"),
    }
    selected = [Field(name, name) for name in fields]
    specs = {name: field.resolver for name, field in fields.items() if field.resolver}
    expected = [selected[2], selected[1], selected[0], selected[3]]
    assert order_fields(selected, specs, type_name="Thing") == expected
    assert order_fields(selected, fields, type_name="Thing", schema_fields=True) == expected
