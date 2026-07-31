"""OpenAPI generation tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import pytest

import wreath.binding as binding
from wreath import Wreath
from wreath.binding import Field as SchemaField
from wreath.openapi import ResponseSpec, compare_openapi, generate_openapi


@dataclass
class Widget:
    name: str
    weight: float
    labels: list[str] = field(default_factory=list)


def test_application_image_shares_one_binding_inspection(monkeypatch) -> None:
    calls = 0
    target = None
    real_signature = binding.inspect.signature

    def counting_signature(*args, **kwargs):
        nonlocal calls
        if args and args[0] is target:
            calls += 1
        return real_signature(*args, **kwargs)

    monkeypatch.setattr(binding.inspect, "signature", counting_signature)
    app = Wreath()

    @app.get("/items/{item_id}")
    async def item(request: Any, item_id: int) -> str:
        return str(item_id)

    target = item
    app._compile_routes()
    generate_openapi(app)
    from wreath.typegen import build_api_model

    build_api_model(app, allow_unknown=True)

    assert calls == 1


def build_app() -> Wreath:
    app = Wreath()

    @app.get("/widgets/{widget_id}")
    async def get_widget(request: Any, widget_id: int, expand: bool = False) -> Widget:
        """Fetch one widget."""
        raise NotImplementedError

    @app.post("/widgets")
    async def create_widget(request: Any, widget: Widget) -> Widget:
        raise NotImplementedError

    @app.get("/plain")
    async def plain(request: Any) -> Any:
        raise NotImplementedError

    return app


def test_paths_parameters_and_schemas() -> None:
    spec = generate_openapi(build_app(), title="Test API", version="1.2.3")
    assert spec["openapi"] == "3.1.0"
    assert spec["info"] == {"title": "Test API", "version": "1.2.3"}

    get = spec["paths"]["/widgets/{widget_id}"]["get"]
    assert get["description"] == "Fetch one widget."
    params = {p["name"]: p for p in get["parameters"]}
    assert params["widget_id"]["in"] == "path"
    assert params["widget_id"]["required"] is True
    assert params["widget_id"]["schema"] == {"type": "integer"}
    assert params["expand"]["in"] == "query"
    assert params["expand"]["required"] is False
    assert params["expand"]["schema"] == {"type": "boolean", "default": False}
    assert get["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Widget"
    }

    post = spec["paths"]["/widgets"]["post"]
    body = post["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/Widget"}

    widget = spec["components"]["schemas"]["Widget"]
    assert widget["required"] == ["name", "weight"]
    assert widget["properties"]["weight"] == {"type": "number"}
    assert widget["properties"]["labels"] == {"type": "array", "items": {"type": "string"}}

    plain = spec["paths"]["/plain"]["get"]
    assert "requestBody" not in plain
    assert plain["responses"]["200"] == {"description": "Successful response"}


@pytest.mark.asyncio
async def test_enable_docs_routes() -> None:
    app = build_app()
    # `enable_docs` is environment-gated now (production registers neither
    # route); `environments=None` is the explicit "everywhere" this test wants.
    assert app.enable_docs(title="Test API", environments=None) is True

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def call(path: str) -> tuple[int, bytes, bytes]:
        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 50000),
            "root_path": "",
        }
        await app(scope, receive, send)
        content_type = dict(sent[0]["headers"]).get(b"content-type", b"")
        body = b"".join(
            m.get("body", b"") for m in sent if m["type"] == "http.response.body"
        )
        return sent[0]["status"], content_type, body

    status, content_type, body = await call("/openapi.json")
    assert status == 200
    assert content_type == b"application/json"
    spec = json.loads(body)
    assert "/widgets/{widget_id}" in spec["paths"]

    status, content_type, body = await call("/docs")
    assert status == 200
    assert content_type == b"text/html; charset=utf-8"
    # The docs page is now self-contained (no CDN Swagger UI).
    assert b"<!DOCTYPE html>" in body
    assert b"swagger" not in body.lower()
    assert b"unpkg" not in body


def test_operation_ids_match_typegen() -> None:
    from wreath.typegen import build_api_model

    app = build_app()
    spec = generate_openapi(app)
    api = build_api_model(app, allow_unknown=True)
    typegen_ids = {op.id for op in api.operations}

    openapi_ids = {
        operation["operationId"]
        for path in spec["paths"].values()
        for operation in path.values()
    }
    assert openapi_ids == typegen_ids
    # The canonical derivation, not handler.__name__.
    assert "getWidgetsByWidgetId" in openapi_ids


def test_route_tags_and_summary() -> None:
    app = Wreath()

    @app.get("/tagged", tags=["items", "public"], summary="List the things")
    async def tagged(request: Any) -> Any:
        raise NotImplementedError

    spec = generate_openapi(app)
    operation = spec["paths"]["/tagged"]["get"]
    assert operation["tags"] == ["items", "public"]
    assert operation["summary"] == "List the things"


@dataclass
class RichWidget:
    widget_id: UUID
    amount: Decimal
    kind: Literal["physical", "digital"]
    due: date
    display_name: Annotated[
        str,
        SchemaField(
            alias="displayName",
            min_length=3,
            max_length=40,
            pattern=r"^[A-Z]",
            description="Customer-facing name",
            examples=("Wreath",),
        ),
    ]
    score: Annotated[int, SchemaField(gt=0, ge=1, lt=6, le=5)]


@dataclass
class ConflictBody:
    code: str


def test_schema_metadata_and_query_constraints_share_the_runtime_contract() -> None:
    app = Wreath()

    @app.post("/widgets")
    async def create(
        request: Any,
        body: RichWidget,
        limit: Annotated[int, binding.Query(minimum=1, maximum=100)] = 20,
    ) -> RichWidget:
        raise NotImplementedError

    spec = generate_openapi(app)
    model = spec["components"]["schemas"]["RichWidget"]
    assert model["properties"]["widget_id"] == {
        "type": "string",
        "format": "uuid",
    }
    assert model["properties"]["amount"] == {
        "type": "string",
        "format": "decimal",
    }
    assert model["properties"]["kind"] == {"enum": ["physical", "digital"]}
    assert model["properties"]["due"] == {"type": "string", "format": "date"}
    assert model["properties"]["displayName"] == {
        "type": "string",
        "description": "Customer-facing name",
        "examples": ["Wreath"],
        "minLength": 3,
        "maxLength": 40,
        "pattern": r"^[A-Z]",
    }
    assert model["properties"]["score"]["minimum"] == 1
    assert model["properties"]["score"]["maximum"] == 5
    assert model["properties"]["score"]["exclusiveMinimum"] == 0
    assert model["properties"]["score"]["exclusiveMaximum"] == 6
    parameter = spec["paths"]["/widgets"]["post"]["parameters"][0]
    assert parameter["schema"]["minimum"] == 1
    assert parameter["schema"]["maximum"] == 100


def test_operation_responses_security_deprecation_and_visibility() -> None:
    app = Wreath()
    app.add_security_scheme(
        "bearerAuth",
        {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
    )

    @app.post(
        "/widgets",
        status_code=201,
        response_description="Created widget",
        responses={
            400: ConflictBody,
            409: ResponseSpec(ConflictBody, description="Name conflict"),
        },
        deprecated=True,
        security={"bearerAuth": ()},
    )
    async def create(request: Any, body: RichWidget) -> RichWidget:
        raise NotImplementedError

    @app.get("/internal", include_in_schema=False)
    async def internal(request: Any) -> Any:
        raise NotImplementedError

    spec = generate_openapi(app)
    operation = spec["paths"]["/widgets"]["post"]
    assert operation["deprecated"] is True
    assert operation["security"] == [{"bearerAuth": []}]
    assert operation["responses"]["201"]["description"] == "Created widget"
    assert operation["responses"]["409"] == {
        "description": "Name conflict",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ConflictBody"}
            }
        },
    }
    assert operation["responses"]["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ConflictBody"
    }
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert "/internal" not in spec["paths"]


@pytest.mark.asyncio
async def test_declared_success_status_is_the_runtime_status() -> None:
    from wreath.testing import TestClient

    app = Wreath()

    @app.post("/widgets", status_code=201)
    async def create(request: Any) -> dict[str, bool]:
        return {"created": True}

    async with TestClient(app) as client:
        response = await client.post("/widgets")

    assert response.status == 201


def test_openapi_compatibility_reports_breaking_contract_changes() -> None:
    previous = {
        "paths": {
            "/widgets": {
                "get": {
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    }
    current = {
        "paths": {
            "/widgets": {
                "get": {
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {"201": {"description": "ok"}},
                }
            }
        }
    }

    changes = compare_openapi(previous, current)

    assert {(change.kind, change.location) for change in changes} == {
        ("parameter-became-required", "GET /widgets query:limit"),
        ("response-removed", "GET /widgets 200"),
    }


def test_openapi_refuses_an_unsupported_annotation_instead_of_empty_schema() -> None:
    app = Wreath()

    @app.get("/unsupported")
    async def unsupported(request: Any) -> complex:
        raise NotImplementedError

    with pytest.raises(TypeError, match="unsupported annotation"):
        generate_openapi(app)


def test_openapi_refuses_a_route_that_names_an_undeclared_security_scheme() -> None:
    app = Wreath()

    @app.get("/private", security={"missing": ()})
    async def private(request: Any) -> Any:
        raise NotImplementedError

    with pytest.raises(ValueError, match="undeclared OpenAPI security scheme"):
        generate_openapi(app)
