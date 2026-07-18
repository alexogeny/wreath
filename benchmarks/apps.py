"""Equivalent benchmark applications selected through WREATH_BENCH_FRAMEWORK."""

from __future__ import annotations

import json as stdlib_json
import os
from typing import Any, cast

from .scenarios import (
    LARGE_RESPONSE_BODY,
    STREAM_CHUNKS,
    TEMPLATE_ROW_COUNT,
    WEBHOOK_KEY_ID,
    WEBHOOK_SECRET,
)

# One HTML row per record, with cells that need escaping so the template
# scenario exercises real escaping rather than a passthrough.
TEMPLATE_ROWS = [
    {"id": index, "message": f"item <{index}> & 'quote'"}
    for index in range(TEMPLATE_ROW_COUNT)
]
# Competitors render the same markup through Jinja2 (autoescaping on).
JINJA_TABLE_SOURCE = (
    "<table>{% for r in rows %}"
    "<tr><td>{{ r.id }}</td><td>{{ r.message }}</td></tr>"
    "{% endfor %}</table>"
)
CACHE_CONTROL_VALUE = "public, max-age=60"

FRAMEWORK = os.environ.get("WREATH_BENCH_FRAMEWORK", "wreath")

# This 10,000-route table is what the routing-* scenarios measure against, and
# it is why Litestar is not in the suite (see FRAMEWORKS in scenarios.py).
# Litestar's route registration is O(n^2) -- construct_routing_trie/validate_node
# re-walks the entire trie on every add (~n^2/2 validate_node calls; measured
# 8.0M for 4,000 routes) -- so building this app takes ~30s and flakily loses the
# race with the 30s readiness probe. It is a Litestar-internal cost we can't fix
# from here, and bumping the timeout would just mask a boot cost that is part of
# what the routing benchmark compares. Every other framework builds this table
# in well under a second. Cap this far lower only if you also re-add a framework
# that cannot tolerate it.
ROUTE_COUNT = 10_000
ROUTE_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _route_spec(index: int) -> tuple[str, str]:
    method = ROUTE_METHODS[index % len(ROUTE_METHODS)]
    match index % 5:
        case 0:
            path = f"/status/leaf-{index}"
        case 1:
            path = f"/api/v1/items/category-{index % 97}/leaf-{index}"
        case 2:
            path = f"/api/v2/groups/group-{index}/members/"
        case 3:
            path = f"/tenants/{{tenant_id}}/collections/collection-{index}/items/{{item_id}}"
        case _:
            path = f"/api/internal/v3/regions/region-{index}/zones/primary/nodes/current/status"
    return method, path


def _route_path(path: str, style: str) -> str:
    if style == "sanic":
        return path.replace("{tenant_id}", "<tenant_id:str>").replace(
            "{item_id}", "<item_id:str>"
        )
    if style == "django":
        return path.replace("{tenant_id}", "<str:tenant_id>").replace(
            "{item_id}", "<str:item_id>"
        )
    if style == "flask":
        return path.replace("{tenant_id}", "<tenant_id>").replace("{item_id}", "<item_id>")
    return path


ROUTE_SPECS = tuple(_route_spec(index) for index in range(ROUTE_COUNT))

if FRAMEWORK in {"wreath", "wreath-native"}:
    from dataclasses import dataclass

    from wreath import Response, Wreath
    from wreath.auth import BearerTokenBackend, Identity, authenticated
    from wreath.authorization import roles
    from wreath.response import StreamingResponse, TextResponse

    # Default routing mode unless asked otherwise, so this app tracks whatever
    # Wreath's default is. Set WREATH_BENCH_ROUTING to A/B a backend end-to-end.
    _ROUTING = os.environ.get("WREATH_BENCH_ROUTING")
    app = Wreath(**({"routing": _ROUTING} if _ROUTING else {}))

    async def verify_benchmark_token(token: str) -> Identity | None:
        if token == "admin":
            return Identity("admin", roles=frozenset({"admin"}))
        if token == "user":
            return Identity("user", roles=frozenset({"user"}))
        return None

    app.configure_auth(BearerTokenBackend(verify_benchmark_token))

    @app.get("/")
    async def plaintext(request):
        return TextResponse("hello, world")

    @app.get("/json")
    async def json_response(request):
        return {"message": "hello"}

    @app.get("/users/{user_id}")
    async def parameter(request):
        return {"user_id": request.path_params["user_id"]}

    async def noop_middleware(request, call_next):
        return await call_next(request)

    @app.get("/middleware/noop", middleware=(noop_middleware,))
    async def middleware_noop(request):
        return TextResponse("hello, world")

    @app.get("/auth/profile")
    @authenticated()
    async def auth_profile(request):
        return TextResponse(request.identity.id)

    @app.get("/auth/admin")
    @roles("admin")
    async def auth_admin(request):
        return TextResponse("admin")

    @app.get("/headers")
    async def header_lookup(request):
        return TextResponse(request.header("x-benchmark", ""))

    @app.post("/body")
    async def request_body(request):
        return TextResponse(str(len(await request.body())))

    @app.post("/json-body")
    async def request_json(request):
        return await request.json()

    @app.get("/response-64k")
    async def large_response(request):
        return Response(LARGE_RESPONSE_BODY)

    @app.get("/stream-4x256")
    async def streaming_response(request):
        async def chunks():
            for chunk in STREAM_CHUNKS:
                yield chunk

        return StreamingResponse(chunks())

    async def routing_leaf(request):
        return TextResponse("route-hit")

    for method, path in ROUTE_SPECS:
        app.route(path, methods=[method])(routing_leaf)

    @app.websocket("/ws-echo")
    async def ws_echo(ws):
        await ws.accept()
        async for message in ws:
            await ws.send(message)

    @dataclass
    class BenchItem:
        name: str
        price: float
        tags: list[str]
        active: bool = True

    @app.post("/typed-items/{item_id}")
    async def typed_items(request, item_id: int, item: BenchItem, verbose: bool = False):
        return {
            "id": item_id,
            "name": item.name,
            "price": item.price,
            "tag_count": len(item.tags),
            "active": item.active,
            "verbose": verbose,
        }

    from wreath.cache_control import CacheControl
    from wreath.response import HTMLResponse
    from wreath.templates import Template
    from wreath.webhooks import HMACWebhookVerifier

    _bench_template = Template.from_string(JINJA_TABLE_SOURCE)

    @app.get("/template")
    async def template_render(request):
        return HTMLResponse(_bench_template.render(rows=TEMPLATE_ROWS))

    _cache_policy = CacheControl(public=True, max_age=60)

    @app.get("/cached")
    async def cached(request):
        response = TextResponse("cacheable")
        response.set_cache_control(_cache_policy)
        return response

    # A large age window keeps the fixed benchmark timestamp valid for the run.
    _webhook_verifier = HMACWebhookVerifier(
        {WEBHOOK_KEY_ID: WEBHOOK_SECRET}, max_age=10**9
    )

    @app.post("/webhook")
    async def webhook(request):
        body = await request.body()
        try:
            _webhook_verifier.verify(body=body, headers=dict(request.scope["headers"]))
        except Exception:
            return TextResponse("invalid", status=401)
        return TextResponse("ok")

    # Process-local counters owned by the benchmark app. The measured task
    # increments started, performs identical work, then increments completed;
    # in-flight is started-minus-completed and its peak is retained. The stats
    # endpoint is queried out of band and never joins the timed samples.
    import asyncio as _bg_asyncio

    from wreath.background import BackgroundTask

    _bg = {"started": 0, "completed": 0, "failed": 0, "inflight": 0, "max_inflight": 0}

    def _bg_enter() -> None:
        _bg["started"] += 1
        _bg["inflight"] += 1
        if _bg["inflight"] > _bg["max_inflight"]:
            _bg["max_inflight"] = _bg["inflight"]

    def _bg_exit() -> None:
        _bg["inflight"] -= 1
        _bg["completed"] += 1

    async def _bg_noop() -> None:
        _bg_enter()
        _bg_exit()

    async def _bg_yield() -> None:
        _bg_enter()
        await _bg_asyncio.sleep(0)
        _bg_exit()

    @app.get("/background-noop")
    async def background_noop(request):
        return TextResponse("ok", background=BackgroundTask(_bg_noop))

    @app.get("/background-yield")
    async def background_yield(request):
        return TextResponse("ok", background=BackgroundTask(_bg_yield))

    @app.get("/background-stats")
    async def background_stats(request):
        return dict(_bg)

elif FRAMEWORK == "starlette":
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
    from starlette.routing import Route, WebSocketRoute

    async def plaintext(request):
        return PlainTextResponse("hello, world")

    async def json_response(request):
        return JSONResponse({"message": "hello"})

    async def parameter(request):
        return JSONResponse({"user_id": request.path_params["user_id"]})

    async def header_lookup(request):
        return PlainTextResponse(request.headers.get("x-benchmark", ""))

    async def request_body(request):
        return PlainTextResponse(str(len(await request.body())))

    async def request_json(request):
        return JSONResponse(await request.json())

    async def large_response(request):
        return Response(LARGE_RESPONSE_BODY)

    async def streaming_response(request):
        async def chunks():
            for chunk in STREAM_CHUNKS:
                yield chunk

        return StreamingResponse(chunks())

    async def routing_leaf(request):
        return PlainTextResponse("route-hit")

    async def ws_echo(websocket):
        await websocket.accept()
        try:
            while True:
                message = await websocket.receive_text()
                await websocket.send_text(message)
        except Exception:
            pass

    import asyncio as _bg_asyncio

    from starlette.background import BackgroundTask as StarletteBackgroundTask

    _bg = {"started": 0, "completed": 0, "failed": 0, "inflight": 0, "max_inflight": 0}

    def _bg_enter() -> None:
        _bg["started"] += 1
        _bg["inflight"] += 1
        if _bg["inflight"] > _bg["max_inflight"]:
            _bg["max_inflight"] = _bg["inflight"]

    def _bg_exit() -> None:
        _bg["inflight"] -= 1
        _bg["completed"] += 1

    async def _bg_noop() -> None:
        _bg_enter()
        _bg_exit()

    async def _bg_yield() -> None:
        _bg_enter()
        await _bg_asyncio.sleep(0)
        _bg_exit()

    async def background_noop(request):
        return PlainTextResponse("ok", background=StarletteBackgroundTask(_bg_noop))

    async def background_yield(request):
        return PlainTextResponse("ok", background=StarletteBackgroundTask(_bg_yield))

    async def background_stats(request):
        return JSONResponse(dict(_bg))

    from jinja2 import Template as JinjaTemplate
    from starlette.responses import HTMLResponse as StarletteHTMLResponse

    _jinja_table = JinjaTemplate(JINJA_TABLE_SOURCE, autoescape=True)

    async def template_render(request):
        return StarletteHTMLResponse(_jinja_table.render(rows=TEMPLATE_ROWS))

    async def cached(request):
        return PlainTextResponse(
            "cacheable", headers={"cache-control": CACHE_CONTROL_VALUE}
        )

    app = Starlette(
        routes=[
            WebSocketRoute("/ws-echo", ws_echo),
            Route("/", plaintext),
            Route("/json", json_response),
            Route("/users/{user_id}", parameter),
            Route("/headers", header_lookup),
            Route("/body", request_body, methods=["POST"]),
            Route("/json-body", request_json, methods=["POST"]),
            Route("/response-64k", large_response),
            Route("/stream-4x256", streaming_response),
            Route("/background-noop", background_noop),
            Route("/background-yield", background_yield),
            Route("/background-stats", background_stats),
            Route("/template", template_render),
            Route("/cached", cached),
            *(Route(path, routing_leaf, methods=[method]) for method, path in ROUTE_SPECS),
        ]
    )

elif FRAMEWORK == "fastapi":
    from fastapi import FastAPI, Request
    from fastapi.responses import PlainTextResponse, Response, StreamingResponse

    app = FastAPI()

    @app.get("/", response_class=PlainTextResponse)
    async def plaintext():
        return "hello, world"

    @app.get("/json")
    async def json_response():
        return {"message": "hello"}

    @app.get("/users/{user_id}")
    async def parameter(user_id: str):
        return {"user_id": user_id}

    @app.get("/headers", response_class=PlainTextResponse)
    async def header_lookup(request: Request):
        return request.headers.get("x-benchmark", "")

    @app.post("/body", response_class=PlainTextResponse)
    async def request_body(request: Request):
        return str(len(await request.body()))

    @app.post("/json-body")
    async def request_json(request: Request):
        return await request.json()

    @app.get("/response-64k")
    async def large_response():
        return Response(LARGE_RESPONSE_BODY)

    @app.get("/stream-4x256")
    async def streaming_response():
        async def chunks():
            for chunk in STREAM_CHUNKS:
                yield chunk

        return StreamingResponse(chunks())

    from pydantic import BaseModel

    class BenchItemModel(BaseModel):
        name: str
        price: float
        tags: list[str]
        active: bool = True

    @app.post("/typed-items/{item_id}")
    async def typed_items(item_id: int, item: BenchItemModel, verbose: bool = False):
        return {
            "id": item_id,
            "name": item.name,
            "price": item.price,
            "tag_count": len(item.tags),
            "active": item.active,
            "verbose": verbose,
        }

    from fastapi.responses import HTMLResponse as FastAPIHTMLResponse
    from jinja2 import Template as JinjaTemplate

    _jinja_table = JinjaTemplate(JINJA_TABLE_SOURCE, autoescape=True)

    @app.get("/template", response_class=FastAPIHTMLResponse)
    async def template_render():
        return _jinja_table.render(rows=TEMPLATE_ROWS)

    @app.get("/cached", response_class=PlainTextResponse)
    async def cached():
        return PlainTextResponse(
            "cacheable", headers={"cache-control": CACHE_CONTROL_VALUE}
        )

    async def routing_leaf():
        return PlainTextResponse("route-hit")

    async def routing_param_leaf(tenant_id: str, item_id: str):
        return PlainTextResponse("route-hit")

    for method, path in ROUTE_SPECS:
        handler = routing_param_leaf if "{tenant_id}" in path else routing_leaf
        app.add_api_route(path, handler, methods=[method])

    from fastapi import WebSocket as FastAPIWebSocket
    from fastapi import WebSocketDisconnect as FastAPIWebSocketDisconnect

    @app.websocket("/ws-echo")
    async def ws_echo(websocket: FastAPIWebSocket):
        await websocket.accept()
        try:
            while True:
                message = await websocket.receive_text()
                await websocket.send_text(message)
        except FastAPIWebSocketDisconnect:
            pass

elif FRAMEWORK == "sanic":
    from sanic import Sanic
    from sanic.response import json, raw, text

    app = Sanic("wreath_benchmark")

    @app.get("/")
    async def plaintext(request):
        return text("hello, world")

    @app.get("/json")
    async def json_response(request):
        return json({"message": "hello"})

    @app.get("/users/<user_id:str>")
    async def parameter(request, user_id: str):
        return json({"user_id": user_id})

    @app.get("/headers")
    async def header_lookup(request):
        return text(request.headers.get("x-benchmark", ""))

    @app.post("/body")
    async def request_body(request):
        return text(str(len(request.body)))

    @app.post("/json-body")
    async def request_json(request):
        return json(request.json)

    @app.get("/response-64k")
    async def large_response(request):
        return raw(LARGE_RESPONSE_BODY)

    from jinja2 import Template as JinjaTemplate
    from sanic.response import html

    _jinja_table = JinjaTemplate(JINJA_TABLE_SOURCE, autoescape=True)

    @app.get("/template")
    async def template_render(request):
        return html(_jinja_table.render(rows=TEMPLATE_ROWS))

    @app.get("/cached")
    async def cached(request):
        return text("cacheable", headers={"Cache-Control": CACHE_CONTROL_VALUE})

    async def routing_leaf(request, **params):
        return text("route-hit")

    for index, (method, path) in enumerate(ROUTE_SPECS):
        app.add_route(
            routing_leaf,
            _route_path(path, "sanic"),
            methods=[method],
            name=f"tree_{index}",
        )

    @app.websocket("/ws-echo")
    async def ws_echo(request, ws):
        while True:
            message = await ws.recv()
            if message is None:
                return
            await ws.send(message)

elif FRAMEWORK == "blacksheep":
    from blacksheep import Application
    from blacksheep.server.responses import json, text

    app = Application()

    @app.router.get("/")
    async def plaintext():
        return text("hello, world")

    @app.router.get("/json")
    async def json_response():
        return json({"message": "hello"})

    @app.router.get("/users/{user_id}")
    async def parameter(user_id: str):
        return json({"user_id": user_id})

    from blacksheep.server.responses import html
    from jinja2 import Template as JinjaTemplate

    _jinja_table = JinjaTemplate(JINJA_TABLE_SOURCE, autoescape=True)

    @app.router.get("/template")
    async def template_render():
        return html(_jinja_table.render(rows=TEMPLATE_ROWS))

    @app.router.get("/cached")
    async def cached():
        response = text("cacheable")
        response.add_header(b"cache-control", CACHE_CONTROL_VALUE.encode("ascii"))
        return response

    async def routing_leaf():
        return text("route-hit")

    async def routing_param_leaf(tenant_id: str, item_id: str):
        return text("route-hit")

    for method, path in ROUTE_SPECS:
        handler = routing_param_leaf if "{tenant_id}" in path else routing_leaf
        getattr(app.router, method.lower())(path)(handler)

    from blacksheep import WebSocket as BlackSheepWebSocket
    from blacksheep import WebSocketDisconnectError

    @app.router.ws("/ws-echo")
    async def ws_echo(websocket: BlackSheepWebSocket):
        await websocket.accept()
        try:
            while True:
                message = await websocket.receive_text()
                await websocket.send_text(message)
        except WebSocketDisconnectError:
            pass

elif FRAMEWORK == "django":
    from django.conf import settings
    from django.core.asgi import get_asgi_application
    from django.http import HttpResponse, JsonResponse
    from django.urls import path

    settings.configure(
        DEBUG=False,
        SECRET_KEY="benchmark-only",
        ROOT_URLCONF=__name__,
        ALLOWED_HOSTS=["*"],
        MIDDLEWARE=[],
    )

    async def plaintext(request):
        return HttpResponse("hello, world", content_type="text/plain")

    async def json_response(request):
        return JsonResponse({"message": "hello"})

    async def parameter(request, user_id: str):
        return JsonResponse({"user_id": user_id})

    async def header_lookup(request):
        return HttpResponse(request.headers.get("x-benchmark", ""), content_type="text/plain")

    async def request_body(request):
        return HttpResponse(str(len(request.body)), content_type="text/plain")

    async def request_json(request):
        return JsonResponse(stdlib_json.loads(request.body))

    async def large_response(request):
        return HttpResponse(LARGE_RESPONSE_BODY, content_type="application/octet-stream")

    async def routing_leaf(request, **params):
        return HttpResponse("route-hit", content_type="text/plain")

    urlpatterns = [
        path("", plaintext),
        path("json", json_response),
        path("users/<str:user_id>", parameter),
        path("headers", header_lookup),
        path("body", request_body),
        path("json-body", request_json),
        path("response-64k", large_response),
        *(
            path(_route_path(route_path, "django").lstrip("/"), routing_leaf)
            for _, route_path in ROUTE_SPECS
        ),
    ]
    app = get_asgi_application()

elif FRAMEWORK == "flask":
    from flask import Flask, Response, jsonify, request
    from uvicorn.middleware.wsgi import WSGIMiddleware

    flask_app = Flask("wreath_benchmark")

    @flask_app.get("/")
    def plaintext():
        return "hello, world", 200, {"content-type": "text/plain"}

    @flask_app.get("/json")
    def json_response():
        return jsonify(message="hello")

    @flask_app.get("/users/<user_id>")
    def parameter(user_id: str):
        return jsonify(user_id=user_id)

    @flask_app.get("/headers")
    def header_lookup():
        return request.headers.get("x-benchmark", ""), 200, {"content-type": "text/plain"}

    @flask_app.post("/body")
    def request_body():
        return str(len(request.get_data())), 200, {"content-type": "text/plain"}

    @flask_app.post("/json-body")
    def request_json():
        return jsonify(request.get_json())

    @flask_app.get("/response-64k")
    def large_response():
        return Response(LARGE_RESPONSE_BODY, content_type="application/octet-stream")

    def routing_leaf(**params):
        return "route-hit", 200, {"content-type": "text/plain"}

    for index, (method, path) in enumerate(ROUTE_SPECS):
        flask_app.add_url_rule(
            _route_path(path, "flask"),
            endpoint=f"tree_{index}",
            view_func=routing_leaf,
            methods=[method],
        )

    app = WSGIMiddleware(cast(Any, flask_app))

else:
    raise RuntimeError(f"unknown benchmark framework: {FRAMEWORK}")
