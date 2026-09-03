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
    assert policy.to_targeted_header() == b"public, immutable, max-age=3600"
    with pytest.raises(ValueError):
        CacheControl(public=True, private=True)
    with pytest.raises(ValueError):
        CacheControl(immutable=True)


@pytest.mark.parametrize(
    ("policy", "headers"),
    [
        (CachePolicy(), ()),
        (
            CachePolicy(cdn_default=CacheControl(public=True, max_age=60)),
            ("CDN-Cache-Control",),
        ),
        (
            CachePolicy(cdn_policy=lambda request, response: CacheControl(private=True)),
            ("CDN-Cache-Control",),
        ),
        (
            CachePolicy(default=CacheControl(public=True, max_age=60)),
            ("Cache-Control",),
        ),
    ],
)
def test_cache_contract_declares_cdn_headers_for_static_and_dynamic_policy(
    policy: CachePolicy, headers: tuple[str, ...]
) -> None:
    assert tuple(spec.name for _status, spec in policy.describe().response_headers) == headers


def test_cache_contract_exposes_constants_only_for_fixed_policies() -> None:
    fixed = CachePolicy(
        default=CacheControl(public=True, max_age=60),
        cdn_default=CacheControl(private=True, max_age=600),
    ).describe()
    assert [(spec.name, spec.const) for _status, spec in fixed.response_headers] == [
        ("Cache-Control", "public, max-age=60"),
        ("CDN-Cache-Control", "private, max-age=600"),
    ]

    dynamic = CachePolicy(
        default=CacheControl(public=True),
        policy=lambda request, response: None,
        cdn_default=CacheControl(private=True),
        cdn_policy=lambda request, response: None,
    ).describe()
    assert [(spec.name, spec.const) for _status, spec in dynamic.response_headers] == [
        ("Cache-Control", None),
        ("CDN-Cache-Control", None),
    ]


def test_cache_contract_declares_a_browser_policy_without_a_default() -> None:
    contract = CachePolicy(policy=lambda request, response: None).describe()
    assert [spec.name for _status, spec in contract.response_headers] == ["Cache-Control"]


def test_cache_policy_refuses_an_empty_cdn_default() -> None:
    with pytest.raises(ValueError, match="cdn_default needs at least one cache directive"):
        CachePolicy(cdn_default=CacheControl())


def test_targeted_cache_control_uses_structured_dictionary_types() -> None:
    policy = CacheControl(
        private=True,
        no_store=True,
        no_transform=True,
        must_revalidate=True,
        proxy_revalidate=True,
        max_age=60,
        shared_max_age=120,
        stale_while_revalidate=30,
        stale_if_error=300,
    )

    assert policy.to_targeted_header() == (
        b"private, no-store, no-transform, must-revalidate, proxy-revalidate, "
        b"max-age=60, s-maxage=120, stale-while-revalidate=30, stale-if-error=300"
    )


def test_response_sets_cdn_cache_control_independently() -> None:
    response = Response(
        b"ok",
        headers=[
            (b"cache-control", b"private, max-age=60"),
            (b"CDN-Cache-Control", b"max-age=120"),
        ],
    )

    response.set_cdn_cache_control(CacheControl(public=True, max_age=600))

    assert (b"cache-control", b"private, max-age=60") in response.headers
    assert [value for name, value in response.headers if name.lower() == b"cdn-cache-control"] == [
        b"public, max-age=600"
    ]


def test_response_refuses_an_empty_cdn_cache_policy() -> None:
    with pytest.raises(ValueError, match="at least one directive"):
        Response(b"ok").set_cdn_cache_control(CacheControl())


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
async def test_cache_middleware_can_set_browser_and_cdn_policies() -> None:
    middleware = CachePolicy(
        CacheControl(private=True, max_age=60),
        cdn_default=CacheControl(public=True, max_age=600),
    )
    response = Response(b"ok")

    await middleware.after(_request(), response)

    assert (b"cache-control", b"private, max-age=60") in response.headers
    assert (b"cdn-cache-control", b"public, max-age=600") in response.headers


@pytest.mark.asyncio
async def test_cache_middleware_preserves_an_explicit_cdn_policy() -> None:
    middleware = CachePolicy(cdn_default=CacheControl(public=True, max_age=600))
    response = Response(b"ok", headers=[(b"cdn-cache-control", b"no-store")])

    await middleware.after(_request(), response)

    assert [value for name, value in response.headers if name.lower() == b"cdn-cache-control"] == [
        b"no-store"
    ]


@pytest.mark.asyncio
async def test_cache_middleware_protects_cdn_policy_when_a_cookie_is_set() -> None:
    middleware = CachePolicy(cdn_default=CacheControl(public=True, max_age=600))
    response = Response(b"ok", headers=[(b"set-cookie", b"session=x")])

    await middleware.after(_request(), response)

    assert (b"cdn-cache-control", b"private, no-store") in response.headers


@pytest.mark.asyncio
async def test_cache_middleware_uses_a_dynamic_cdn_policy() -> None:
    middleware = CachePolicy(
        cdn_default=CacheControl(private=True),
        cdn_policy=lambda request, response: CacheControl(public=True, max_age=120),
    )
    response = Response(b"ok")
    await middleware.after(_request(), response)
    assert (b"cdn-cache-control", b"public, max-age=120") in response.headers


@pytest.mark.asyncio
async def test_a_declining_cdn_policy_without_a_default_is_a_noop() -> None:
    middleware = CachePolicy(cdn_policy=lambda request, response: None)
    response = Response(b"ok")
    await middleware.after(_request(), response)
    assert all(name.lower() != b"cdn-cache-control" for name, _value in response.headers)


@pytest.mark.asyncio
async def test_a_private_dynamic_cdn_policy_with_a_cookie_stays_private() -> None:
    middleware = CachePolicy(
        cdn_policy=lambda request, response: CacheControl(private=True, max_age=30)
    )
    response = Response(b"ok", headers=[(b"set-cookie", b"session=x")])
    await middleware.after(_request(), response)
    assert (b"cdn-cache-control", b"private, max-age=30") in response.headers


@pytest.mark.asyncio
async def test_browser_and_cdn_public_policies_share_the_cookie_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wreath.policy import cache as cache_module

    original = cache_module.find_response_header
    cookie_lookups = 0

    def count_cookie(headers, name: bytes):
        nonlocal cookie_lookups
        if name == b"set-cookie":
            cookie_lookups += 1
        return original(headers, name)

    monkeypatch.setattr(cache_module, "find_response_header", count_cookie)
    middleware = CachePolicy(
        CacheControl(public=True),
        cdn_default=CacheControl(public=True),
    )
    await middleware.after(_request(), Response(b"ok"))
    assert cookie_lookups == 1


@pytest.mark.asyncio
async def test_a_dynamic_cdn_policy_must_have_a_cache_directive() -> None:
    middleware = CachePolicy(cdn_policy=lambda request, response: CacheControl())
    with pytest.raises(ValueError, match="cdn_policy returned a policy with no cache directives"):
        await middleware.after(_request(), Response(b"ok"))


@pytest.mark.asyncio
async def test_no_cdn_configuration_skips_the_cdn_header_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wreath.policy import cache as cache_module

    original = cache_module.find_response_header

    def reject_cdn_lookup(headers, name: bytes):
        if name == b"cdn-cache-control":
            raise AssertionError("no CDN policy means no CDN header lookup")
        return original(headers, name)

    monkeypatch.setattr(cache_module, "find_response_header", reject_cdn_lookup)
    await CachePolicy().after(_request(), Response(b"ok"))


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
