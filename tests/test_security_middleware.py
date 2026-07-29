from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath._native import _core
from wreath._pure.security import host_allowed as pure_host_allowed
from wreath.middleware import SecurityHeadersMiddleware, TrustedHostMiddleware
from wreath.testing import TestClient


def test_native_trusted_host_matcher_matches_pure_reference() -> None:
    patterns = ("api.example.com", "*.internal.test")
    for host, expected in (
        ("api.example.com", True),
        ("node.internal.test", True),
        ("internal.test", False),
        ("evil.example", False),
    ):
        assert pure_host_allowed(host, patterns) is expected
        if _core is not None:
            assert _core.host_allowed(host, patterns) is expected


@pytest.mark.asyncio
async def test_trusted_host_rejects_before_handler_and_accepts_subdomains() -> None:
    app = Wreath()
    called = False
    app.add_middleware(TrustedHostMiddleware(("api.example.com", "*.internal.test")))

    @app.get("/")
    async def index(request: Any) -> str:
        nonlocal called
        called = True
        return "ok"

    async with TestClient(app) as client:
        rejected = await client.get("/", headers={"host": "evil.example"})
        assert not called
        accepted = await client.get("/", headers={"host": "node.internal.test:8000"})

    assert rejected.status == 400
    assert rejected.header("content-type") == "application/problem+json"
    assert accepted.status == 200
    assert called


@pytest.mark.asyncio
async def test_trusted_host_rejects_authorities_with_userinfo_or_malformed_ports() -> None:
    app = Wreath()
    called = 0
    app.add_middleware(TrustedHostMiddleware(("good.example", "[::1]")))

    @app.get("/")
    async def index(request: Any) -> str:
        nonlocal called
        called += 1
        return f"https://{request.header('host')}/reset?token=secret"

    async with TestClient(app) as client:
        for host in (
            "good.example:@evil.example",
            "good.example:garbage",
            "[::1]junk",
        ):
            response = await client.get("/", headers={"host": host})
            assert response.status == 400, host
        accepted = await client.get("/", headers={"host": "[::1]:8000"})

    assert accepted.status == 200
    assert called == 1


@pytest.mark.asyncio
async def test_security_headers_do_not_replace_handler_values() -> None:
    app = Wreath()
    app.add_middleware(
        SecurityHeadersMiddleware(
            content_security_policy="default-src 'none'",
            strict_transport_security="max-age=31536000",
        )
    )

    @app.get("/")
    async def index(request: Any):
        from wreath import Response

        return Response(b"ok", headers=[(b"x-frame-options", b"SAMEORIGIN")])

    async with TestClient(app) as client:
        response = await client.get("/", headers={"host": "example.test"})

    assert response.header("content-security-policy") == "default-src 'none'"
    assert response.header("x-frame-options") == "SAMEORIGIN"
    assert response.header("x-content-type-options") == "nosniff"
    # TestClient uses an HTTP scope; HSTS must only be emitted for HTTPS.
    assert response.header("strict-transport-security") is None


@pytest.mark.asyncio
async def test_structured_hsts_is_https_only() -> None:
    middleware = SecurityHeadersMiddleware(
        hsts_max_age=31_536_000,
        hsts_include_subdomains=True,
        hsts_preload=True,
    )

    class Request:
        # `scheme` is the member the middleware reads; going through `scope`
        # would materialize the lazy native scope dict on every response.
        scheme = "https"

    class Response:
        headers: list[tuple[bytes, bytes]] = []

    response = await middleware.after(Request(), Response())
    assert response.headers[-1] == (
        b"strict-transport-security",
        b"max-age=31536000; includeSubDomains; preload",
    )

    with pytest.raises(ValueError):
        SecurityHeadersMiddleware(hsts_max_age=10, hsts_preload=True)
