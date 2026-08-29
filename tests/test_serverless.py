from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from wreath import Wreath
from wreath.response import JSONResponse
from wreath.serverless import GoogleFunctionAdapter, azure_function_app


class GoogleRequest:
    method = "POST"
    path = "/hello"
    query_string = b"x=1"
    headers = {"content-type": "text/plain"}
    host = "functions.example"
    scheme = "https"
    remote_addr = "203.0.113.9"

    def get_data(self) -> bytes:
        return b"payload"


def test_google_function_runs_the_same_asgi_app_and_lifespan() -> None:
    app = Wreath()
    lifecycle: list[str] = []

    @app.on_startup
    async def startup(_app):
        lifecycle.append("start")

    @app.on_shutdown
    async def shutdown(_app):
        lifecycle.append("stop")

    @app.post("/hello")
    async def hello(request):
        return JSONResponse(
            {
                "query": request.query_string.decode(),
                "body": (await request.body()).decode(),
                "client": request.scope["client"],
                "google": "wreath.google" in request.scope["extensions"],
            }
        )

    adapter = GoogleFunctionAdapter(app)
    body, status, headers = adapter(GoogleRequest())
    adapter.close()
    assert status == 200
    assert b'"query":"x=1"' in body
    assert b'"body":"payload"' in body
    assert b'"client":["203.0.113.9",null]' in body
    assert b'"google":true' in body
    assert ("content-type", "application/json") in headers
    assert lifecycle == ["start", "stop"]


def test_google_function_refuses_headers_without_a_mapping_interface() -> None:
    app = Wreath()
    request = GoogleRequest()
    request.headers = object()
    adapter = GoogleFunctionAdapter(app)
    try:
        with pytest.raises(TypeError, match="mapping-like headers"):
            adapter(request)
    finally:
        adapter.close()


def test_azure_uses_the_platforms_native_asgi_adapter(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def adapter(**kwargs):
        calls.append(kwargs)
        return "azure-app"

    azure = SimpleNamespace(functions=SimpleNamespace(AsgiFunctionApp=adapter))
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.functions", azure.functions)
    app = object()
    assert azure_function_app(app, http_auth_level="ANONYMOUS") == "azure-app"
    assert calls == [
        {
            "app": app,
            "http_auth_level": "ANONYMOUS",
            "function_name": "http_app_func",
        }
    ]
