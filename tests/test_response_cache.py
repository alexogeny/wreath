from __future__ import annotations

from typing import cast

import pytest

from wreath.response import Response
from wreath.response_cache import cache_key_for, cached, default_cache_key


class _Req:
    def __init__(self, method="GET", path="/", query=b""):
        self.method = method
        self.path = path
        self.query_string = query


class _QueryReq(_Req):
    def __init__(
        self,
        body: bytes,
        *,
        content_type: bytes = b"application/json",
        content_encoding: bytes | None = None,
        content_language: bytes | None = None,
        content_location: bytes | None = None,
    ) -> None:
        super().__init__(method="QUERY", path="/search")
        self._body = body
        self.body_reads = 0
        self.headers = [(b"content-type", content_type)]
        if content_encoding is not None:
            self.headers.append((b"content-encoding", content_encoding))
        if content_language is not None:
            self.headers.append((b"content-language", content_language))
        if content_location is not None:
            self.headers.append((b"content-location", content_location))

    async def body(self) -> bytes:
        self.body_reads += 1
        return self._body


pytestmark = pytest.mark.asyncio


async def test_default_key_has_no_dangling_query_separator() -> None:
    assert default_cache_key(_Req(method="GET", path="/treks")) == "GET /treks"
    assert (
        default_cache_key(_Req(method="GET", path="/treks", query=b"page=2")) == "GET /treks?page=2"
    )


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
    assert calls == 1  # handler ran once
    assert handler.cache_store.stats.hits == 1


async def test_cache_status_distinguishes_a_stored_miss_from_a_hit() -> None:
    @cached(ttl=100, cache_status="Wreath")
    async def handler(request):
        return Response(b"body")

    miss = await handler(_Req())
    hit = await handler(_Req())

    assert (b"cache-status", b'"Wreath";fwd=uri-miss;stored') in miss.headers
    assert (b"cache-status", b'"Wreath";hit') in hit.headers
    assert all(b"GET /" not in value for name, value in hit.headers if name == b"cache-status")


async def test_cache_status_reports_method_and_policy_bypasses() -> None:
    @cached(ttl=100, cache_status="Wreath")
    async def handler(request):
        return Response(b"body")

    method = await handler(_Req(method="POST"))
    policy = await handler(_Identified("ada"))

    assert (b"cache-status", b'"Wreath";fwd=method') in method.headers
    assert (b"cache-status", b'"Wreath";fwd=bypass') in policy.headers


async def test_cache_status_is_opt_in() -> None:
    @cached(ttl=100)
    async def handler(request):
        return Response(b"body")

    response = await handler(_Req())

    assert not [value for name, value in response.headers if name == b"cache-status"]


async def test_invalid_cache_status_identifier_is_refused_when_declared() -> None:
    with pytest.raises(ValueError, match="structured string"):
        cached(ttl=100, cache_status="caf\N{LATIN SMALL LETTER E WITH ACUTE}")

    with pytest.raises(TypeError, match="cache_status.*must be str, got int"):
        cached(ttl=100, cache_status=cast(str, 7))


async def test_query_string_is_part_of_the_key() -> None:
    calls = 0

    @cached
    async def handler(request):
        nonlocal calls
        calls += 1
        return Response(b"x")

    await handler(_Req(query=b"a=1"))
    await handler(_Req(query=b"a=2"))
    assert calls == 2  # different query -> different key


async def test_query_cache_keys_include_content_and_representation_metadata() -> None:
    calls = 0

    @cached(ttl=100, key=lambda request: "same-base-key")
    async def handler(request):
        nonlocal calls
        calls += 1
        return Response(f"response-{calls}".encode())

    first = _QueryReq(b'{"term":"one"}')
    repeated = _QueryReq(b'{"term":"one"}')
    other_body = _QueryReq(b'{"term":"two"}')
    other_type = _QueryReq(b'{"term":"one"}', content_type=b"application/cbor")
    other_encoding = _QueryReq(b'{"term":"one"}', content_encoding=b"gzip")
    other_language = _QueryReq(b'{"term":"one"}', content_language=b"mi")
    other_location = _QueryReq(b'{"term":"one"}', content_location=b"/queries/one")
    normalized_metadata = _QueryReq(
        b'{"term":"one"}',
        content_type=b" Application/JSON ",
    )

    assert (await handler(first)).body == b"response-1"
    assert (await handler(repeated)).body == b"response-1"
    assert (await handler(other_body)).body == b"response-2"
    assert (await handler(other_type)).body == b"response-3"
    assert (await handler(other_encoding)).body == b"response-4"
    assert (await handler(other_language)).body == b"response-5"
    assert (await handler(other_location)).body == b"response-6"
    assert (await handler(normalized_metadata)).body == b"response-1"
    assert calls == 6
    requests = (
        first,
        repeated,
        other_body,
        other_type,
        other_encoding,
        other_language,
        other_location,
        normalized_metadata,
    )
    assert [request.body_reads for request in requests] == [
        1,
        1,
        1,
        1,
        1,
        1,
        1,
        1,
    ]


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


@pytest.mark.parametrize(
    "response",
    [
        Response(b"err", status=404),
        Response(b"x", headers=[(b"set-cookie", b"sid=1")]),
        Response(b"x", headers=[(b"cache-control", b"no-store")]),
        Response(b"x", headers=[(b"cache-control", b"private, max-age=60")]),
    ],
)
async def test_unsafe_responses_are_never_cached(response) -> None:
    calls = 0

    @cached
    async def handler(request):
        nonlocal calls
        calls += 1
        return response

    await handler(_Req())
    await handler(_Req())
    assert calls == 2  # not cached -> handler re-ran


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
    handler.invalidate()  # clear all
    await handler(_Req())
    assert calls == 2


async def test_cached_hit_has_isolated_headers() -> None:
    @cached
    async def handler(request):
        return Response(b"x", headers=[(b"x-a", b"1")])

    first = await handler(_Req())
    first.headers.append((b"x-mutated", b"1"))  # a middleware might do this
    second = await handler(_Req())
    assert (b"x-mutated", b"1") not in second.headers  # cache not poisoned


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


class _Identified(_Req):
    def __init__(self, who: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.identity = who


async def test_an_identified_caller_bypasses_a_publicly_keyed_cache() -> None:
    calls: list[str | None] = []

    @cached(ttl=100)
    async def handler(request):
        calls.append(getattr(request, "identity", None))
        return Response(f"for-{getattr(request, 'identity', 'anon')}".encode())

    ada = await handler(_Identified("ada"))
    bo = await handler(_Identified("bo"))
    assert ada.body == b"for-ada"
    assert bo.body == b"for-bo"  # not ada's response
    assert calls == ["ada", "bo"]  # the handler ran for each
    assert handler.cache_store.stats.hits == 0

    # And nothing they produced was left behind for an anonymous caller.
    anon = await handler(_Req())
    assert anon.body == b"for-anon"
    assert calls == ["ada", "bo", None]


async def test_an_anonymous_caller_still_gets_the_shared_cache() -> None:
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
    assert calls == 1  # cached, because the key names them
    assert (await handler(_Identified("bo"))).body == b"for-bo"
    assert calls == 2  # ... and bo is a different key


async def test_a_key_marked_public_is_treated_as_shared_even_though_it_is_custom() -> None:
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
    assert calls == 2  # both bypassed, neither cached
    assert handler.cache_store.stats.hits == 0


async def test_key_and_query_params_together_are_refused() -> None:
    with pytest.raises(ValueError, match="not both"):
        cached(ttl=100, key=lambda request: "k", query_params=("a",))


async def test_query_params_builds_a_key_from_the_named_parameters_only() -> None:
    calls = 0

    @cached(ttl=100, query_params=("a",))
    async def handler(request):
        nonlocal calls
        calls += 1
        return Response(b"x")

    await handler(_Req(query=b"a=1&b=1"))
    await handler(_Req(query=b"a=1&b=2"))  # `b` is not in the key
    assert calls == 1
    await handler(_Req(query=b"a=2&b=1"))
    assert calls == 2


@pytest.mark.parametrize(
    ("declared", "query"),
    [
        (("q",), b"q=first&q=second"),
        (("q", "missing"), b"q=hello+world"),
        (("empty",), b"empty="),
        (("unicode",), b"unicode=na%C3%AFve"),
        (("reserved",), b"reserved=%2F%3F%26%3D%2B"),
        (("bad",), b"bad=%FF"),
        (("raw",), b"raw=\xff"),
        (("space name",), b"space+name=a+b"),
        ((), b"ignored=1"),
    ],
)
async def test_declared_query_key_matches_stdlib_form_semantics(
    declared: tuple[str, ...], query: bytes
) -> None:
    from urllib.parse import parse_qsl, urlencode

    request = _Req(method="PATCH", path="/search", query=query)
    parsed: dict[str, str] = {}
    for raw_name, raw_value in parse_qsl(query, keep_blank_values=True):
        name = raw_name.decode("utf-8", "replace")
        parsed.setdefault(name, raw_value.decode("utf-8", "replace"))
    selected = [(name, parsed[name]) for name in declared if name in parsed]
    suffix = urlencode(selected)
    expected = f"PATCH /search?{suffix}" if suffix else "PATCH /search"
    assert cache_key_for(declared)(request) == expected


async def test_declared_query_names_are_refused_when_declared() -> None:
    with pytest.raises(TypeError, match=r"cache_key_for names\[1\] must be str, got int"):
        cache_key_for(("valid", 7))  # ty: ignore[invalid-argument-type]


async def test_concurrent_callers_of_a_cold_key_share_one_computation() -> None:
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
    await asyncio.sleep(0)  # let the second reach the wait
    release.set()

    assert (await first).body == b"slow"
    assert (await second).body == b"slow"
    assert calls == 1  # one computation, two callers
