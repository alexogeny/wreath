from __future__ import annotations

from typing import Any

import pytest

from wreath import Response, Wreath
from wreath.cache_control import CacheControl
from wreath.policy import CachePolicy, HttpPolicy
from wreath.request import Request
from wreath.testing import TestClient


def test_cache_control_validation_and_serialization() -> None:
    policy = CacheControl(public=True, max_age=3600, immutable=True)
    assert policy.to_header() == b"public, immutable, max-age=3600"
    with pytest.raises(ValueError):
        CacheControl(public=True, private=True)
    with pytest.raises(ValueError):
        CacheControl(immutable=True)


@pytest.mark.parametrize(
    "options",
    [
        {"public": True},
        {"private": True},
        {"max_age": 0},
    ],
)
def test_cache_control_accepts_each_independent_boundary(
    options: dict[str, Any],
) -> None:
    CacheControl(**options)


@pytest.mark.parametrize(
    "name",
    [
        "max_age",
        "shared_max_age",
        "stale_while_revalidate",
        "stale_if_error",
    ],
)
@pytest.mark.parametrize("value", [True, 1.5, -1])
def test_cache_control_rejects_every_invalid_duration(
    name: str,
    value: Any,
) -> None:
    with pytest.raises(ValueError, match=rf"^{name} must be"):
        CacheControl(**{name: value})


def test_immutable_cache_control_requires_strictly_positive_max_age() -> None:
    with pytest.raises(ValueError, match="positive max_age"):
        CacheControl(immutable=True, max_age=0)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [],
        },
        None,
        None,
    )


@pytest.mark.asyncio
async def test_cache_middleware_preserves_one_explicit_header() -> None:
    middleware = CachePolicy(CacheControl(public=True))
    response = Response(b"ok", headers=[(b"cache-control", b"no-cache")])
    original_headers = response.headers.copy()

    result = await middleware.after(_request(), response)

    assert result is response
    assert response.headers == original_headers


@pytest.mark.asyncio
async def test_cache_middleware_policy_wins_over_default() -> None:
    middleware = CachePolicy(
        CacheControl(public=True),
        policy=lambda request, response: CacheControl(private=True),
    )
    response = Response(b"ok")

    await middleware.after(_request(), response)

    assert response.headers[-1] == (b"cache-control", b"private")


@pytest.mark.asyncio
async def test_cache_middleware_declining_policy_uses_default() -> None:
    middleware = CachePolicy(
        CacheControl(no_cache=True),
        policy=lambda request, response: None,
    )
    response = Response(b"ok")

    await middleware.after(_request(), response)

    assert response.headers[-1] == (b"cache-control", b"no-cache")


@pytest.mark.asyncio
async def test_cache_middleware_without_a_policy_or_default_is_a_noop() -> None:
    middleware = CachePolicy()
    response = Response(b"ok", headers=[(b"x-test", b"kept")])
    original_headers = response.headers.copy()

    result = await middleware.after(_request(), response)

    assert result is response
    assert response.headers == original_headers


@pytest.mark.asyncio
async def test_public_cache_policy_without_a_cookie_stays_public() -> None:
    middleware = CachePolicy(CacheControl(public=True))
    response = Response(b"ok")

    await middleware.after(_request(), response)

    assert response.headers[-1] == (b"cache-control", b"public")


@pytest.mark.asyncio
async def test_private_cache_policy_with_a_cookie_stays_private() -> None:
    middleware = CachePolicy(CacheControl(private=True))
    response = Response(b"ok", headers=[(b"set-cookie", b"session=x")])

    await middleware.after(_request(), response)

    assert response.headers[-1] == (b"cache-control", b"private")


@pytest.mark.asyncio
async def test_cache_policy_preserves_explicit_and_protects_set_cookie() -> None:
    app = Wreath()
    app.configure_http_policy(
        HttpPolicy(cache_control=CachePolicy(CacheControl(public=True, max_age=60)))
    )

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
