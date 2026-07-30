"""The @cached response decorator: hits, TTL, and the safety guards."""
from __future__ import annotations

import pytest

from wreath.response import Response
from wreath.response_cache import cached


class _Req:
    def __init__(self, method="GET", path="/", query=b""):
        self.method = method
        self.path = path
        self.query_string = query


pytestmark = pytest.mark.asyncio


async def test_second_call_is_served_from_cache() -> None:
    calls = 0

    @cached(ttl=100)
    async def handler(request):
        nonlocal calls
        calls += 1
        return Response(b"body", status=200)

    r1 = await handler(_Req())
    r2 = await handler(_Req())
    assert r1.body == r2.body == b"body"
    assert calls == 1                       # handler ran once
    assert handler.cache_store.stats.hits == 1


async def test_query_string_is_part_of_the_key() -> None:
    calls = 0

    @cached
    async def handler(request):
        nonlocal calls
        calls += 1
        return Response(b"x")

    await handler(_Req(query=b"a=1"))
    await handler(_Req(query=b"a=2"))
    assert calls == 2                       # different query -> different key


async def test_non_get_is_not_cached() -> None:
    calls = 0

    @cached(methods=("GET",))
    async def handler(request):
        nonlocal calls
        calls += 1
        return Response(b"x")

    await handler(_Req(method="POST"))
    await handler(_Req(method="POST"))
    assert calls == 2


@pytest.mark.parametrize("response", [
    Response(b"err", status=404),
    Response(b"x", headers=[(b"set-cookie", b"sid=1")]),
    Response(b"x", headers=[(b"cache-control", b"no-store")]),
    Response(b"x", headers=[(b"cache-control", b"private, max-age=60")]),
])
async def test_unsafe_responses_are_never_cached(response) -> None:
    calls = 0

    @cached
    async def handler(request):
        nonlocal calls
        calls += 1
        return response

    await handler(_Req())
    await handler(_Req())
    assert calls == 2                       # not cached -> handler re-ran


async def test_ttl_expiry_reruns_handler() -> None:
    from wreath.cache import BoundedCache

    clock = type("C", (), {"now": 0.0, "__call__": lambda s: s.now})()
    store: BoundedCache = BoundedCache(max_entries=8, ttl=10, clock=clock)
    calls = 0

    @cached(store=store)
    async def handler(request):
        nonlocal calls
        calls += 1
        return Response(b"x")

    await handler(_Req())
    clock.now = 11
    await handler(_Req())
    assert calls == 2


async def test_invalidate_clears_and_targets() -> None:
    calls = 0

    @cached
    async def handler(request):
        nonlocal calls
        calls += 1
        return Response(b"x")

    await handler(_Req())
    handler.invalidate()                    # clear all
    await handler(_Req())
    assert calls == 2


async def test_cached_hit_has_isolated_headers() -> None:
    @cached
    async def handler(request):
        return Response(b"x", headers=[(b"x-a", b"1")])

    first = await handler(_Req())
    first.headers.append((b"x-mutated", b"1"))   # a middleware might do this
    second = await handler(_Req())
    assert (b"x-mutated", b"1") not in second.headers   # cache not poisoned


async def test_dict_return_is_cached() -> None:
    calls = 0

    @cached
    async def handler(request):
        nonlocal calls
        calls += 1
        return {"ok": True}

    a = await handler(_Req())
    b = await handler(_Req())
    assert a == b == {"ok": True} and calls == 1


# --- the cross-user guard, which had no regression test -----------------------
#
# `cached`'s own comment records why this branch exists: "The default key is a
# *shared* key: it carries no principal, so an entry stored for one caller would
# be served to the next. The docstring said to pass a `key` that includes the
# principal; nothing enforced it, and the failure is silent and cross-user."
#
# `wreath mutant` deleted the branch and every test stayed green -- the whole
# file builds requests with no `identity` attribute at all, so the case the
# guard was written for was never presented to it.


class _Identified(_Req):
    def __init__(self, who: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.identity = who


async def test_an_identified_caller_bypasses_a_publicly_keyed_cache() -> None:
    """Not served from it, and not stored into it. Both halves matter.

    Serving is the leak. Storing is the same leak one request later: an
    identified caller's response sitting under a shared key is handed to the
    next anonymous one.
    """
    calls: list[str | None] = []

    @cached(ttl=100)
    async def handler(request):
        calls.append(getattr(request, "identity", None))
        return Response(f"for-{getattr(request, 'identity', 'anon')}".encode())

    ada = await handler(_Identified("ada"))
    bo = await handler(_Identified("bo"))
    assert ada.body == b"for-ada"
    assert bo.body == b"for-bo"              # not ada's response
    assert calls == ["ada", "bo"]            # the handler ran for each
    assert handler.cache_store.stats.hits == 0

    # And nothing they produced was left behind for an anonymous caller.
    anon = await handler(_Req())
    assert anon.body == b"for-anon"
    assert calls == ["ada", "bo", None]


async def test_an_anonymous_caller_still_gets_the_shared_cache() -> None:
    """The guard must not disable caching outright -- that is its failure mode."""
    calls = 0

    @cached(ttl=100)
    async def handler(request):
        nonlocal calls
        calls += 1
        return Response(b"shared")

    await handler(_Req())
    await handler(_Req())
    assert calls == 1
    assert handler.cache_store.stats.hits == 1


async def test_a_custom_key_carrying_the_principal_caches_per_caller() -> None:
    """The documented way to cache an identified response: key on who it is for."""
    calls = 0

    def per_caller(request):
        return f"{request.identity}:{request.path}"

    @cached(ttl=100, key=per_caller)
    async def handler(request):
        nonlocal calls
        calls += 1
        return Response(f"for-{request.identity}".encode())

    assert (await handler(_Identified("ada"))).body == b"for-ada"
    assert (await handler(_Identified("ada"))).body == b"for-ada"
    assert calls == 1                        # cached, because the key names them
    assert (await handler(_Identified("bo"))).body == b"for-bo"
    assert calls == 2                        # ... and bo is a different key


async def test_a_key_marked_public_is_treated_as_shared_even_though_it_is_custom() -> None:
    """`_wreath_public` is the opt-in that says "this key is deliberately shared".

    Without reading it, a custom key that genuinely carries no principal would
    be trusted for identified callers -- which is the exact hole the default key
    is guarded against.
    """
    calls = 0

    def public_key(request):
        return request.path

    public_key._wreath_public = True

    @cached(ttl=100, key=public_key)
    async def handler(request):
        nonlocal calls
        calls += 1
        return Response(b"shared")

    await handler(_Identified("ada"))
    await handler(_Identified("bo"))
    assert calls == 2                        # both bypassed, neither cached
    assert handler.cache_store.stats.hits == 0


async def test_key_and_query_params_together_are_refused() -> None:
    """Two ways of saying what the key is; taking one silently would ignore the other."""
    with pytest.raises(ValueError, match="not both"):
        cached(ttl=100, key=lambda request: "k", query_params=("a",))


async def test_query_params_builds_a_key_from_the_named_parameters_only() -> None:
    """The `query_params=` path, which nothing had constructed."""
    calls = 0

    @cached(ttl=100, query_params=("a",))
    async def handler(request):
        nonlocal calls
        calls += 1
        return Response(b"x")

    await handler(_Req(query=b"a=1&b=1"))
    await handler(_Req(query=b"a=1&b=2"))    # `b` is not in the key
    assert calls == 1
    await handler(_Req(query=b"a=2&b=1"))
    assert calls == 2


async def test_concurrent_callers_of_a_cold_key_share_one_computation() -> None:
    """The in-flight map, which is why an expiring key does not stampede.

    `wreath mutant` deleted the "somebody else is already computing this"
    branch and nothing objected, because every test here awaits its calls one
    at a time.
    """
    import asyncio

    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    @cached(ttl=100)
    async def handler(request):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return Response(b"slow")

    first = asyncio.create_task(handler(_Req()))
    await started.wait()
    second = asyncio.create_task(handler(_Req()))
    await asyncio.sleep(0)                   # let the second reach the wait
    release.set()

    assert (await first).body == b"slow"
    assert (await second).body == b"slow"
    assert calls == 1                        # one computation, two callers
