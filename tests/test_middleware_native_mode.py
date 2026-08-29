from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

from wreath import Response, Wreath
from wreath.policy import (
    AIScrapingPolicy,
    CorsPolicy,
    HttpPolicy,
    RequestIdPolicy,
    SecurityHeadersPolicy,
    ServerTimingPolicy,
    TieredRateLimitPolicy,
    TrustedHostPolicy,
)
from wreath.response import HTMLResponse
from wreath.server import ServerConfig
from wreath.signatures import robots_txt

_server = importlib.import_module("wreath._native._server")
app_module = importlib.import_module("wreath.app")


class Recorder(asyncio.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[bytes] = []

    def write(self, data: Any) -> None:
        self.seen.append(bytes(data))

    def writelines(self, chunks: Any) -> None:
        self.seen.extend(bytes(chunk) for chunk in chunks)

    def close(self) -> None:
        return None

    def abort(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "sockname":
            return ("127.0.0.1", 8000)
        if name == "peername":
            return ("127.0.0.1", 5000)
        return default


def build() -> tuple[Wreath, list[bool]]:
    materialized: list[bool] = []
    app = Wreath(
        http_policy=HttpPolicy(
            trusted_host=TrustedHostPolicy(("allowed.test",)),
            request_id=RequestIdPolicy(),
            server_timing=ServerTimingPolicy(),
            cors=CorsPolicy(allow_origins=["https://app.test"]),
            security_headers=SecurityHeadersPolicy(),
        )
    )

    @app.get("/x")
    async def handler(request: Any) -> Response:
        materialized.append(True)
        return Response(b"ok")

    app._compile_routes()
    return app, materialized


async def serve(app: Wreath, raw: bytes) -> bytes:
    protocol = _server.HttpProtocol(app, ServerConfig(), asyncio.get_running_loop(), set())
    transport = Recorder()
    protocol.connection_made(transport)
    protocol.data_received(raw)
    for _ in range(200):
        await asyncio.sleep(0)
        if transport.seen:
            break
    return b"".join(transport.seen)


@pytest.mark.asyncio
async def test_preflight_is_answered_without_materializing_a_request() -> None:
    app, materialized = build()
    response = await serve(
        app,
        b"OPTIONS /x HTTP/1.1\r\n"
        b"host: allowed.test\r\n"
        b"origin: https://app.test\r\n"
        b"access-control-request-method: GET\r\n\r\n",
    )
    assert response.startswith(b"HTTP/1.1 204 No Content\r\n")
    assert b"access-control-allow-origin: https://app.test\r\n" in response
    assert materialized == []


@pytest.mark.asyncio
async def test_ingress_refusal_is_native_and_never_calls_the_handler() -> None:
    app, materialized = build()
    response = await serve(app, b"GET /x HTTP/1.1\r\nhost: evil.test\r\n\r\n")
    assert response.startswith(b"HTTP/1.1 400 Bad Request\r\n")
    assert materialized == []


@pytest.mark.asyncio
async def test_default_ai_scraping_refusal_stays_before_python_activation() -> None:
    reached = False
    app = Wreath()

    @app.get("/")
    async def home() -> str:
        nonlocal reached
        reached = True
        return "ok"

    app._compile_routes()
    response = await serve(
        app,
        b"GET / HTTP/1.1\r\nhost: allowed.test\r\nuser-agent: GPTBot/1.0\r\n\r\n",
    )
    assert response.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    assert b"AI scraper traffic is disabled by default" in response
    assert reached is False


@pytest.mark.asyncio
async def test_native_ai_scraping_checks_every_recognized_product() -> None:
    app = Wreath()

    @app.get("/")
    async def home() -> str:
        return "ok"

    app._compile_routes()
    response = await serve(
        app,
        b"GET / HTTP/1.1\r\nhost: allowed.test\r\nuser-agent: Googlebot/1.0 GPTBot/1.0\r\n\r\n",
    )
    assert response.startswith(b"HTTP/1.1 403 Forbidden\r\n")


@pytest.mark.asyncio
async def test_native_ai_scraping_policy_allows_its_robots_declaration() -> None:
    app = Wreath()

    @app.get("/robots.txt")
    async def robots() -> str:
        return robots_txt(app)

    app._compile_routes()
    response = await serve(
        app,
        b"GET /robots.txt HTTP/1.1\r\nhost: allowed.test\r\nuser-agent: GPTBot/1.0\r\n\r\n",
    )
    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"User-agent: gptbot" in response
    assert b"Disallow: /" in response


@pytest.mark.asyncio
async def test_explicit_ai_scraping_opt_in_keeps_the_policy_free_fast_path() -> None:
    app = Wreath(ai_scraping="allow")

    @app.get("/")
    async def home() -> str:
        return "ok"

    app._compile_routes()
    assert app._http_policy is None
    assert app._wreath_policy is None
    response = await serve(
        app,
        b"GET / HTTP/1.1\r\nhost: allowed.test\r\nuser-agent: GPTBot/1.0\r\n\r\n",
    )
    assert response.startswith(b"HTTP/1.1 200 OK\r\n")


@pytest.mark.asyncio
async def test_policy_free_native_json_uses_the_one_shot_encoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded: list[object] = []
    original = app_module._json_dumps

    def watched(value: object) -> bytes:
        encoded.append(value)
        return original(value)

    monkeypatch.setattr(app_module, "_json_dumps", watched)
    app = Wreath(ai_scraping="allow")

    @app.get("/")
    async def home():
        return {"ready": True}

    app._compile_routes()
    response = await serve(app, b"GET / HTTP/1.1\r\nhost: allowed.test\r\n\r\n")

    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert encoded == [{"ready": True}]


@pytest.mark.asyncio
async def test_ingress_only_policy_keeps_native_json_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded: list[object] = []
    original = app_module._json_dumps

    def watched(value: object) -> bytes:
        encoded.append(value)
        return original(value)

    monkeypatch.setattr(app_module, "_json_dumps", watched)
    app = Wreath(
        http_policy=HttpPolicy(
            ai_scraping=AIScrapingPolicy(allow=True),
        ),
        ai_scraping="allow",
    )

    @app.get("/")
    async def home():
        return {"ready": True}

    app._compile_routes()
    response = await serve(app, b"GET / HTTP/1.1\r\nhost: allowed.test\r\n\r\n")

    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert encoded == [{"ready": True}]


@pytest.mark.asyncio
async def test_egress_policy_still_wraps_a_raw_json_handler_result() -> None:
    app = Wreath(
        http_policy=HttpPolicy(security_headers=SecurityHeadersPolicy()),
        ai_scraping="allow",
    )

    @app.get("/")
    async def home():
        return {"ready": True}

    app._compile_routes()
    response = await serve(app, b"GET / HTTP/1.1\r\nhost: allowed.test\r\n\r\n")

    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"x-content-type-options: nosniff\r\n" in response


def test_a_route_without_middleware_does_not_invoke_the_chain_compiler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled: list[tuple[object, object]] = []
    original = app_module.compile_middleware

    def watched(endpoint: object, middleware: object, **options: object) -> object:
        compiled.append((endpoint, middleware))
        return original(endpoint, middleware, **options)

    monkeypatch.setattr(app_module, "compile_middleware", watched)
    app = Wreath(ai_scraping="allow")

    @app.get("/")
    async def home() -> str:
        return "ready"

    app._compile_routes()

    assert compiled == []


def test_a_route_with_middleware_does_invoke_the_chain_compiler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled: list[tuple[object, object]] = []
    original = app_module.compile_middleware

    def watched(endpoint: object, middleware: object, **options: object) -> object:
        compiled.append((endpoint, middleware))
        return original(endpoint, middleware, **options)

    monkeypatch.setattr(app_module, "compile_middleware", watched)
    app = Wreath(ai_scraping="allow")

    async def transparent(request: Any, call_next: Any) -> object:
        return await call_next(request)

    app.add_middleware(transparent)

    @app.get("/")
    async def home() -> str:
        return "ready"

    app._compile_routes()

    assert compiled


@pytest.mark.asyncio
async def test_accepted_response_receives_native_egress_policy() -> None:
    app, materialized = build()
    response = await serve(
        app,
        b"GET /x HTTP/1.1\r\nhost: allowed.test\r\norigin: https://app.test\r\n\r\n",
    )
    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"access-control-allow-origin: https://app.test\r\n" in response
    assert b"x-content-type-options: nosniff\r\n" in response
    assert b"server-timing: total;dur=" in response
    assert b"x-request-id: " in response
    assert materialized == [True]


@pytest.mark.asyncio
async def test_native_timing_replaces_only_its_metric_in_existing_fields() -> None:
    app = Wreath(http_policy=HttpPolicy(server_timing=ServerTimingPolicy(metric="total")))

    @app.get("/timed")
    async def timed(request: Any) -> Response:
        return Response(
            b"ok",
            headers=[
                (b"server-timing", b"db;dur=2, total;dur=9"),
                (b"server-timing", b"cache;dur=1"),
            ],
        )

    app._compile_routes()
    response = await serve(app, b"GET /timed HTTP/1.1\r\nhost: allowed.test\r\n\r\n")

    assert response.count(b"server-timing:") == 1
    assert b"server-timing: db;dur=2, cache;dur=1, total;dur=" in response
    assert b"total;dur=9" not in response


@pytest.mark.asyncio
async def test_html_response_materializes_mutable_headers_for_native_egress() -> None:
    app = Wreath(http_policy=HttpPolicy(security_headers=SecurityHeadersPolicy()))

    @app.get("/html")
    async def html(request: Any) -> HTMLResponse:
        return HTMLResponse("<p>safe</p>")

    app._compile_routes()
    response = await serve(app, b"GET /html HTTP/1.1\r\nhost: allowed.test\r\n\r\n")

    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"content-type: text/html; charset=utf-8\r\n" in response
    assert b"x-content-type-options: nosniff\r\n" in response
    assert response.endswith(b"<p>safe</p>")


@pytest.mark.parametrize(
    "policy",
    [
        CorsPolicy(allow_origins=["*"]),
        HttpPolicy(),
        TieredRateLimitPolicy(tiers={"pro": (5, 60.0)}, default=(1, 60.0)),
    ],
)
def test_standard_policy_is_refused_by_the_custom_hook_api(policy: Any) -> None:
    app = Wreath()
    with pytest.raises(TypeError, match="first-class HTTP policy"):
        app.add_middleware(policy)


def test_policy_objects_expose_no_public_middleware_protocol() -> None:
    policy = CorsPolicy(allow_origins=["*"])
    for name in ("before", "before_sync", "after", "after_sync", "after_inplace"):
        assert not hasattr(policy, name)


def test_first_class_features_can_be_configured_incrementally_before_startup() -> None:
    app = Wreath()
    cors = CorsPolicy(allow_origins=["https://app.test"])
    security = SecurityHeadersPolicy()
    app.configure_http_policy(HttpPolicy(cors=cors))
    app.configure_http_policy(HttpPolicy(security_headers=security))

    assert app._http_policy.cors is cors
    assert app._http_policy.security_headers is security
    with pytest.raises(ValueError, match="cors.*already configured"):
        app.configure_http_policy(HttpPolicy(cors=CorsPolicy(allow_origins=["*"])))
