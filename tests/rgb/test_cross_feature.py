"""The cross-feature interaction and the small signals (report 23: G-06, G-08,
G-17, G-31, G-40, G-55, G-64, B-12)."""

from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.policy import CsrfPolicy, HttpPolicy, csrf_token
from wreath.response_cache import cached
from wreath.testing import TestClient

_SECRET = "k" * 32


class TestCsrfDoesNotDisableCaching:
    """G-08 / G-55: CSRF mints a token cookie on anonymous GETs, and
    `_cacheable_response` refuses anything carrying `Set-Cookie`. Mount both and
    `@cached` silently caches nothing -- the cache is configured, reports no
    errors, and never serves a hit."""

    async def test_a_cached_page_that_never_asks_for_a_token_still_caches(self):
        app = Wreath(http_policy=HttpPolicy(csrf=CsrfPolicy(_SECRET)))
        calls = 0

        @app.get("/report")
        @cached(ttl=60)
        async def report(request):
            nonlocal calls
            calls += 1
            return {"n": calls}

        async with TestClient(app) as client:
            first = await client.get("/report")
            second = await client.get("/report")

        assert first.json() == {"n": 1}
        assert second.json() == {"n": 1}, "CSRF's cookie disabled the cache"
        assert calls == 1

    async def test_a_page_that_reads_the_token_still_gets_a_cookie(self):
        app = Wreath(http_policy=HttpPolicy(csrf=CsrfPolicy(_SECRET)))

        @app.get("/form")
        async def form(request):
            return {"token": csrf_token(request)}

        async with TestClient(app) as client:
            response = await client.get("/form")

        assert response.header("set-cookie") is not None
        assert response.json()["token"]

    async def test_an_unsafe_request_is_still_protected(self):
        app = Wreath(
            http_policy=HttpPolicy(csrf=CsrfPolicy(_SECRET, secure=False))
        )

        @app.post("/write")
        async def write(request):
            return {"ok": True}

        async with TestClient(app) as client:
            refused = await client.post("/write")
        assert refused.status == 403

    async def test_the_token_round_trips_from_a_form_to_a_write(self):
        app = Wreath(
            http_policy=HttpPolicy(csrf=CsrfPolicy(_SECRET, secure=False))
        )

        @app.get("/form")
        async def form(request):
            return {"token": csrf_token(request)}

        @app.post("/write")
        async def write(request):
            return {"ok": True}

        async with TestClient(app) as client:
            issued = await client.get("/form")
            token = issued.json()["token"]
            cookie = issued.header("set-cookie").split(";")[0]
            accepted = await client.post(
                "/write",
                headers={
                    "cookie": cookie,
                    "x-csrf-token": token,
                    "origin": "http://testserver",
                    "host": "testserver",
                },
            )
        assert accepted.status == 200


class TestRateLimitVisibility:
    """B-12: nothing counts throttled requests, so a limiter keying everyone
    identically looks exactly like one with nothing to do."""

    def test_refusals_are_counted(self):
        from wreath.policy.ratelimit import RateLimitPolicy

        middleware = RateLimitPolicy(limit=1, window=60.0)

        class _Request:
            method = "GET"
            path = "/x"
            client = ("198.51.100.9", 5000)
            identity = None

        assert middleware._ingress_sync(_Request()) is None
        assert middleware.throttled == 0
        assert middleware._ingress_sync(_Request()) is not None
        assert middleware.throttled == 1


class TestIdempotencyIgnoredSignal:
    """G-06: an unauthenticated request is silently unguarded, so idempotency
    "appears to work" right up until it matters."""

    async def test_an_ignored_key_says_so(self):
        from wreath.policy.idempotency import IdempotencyPolicy

        middleware = IdempotencyPolicy()

        class _State:
            def get(self, name, default=None):
                return getattr(self, name, default)

        class _Request:
            method = "POST"
            path = "/orders"
            identity = None                     # anonymous: not guarded
            state = _State()

            def header(self, name, default=None):
                return "abc" if name == "idempotency-key" else default

        request = _Request()
        assert await middleware.action(request) is None
        assert middleware.ignored == 1

        from wreath.response import JSONResponse

        response = await middleware.after(request, JSONResponse({"ok": True}))
        assert response.header("idempotency-ignored") if hasattr(
            response, "header"
        ) else any(
            name.lower() == b"idempotency-ignored" for name, _ in response.headers
        )

    async def test_a_guarded_request_says_nothing(self):
        from wreath.auth import Identity
        from wreath.policy.idempotency import IdempotencyPolicy
        from wreath.response import JSONResponse

        middleware = IdempotencyPolicy()

        class _State:
            def __init__(self):
                self._values = {}

            def __setattr__(self, name, value):
                if name == "_values":
                    object.__setattr__(self, name, value)
                else:
                    self._values[name] = value

            def get(self, name, default=None):
                return self._values.get(name, default)

        class _Request:
            method = "POST"
            path = "/orders"
            identity = Identity(id="u1")

            def __init__(self):
                self.state = _State()

            def header(self, name, default=None):
                return "abc" if name == "idempotency-key" else default

        request = _Request()
        assert await middleware.action(request) is None
        assert middleware.ignored == 0
        response = await middleware.after(request, JSONResponse({"ok": True}))
        assert not any(
            name.lower() == b"idempotency-ignored" for name, _ in response.headers
        )


class TestManifestResourceSentinel:
    """G-31: the manifest asks about the literal entity id `"*"`, which is a
    real id somebody's data can hold."""

    def test_the_type_level_entity_is_not_a_usable_id(self):
        from wreath._auth.permissions import TYPE_LEVEL_ID

        assert TYPE_LEVEL_ID != "*"
        # Whatever it is, it must not be something a URL path segment can carry.
        assert not TYPE_LEVEL_ID.isprintable() or "\x00" in TYPE_LEVEL_ID


class TestTieredKeyDeclaration:
    """G-64: the tiered limiter reaches into `child._key` to bypass the
    constructor's own refusal of `principal_key`."""

    def test_the_exemption_is_declared_not_poked(self):
        import inspect

        from wreath.policy import ratelimit

        source = inspect.getsource(ratelimit.TieredRateLimitPolicy)
        assert "child._key" not in source, "the guard is bypassed by assignment"

    def test_a_global_limiter_still_refuses_the_principal_key(self):
        from wreath.policy.ratelimit import RateLimitPolicy, principal_key

        with pytest.raises(ValueError, match="global"):
            RateLimitPolicy(limit=10, window=60.0, key=principal_key)

    def test_the_tiered_limiter_still_keys_on_the_principal(self):
        from wreath.auth import Identity
        from wreath.policy.ratelimit import TieredRateLimitPolicy

        middleware = TieredRateLimitPolicy(
            tiers={"pro": (10, 60.0)}, default=(1, 60.0)
        )

        class _Request:
            method = "GET"
            path = "/x"
            client = ("198.51.100.1", 1)

            def __init__(self, who):
                self.identity = Identity(id=who)

        import asyncio

        async def run():
            assert await middleware._ingress(_Request("ann")) is None
            # A different principal from the same address has its own bucket.
            assert await middleware._ingress(_Request("bo")) is None
            assert await middleware._ingress(_Request("ann")) is not None

        asyncio.run(run())


class TestGeneratedExtractor:
    """G-40: the projection extractor is built with `exec` from a generated
    body. Safe while every fragment is a developer-declared identifier -- and
    nothing said so, let alone checked it."""

    def test_the_generated_body_is_checked(self):
        import inspect

        from wreath.orm import compiler

        source = inspect.getsource(compiler)
        assert "exec(" in source
        # The precondition is asserted, not assumed.
        assert "isidentifier" in source or "_safe_attribute" in source


class TestDrainReleasesUnstartedClaims:
    """G-17: `drain` waits for in-flight handlers, but a job that was claimed
    and not yet started stays `leased` until its lease expires -- so a rolling
    deploy parks work for `lease` seconds per restart."""

    async def test_a_claimed_but_unstarted_job_is_released(self):
        import asyncio

        from wreath.jobs import JobRunner, _Claimed

        released: list[tuple[str, tuple]] = []

        class _Connection:
            async def execute(self, sql, *args):
                released.append((sql, args))
                return "OK"

        class _Database:
            async def acquire(self, workload):
                return _Connection()

            async def release(self, workload, connection):
                pass

        runner = JobRunner(_Database(), name="work")
        runner._claimed_not_started.append(
            _Claimed(id=5, task="t", args=[], tenant="", attempts=0,
                     max_attempts=3, fence=2, key=None)
        )
        await runner.drain(asyncio.get_running_loop().time())
        assert any("state='ready'" in sql for sql, _args in released), (
            "an unstarted claim was left leased"
        )
