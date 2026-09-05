from dataclasses import dataclass
from typing import Annotated, Any

from wreath._mcp import schema
from wreath.binding import Body
from wreath.binding import Field as SchemaField
from wreath.openapi import _component_schema
from wreath.typegen.model import Field, Model, TypeRef


def test_simple_mcp_schema_does_not_recopy_generated_tree(monkeypatch):
    visits = []
    original = schema._rebase_refs

    def counted(node):
        visits.append(node)
        return original(node)

    monkeypatch.setattr(schema, "_rebase_refs", counted)

    def handler(request, query: str, limit: int = 20):
        return query[:limit]

    rendered, _ = schema.derive_input_schema(handler, "search")
    assert rendered["properties"]["limit"] == {"type": "integer", "default": 20}
    assert visits == []


def test_mutable_defaults_are_cloned_and_rebased_but_tuples_untouched():
    inner_tuple = ({"$ref": "#/components/schemas/Unchanged"},)
    default = {
        "$ref": "prefix#/components/schemas/Model",
        "nested": [{"key": "value"}],
        "tuple": inner_tuple,
    }

    def handler(request, options: Any = default):
        return options

    rendered, _ = schema.derive_input_schema(handler, "default")
    value = rendered["properties"]["options"]["default"]
    assert value == {
        "$ref": "prefix#/$defs/Model",
        "nested": [{"key": "value"}],
        "tuple": inner_tuple,
    }
    assert value is not default
    assert value["nested"] is not default["nested"]
    assert value["nested"][0] is not default["nested"][0]
    assert value["tuple"] is inner_tuple
    value["nested"][0]["key"] = "changed"
    assert default["nested"][0]["key"] == "value"


@dataclass
class Child:
    label: str


@dataclass
class Parent:
    children: list[Child]
    lookup: dict[str, Child | None]
    pair: tuple[Child, int]


def test_nested_model_reference_shapes_are_emitted_for_mcp():
    def handler(request, body: Annotated[Parent, Body()]):
        return body

    rendered, _ = schema.derive_input_schema(handler, "model")
    assert rendered["properties"]["body"] == {"$ref": "#/$defs/Parent"}
    fields = rendered["$defs"]["Parent"]["properties"]
    assert fields["children"]["items"] == {"$ref": "#/$defs/Child"}
    assert fields["lookup"]["additionalProperties"]["anyOf"][0] == {"$ref": "#/$defs/Child"}
    assert fields["pair"]["prefixItems"][0] == {"$ref": "#/$defs/Child"}


def test_component_examples_keep_openapi_shallow_ownership():
    example = {"$ref": "#/components/schemas/Child", "nested": [1]}
    model = Model("Example", (Field("value", TypeRef("string"), True, examples=(example,)),))
    rendered = _component_schema(model)
    assert rendered["properties"]["value"]["examples"][0] is example


def test_component_examples_are_cloned_and_rebased_for_mcp():
    example = {"$ref": "#/components/schemas/Child", "nested": [1]}
    model = Model("Example", (Field("value", TypeRef("string"), True, examples=(example,)),))
    rendered = _component_schema(model, "#/$defs/", schema._rebase_refs)
    value = rendered["properties"]["value"]["examples"][0]
    assert value == {"$ref": "#/$defs/Child", "nested": [1]}
    assert value is not example
    assert value["nested"] is not example["nested"]


@dataclass
class Recursive:
    children: list[Recursive]


def test_recursive_model_references_resolve_inside_defs():
    def handler(request, body: Annotated[Recursive, Body()]):
        return body

    rendered, _ = schema.derive_input_schema(handler, "recursive")
    assert set(rendered["$defs"]) == {"Recursive"}
    children = rendered["$defs"]["Recursive"]["properties"]["children"]
    assert children == {"type": "array", "items": {"$ref": "#/$defs/Recursive"}}


def test_tool_model_examples_preserve_the_payload_copy_boundary():
    example = {"$ref": "#/components/schemas/Child", "nested": [1]}

    @dataclass
    class ExampleBody:
        value: Annotated[str, SchemaField(examples=(example,))]

    def handler(request, body: Annotated[ExampleBody, Body()]):
        return body

    rendered, _ = schema.derive_input_schema(handler, "example")
    value = rendered["$defs"]["ExampleBody"]["properties"]["value"]["examples"][0]
    assert value == {"$ref": "#/$defs/Child", "nested": [1]}
    value["nested"].append(2)
    assert example["nested"] == [1]


def test_orm_model_schema_uses_the_same_reference_policy():
    from wreath.orm import Mapped, column
    from wreath.orm import Model as ORMModel
    from wreath.orm.types import Text

    class Item(ORMModel, table="mcp_schema_resource_item"):
        label: Mapped[str] = column(Text, primary_key=True)

    def handler(request, body: Annotated[Item, Body()]):
        return body

    rendered, _ = schema.derive_input_schema(handler, "orm")
    assert rendered["properties"]["body"] == {"$ref": "#/$defs/Item"}
    assert rendered["$defs"]["Item"]["properties"]["label"] == {"type": "string"}
