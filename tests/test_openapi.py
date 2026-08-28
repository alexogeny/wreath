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
from wreath._flight_metadata import build_metadata_image
from wreath.binding import Field as SchemaField
from wreath.binding import File, Form, Query
from wreath.openapi import ResponseSpec, compare_openapi, generate_openapi
from wreath.request import UploadedFile
from wreath.response import Response


@dataclass
class Widget:
    name: str
    weight: float
    labels: list[str] = field(default_factory=list)


def test_application_image_shares_handler_facts(monkeypatch) -> None:
    signature_calls = 0
    hint_calls = 0
    target = None
    real_signature = binding.inspect.signature
    real_hints = binding.typing.get_type_hints

    def counting_signature(*args, **kwargs):
        nonlocal signature_calls
        if args and args[0] is target:
            signature_calls += 1
        return real_signature(*args, **kwargs)

    def counting_hints(*args, **kwargs):
        nonlocal hint_calls
        if args and args[0] is target:
            hint_calls += 1
        return real_hints(*args, **kwargs)

    monkeypatch.setattr(binding.inspect, "signature", counting_signature)
    monkeypatch.setattr(binding.typing, "get_type_hints", counting_hints)
    app = Wreath()

    @app.get("/items")
    async def item() -> str:
        return "item"

    target = item
    app._compile_routes()
    generate_openapi(app)
    from wreath.typegen import build_api_model

    build_api_model(app, allow_unknown=True)

    assert signature_calls == 1
    assert hint_calls == 1


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


def test_operation_ids_are_one_fact_across_all_control_planes() -> None:
    """Multi-method explicit ids used to drift by consumer."""
    from wreath.doctor import route_manifest
    from wreath.typegen import build_api_model

    app = Wreath()

    @app.route(
        "/widgets/{widget_id}",
        methods=("GET", "PATCH"),
        operation_id="widgetById",
    )
    async def widget(request: Any, widget_id: int) -> Widget:
        raise NotImplementedError

    expected = {"widgetByIdGET", "widgetByIdPATCH"}
    openapi_ids = {
        operation["operationId"]
        for operation in generate_openapi(app)["paths"]["/widgets/{widget_id}"].values()
    }
    typegen_ids = {operation.id for operation in build_api_model(app).operations}
    flight_ids = {route.operation_id for route in build_metadata_image(app).routes}
    manifest_ids = {route["operation_id"] for route in route_manifest(app)["routes"]}

    assert openapi_ids == typegen_ids == flight_ids == manifest_ids == expected


def test_route_tags_and_summary() -> None:
    app = Wreath()

    @app.get("/tagged", tags=["items", "public"], summary="List the things")
    async def tagged(request: Any) -> Any:
        raise NotImplementedError

    spec = generate_openapi(app)
    operation = spec["paths"]["/tagged"]["get"]
    assert operation["tags"] == ["items", "public"]
    assert operation["summary"] == "List the things"


def test_empty_route_metadata_and_components_are_omitted() -> None:
    app = Wreath()

    @app.get("/plain")
    async def plain(request: Any) -> Any:
        raise NotImplementedError

    spec = generate_openapi(app)
    operation = spec["paths"]["/plain"]["get"]

    assert set(operation) == {"operationId", "responses"}
    assert operation["responses"] == {
        "200": {"description": "Successful response"}
    }
    assert "components" not in spec


def test_response_and_parameterized_return_annotations_have_distinct_schemas() -> None:
    app = Wreath()

    @app.get("/response")
    async def response(request: Any) -> Response:
        raise NotImplementedError

    @app.get("/names")
    async def names(request: Any) -> list[str]:
        raise NotImplementedError

    spec = generate_openapi(app)
    assert spec["paths"]["/response"]["get"]["responses"]["200"] == {
        "description": "Successful response"
    }
    assert spec["paths"]["/names"]["get"]["responses"]["200"]["content"] == {
        "application/json": {
            "schema": {"type": "array", "items": {"type": "string"}}
        }
    }


def test_query_defaults_and_one_sided_bounds_are_exact() -> None:
    app = Wreath()

    @app.get("/filters")
    async def filters(
        request: Any,
        required: int,
        optional: int | None = None,
        minimum_only: Annotated[int, Query(minimum=1)] = 1,
        maximum_only: Annotated[int, Query(maximum=10)] = 10,
    ) -> Any:
        raise NotImplementedError

    operation = generate_openapi(app)["paths"]["/filters"]["get"]
    parameters = {
        parameter["name"]: parameter
        for parameter in operation["parameters"]
    }
    assert parameters["required"]["required"] is True
    assert "default" not in parameters["required"]["schema"]
    assert parameters["optional"]["required"] is False
    assert "default" not in parameters["optional"]["schema"]
    assert parameters["minimum_only"]["schema"]["minimum"] == 1
    assert "maximum" not in parameters["minimum_only"]["schema"]
    assert parameters["maximum_only"]["schema"]["maximum"] == 10
    assert "minimum" not in parameters["maximum_only"]["schema"]
    assert "requestBody" not in operation


def test_multipart_form_and_file_schema_tracks_required_fields() -> None:
    app = Wreath()

    @app.post("/upload")
    async def upload(
        request: Any,
        title: Annotated[str, Form()],
        attachment: Annotated[UploadedFile, File()],
        note: Annotated[str, Form()] = "",
    ) -> Any:
        raise NotImplementedError

    request_body = generate_openapi(app)["paths"]["/upload"]["post"]["requestBody"]
    schema = request_body["content"]["multipart/form-data"]["schema"]
    assert request_body["required"] is True
    assert schema == {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "note": {"type": "string"},
            "attachment": {"type": "string", "format": "binary"},
        },
        "required": ["title", "attachment"],
    }


def test_form_only_and_file_only_routes_each_produce_multipart_schemas() -> None:
    app = Wreath()

    @app.post("/form")
    async def form(
        request: Any,
        note: Annotated[str, Form()] = "",
    ) -> Any:
        raise NotImplementedError

    @app.post("/file")
    async def file(
        request: Any,
        attachment: Annotated[UploadedFile, File()],
    ) -> Any:
        raise NotImplementedError

    paths = generate_openapi(app)["paths"]
    form_body = paths["/form"]["post"]["requestBody"]
    assert form_body == {
        "required": False,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                }
            }
        },
    }
    file_body = paths["/file"]["post"]["requestBody"]
    assert file_body["required"] is True
    assert file_body["content"]["multipart/form-data"]["schema"] == {
        "type": "object",
        "properties": {
            "attachment": {"type": "string", "format": "binary"},
        },
        "required": ["attachment"],
    }


def test_untyped_path_parameters_are_still_documented() -> None:
    app = Wreath()

    @app.get("/raw/{slug}")
    async def raw(request):
        raise NotImplementedError

    operation = generate_openapi(app)["paths"]["/raw/{slug}"]["get"]
    assert operation["parameters"] == [
        {
            "name": "slug",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        }
    ]


def test_additional_response_without_a_model_has_no_empty_content_schema() -> None:
    app = Wreath()

    @app.get("/health", responses={204: ResponseSpec(description="No content")})
    async def health(request: Any) -> Any:
        raise NotImplementedError

    operation = generate_openapi(app)["paths"]["/health"]["get"]
    assert operation["responses"]["204"] == {"description": "No content"}


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
