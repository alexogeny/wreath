from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity, authenticated
from wreath.response import (
    JSONResponse,
    PreparedResponse,
    Response,
    TextResponse,
    coerce_bytes,
    coerce_json,
    coerce_text,
)


def _shape(response: Response) -> tuple[int, list, bytes]:
    return (response.status, list(response.headers), response.body)


@pytest.mark.parametrize("body", ["", "ok", "a" * 2000, "unicode: café ☕", "x" * 1023])
def test_coerce_text_matches_text_response(body: str) -> None:
    assert _shape(coerce_text(body)) == _shape(TextResponse(body))


@pytest.mark.parametrize("data", [{}, {"a": 1}, {"nested": {"list": [1, 2, 3]}}, {"k": "v" * 500}])
def test_coerce_json_matches_json_response(data: dict) -> None:
    assert _shape(coerce_json(data)) == _shape(JSONResponse(data))


@pytest.mark.parametrize("body", [b"", b"bytes", b"\x00\x01\x02", b"z" * 5000])
def test_coerce_bytes_matches_response(body: bytes) -> None:
    assert _shape(coerce_bytes(body)) == _shape(Response(body))


def test_fast_paths_produce_a_plain_response_that_emits_natively() -> None:
    # The native metal write path keys on `type(response).__call__ is
    # Response.__call__`; the fast path must satisfy that (it returns a bare
    # Response, and the subclasses do not override __call__).
    for response in (coerce_text("x"), coerce_json({"a": 1}), coerce_bytes(b"x")):
        assert type(response).__call__ is Response.__call__


def test_response_slots_are_fully_populated_by_the_fast_path() -> None:
    # The fast path sets slots by hand; if Response gains a slot this trips so
    # the shortcut is updated rather than silently leaving it unset.
    assert Response.__slots__ == ("_headers", "background", "body", "status")
    response = coerce_text("ok")
    for slot in Response.__slots__:
        assert hasattr(response, slot)


def _assert_exact_response_shortcuts() -> None:
    source = (Path(__file__).parents[1] / "src" / "wreath" / "app.py").read_text()
    tree = ast.parse(source)
    expected = {
        "_activate_http_plain_auth_sync": 1,
        "_handle_http_plain": 1,
        "_handle_http_plain_sync": 1,
        "_finish_http_plain_await_value": 1,
        "_handle_http_plain_auth": 1,
        "_handle_http_compartment": 1,
        "_handle_http": 2,
    }

    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name, count in expected.items():
        shortcuts = 0
        for node in ast.walk(functions[name]):
            if not isinstance(node, ast.IfExp) or not isinstance(node.test, ast.BoolOp):
                continue
            names = {
                operand.comparators[0].id
                for operand in node.test.values
                if isinstance(operand, ast.Compare)
                and len(operand.comparators) == 1
                and isinstance(operand.comparators[0], ast.Name)
            }
            if names == {"Response", "PreparedResponse"}:
                shortcuts += 1
        assert shortcuts == count, f"{name} lost an exact-response activation shortcut"

    finish_tests = {
        ast.unparse(node.test)
        for node in ast.walk(functions["_finish_http"])
        if isinstance(node, ast.If)
    }
    plain_finish_tests = {
        ast.unparse(node.test)
        for node in ast.walk(functions["_finish_http_plain"])
        if isinstance(node, ast.If)
    }
    assert "response.__class__ is PreparedResponse and native_response" in finish_tests
    assert "response.__class__ is PreparedResponse and native_response" in plain_finish_tests
    select_source = ast.unparse(functions["_select_dispatch"])
    assert select_source.count("self._classify is not None") == 3
    plain_source = ast.unparse(functions["_handle_http_plain"])
    assert plain_source.count("if matched is None:") == 1


def test_every_compiled_dispatcher_activates_exact_responses_without_coercion() -> None:
    _assert_exact_response_shortcuts()


async def _invoke_asgi(
    app: Wreath,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "headers": headers or [],
            "query_string": b"",
        },
        receive,
        send,
    )
    return sent


class _GlobalHook:
    global_scope = True

    def before_sync(self, request: Any) -> None:
        return None

    def after_inplace(self, request: Any, response: Any) -> None:
        return None


class _ScopedHook(_GlobalHook):
    def applies_to(self, method: str, path: str) -> bool:
        return path == "/cold"


@pytest.mark.parametrize("response_type", [Response, PreparedResponse])
@pytest.mark.asyncio
async def test_exact_response_bypasses_coercion_in_every_dispatch_shape(
    monkeypatch: pytest.MonkeyPatch,
    response_type: type[Response] | type[PreparedResponse],
) -> None:
    import wreath.app as app_module

    response = Response(b"ready") if response_type is Response else PreparedResponse.text("ready")
    coerce_response = app_module._coerce_response

    def reject_exact_response(value: Any, *, status: int = 200) -> Any:
        if value.__class__ is Response or value.__class__ is PreparedResponse:
            raise AssertionError("an exact response entered the generic coercion ladder")
        return coerce_response(value, status=status)

    monkeypatch.setattr(app_module, "_coerce_response", reject_exact_response)

    plain = Wreath()

    @plain.get("/health")
    async def plain_handler(request: Any) -> PreparedResponse:
        return response

    assert (await _invoke_asgi(plain))[1]["body"] == b"ready"
    assert plain._dispatch_http.__name__ == "_handle_http_plain"

    sync = Wreath()

    @sync.get("/health")
    def sync_handler(request: Any) -> PreparedResponse:
        return response

    assert (await _invoke_asgi(sync))[1]["body"] == b"ready"
    assert sync._dispatch_http.__name__ == "_handle_http_plain_sync"

    awaiting = Wreath()

    @awaiting.get("/health")
    def awaiting_handler(request: Any) -> Any:
        async def resolved() -> PreparedResponse:
            return response

        return resolved()

    assert (await _invoke_asgi(awaiting))[1]["body"] == b"ready"
    assert awaiting._dispatch_http.__name__ == "_handle_http_plain_sync"

    auth = Wreath()
    auth.configure_auth(BearerTokenBackend(lambda token: Identity("u")))

    @auth.get("/health")
    @authenticated()
    async def auth_handler(request: Any) -> PreparedResponse:
        return response

    assert (await _invoke_asgi(auth, headers=[(b"authorization", b"Bearer token")]))[1][
        "body"
    ] == b"ready"
    assert auth._dispatch_http.__name__ == "_handle_http_plain_auth"

    compartment = Wreath()
    compartment.add_middleware(_ScopedHook())

    @compartment.get("/health")
    async def compartment_handler(request: Any) -> PreparedResponse:
        return response

    assert (await _invoke_asgi(compartment))[1]["body"] == b"ready"
    assert compartment._dispatch_http.__name__ == "_handle_http_compartment"

    general = Wreath()
    general.add_middleware(_GlobalHook())

    @general.get("/health")
    async def general_handler(request: Any) -> PreparedResponse:
        return response

    assert (await _invoke_asgi(general))[1]["body"] == b"ready"
    assert general._dispatch_http.__name__ == "_handle_http"

    calls: list[tuple[Any, Any, Any]] = []

    class Protocol:
        async def send(self, message: Any) -> None:
            raise AssertionError(f"prepared response used generic ASGI send: {message!r}")

        async def _wreath_response(self, status: Any, headers: Any, body: Any) -> None:
            calls.append((status, headers, body))

        def _wreath_response_nowait(self, status: Any, headers: Any, body: Any) -> None:
            calls.append((status, headers, body))

    sync_auth = Wreath()
    sync_auth.configure_auth(BearerTokenBackend(lambda token: Identity("u")))

    @sync_auth.get("/health")
    @authenticated()
    def sync_auth_handler(request: Any) -> PreparedResponse:
        return response

    context = SimpleNamespace(
        _bearer_verify=lambda verifier: verifier("token"),
        flight=0,
        headers=[],
        method="GET",
        path="/health",
        policy_native=False,
    )
    protocol = Protocol()
    await sync_auth._wreath_http(context, None, protocol.send)
    assert sync_auth._dispatch_http.__name__ == "_handle_http_plain_auth_sync"

    flight_context = SimpleNamespace(
        _flight_phase=lambda *args: None,
        _flight_stamp=lambda *args: None,
        flight=2,
        headers=[],
        method="GET",
        path="/health",
        policy_native=False,
    )
    await general._wreath_http(flight_context, None, protocol.send)

    assert calls == [
        (200, response.headers, b"ready"),
        (200, response.headers, b"ready"),
    ]
    _assert_exact_response_shortcuts()


@pytest.mark.asyncio
async def test_prepared_response_uses_native_one_shot_dispatch() -> None:
    app = Wreath()
    response = PreparedResponse.text("ready")

    @app.get("/health")
    async def health(request: Any) -> PreparedResponse:
        return response

    calls: list[tuple[Any, Any, Any]] = []

    class Protocol:
        async def send(self, message: Any) -> None:
            raise AssertionError(f"prepared response used generic ASGI send: {message!r}")

        async def _wreath_response(self, status: Any, headers: Any, body: Any) -> None:
            calls.append((status, headers, body))

    context = SimpleNamespace(
        flight=0,
        headers=[],
        method="GET",
        path="/health",
        policy_native=False,
    )
    protocol = Protocol()

    await app._wreath_http(context, None, protocol.send)

    assert calls == [(200, response.headers, b"ready")]
    _assert_exact_response_shortcuts()


@pytest.mark.asyncio
async def test_end_to_end_handler_returns_are_coerced_correctly() -> None:
    from wreath import Wreath

    app = Wreath()

    @app.get("/text")
    async def text_endpoint(request):
        return "hello"

    @app.get("/json")
    async def json_endpoint(request):
        return {"ok": True}

    @app.get("/bytes")
    async def bytes_endpoint(request):
        return b"raw"

    async def call(path: str) -> list[dict]:
        messages = iter([{"type": "http.request", "body": b"", "more_body": False}])
        sent: list[dict] = []

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        await app(
            {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""},
            receive,
            send,
        )
        return sent

    text = await call("/text")
    assert text[0]["status"] == 200
    assert (b"content-type", b"text/plain; charset=utf-8") in text[0]["headers"]
    assert (b"content-length", b"5") in text[0]["headers"]
    assert text[1]["body"] == b"hello"

    js = await call("/json")
    assert (b"content-type", b"application/json") in js[0]["headers"]
    assert js[1]["body"] == b'{"ok":true}'

    raw = await call("/bytes")
    assert (b"content-type", b"application/octet-stream") in raw[0]["headers"]
    assert raw[1]["body"] == b"raw"


@pytest.mark.asyncio
async def test_typed_json_response_keeps_conversion_fallback_and_declared_status() -> None:
    from wreath import Wreath

    identifier = uuid.UUID("12345678-1234-5678-1234-567812345678")
    app = Wreath()

    @app.get("/typed", status_code=201)
    async def typed(request: Any) -> dict[str, Any]:
        return {"id": identifier, "ok": True}

    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {"type": "http", "method": "GET", "path": "/typed", "headers": []},
        receive,
        send,
    )

    assert sent[0]["status"] == 201
    assert (b"content-type", b"application/json") in sent[0]["headers"]
    assert sent[1]["body"] == (b'{"id":"12345678-1234-5678-1234-567812345678","ok":true}')
