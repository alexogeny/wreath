"""What a shared response cache must refuse (report 23: R-09, R-10, R-12, R-76)."""

from __future__ import annotations

from wreath import Wreath
from wreath.auth import Identity
from wreath.response_cache import cached
from wreath.testing import TestClient


class _Backend:
    """Authenticates whoever the `x-user` header names."""

    async def authenticate(self, request):
        who = request.header("x-user")
        return None if who is None else Identity(id=who, roles=frozenset({"member"}))

    def challenge(self, request):
        return "Bearer"


class TestVaryIsHonoured:
    """R-09 / R-76: `Vary` is ignored by the key, so one entry is served to
    every variant -- including a msgpack body handed to a JSON client, because
    `serialize()` sets `Vary: Accept` and the key never mentions it."""

    async def test_a_varying_response_is_not_cached_under_one_key(self):
        app = Wreath()

        @app.get("/report")
        @cached(ttl=60)
        async def report(request):
            from wreath.negotiation import serialize

            return serialize(request, {"rows": [1]})

        async with TestClient(app) as client:
            first = await client.get("/report", headers={"accept": "application/json"})
            second = await client.get("/report", headers={"accept": "application/msgpack"})

        assert first.header("content-type").startswith("application/json")
        assert second.header("content-type").startswith("application/msgpack")

    async def test_a_response_without_vary_still_caches(self):
        calls = 0
        app = Wreath()

        @app.get("/plain")
        @cached(ttl=60)
        async def plain(request):
            nonlocal calls
            calls += 1
            return {"n": calls}

        async with TestClient(app) as client:
            await client.get("/plain")
            await client.get("/plain")
        assert calls == 1


class TestPublicKeyAndPrincipals:
    """R-10: the default key is method+path+query with no principal, so
    `@cached` on an authenticated route serves one caller's body to the next."""

    async def test_the_public_key_refuses_an_authenticated_request(self):
        app = Wreath()
        app.configure_auth(_Backend())

        from wreath.auth import authenticated

        @app.get("/me")
        @authenticated()
        @cached(ttl=60)
        async def me(request):
            return {"who": request.identity.id}

        async with TestClient(app) as client:
            first = await client.get("/me", headers={"x-user": "ann"})
            second = await client.get("/me", headers={"x-user": "bo"})

        assert first.json() == {"who": "ann"}
        assert second.json() == {"who": "bo"}, "one principal was served another's body"

    async def test_a_declared_query_key_refuses_an_authenticated_request(self):
        app = Wreath()
        app.configure_auth(_Backend())

        from wreath.auth import authenticated

        @app.get("/me")
        @authenticated()
        @cached(ttl=60, query_params=("view",))
        async def me(request):
            return {"who": request.identity.id}

        async with TestClient(app) as client:
            first = await client.get(
                "/me?view=profile", headers={"x-user": "ann"}
            )
            second = await client.get(
                "/me?view=profile", headers={"x-user": "bo"}
            )

        assert first.json() == {"who": "ann"}
        assert second.json() == {"who": "bo"}, "one principal was served another's body"

    async def test_an_explicit_declared_query_key_refuses_authentication(self):
        from wreath.auth import authenticated
        from wreath.response_cache import cache_key_for

        app = Wreath()
        app.configure_auth(_Backend())

        @app.get("/me")
        @authenticated()
        @cached(ttl=60, key=cache_key_for(("view",)))
        async def me(request):
            return {"who": request.identity.id}

        async with TestClient(app) as client:
            first = await client.get(
                "/me?view=profile", headers={"x-user": "ann"}
            )
            second = await client.get(
                "/me?view=profile", headers={"x-user": "bo"}
            )

        assert first.json() == {"who": "ann"}
        assert second.json() == {"who": "bo"}

    async def test_a_principal_scoped_key_still_caches_per_caller(self):
        calls = 0
        app = Wreath()
        app.configure_auth(_Backend())

        from wreath.auth import authenticated

        @app.get("/mine")
        @authenticated()
        @cached(ttl=60, key=lambda r: f"{r.identity.id}:{r.path}")
        async def mine(request):
            nonlocal calls
            calls += 1
            return {"who": request.identity.id}

        async with TestClient(app) as client:
            assert (await client.get("/mine", headers={"x-user": "ann"})).json() == {"who": "ann"}
            assert (await client.get("/mine", headers={"x-user": "ann"})).json() == {"who": "ann"}
            assert (await client.get("/mine", headers={"x-user": "bo"})).json() == {"who": "bo"}
        assert calls == 2, "a principal-scoped key must still cache per principal"


class TestCacheBusting:
    """R-12: the key carries the whole query string, so an unauthenticated
    caller can vary it and evict every real entry."""

    async def test_only_declared_query_parameters_reach_the_key(self):
        calls = 0
        app = Wreath()

        @app.get("/list")
        @cached(ttl=60, query_params=("page",))
        async def listing(request):
            nonlocal calls
            calls += 1
            return {"n": calls}

        async with TestClient(app) as client:
            await client.get("/list?page=1")
            await client.get("/list?page=1&_=99999")
            await client.get("/list?page=1&utm_source=x")
        assert calls == 1, "an undeclared parameter created a new cache entry"
        assert listing.cache_store.stats.size == 1

    async def test_a_declared_parameter_still_separates_entries(self):
        calls = 0
        app = Wreath()

        @app.get("/list")
        @cached(ttl=60, query_params=("page",))
        async def listing(request):
            nonlocal calls
            calls += 1
            return {"n": calls}

        async with TestClient(app) as client:
            await client.get("/list?page=1")
            await client.get("/list?page=2")
        assert calls == 2

    def test_declared_parameters_are_order_independent(self):
        from wreath.response_cache import cache_key_for

        class _Request:
            method = "GET"
            path = "/x"

            def __init__(self, query):
                self.query_string = query

        key = cache_key_for(("a", "b"))
        assert key(_Request(b"a=1&b=2")) == key(_Request(b"b=2&a=1"))

    def test_encoded_delimiters_do_not_collide(self):
        from wreath.response_cache import cache_key_for

        class _Request:
            method = "GET"
            path = "/document"

            def __init__(self, query):
                self.query_string = query

        key = cache_key_for(("tenant", "document"))
        victim = _Request(b"tenant=acme&document=payroll")
        attacker = _Request(b"tenant=acme%26document%3Dpayroll")
        assert key(victim) != key(attacker)
