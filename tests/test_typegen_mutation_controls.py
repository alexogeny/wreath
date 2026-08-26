"""Focused objections for the typegen inspection boundary."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Annotated, Any

import pytest

import wreath.typegen.inspect as typegen_inspect
from wreath import Wreath
from wreath.binding import Field as SchemaField
from wreath.orm import MISSING, Mapped, Model, column
from wreath.orm.types import Int64, Text
from wreath.pagination import Page
from wreath.typegen.inspect import (
    _Builder,
    _ModelRegistry,
    _operation_shape,
    _permission_sets,
    _series_shape,
    _series_shapes,
    build_api_model,
    is_valid_identifier,
    resolve_operation_ids,
)
from wreath.typegen.model import INTEGER, NULL, STRING, UNKNOWN, PermissionSet, TypeRef


class _TypegenRecord(Model, table="typegen_mutation_records"):
    id: Mapped[int] = column(Int64, primary_key=True)
    nickname: Mapped[str] = column(Text, nullable=True)
    rank: Mapped[int] = column(Int64, default=1)


class _UntypedRecord(Model, table="typegen_untyped_mutation_records"):
    id = column(Int64, primary_key=True)


_series_private: Any = None
series_noise = object()


@pytest.mark.parametrize("name", ["class", "await", "delete", "false", "yield"])
def test_javascript_keywords_are_not_client_identifiers(name: str) -> None:
    assert not is_valid_identifier(name)


@pytest.mark.parametrize("name", ["getWidget", "_private", "café"])
def test_non_keyword_python_identifiers_are_client_identifiers(name: str) -> None:
    assert is_valid_identifier(name)


@pytest.mark.parametrize("name", ["", "not valid", "two-parts", "3things"])
def test_invalid_python_identifiers_are_not_client_identifiers(name: str) -> None:
    assert not is_valid_identifier(name)


def test_invalid_id_on_methodless_route_has_no_invented_method() -> None:
    route = SimpleNamespace(operation_id="class", methods=(), path="/items/{id:int}")

    resolved, diagnostics = resolve_operation_ids([route])

    assert resolved == {}
    assert len(diagnostics) == 1
    assert diagnostics[0].method is None
    assert diagnostics[0].path == "/items/{id}"


def test_invalid_id_on_routed_method_reports_that_method() -> None:
    route = SimpleNamespace(operation_id="class", methods=("PATCH",), path="/items")

    _resolved, diagnostics = resolve_operation_ids([route])

    assert diagnostics[0].method == "PATCH"


def test_explicit_id_is_suffixed_for_every_method_on_a_multi_method_route() -> None:
    route = SimpleNamespace(
        operation_id="items", methods=("GET", "POST"), path="/items"
    )

    resolved, diagnostics = resolve_operation_ids([route])

    assert diagnostics == ()
    assert resolved == {(0, "GET"): "itemsGET", (0, "POST"): "itemsPOST"}


def _named_type(name: str, module: str, qualname: str | None = None) -> type:
    namespace = {"__module__": module}
    if qualname is not None:
        namespace["__qualname__"] = qualname
    return type(name, (), namespace)


def test_model_registry_uses_each_collision_fallback_before_refusing() -> None:
    registry = _ModelRegistry()
    first = _named_type("Item", "one")
    second = _named_type("Item", "two")
    third = _named_type("Item", "two", "Outer.Item")
    duplicate = _named_type("Item", "two", "Outer.Item")

    assert registry.reference(first, list) == "Item"
    assert registry.reference(second, list) == "Item_two"
    assert registry.reference(third, list) == "Item_two_OuterItem"
    with pytest.raises(KeyError, match="Item"):
        registry.reference(duplicate, list)


def test_model_registry_normalizes_a_missing_module_on_collision() -> None:
    registry = _ModelRegistry()
    first = _named_type("Item", "one")
    missing_module = type("Item", (), {"__module__": None})

    assert registry.reference(first, list) == "Item"
    assert registry.reference(missing_module, list) == "Item_"


@pytest.mark.parametrize("annotation", [Any, object, inspect.Parameter.empty])
def test_unknown_annotations_have_the_unknown_shape(annotation: object) -> None:
    builder = _Builder(False)

    assert builder.type_ref(annotation) == UNKNOWN
    assert builder.diagnostics == []


def test_response_declarations_are_opaque_without_a_diagnostic() -> None:
    from wreath.response import Response

    builder = _Builder(False)

    assert builder.type_ref(Response) == UNKNOWN
    assert builder.diagnostics == []


@pytest.mark.parametrize("annotation", [None, type(None)])
def test_none_annotations_have_the_null_shape(annotation: object) -> None:
    assert _Builder(False).type_ref(annotation) == NULL


@pytest.mark.parametrize("annotation", [list, tuple, set, frozenset])
def test_bare_collections_have_an_unknown_element(annotation: object) -> None:
    assert _Builder(False).type_ref(annotation) == TypeRef(
        "array", arguments=(UNKNOWN,)
    )


@pytest.mark.parametrize("annotation", [list[int], set[int], frozenset[int]])
def test_typed_collections_preserve_their_element(annotation: object) -> None:
    assert _Builder(False).type_ref(annotation) == TypeRef(
        "array", arguments=(INTEGER,)
    )


def test_bare_and_typed_records_have_distinct_value_shapes() -> None:
    builder = _Builder(False)

    assert builder.type_ref(dict) == TypeRef("record", arguments=(UNKNOWN,))
    assert builder.type_ref(dict[str, int]) == TypeRef(
        "record", arguments=(INTEGER,)
    )


def test_record_key_must_be_a_string() -> None:
    builder = _Builder(False)

    assert builder.type_ref(dict[int, str]) == UNKNOWN
    assert len(builder.diagnostics) == 1
    assert "dict[int, str]" in builder.diagnostics[0].message


def test_annotated_and_page_types_preserve_the_inner_type() -> None:
    builder = _Builder(False)

    assert builder.type_ref(Annotated[int, "wire metadata"]) == INTEGER
    assert builder.type_ref(Page[str]) == TypeRef("page", arguments=(STRING,))


@dataclass
class _TaggedSet:
    tags: Annotated[set[int], "wire metadata"]


@dataclass
class _ConstrainedField:
    value: Annotated[
        int,
        SchemaField(
            alias="wireValue",
            description="Measured value",
            examples=(3,),
            gt=0,
            ge=1,
            lt=5,
            le=4,
            min_length=1,
            max_length=2,
            pattern=r"^[0-9]+$",
        ),
    ]
    ordinary: Annotated[int, SchemaField(description="No alias")]


def test_annotated_set_field_remains_unique_and_typed() -> None:
    builder = _Builder(False)

    reference = builder.type_ref(_TaggedSet)

    assert reference.kind == "reference"
    model = builder.registry.models()[0]
    assert model.fields[0].type == TypeRef("array", arguments=(INTEGER,))
    assert model.fields[0].unique_items is True


def test_binding_field_metadata_reaches_the_model() -> None:
    builder = _Builder(False)

    builder.type_ref(_ConstrainedField)

    field = builder.registry.models()[0].fields[0]
    assert field.wire_name == "wireValue"
    assert field.description == "Measured value"
    assert field.examples == (3,)
    assert (field.gt, field.ge, field.lt, field.le) == (0, 1, 5, 4)
    assert (field.min_length, field.max_length) == (1, 2)
    assert field.pattern == r"^[0-9]+$"
    ordinary = builder.registry.models()[0].fields[1]
    assert ordinary.wire_name == "ordinary"
    assert ordinary.description == "No alias"


def test_wreath_model_uses_its_declared_columns() -> None:
    builder = _Builder(False)

    reference = builder.type_ref(_TypegenRecord)

    assert reference.kind == "reference"
    assert [
        (field.wire_name, field.type, field.required)
        for field in builder.registry.models()[0].fields
    ] == [
        ("id", INTEGER, True),
        ("nickname", STRING, False),
        ("rank", INTEGER, False),
    ]


def test_unresolved_wreath_hints_preserve_inheritance_without_copying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Base:
        __annotations__ = {
            "inherited": Mapped[int],
            "overridden": Mapped[int],
        }

    class Derived(Base):
        __annotations__ = {"overridden": Mapped[str]}
        __wreath_columns__ = (
            SimpleNamespace(
                python_name="inherited", nullable=False, default=MISSING
            ),
            SimpleNamespace(
                python_name="overridden", nullable=False, default=MISSING
            ),
        )

    def unresolved(*_args: Any, **_kwargs: Any) -> Any:
        raise NameError("unresolved annotation")

    monkeypatch.setattr(typegen_inspect.typing, "get_type_hints", unresolved)

    fields = _Builder(False)._wreath_model_fields(Derived)

    assert [(field.wire_name, field.type) for field in fields] == [
        ("inherited", INTEGER),
        ("overridden", STRING),
    ]


def test_unannotated_wreath_column_has_an_unknown_type() -> None:
    builder = _Builder(False)

    builder.type_ref(_UntypedRecord)

    field = builder.registry.models()[0].fields[0]
    assert field.type == UNKNOWN
    assert field.required is True


def test_hidden_route_is_absent_and_docstrings_are_normalized() -> None:
    app = Wreath()

    @app.get("/hidden", include_in_schema=False)
    async def hidden(request) -> str:
        """This must not enter the client model."""
        return "hidden"

    @app.get("/plain")
    async def plain(request) -> str:
        return "plain"

    @app.get("/documented")
    async def documented(request) -> str:
        """Public documentation."""
        return "documented"

    operations = {operation.path: operation for operation in build_api_model(app).operations}

    assert set(operations) == {"/documented", "/plain"}
    assert operations["/plain"].description is None
    assert operations["/documented"].description == "Public documentation."


def test_permission_sets_drop_the_empty_resource_type(monkeypatch) -> None:
    monkeypatch.setattr(
        "wreath._auth.permissions.declared_actions",
        lambda app: {"": ("ignored",), "Widget": ("read", "write")},
    )

    assert _permission_sets(object()) == (
        PermissionSet(resource_type="Widget", actions=("read", "write")),
    )


def test_unbound_operation_only_recognizes_complete_path_placeholders() -> None:
    builder = _Builder(False)
    definition = SimpleNamespace(path="/{id}/literal}/{unfinished", endpoint=None)

    parameters, body, media, response = _operation_shape(
        builder, None, definition, "GET", str
    )

    assert [(parameter.wire_name, parameter.type) for parameter in parameters] == [
        ("id", STRING)
    ]
    assert body is None
    assert media is None
    assert response == STRING


def _multipart_shape(
    form_params: tuple[object, ...], file_params: tuple[object, ...]
) -> tuple[TypeRef | None, str | None]:
    spec = SimpleNamespace(
        path_params=(),
        query_params=(),
        header_params=(),
        cookie_params=(),
        body=None,
        form_params=form_params,
        file_params=file_params,
    )

    parameters, body, media, response = _operation_shape(
        _Builder(False), spec, SimpleNamespace(path="/upload"), "POST", str
    )

    assert parameters == []
    assert response == STRING
    return body, media


def test_form_binding_declares_a_multipart_record() -> None:
    body, media = _multipart_shape((object(),), ())

    assert body == TypeRef("record", arguments=(UNKNOWN,))
    assert media == "multipart/form-data"


def test_file_binding_declares_a_multipart_record() -> None:
    body, media = _multipart_shape((), (object(),))

    assert body == TypeRef("record", arguments=(UNKNOWN,))
    assert media == "multipart/form-data"


def test_series_discovery_ignores_endpoints_without_function_metadata() -> None:
    assert _series_shapes([SimpleNamespace(endpoint=object())]) == ()


def test_series_discovery_requires_code_even_with_a_namespace() -> None:
    endpoint = SimpleNamespace(__globals__={}, __code__=None)

    assert _series_shapes([SimpleNamespace(endpoint=endpoint)]) == ()


def test_series_discovery_requires_a_dict_namespace_even_with_code() -> None:
    endpoint = SimpleNamespace(
        __globals__=None, __code__=SimpleNamespace(co_names=("used",))
    )

    assert _series_shapes([SimpleNamespace(endpoint=endpoint)]) == ()


def test_series_discovery_ignores_private_declarations_and_other_globals() -> None:
    from tests.series.test_typegen import _private

    global _series_private
    _series_private = _private

    def endpoint():
        return _series_private, series_noise

    assert _series_shapes([SimpleNamespace(endpoint=endpoint)]) == ()


def test_series_without_events_reports_that_absence() -> None:
    from tests.series.test_typegen import _private

    assert _series_shape("plain", _private).events is False
