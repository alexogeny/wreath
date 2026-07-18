from __future__ import annotations

from typing import Any

import pytest

from wreath import Response, Wreath
from wreath.cache_control import CacheControl
from wreath.middleware import CacheControlMiddleware
from wreath.testing import TestClient


def test_cache_control_validation_and_serialization() -> None:
    policy = CacheControl(public=True, max_age=3600, immutable=True)
    assert policy.to_header() == b"public, immutable, max-age=3600"
    with pytest.raises(ValueError):
        CacheControl(public=True, private=True)
    with pytest.raises(ValueError):
        CacheControl(immutable=True)


@pytest.mark.asyncio
async def test_cache_middleware_preserves_explicit_and_protects_set_cookie() -> None:
    app = Wreath()
    app.add_middleware(CacheControlMiddleware(CacheControl(public=True, max_age=60)))

    @app.get("/cookie")
    async def cookie(request: Any) -> Response:
        response = Response(b"ok")
        response.set_cookie("session", "x")
        return response

    @app.get("/explicit")
    async def explicit(request: Any) -> Response:
        return Response(b"ok", headers=[(b"cache-control", b"no-cache")])

    async with TestClient(app) as client:
        cookie_response = await client.get("/cookie")
        explicit_response = await client.get("/explicit")
    assert cookie_response.header("cache-control") == "private, no-store"
    assert explicit_response.header("cache-control") == "no-cache"


@pytest.mark.asyncio
async def test_static_cache_policy_is_preserved_on_304(tmp_path) -> None:
    tmp_path.joinpath("asset.txt").write_text("asset")
    app = Wreath()
    policy = CacheControl(public=True, max_age=3600)
    app.static("/assets", str(tmp_path), cache_control=policy)

    async with TestClient(app) as client:
        first = await client.get("/assets/asset.txt")
        second = await client.get(
            "/assets/asset.txt", headers={"if-none-match": first.header("etag") or ""}
        )

    assert first.header("cache-control") == "public, max-age=3600"
    assert second.status == 304
    assert second.header("cache-control") == "public, max-age=3600"
