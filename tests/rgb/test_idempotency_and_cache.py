"""Idempotency replay policy and response-cache snapshots (report 23: R-04,
R-05, R-11, G-04, G-10, and R-79 found while testing R-04)."""

from __future__ import annotations

from wreath import Wreath
from wreath.auth import Identity
from wreath.middleware.idempotency import IdempotencyMiddleware
from wreath.response import Response
from wreath.response_cache import cached
from wreath.testing import TestClient


class _Backend:
    async def authenticate(self, request):
        return Identity(id="u1", roles=frozenset({"member"}))

    def challenge(self, request):
        return "Bearer"


def _app_with_idempotency(handler_factory):
    app = Wreath()
    app.configure_auth(_Backend())
    app.add_middleware(IdempotencyMiddleware())
    from wreath.auth import authenticated

    @app.post("/orders")
    @authenticated()
    async def orders(request):
        return handler_factory()

    return app


class TestIdempotencyReachesAnIdentity:
    """R-79: `_key` needs `request.identity`, but a global `before` hook runs at
    ingress -- before the pipeline authenticates anyone -- so the identity is
    always None there and the middleware guards nothing at all. This is the
    failure `RateLimitMiddleware` refuses at startup for `principal_key`."""

    async def test_a_repeated_key_replays_instead_of_re_running(self):
        runs = 0

        def handler():
            nonlocal runs
            runs += 1
            return {"order": runs}

        app = _app_with_idempotency(handler)
        async with TestClient(app) as client:
            headers = {"idempotency-key": "abc"}
            first = await client.post("/orders", headers=headers)
            second = await client.post("/orders", headers=headers)

        assert first.json() == {"order": 1}
        assert runs == 1, "the handler ran again, so the key guarded nothing"
        assert second.json() == {"order": 1}
        assert second.header("idempotency-replayed") == "true"


class TestIdempotencyReplayPolicy:
    async def test_a_client_error_is_not_replayed_for_the_whole_ttl(self):
        """R-04: only >= 500 releases the key, so one 429/401/422 poisons that
        Idempotency-Key for 24 hours."""
        statuses = iter([429, 200])

        def handler():
            return Response(b"{}", status=next(statuses), media_type=b"application/json")

        app = _app_with_idempotency(handler)
        async with TestClient(app) as client:
            headers = {"idempotency-key": "abc"}
            first = await client.post("/orders", headers=headers)
            second = await client.post("/orders", headers=headers)

        assert first.status == 429
        assert second.status == 200, "the 429 was replayed instead of retried"

    async def test_a_replay_does_not_re_send_set_cookie(self):
        """R-05: stored headers are replayed verbatim, cookies included."""

        def handler():
            response = Response(b"{}", media_type=b"application/json")
            response.set_cookie("session", "s3cret")
            return response

        app = _app_with_idempotency(handler)
        async with TestClient(app) as client:
            headers = {"idempotency-key": "abc"}
            await client.post("/orders", headers=headers)
            second = await client.post("/orders", headers=headers)

        assert second.header("idempotency-replayed") == "true"
        assert second.header("set-cookie") is None

    async def test_an_in_flight_conflict_says_when_to_retry(self):
        """G-04: the 409 carries no Retry-After."""
        from wreath.middleware.idempotency import MemoryIdempotencyStore

        store = MemoryIdempotencyStore()
        await store.reserve("k")  # claim it, leaving it in flight
        middleware = IdempotencyMiddleware(store=store)

        class _Request:
            method = "POST"
            path = "/orders"
            identity = Identity(id="u1")

            def header(self, name, default=None):
                return "abc" if name == "idempotency-key" else default

        # The key the middleware derives is not "k", so claim the real one too.
        key = middleware._key(_Request())
        await store.reserve(key)
        response = await middleware.action(_Request())
        assert response.status == 409
        assert any(name.lower() == b"retry-after" for name, _ in response.headers)


class TestResponseCacheSnapshot:
    async def test_a_cached_mapping_is_not_shared_by_reference(self):
        """R-11: `_snapshot` stores the handler's own dict, so a later mutation
        of it rewrites what every later caller is served."""
        rows = {"rows": [1, 2, 3]}
        app = Wreath()

        @app.get("/report")
        @cached(ttl=60)
        async def report(request):
            return rows

        async with TestClient(app) as client:
            first = await client.get("/report")
        assert first.json() == {"rows": [1, 2, 3]}

        # The handler kept a reference to what it returned, as any handler
        # serving a memoised or module-level structure does.
        rows["rows"].append(999)

        async with TestClient(app) as client:
            second = await client.get("/report")
        assert second.json() == {"rows": [1, 2, 3]}, "the stored entry aliased the handler's object"

    async def test_two_hits_do_not_share_one_mutable_object(self):
        """The other direction: whatever a hit hands back must not be the entry."""
        from wreath.response_cache import _revive, _snapshot

        entry = _snapshot({"rows": [1]})
        first = _revive(entry)
        first["rows"].append(2)
        assert _revive(entry) == {"rows": [1]}

    async def test_a_redirect_is_not_cached(self):
        """G-10: `status < 400` admits a 3xx whose Location can be per-user."""
        from wreath.response import RedirectResponse

        seen = 0

        app = Wreath()

        @app.get("/go")
        @cached(ttl=60)
        async def go(request):
            nonlocal seen
            seen += 1
            return RedirectResponse(f"/user/{seen}")

        async with TestClient(app) as client:
            first = await client.get("/go")
            second = await client.get("/go")

        assert first.header("location") == "/user/1"
        assert second.header("location") == "/user/2", "a redirect was cached"
