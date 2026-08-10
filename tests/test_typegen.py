"""Canonical model, schema shapes, operation ids, and TypeScript rendering."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest

from tests.typegen.other import Item as OtherItem
from wreath import Wreath
from wreath.typegen import build_api_model, render_typescript
from wreath.typegen.inspect import derive_operation_id, resolve_operation_ids
from wreath.typegen.model import TypegenError, TypeRef

EXPECTED = Path(__file__).parent / "typegen" / "expected"


# Models must live at module scope: with `from __future__ import annotations`
# active, get_type_hints resolves handler return annotations against module
# globals, exactly as a real application's module-level models would.
class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


@dataclass
class RequiredOptionalNullable:
    required: str
    optional: str = "x"
    nullable: str | None = None


@dataclass
class LiteralEnumShape:
    mode: Literal["a", "b"]
    color: Color


@dataclass
class ContainerShape:
    items: list[int]
    pair: tuple[int, str]
    variadic: tuple[int, ...]
    mapping: dict[str, float]


@dataclass
class Node:
    value: int
    next: Node | None = None


@dataclass
class LocalItem:
    here: str


@dataclass
class UnsupportedShape:
    id: complex


def _model(app: Wreath, **kwargs: Any) -> Any:
    return build_api_model(app, **kwargs)


# --- operation ids ---------------------------------------------------------


def test_derives_deterministic_operation_id() -> None:
    assert derive_operation_id("GET", "/widgets/{widget_id}") == "getWidgetsByWidgetId"
    assert derive_operation_id("POST", "/widgets") == "postWidgets"
    assert derive_operation_id("GET", "/") == "getRoot"


def test_explicit_operation_id_is_preserved() -> None:
    app = Wreath()

    @app.get("/widgets/{widget_id}", operation_id="getWidget")
    async def handler(request, widget_id: int) -> None: ...

    api = _model(app)
    assert [op.id for op in api.operations] == ["getWidget"]


def test_duplicate_operation_ids_are_rejected() -> None:
    app = Wreath()

    @app.get("/a", operation_id="dup")
    async def a(request) -> None: ...

    @app.get("/b", operation_id="dup")
    async def b(request) -> None: ...

    with pytest.raises(TypegenError, match="duplicate operation id 'dup'"):
        _model(app)


def test_invalid_explicit_operation_id_is_rejected() -> None:
    app = Wreath()

    @app.get("/a", operation_id="not valid!")
    async def a(request) -> None: ...

    with pytest.raises(TypegenError, match="not a valid client identifier"):
        _model(app)


def test_router_prefix_preserves_operation_ids() -> None:
    from wreath import Router

    router = Router(prefix="/v1")

    @router.get("/widgets/{widget_id}", operation_id="getWidget")
    async def handler(request, widget_id: int) -> None: ...

    app = Wreath()
    app.include_router(router)
    api = _model(app)
    assert [op.id for op in api.operations] == ["getWidget"]
    assert api.operations[0].path == "/v1/widgets/{widget_id}"


def test_two_methods_one_route_get_distinct_ids() -> None:
    class FakeRoute:
        def __init__(self) -> None:
            self.methods = ("GET", "POST")
            self.path = "/thing"
            self.operation_id = "thing"

    resolved, diagnostics = resolve_operation_ids([FakeRoute()])
    assert diagnostics == ()
    assert set(resolved.values()) == {"thingGET", "thingPOST"}


# --- schema shapes ---------------------------------------------------------


def _returns(app: Wreath) -> TypeRef:
    return _model(app).operations[0].response_body


def test_required_vs_optional_vs_nullable() -> None:
    app = Wreath()

    @app.get("/")
    async def handler(request) -> RequiredOptionalNullable: ...

    model = _model(app).models[0]
    by_name = {f.wire_name: f for f in model.fields}
    assert by_name["required"].required is True
    assert by_name["optional"].required is False
    # nullable is a union with null but still optional here (has a default).
    assert by_name["nullable"].required is False
    assert by_name["nullable"].type.kind == "union"
    assert TypeRef("null") in by_name["nullable"].type.arguments


def test_literal_and_enum() -> None:
    app = Wreath()

    @app.get("/")
    async def handler(request) -> LiteralEnumShape: ...

    model = _model(app).models[0]
    by_name = {f.wire_name: f for f in model.fields}
    assert by_name["mode"].type == TypeRef("literal", literals=("a", "b"))
    assert by_name["color"].type == TypeRef("literal", literals=("red", "blue"))


def test_list_tuple_and_record() -> None:
    app = Wreath()

    @app.get("/")
    async def handler(request) -> ContainerShape: ...

    by_name = {f.wire_name: f.type for f in _model(app).models[0].fields}
    assert by_name["items"] == TypeRef("array", arguments=(TypeRef("integer"),))
    assert by_name["pair"] == TypeRef(
        "tuple", arguments=(TypeRef("integer"), TypeRef("string"))
    )
    assert by_name["variadic"] == TypeRef("array", arguments=(TypeRef("integer"),))
    assert by_name["mapping"] == TypeRef("record", arguments=(TypeRef("number"),))


def test_recursive_model_terminates() -> None:
    app = Wreath()

    @app.get("/")
    async def handler(request) -> Node: ...

    api = _model(app)
    assert [m.name for m in api.models] == ["Node"]
    next_field = {f.wire_name: f for f in api.models[0].fields}["next"]
    assert any(arg.name == "Node" for arg in next_field.type.arguments)


def test_same_name_models_get_distinct_aliases() -> None:

    app = Wreath()

    @app.get("/a")
    async def a(request) -> LocalItem: ...

    @app.get("/b")
    async def b(request) -> OtherItem: ...

    names = {m.name for m in _model(app).models}
    # LocalItem keeps its name; OtherItem ("Item") stays distinct, never merged.
    assert "Item" in names
    assert "LocalItem" in names
    assert len(names) == 2


# --- strictness ------------------------------------------------------------


def test_strict_rejects_unsupported_annotation() -> None:
    app = Wreath()

    @app.get("/")
    async def handler(request) -> UnsupportedShape: ...

    with pytest.raises(TypegenError, match="unsupported annotation"):
        _model(app)


def test_allow_unknown_maps_to_unknown_not_any() -> None:
    app = Wreath()

    @app.get("/")
    async def handler(request) -> UnsupportedShape: ...

    api = _model(app, allow_unknown=True)
    field_type = api.models[0].fields[0].type
    assert field_type == TypeRef("unknown")
    files = render_typescript(api)
    assert ": unknown;" in files["models.ts"]
    assert ": any" not in files["models.ts"]


# --- rendering / golden ----------------------------------------------------


def _generate_fixture(**kwargs: Any) -> dict[str, str]:
    from tests.typegen.app import app

    api = build_api_model(app, title="Fixture API", version="2.0.0")
    return render_typescript(
        api, react_query=True, base_url_env="VITE_API_URL", **kwargs
    )


def test_generation_matches_golden() -> None:
    files = _generate_fixture()
    for name, contents in files.items():
        expected = (EXPECTED / name).read_text(encoding="utf-8")
        assert contents == expected, f"{name} drifted from golden"


def test_generation_is_deterministic() -> None:
    assert _generate_fixture() == _generate_fixture()


def test_no_secrets_paths_or_timestamps_in_output() -> None:
    import re

    files = _generate_fixture()
    blob = "\n".join(files.values())
    assert "/home/" not in blob
    assert "Bearer" not in blob
    # No ISO-ish timestamp embedded.
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", blob)


def test_client_transport_conventions_are_present() -> None:
    client = _generate_fixture()["client.ts"]
    assert "encodeURIComponent(String(parameters." in client
    assert "!== undefined) url.searchParams.set(" in client
    assert "JSON.stringify(body)" in client
    assert "throw new WreathApiError(" in client
