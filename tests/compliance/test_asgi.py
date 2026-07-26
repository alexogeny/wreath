"""ASGI 3.0 conformance, codified — wreath as an ASGI application.

Asserts the message shapes and ordering the spec requires (HTTP response events,
lifespan protocol) and that the native server hands the app a conformant scope.
"""
from __future__ import annotations

import asyncio

from wreath import Response, Wreath

from .conftest import drive_request


def _http_scope(path: str = "/", method: str = "GET") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"x")],
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 5555),
        "root_path": "",
    }


def _run(app, scope: dict) -> list[dict]:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def _app_with_root() -> Wreath:
    app = Wreath()

    @app.get("/")
    async def root(request):  # noqa: ANN001, ANN202
        return Response(b"ok")

    return app


def test_response_start_precedes_body_with_valid_shapes() -> None:
    sent = _run(_app_with_root(), _http_scope())
    types = [m["type"] for m in sent]
    assert types[0] == "http.response.start"
    assert types.index("http.response.start") < types.index("http.response.body")

    start = sent[0]
    assert isinstance(start["status"], int)
    for name, value in start["headers"]:
        assert isinstance(name, bytes) and isinstance(value, bytes)


def test_lifespan_startup_and_shutdown_complete() -> None:
    app = Wreath()
    order: list[str] = []

    @app.on_startup
    async def _startup(app) -> None:  # noqa: ANN001 - hook is called handler(app)
        order.append("startup")

    @app.on_shutdown
    async def _shutdown(app) -> None:  # noqa: ANN001
        order.append("shutdown")

    sent: list[dict] = []
    incoming = ["lifespan.startup", "lifespan.shutdown"]

    async def receive() -> dict:
        return {"type": incoming.pop(0)}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send))

    types = [m["type"] for m in sent]
    assert "lifespan.startup.complete" in types
    assert "lifespan.shutdown.complete" in types
    assert order == ["startup", "shutdown"]


def test_handler_exception_becomes_500_not_a_crash() -> None:
    app = Wreath()

    @app.get("/boom")
    async def boom(request):  # noqa: ANN001, ANN202
        raise RuntimeError("kaboom")

    sent = _run(app, _http_scope("/boom"))
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 500


def test_native_server_builds_a_conformant_http_scope() -> None:
    captured: dict = {}

    async def capture_app(scope, receive, send) -> None:
        captured.update(scope)
        while True:
            message = await receive()
            if message["type"] == "http.disconnect" or not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    drive_request(b"GET /path?q=1 HTTP/1.1\r\nHost: x\r\n\r\n", app=capture_app)

    assert captured["type"] == "http"
    assert captured["asgi"]["version"] == "3.0"
    assert captured["http_version"] in ("1.0", "1.1")
    assert captured["method"] == "GET"
    assert captured["path"] == "/path"
    assert captured["query_string"] == b"q=1"
    assert isinstance(captured["headers"], list)
    assert captured["scheme"] in ("http", "https")
    # raw_path and client/server are required scope keys.
    assert "raw_path" in captured and "client" in captured and "server" in captured
