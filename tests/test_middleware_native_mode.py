"""First-class HTTP policy crosses the native server boundary exactly once."""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

from wreath import Response, Wreath
from wreath.policy import (
    CorsPolicy,
    HttpPolicy,
    RequestIdPolicy,
    SecurityHeadersPolicy,
    ServerTimingPolicy,
    TieredRateLimitPolicy,
    TrustedHostPolicy,
)
from wreath.server import ServerConfig

_server = importlib.import_module("wreath._native._server")


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
    protocol = _server.HttpProtocol(
        app, ServerConfig(), asyncio.get_running_loop(), set()
    )
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
async def test_accepted_response_receives_native_egress_policy() -> None:
    app, materialized = build()
    response = await serve(
        app,
        b"GET /x HTTP/1.1\r\n"
        b"host: allowed.test\r\n"
        b"origin: https://app.test\r\n\r\n",
    )
    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"access-control-allow-origin: https://app.test\r\n" in response
    assert b"x-content-type-options: nosniff\r\n" in response
    assert b"server-timing: total;dur=" in response
    assert b"x-request-id: " in response
    assert materialized == [True]


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
