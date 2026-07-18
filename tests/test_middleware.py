from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.exceptions import Forbidden, Unauthorized
from wreath.middleware import MiddlewareHooks, MiddlewareTape
from wreath.response import TextResponse


async def invoke(app: Wreath, path: str = "/") -> list[dict[str, Any]]:
    messages = iter([{"type": "http.request", "body": b"", "more_body": False}])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
        },
        receive,
        send,
    )
    return sent


@pytest.mark.asyncio
async def test_app_and_request_state_are_explicit_and_isolated() -> None:
    app = Wreath()
    app.state.shared = "application"
    seen = []

    @app.get("/")
    async def endpoint(request):
        request.state.value = object()
        seen.append(request.state.value)
        return app.state.shared

    first = await invoke(app)
    second = await invoke(app)

    assert first[1]["body"] == b"application"
    assert second[1]["body"] == b"application"
    assert seen[0] is not seen[1]


@pytest.mark.asyncio
async def test_app_and_route_middleware_compile_in_deterministic_order() -> None:
    app = Wreath()
    events: list[str] = []

    async def outer(request, call_next):
        events.append("outer-in")
        response = await call_next(request)
        events.append("outer-out")
        return response

    async def route(request, call_next):
        events.append("route-in")
        response = await call_next(request)
        events.append("route-out")
        return response

    app.add_middleware(outer)

    @app.get("/", middleware=(route,))
    async def endpoint(request):
        events.append("endpoint")
        return "ok"

    sent = await invoke(app)

    assert sent[1]["body"] == b"ok"
    assert events == ["outer-in", "route-in", "endpoint", "route-out", "outer-out"]


@pytest.mark.asyncio
async def test_middleware_can_short_circuit() -> None:
    app = Wreath()
    called = False

    async def reject(request, call_next):
        return TextResponse("blocked", status=403)

    @app.get("/", middleware=(reject,))
    async def endpoint(request):
        nonlocal called
        called = True
        return "never"

    sent = await invoke(app)

    assert sent[0]["status"] == 403
    assert sent[1]["body"] == b"blocked"
    assert not called


@pytest.mark.asyncio
async def test_fused_middleware_runs_one_linear_tape() -> None:
    app = Wreath()
    events: list[str] = []

    async def outer_before(request):
        events.append("outer-in")

    async def outer_after(request, response):
        events.append("outer-out")
        return response

    async def inner_before(request):
        events.append("inner-in")

    async def inner_after(request, response):
        events.append("inner-out")
        return response

    app.add_middleware(MiddlewareHooks(before=outer_before, after=outer_after))

    @app.get(
        "/",
        middleware=(MiddlewareHooks(before=inner_before, after=inner_after),),
    )
    async def endpoint(request):
        events.append("endpoint")
        return "ok"

    sent = await invoke(app)
    matched = app.router.match("GET", "/")

    assert isinstance(matched[0], MiddlewareTape)
    assert matched[0].operations == ("before", "before", "endpoint", "after", "after")
    assert sent[1]["body"] == b"ok"
    assert events == ["outer-in", "inner-in", "endpoint", "inner-out", "outer-out"]


@pytest.mark.asyncio
async def test_fused_middleware_omits_static_misses_and_jumps_on_short_circuit() -> None:
    app = Wreath()
    events: list[str] = []

    async def outer_after(request, response):
        events.append("outer-out")
        return response

    async def irrelevant(request):
        events.append("irrelevant")

    async def auth_check(request):
        events.append("auth")
        return TextResponse("blocked", status=403)

    app.add_middleware(MiddlewareHooks(after=outer_after))
    app.add_middleware(
        MiddlewareHooks(
            before=irrelevant,
            applies_to=lambda route: route.path == "/cors",
        )
    )
    app.add_middleware(MiddlewareHooks(before=auth_check))

    @app.get("/")
    async def endpoint(request):
        events.append("endpoint")
        return "never"

    sent = await invoke(app)
    matched = app.router.match("GET", "/")

    assert isinstance(matched[0], MiddlewareTape)
    assert matched[0].operations == ("before", "endpoint", "after")
    assert sent[0]["status"] == 403
    assert sent[1]["body"] == b"blocked"
    assert events == ["auth", "outer-out"]


@pytest.mark.asyncio
async def test_structured_http_exceptions_and_custom_handlers() -> None:
    app = Wreath()

    @app.exception_handler(Forbidden)
    async def forbidden_handler(request, error):
        return TextResponse(f"denied: {error.detail}", status=error.status)

    @app.get("/forbidden")
    async def forbidden(request):
        raise Forbidden("policy")

    @app.get("/unauthorized")
    async def unauthorized(request):
        raise Unauthorized(challenge="Bearer")

    denied = await invoke(app, "/forbidden")
    challenged = await invoke(app, "/unauthorized")

    assert denied[0]["status"] == 403
    assert denied[1]["body"] == b"denied: policy"
    assert challenged[0]["status"] == 401
    assert (b"www-authenticate", b"Bearer") in challenged[0]["headers"]
