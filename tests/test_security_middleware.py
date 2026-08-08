from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath._native import _core
from wreath._pure.security import host_allowed as pure_host_allowed
from wreath.policy import HttpPolicy, SecurityHeadersPolicy, TrustedHostPolicy
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


def test_trusted_host_matchers_share_their_runtime_type_boundary() -> None:
    invalid: Any = None
    with pytest.raises(TypeError):
        pure_host_allowed(invalid, ("example.com",))
    if _core is not None:
        with pytest.raises(TypeError):
            _core.host_allowed(invalid, ("example.com",))


@pytest.mark.asyncio
async def test_trusted_host_rejects_before_handler_and_accepts_subdomains() -> None:
    app = Wreath(
        http_policy=HttpPolicy(
            trusted_host=TrustedHostPolicy(("api.example.com", "*.internal.test"))
        )
    )
    called = False

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
    app = Wreath(
        http_policy=HttpPolicy(
            trusted_host=TrustedHostPolicy(("good.example", "[::1]"))
        )
    )
    called = 0

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
    app = Wreath(
        http_policy=HttpPolicy(
            security_headers=SecurityHeadersPolicy(
                content_security_policy="default-src 'none'",
                strict_transport_security="max-age=31536000",
            )
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
    middleware = SecurityHeadersPolicy(
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

    response = await middleware._egress(Request(), Response())
    assert response.headers[-1] == (
        b"strict-transport-security",
        b"max-age=31536000; includeSubDomains; preload",
    )

    with pytest.raises(ValueError):
        SecurityHeadersPolicy(hsts_max_age=10, hsts_preload=True)


# --- the configuration refusals nothing had ever made fire --------------------
#
# `wreath mutant --operators guard.remove-raise` reported every refusal in these
# three constructors UNREACHED: each is a documented contract that holds, and
# none had ever been executed. They matter more than most argument checks
# because a bad allow-list does not fail loudly at runtime -- it quietly admits
# the wrong origin or the wrong Host, which is the whole thing the middleware is
# there to stop.


def test_a_trusted_host_list_that_allows_nothing_is_refused() -> None:
    """An empty list would refuse every request; that is a config bug, not a policy."""
    with pytest.raises(ValueError, match="allowed_hosts must not be empty"):
        TrustedHostPolicy([])


@pytest.mark.parametrize(
    "pattern",
    [
        "http://api.example.com",   # a URL, not a host
        "api.example.com/path",
        "api.example.com:8080/x",
        "",
        " ",
        "api example.com",
    ],
)
def test_a_trusted_host_pattern_that_is_not_a_host_is_refused(pattern: str) -> None:
    with pytest.raises(ValueError, match="invalid trusted-host pattern"):
        TrustedHostPolicy([pattern])


@pytest.mark.parametrize("pattern", ["ex*.com", "*example.com", "a.*.com", "**.com"])
def test_a_wildcard_anywhere_but_a_leading_label_is_refused(pattern: str) -> None:
    """`*` and `*.example.com` are the only two wildcard shapes.

    A mid-label wildcard reads as far narrower than it matches -- `*example.com`
    looks like a subdomain rule and would admit `evilexample.com` -- so it is
    refused at construction rather than silently honoured.
    """
    with pytest.raises(ValueError, match="invalid trusted-host pattern"):
        TrustedHostPolicy([pattern])


def test_the_two_legitimate_wildcard_shapes_are_accepted() -> None:
    assert TrustedHostPolicy(["*"]).allowed_hosts == ("*",)
    assert TrustedHostPolicy(["*.example.com"]).allowed_hosts == ("*.example.com",)


def test_a_websocket_origin_list_that_allows_nothing_is_refused() -> None:
    from wreath.policy.security import WebSocketOriginPolicy

    with pytest.raises(ValueError, match="origins must not be empty"):
        WebSocketOriginPolicy([])


@pytest.mark.asyncio
async def test_websocket_origin_requests_are_matched_exactly_and_required() -> None:
    from wreath.policy.security import WebSocketOriginPolicy
    from wreath.request import Request

    middleware = WebSocketOriginPolicy(["https://app.example"])

    def request(*origins: bytes) -> Request:
        return Request(
            {
                "type": "websocket",
                "path": "/socket",
                "headers": [(b"origin", origin) for origin in origins],
            },
            None,
            None,
        )

    assert await middleware._ingress(request(b"https://app.example")) is None
    for refused in (
        request(),
        request(b"https://evil.example"),
        request(b"https://app.example", b"https://evil.example"),
    ):
        response = await middleware._ingress(refused)
        assert response is not None
        assert response.status == 403


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://app.example",            # not a browser origin scheme
        "app.example",                  # no scheme at all
        "https://",                     # no host
        "https://user@app.example",     # userinfo is not part of an origin
        "https://user:pw@app.example",
        "https://app.example/path",     # an origin has no path ...
        "https://app.example?q=1",      # ... no query ...
        "https://app.example#f",        # ... and no fragment
        "https://app.example:notaport",
    ],
)
def test_an_origin_that_is_not_a_browser_origin_is_refused(origin: str) -> None:
    """Refused at construction, because a bad entry is a hole, not an error.

    A value that does not normalise is one no browser will ever send, so it
    would sit in the allow-list matching nothing while looking like cover.
    """
    from wreath.policy.security import WebSocketOriginPolicy

    with pytest.raises(ValueError, match="invalid WebSocket origin"):
        WebSocketOriginPolicy([origin])


def test_origins_are_normalised_to_scheme_host_and_non_default_port() -> None:
    """Comparison is exact bytes, so normalisation is what makes it correct."""
    from wreath.policy.security import WebSocketOriginPolicy

    allowed = WebSocketOriginPolicy([
        "https://App.Example",          # case-folded
        "https://app.example:443",      # the default port is dropped
        "http://app.example:80",
        "http://app.example:8080",      # a non-default one is kept
        "https://app.example/",         # a bare slash is not a path
    ]).allowed_origins
    assert allowed == (
        b"https://app.example",
        b"https://app.example",
        b"http://app.example",
        b"http://app.example:8080",
        b"https://app.example",
    )


def test_hsts_settings_that_contradict_each_other_are_refused() -> None:
    """Two spellings of one header, and a max-age that cannot mean anything."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        SecurityHeadersPolicy(
            hsts_max_age=31_536_000, strict_transport_security="max-age=600",
        )
    for bad in (-1, "31536000", True):
        with pytest.raises(ValueError, match="non-negative integer"):
            SecurityHeadersPolicy(hsts_max_age=bad)


@pytest.mark.parametrize(
    "settings",
    [
        {"hsts_max_age": 31_536_000, "hsts_preload": True},
        {"hsts_include_subdomains": True, "hsts_preload": True},
        {
            "hsts_max_age": 31_535_999,
            "hsts_include_subdomains": True,
            "hsts_preload": True,
        },
    ],
)
def test_hsts_preload_requires_each_prerequisite(
    settings: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="HSTS preload requires"):
        SecurityHeadersPolicy(**settings)


def test_normalize_host_is_the_gate_that_makes_the_shape_check_dead() -> None:
    """Which of the two wildcard checks actually refuses a bad pattern.

    `TrustedHostPolicy.__init__` refuses a malformed wildcard twice: once
    because `_normalize_host` returns None, and again in an explicit shape loop.
    `wreath mutant` reported the second one UNREACHED, and it is: `*` survives
    normalisation only as the whole pattern or as a leading `*.` label, so
    nothing that reaches the loop can fail it.

    That is fine as defence in depth, but it means `_normalize_host` is the only
    live gate on an *allowlist* -- and `*example.com` reading like a subdomain
    rule while matching `evilexample.com` is exactly the mistake that must not
    get through. This pins the coupling, so loosening `_normalize_host` turns
    the second check live rather than silently opening a hole.
    """
    from wreath.policy.security import _normalize_host

    for bad in ("ex*.com", "*example.com", "a.*.com", "**.com", "*.*.com", "x*"):
        assert _normalize_host(bad, pattern=True) is None, bad
    for good in ("*", "*.example.com"):
        assert _normalize_host(good, pattern=True) == good
