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
