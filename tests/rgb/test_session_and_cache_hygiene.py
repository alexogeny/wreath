"""Session cookie hygiene, cache stampede, and small correctness items
(report 23: R-07, R-08, R-34, R-47, G-07, G-57, G-58)."""

from __future__ import annotations

import asyncio

import pytest

from wreath.middleware.sessions import SessionMiddleware

_SECRET = "s" * 32
_OLD = "o" * 32


class _Response:
    def __init__(self):
        self.headers: list[tuple[bytes, bytes]] = []
        self.deleted: list[str] = []
        self.set: list[tuple[str, str]] = []

    def set_cookie(self, name, value, **kwargs):
        self.set.append((name, value))

    def delete_cookie(self, name, **kwargs):
        self.deleted.append(name)


class _State:
    def __init__(self, **values):
        self.__dict__.update(values)

    def get(self, name, default=None):
        return self.__dict__.get(name, default)


class _Request:
    def __init__(self, cookies=None, state=None):
        self.cookies = cookies or {}
        self.state = state if state is not None else _State()


class TestAnonymousResponsesStayClean:
    """G-57: an empty session emits `delete_cookie` on every response, so the
    anonymous path -- the common one -- carries a pointless `Set-Cookie` that
    also makes the response uncacheable."""

    async def test_no_cookie_in_and_no_session_means_no_cookie_out(self):
        middleware = SessionMiddleware(secret=_SECRET)
        request = _Request()
        await middleware.before(request)
        response = await middleware.after(request, _Response())
        assert response.deleted == [], "a cookie was cleared that was never set"
        assert response.set == []

    async def test_an_emptied_session_is_still_cleared(self):
        middleware = SessionMiddleware(secret=_SECRET)
        request = _Request()
        await middleware.before(request)
        request.state.session["a"] = 1
        first = await middleware.after(request, _Response())
        assert first.set, "a written session must set a cookie"

        # Round two: the client sends it back and the handler empties it.
        _name, value = first.set[0]
        second_request = _Request(cookies={"wreath_session": value})
        await middleware.before(second_request)
        second_request.state.session.clear()
        second = await middleware.after(second_request, _Response())
        assert second.deleted == ["wreath_session"]


class TestSecretRotation:
    """G-58: one secret and no accept-old-sign-new list, so rotating it logs
    every user out."""

    async def test_a_cookie_signed_with_a_previous_secret_is_accepted(self):
        old = SessionMiddleware(secret=_OLD)
        request = _Request()
        await old.before(request)
        request.state.session["who"] = "ann"
        response = await old.after(request, _Response())
        _name, cookie = response.set[0]

        rotated = SessionMiddleware(secret=_SECRET, previous_secrets=[_OLD])
        carried = _Request(cookies={"wreath_session": cookie})
        await rotated.before(carried)
        assert carried.state.session == {"who": "ann"}

    async def test_it_is_re_signed_with_the_current_secret(self):
        old = SessionMiddleware(secret=_OLD)
        request = _Request()
        await old.before(request)
        request.state.session["who"] = "ann"
        _name, cookie = (await old.after(request, _Response())).set[0]

        rotated = SessionMiddleware(secret=_SECRET, previous_secrets=[_OLD])
        carried = _Request(cookies={"wreath_session": cookie})
        await rotated.before(carried)
        carried.state.session["who"] = "bo"
        reissued = await rotated.after(carried, _Response())
        assert reissued.set, "the session must be re-signed with the current secret"

        # And the reissued cookie verifies under the *new* secret alone.
        current_only = SessionMiddleware(secret=_SECRET)
        again = _Request(cookies={"wreath_session": reissued.set[0][1]})
        await current_only.before(again)
        assert again.state.session == {"who": "bo"}

    async def test_an_unknown_secret_is_still_rejected(self):
        old = SessionMiddleware(secret=_OLD)
        request = _Request()
        await old.before(request)
        request.state.session["who"] = "ann"
        _name, cookie = (await old.after(request, _Response())).set[0]

        stranger = SessionMiddleware(secret="z" * 32)
        carried = _Request(cookies={"wreath_session": cookie})
        await stranger.before(carried)
        assert carried.state.session == {}


class TestCacheStampede:
    """G-07: N concurrent misses all run the handler, which is the pile-up a
    cache in front of an expensive rollup exists to prevent."""

    async def test_concurrent_misses_run_the_handler_once(self):
        from wreath.response_cache import cached

        runs = 0

        @cached(ttl=60)
        async def report(request):
            nonlocal runs
            runs += 1
            await asyncio.sleep(0.05)
            return {"n": runs}

        class _Req:
            method = "GET"
            path = "/report"
            query_string = b""
            identity = None

        results = await asyncio.gather(*(report(_Req()) for _ in range(8)))
        assert runs == 1, f"the handler ran {runs} times for one cold key"
        assert all(result == {"n": 1} for result in results)

    async def test_a_failing_handler_does_not_wedge_the_key(self):
        from wreath.response_cache import cached

        calls = 0

        @cached(ttl=60)
        async def flaky(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("cold")
            return {"ok": True}

        class _Req:
            method = "GET"
            path = "/flaky"
            query_string = b""
            identity = None

        with pytest.raises(RuntimeError):
            await flaky(_Req())
        assert await flaky(_Req()) == {"ok": True}


class TestIdempotencyKeyEdges:
    """R-07: a NUL byte in a header value fails the jsonb insert *after* the
    handler ran. R-08: a non-str `identity.id` reaches `dedup_key` and raises."""

    def test_a_header_with_a_nul_byte_is_not_stored(self):
        from wreath.middleware.idempotency import _replayable_headers

        stored = _replayable_headers([(b"x-note", b"a\x00b"), (b"x-fine", b"ok")])
        assert stored == ((b"x-fine", b"ok"),)

    def test_a_non_string_identity_id_still_keys(self):
        from wreath.middleware.idempotency import IdempotencyMiddleware

        middleware = IdempotencyMiddleware()

        class _Identity:
            id = 7          # an integer primary key is an ordinary choice

        class _Request:
            method = "POST"
            path = "/orders"
            identity = _Identity()

            def header(self, name, default=None):
                return "abc" if name == "idempotency-key" else default

        assert isinstance(middleware._key(_Request()), str)
