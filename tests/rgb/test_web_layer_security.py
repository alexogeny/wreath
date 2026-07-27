"""CORS, rate limiting, forwarding headers, and gating (report 23: R-48, R-52,
R-54, R-55, R-57, R-58)."""

from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.middleware.cors import CORSMiddleware
from wreath.testing import TestClient


def _header(response, name: bytes) -> bytes | None:
    for key, value in response.headers:
        if key.lower() == name:
            return value
    return None


class _Request:
    """The smallest thing CORS reads."""

    def __init__(self, origin=None, method="GET"):
        self.method = method
        self._origin = origin

    def header(self, name, default=None):
        if name == "origin":
            return self._origin
        if name == "access-control-request-method":
            return "POST" if self._origin else None
        return default


class TestCorsCredentialReflection:
    """R-54: `allow_origins=["*"]` with `allow_credentials=True` reflects *any*
    origin alongside `Access-Control-Allow-Credentials: true` -- the canonical
    catastrophic CORS misconfiguration, built silently rather than refused."""

    def test_wildcard_with_credentials_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="credential"):
            CORSMiddleware(allow_origins=["*"], allow_credentials=True)

    def test_wildcard_without_credentials_is_still_allowed(self):
        middleware = CORSMiddleware(allow_origins=["*"])
        assert middleware._origin_header("https://anything.example") == (
            b"access-control-allow-origin",
            b"*",
        )

    def test_named_origins_with_credentials_are_still_allowed(self):
        middleware = CORSMiddleware(
            allow_origins=["https://app.example"], allow_credentials=True
        )
        assert middleware._origin_header("https://app.example") == (
            b"access-control-allow-origin",
            b"https://app.example",
        )
        assert middleware._origin_header("https://evil.example") is None


class TestCorsVaryOnRejection:
    """R-55: the reject path returns early without `Vary: Origin`, so a shared
    cache can store the no-ACAO response and serve it to an allowed origin."""

    async def test_a_disallowed_origin_still_varies(self):
        middleware = CORSMiddleware(allow_origins=["https://app.example"])

        class _Response:
            def __init__(self):
                self.headers: list[tuple[bytes, bytes]] = []

        response = await middleware.after(_Request(origin="https://evil.example"), _Response())
        assert _header(response, b"vary") == b"origin"
        assert _header(response, b"access-control-allow-origin") is None

    async def test_an_allowed_origin_varies_as_before(self):
        middleware = CORSMiddleware(allow_origins=["https://app.example"])

        class _Response:
            def __init__(self):
                self.headers: list[tuple[bytes, bytes]] = []

        response = await middleware.after(_Request(origin="https://app.example"), _Response())
        assert _header(response, b"vary") == b"origin"
        assert _header(response, b"access-control-allow-origin") == b"https://app.example"

    async def test_a_preflight_rejection_varies(self):
        middleware = CORSMiddleware(allow_origins=["https://app.example"])
        response = await middleware.before(
            _Request(origin="https://evil.example", method="OPTIONS")
        )
        assert response is not None and response.status == 403
        assert _header(response, b"vary") == b"origin"


def _vary_tokens(response) -> set[bytes]:
    """Every `Vary` token on a response, however many headers carry them."""
    tokens: set[bytes] = set()
    for name, value in response.headers:
        if name.lower() == b"vary":
            tokens.update(item.strip().lower() for item in value.split(b",") if item.strip())
    return tokens


def _vary_headers(response) -> list[bytes]:
    return [value for name, value in response.headers if name.lower() == b"vary"]


class TestCorsVaryMergesWithAnExistingVary:
    """The reject path added `Vary: origin` only when the response carried *no*
    `Vary` at all, so a response already varying on something else (compression
    adds `accept-encoding`, content negotiation adds `accept`) never gained
    `origin` -- and a shared cache could replay one origin's answer to another.
    That is exactly the failure the surrounding comment claims to prevent.
    """

    class _Response:
        def __init__(self, headers=()):
            self.headers: list[tuple[bytes, bytes]] = list(headers)

    async def test_a_disallowed_origin_merges_into_an_existing_vary(self):
        middleware = CORSMiddleware(allow_origins=["https://app.example"])
        response = await middleware.after(
            _Request(origin="https://evil.example"),
            self._Response([(b"vary", b"accept-encoding")]),
        )
        assert _vary_tokens(response) == {b"accept-encoding", b"origin"}
        assert len(_vary_headers(response)) == 1
        assert _header(response, b"access-control-allow-origin") is None

    async def test_an_allowed_origin_merges_into_an_existing_vary(self):
        middleware = CORSMiddleware(allow_origins=["https://app.example"])
        response = await middleware.after(
            _Request(origin="https://app.example"),
            self._Response([(b"vary", b"accept-encoding")]),
        )
        assert _vary_tokens(response) == {b"accept-encoding", b"origin"}
        assert len(_vary_headers(response)) == 1
        assert _header(response, b"access-control-allow-origin") == b"https://app.example"

    async def test_an_existing_origin_vary_is_not_duplicated(self):
        middleware = CORSMiddleware(allow_origins=["https://app.example"])
        response = await middleware.after(
            _Request(origin="https://evil.example"),
            self._Response([(b"vary", b"Accept-Encoding, Origin")]),
        )
        assert _vary_tokens(response) == {b"accept-encoding", b"origin"}
        assert len(_vary_headers(response)) == 1


class TestRateLimitKeyIsRequired:
    """R-57: a key function returning None means *no limiting at all*, so any
    deployment where `scope["client"]` is absent is silently unlimited."""

    async def test_a_request_with_no_client_is_still_limited(self):
        from wreath.middleware.ratelimit import RateLimitMiddleware

        middleware = RateLimitMiddleware(limit=1, window=60.0)

        class _NoClient:
            method = "GET"
            path = "/x"
            client = None
            identity = None

            def header(self, name, default=None):
                return default

        first = middleware.before_sync(_NoClient())
        second = middleware.before_sync(_NoClient())
        assert first is None
        assert second is not None and second.status == 429

    async def test_an_exempt_request_is_still_exempt(self):
        from wreath.middleware.ratelimit import RateLimitMiddleware

        middleware = RateLimitMiddleware(
            limit=1, window=60.0, exempt=lambda request: True
        )

        class _NoClient:
            method = "GET"
            path = "/x"
            client = None
            identity = None

        assert middleware.before_sync(_NoClient()) is None
        assert middleware.before_sync(_NoClient()) is None


class TestForwardedForFallback:
    """R-58: one malformed hop makes `forwarded_client` return None, leaving the
    peer as the proxy -- so every client behind it shares one bucket. A caller
    can trigger that at will."""

    def test_a_malformed_hop_does_not_collapse_the_chain(self):
        from wreath._pure.proxy import TrustedNetworks

        networks = TrustedNetworks(["10.0.0.0/8"])
        # The rightmost hops are the trusted proxy; the client prepended junk.
        assert networks.forwarded_client(b"garbage, 203.0.113.9, 10.0.0.5") == "203.0.113.9"

    def test_a_wholly_untrusted_chain_still_resolves_the_nearest_hop(self):
        from wreath._pure.proxy import TrustedNetworks

        networks = TrustedNetworks(["10.0.0.0/8"])
        assert networks.forwarded_client(b"203.0.113.9, 198.51.100.7") == "198.51.100.7"

    def test_a_chain_of_only_garbage_is_still_refused(self):
        from wreath._pure.proxy import TrustedNetworks

        networks = TrustedNetworks(["10.0.0.0/8"])
        assert networks.forwarded_client(b"garbage") is None


class TestDocsGating:
    """R-48: `enable_docs()` -- kept for compatibility -- registers the docs page
    and the OpenAPI document with no environment gate and no auth, while
    `enable_api_docs` is fail-closed."""

    async def test_the_compat_alias_is_gated_like_the_real_one(self, monkeypatch):
        monkeypatch.setenv("WREATH_ENV", "production")
        app = Wreath()
        registered = app.enable_docs()
        assert registered is False

        async with TestClient(app) as client:
            assert (await client.get("/docs")).status == 404
            assert (await client.get("/openapi.json")).status == 404

    async def test_it_still_serves_outside_production(self, monkeypatch):
        monkeypatch.setenv("WREATH_ENV", "dev")
        app = Wreath()
        assert app.enable_docs() is True

        async with TestClient(app) as client:
            assert (await client.get("/openapi.json")).status == 200


class TestSessionCookieDefaults:
    """R-52: the session cookie defaults to `secure=False` while the CSRF cookie
    defaults to `secure=True` -- the more sensitive cookie has the weaker
    default."""

    def test_secure_is_the_default(self):
        from wreath.middleware.sessions import SessionMiddleware

        assert SessionMiddleware(secret="s" * 32)._secure is True

    def test_it_can_still_be_turned_off_for_local_http(self):
        from wreath.middleware.sessions import SessionMiddleware

        assert SessionMiddleware(secret="s" * 32, secure=False)._secure is False
