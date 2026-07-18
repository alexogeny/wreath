"""OpenAPI generation tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from wreath import Wreath
from wreath.openapi import generate_openapi


@dataclass
class Widget:
    name: str
    weight: float
    labels: list[str] = field(default_factory=list)


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
    app.enable_docs(title="Test API")

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
    assert b"swagger-ui" in body


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
