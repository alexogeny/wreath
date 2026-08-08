"""Auth, middleware, and ORM guards (report 23: R-35, R-45, R-46, R-49, R-51,
R-53, R-56, R-62, R-63, R-64)."""

from __future__ import annotations

import pytest

from wreath.middleware.sessions import SessionMiddleware
from wreath.policy.csrf import CsrfPolicy

_SECRET = "k" * 32


class TestSecretStrength:
    """R-53 / R-62: `CsrfPolicy` requires a 32-byte secret; the session
    cookie and a bare-string JWT key -- both of which sign credentials -- accept
    anything."""

    def test_session_secret_has_a_floor(self):
        with pytest.raises(ValueError):
            SessionMiddleware(secret="short")

    def test_a_long_session_secret_is_accepted(self):
        assert SessionMiddleware(secret=_SECRET) is not None

    def test_a_bare_string_hmac_key_has_a_floor(self):
        from wreath._auth.jwt import JwtError, JwtVerifier

        with pytest.raises((ValueError, JwtError)):
            JwtVerifier(algorithms=["HS256"], key="secret")

    def test_a_long_hmac_key_is_accepted(self):
        from wreath._auth.jwt import JwtVerifier

        assert JwtVerifier(algorithms=["HS256"], key=_SECRET) is not None


class TestCsrfHostileCookie:
    """R-51: a non-ASCII cookie value reaches `cookie.encode("ascii")` and
    raises, turning attacker-controlled input into a 500 rather than a 403."""

    async def test_a_non_ascii_cookie_is_a_refusal_not_a_crash(self):
        middleware = CsrfPolicy(_SECRET, secure=False)

        class _Request:
            method = "POST"
            scheme = "http"
            cookies = {"wreath_csrf": "café"}

            def header(self, name, default=None):
                # The real `Request` has this; these doubles did not, which is
                # the only reason they broke. Absent = legacy client = the
                # token path this test exercises.
                return default

            def _index_headers(self):
                return {b"host": b"example.com", b"x-csrf-token": "café".encode()}

        response = await middleware._ingress(_Request())
        assert response is not None and response.status == 403


class TestCsrfMissingOrigin:
    """R-49: with `secure=False` -- which is what a TLS-terminating proxy leaves
    you with unless ProxyHeaders is mounted -- a request carrying neither Origin
    nor Referer skips the origin check entirely, and it does so as a side effect
    of an unrelated flag."""

    def _request(self):
        class _Request:
            method = "POST"
            scheme = "http"
            cookies: dict[str, str] = {}

            def header(self, name, default=None):
                # The real `Request` has this; these doubles did not, which is
                # the only reason they broke. Absent = legacy client = the
                # token path this test exercises.
                return default

            def _index_headers(self):
                return {b"host": b"example.com"}

        return _Request()

    def test_the_fallback_is_not_derived_from_the_secure_flag(self):
        middleware = CsrfPolicy(_SECRET, secure=False)
        assert middleware._origin_valid(self._request(), {b"host": b"example.com"}) is False

    def test_the_fallback_can_be_asked_for_explicitly(self):
        middleware = CsrfPolicy(_SECRET, secure=False, allow_missing_origin=True)
        assert middleware._origin_valid(self._request(), {b"host": b"example.com"}) is True


class TestJwksRefreshDiscipline:
    """R-63: `_fetch` returns early on a non-200 without advancing
    `_last_refresh`, so the negative cache never engages while the identity
    provider is erroring -- the amplification the module says it prevents.
    R-64: the provider's `max-age` is honoured unclamped."""

    class _Response:
        def __init__(self, status, body=b"{}", cache_control=None):
            self.status = status
            self.body = body
            self._cache_control = cache_control

        def header(self, name):
            if name == b"cache-control" and self._cache_control:
                return self._cache_control
            return None

    class _Client:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = 0

        async def get(self, path):
            self.calls += 1
            return self.responses[min(self.calls - 1, len(self.responses) - 1)]

    def _cache(self, client, **kwargs):
        from wreath._auth.jwks import JwksCache

        return JwksCache(http_client=client, jwks_path="/jwks", **kwargs)

    async def test_a_failing_provider_is_not_re_hit_per_request(self):
        import asyncio
        import json

        good = self._Response(
            200,
            json.dumps({"keys": [{"kty": "oct", "k": "AAAA", "kid": "k1"}]}).encode(),
        )
        # Everything after the first response is an outage.
        client = self._Client([good, self._Response(503)])
        cache = self._cache(client, min_refresh_interval=0.05)
        await cache.prefetch()
        assert client.calls == 1

        # Past the negative-cache interval, so the next unknown kid may refresh
        # once -- and that refresh fails. Every request after it must be held
        # off by the same interval, or a struggling provider gets a request per
        # request from every worker.
        await asyncio.sleep(0.06)
        for _ in range(5):
            await cache.resolve("unknown")
        assert client.calls == 2, f"the failing provider was hit {client.calls} times"

    async def test_a_providers_max_age_is_clamped(self):
        import json

        from wreath._auth.jwks import _MAX_TTL, _ttl_from_headers

        # The clamp itself, asserted where it happens. Reconstructing it as
        # `_expires_at - _last_refresh` compares a *difference of two large
        # monotonic floats* against an exact bound, and whether that lands above
        # or below 86400.0 depends on the float spacing at the current uptime:
        # around three days the spacing at that magnitude is ~3e-11, so the
        # subtraction returns 86400.00000000003 for about half of all clock
        # values and 86399.99999999997 for the rest. That failed on a box that
        # had been up a while and passed on a fresh one, which reads as a
        # regression in whatever was being edited at the time.
        response = self._Response(200, b"{}", cache_control=b"max-age=31536000")
        assert _ttl_from_headers(response, 600.0) == _MAX_TTL

        # And that the cache actually uses the clamped value. A tolerance here
        # because this one *is* a difference of two instants, and the property
        # is "clamped to a day", not "clamped to the last bit of a day".
        body = json.dumps({"keys": [{"kty": "oct", "k": "AAAA", "kid": "k1"}]}).encode()
        client = self._Client([self._Response(200, body, cache_control=b"max-age=31536000")])
        cache = self._cache(client, ttl=600.0)
        await cache.prefetch()
        assert cache._expires_at - cache._last_refresh == pytest.approx(_MAX_TTL)


class TestFusedMiddlewareDetection:
    """R-46: `_is_fused` accepts any object with a `before`/`after` attribute, so
    a legacy `(request, call_next)` middleware that also defines `after` is
    silently never called."""

    def test_an_ambiguous_middleware_is_refused_rather_than_half_run(self):
        from wreath.middleware.base import compile_middleware

        class Ambiguous:
            async def __call__(self, request, call_next):  # pragma: no cover
                return await call_next(request)

            async def after(self, request, response):  # pragma: no cover
                return response

        async def endpoint(request):  # pragma: no cover
            return None

        with pytest.raises(TypeError, match="both"):
            compile_middleware(endpoint, [Ambiguous()])

    def test_a_plain_legacy_middleware_still_compiles(self):
        from wreath.middleware.base import compile_middleware

        class Legacy:
            async def __call__(self, request, call_next):  # pragma: no cover
                return await call_next(request)

        async def endpoint(request):  # pragma: no cover
            return None

        assert compile_middleware(endpoint, [Legacy()]) is not endpoint

    def test_a_plain_hook_middleware_still_compiles(self):
        from wreath.middleware.base import compile_middleware

        class Hooks:
            async def before(self, request):  # pragma: no cover
                return None

        async def endpoint(request):  # pragma: no cover
            return None

        assert compile_middleware(endpoint, [Hooks()]) is not endpoint


class TestTenantRawQuery:
    """R-35: `RawQuery` never checks that a tenant session is inside a bound
    transaction, so raw SQL runs against whatever `search_path` the pooled
    connection last held."""

    def _session(self):
        from wreath.orm.session import Session, TenantContext

        class _SchemaMode:
            kind = "isolated"

        class _Registry:
            schema_mode = _SchemaMode()
            database = None

        return Session(_Registry(), "write", tenant=TenantContext(schema="t_acme"))

    async def test_raw_execute_outside_a_transaction_is_refused(self):
        from wreath.orm.errors import SessionError

        session = self._session()
        with pytest.raises(SessionError, match="transaction"):
            await session.raw("SELECT 1").execute()

    async def test_raw_fetch_outside_a_transaction_is_refused(self):
        from wreath.orm.errors import SessionError

        session = self._session()
        with pytest.raises(SessionError, match="transaction"):
            await session.raw("SELECT 1").fetch()

    async def test_raw_fetchval_outside_a_transaction_is_refused(self):
        from wreath.orm.errors import SessionError

        session = self._session()
        with pytest.raises(SessionError, match="transaction"):
            await session.raw("SELECT 1").fetchval()


@pytest.mark.skip(
    reason="needs a bigger refactor in the source: running route-middleware "
    "`after` hooks on an exception means giving the tape an error path and "
    "deciding what an after hook receives when there is no response. See "
    "report 23 R-45."
)
async def test_route_middleware_after_hooks_run_when_the_handler_raises():
    raise AssertionError("unimplemented")


@pytest.mark.skip(
    reason="needs a bigger refactor in the source: the token-bucket table is a "
    "bounded LRU, so evicting your own throttled bucket resets the limit. A fix "
    "means an admission policy that does not evict live-and-throttled keys "
    "(or a different structure). See report 23 R-56."
)
def test_rate_limit_bucket_survives_key_spraying():
    raise AssertionError("unimplemented")
