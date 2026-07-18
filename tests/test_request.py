"""Request header lookup and JSON decoding regressions.

``Request.header`` serves the first lookup with a single list scan and only
builds the header map when a second lookup proves it worthwhile; both paths
must agree, including first-value-wins duplicate handling.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath.exceptions import PayloadTooLarge
from wreath.request import Request, RequestLimits

HEADERS = [
    (b"host", b"example"),
    (b"accept", b"first"),
    (b"accept", b"second"),
    (b"x-mixed", b"v"),
]


async def _no_receive() -> dict[str, Any]:  # pragma: no cover - never called
    raise AssertionError("receive should not be called")


def _request(headers: list[tuple[bytes, bytes]] | None = HEADERS) -> Request:
    scope: dict[str, Any] = {"type": "http", "method": "GET", "path": "/"}
    if headers is not None:
        scope["headers"] = headers
    return Request(scope, _no_receive)


def test_header_scan_then_map_agree() -> None:
    request = _request()
    # First lookup: scan path. Following lookups: dict path.
    assert request.header("accept") == "first"
    assert request.header("accept") == "first"
    assert request.header("HOST") == "example"
    assert request.header(b"x-mixed") == "v"
    assert request.header("missing") is None
    assert request.header("missing", "fallback") == "fallback"


def test_header_missing_on_first_lookup() -> None:
    request = _request()
    assert request.header("missing", "fallback") == "fallback"
    assert request.header("host") == "example"


def test_header_without_headers_in_scope() -> None:
    request = _request(headers=None)
    assert request.header("host") is None
    assert request.header("host", "d") == "d"


@pytest.mark.asyncio
async def test_json_uses_wreath_decoder() -> None:
    messages = iter(
        [{"type": "http.request", "body": b'{"k": [1, 2.5, null]}', "more_body": False}]
    )

    async def receive() -> dict[str, Any]:
        return next(messages)

    request = Request({"type": "http", "method": "POST", "path": "/"}, receive)
    assert await request.json() == {"k": [1, 2.5, None]}


@pytest.mark.asyncio
async def test_json_malformed_raises_value_error() -> None:
    messages = iter([{"type": "http.request", "body": b'{"k":', "more_body": False}])

    async def receive() -> dict[str, Any]:
        return next(messages)

    request = Request({"type": "http", "method": "POST", "path": "/"}, receive)
    with pytest.raises(ValueError):
        await request.json()


# --- request-local cookie cache ---------------------------------------------
#
# Cookies are parsed once per request and cached, matching the existing
# `_body`/`_header_map` request-local cache conventions. The cache is per
# request object, so it can never leak across requests.

def test_cookies_parse_once_and_return_the_same_object() -> None:
    from wreath._codecs import parse_cookies

    request = _request([(b"host", b"x"), (b"cookie", b"a=1; b=2")])
    first = request.cookies
    assert first == parse_cookies(b"a=1; b=2")
    for _ in range(5):
        assert request.cookies is first


def test_cookies_without_a_cookie_header_cache_one_empty_dict() -> None:
    request = _request([(b"host", b"x")])
    first = request.cookies
    assert first == {}
    assert request.cookies is first  # not a fresh dict per access


def test_duplicate_cookie_first_value_wins_is_unchanged() -> None:
    from wreath._codecs import parse_cookies

    request = _request([(b"cookie", b"a=1; a=2")])
    assert request.cookies == parse_cookies(b"a=1; a=2")
    assert request.cookies["a"] == "1"


def test_header_index_preserves_first_values_and_feeds_cookie_cache() -> None:
    request = _request(
        [
            (b"cookie", b"a=1"),
            (b"cookie", b"a=2"),
            (b"accept", b"first"),
            (b"accept", b"second"),
        ]
    )

    header_map = request._index_headers()
    assert header_map[b"accept"] == b"first"
    assert request.cookies == {"a": "1"}


def test_cookie_cache_is_per_request() -> None:
    one = _request([(b"cookie", b"a=1")])
    two = _request([(b"cookie", b"b=2")])
    assert one.cookies == {"a": "1"}
    assert two.cookies == {"b": "2"}
    assert one.cookies is not two.cookies


def test_cookie_cache_survives_other_request_reads() -> None:
    """Materializing headers or the scope must not invalidate the cache."""
    request = _request([(b"host", b"x"), (b"cookie", b"a=1; b=2")])
    first = request.cookies
    assert request.header("host") == "x"
    assert request.header("cookie") == "a=1; b=2"
    _ = request.headers
    assert request.cookies is first


# --- buffered-body and multipart limits ---------------------------------------


def _chunked_receive(chunks: list[bytes]) -> Any:
    messages: list[dict[str, Any]] = [
        {"type": "http.request", "body": chunk, "more_body": True} for chunk in chunks
    ]
    messages.append({"type": "http.request", "body": b"", "more_body": False})
    iterator = iter(messages)

    async def receive() -> Any:
        return next(iterator)

    return receive


def _limited_request(chunks: list[bytes], limits: RequestLimits, headers: Any = ()) -> Request:
    scope = {"type": "http", "headers": list(headers)}
    return Request(scope, _chunked_receive(chunks), limits=limits)


@pytest.mark.asyncio
async def test_a_body_at_the_limit_is_accepted() -> None:
    request = _limited_request([b"a" * 64], RequestLimits(max_body_bytes=64))
    assert await request.body() == b"a" * 64


@pytest.mark.asyncio
async def test_a_body_one_byte_over_the_limit_is_refused() -> None:
    request = _limited_request([b"a" * 65], RequestLimits(max_body_bytes=64))
    with pytest.raises(PayloadTooLarge) as caught:
        await request.body()
    assert caught.value.status == 413


@pytest.mark.asyncio
async def test_the_body_limit_applies_across_chunk_boundaries() -> None:
    """No single chunk is over the limit; their sum is."""
    request = _limited_request([b"a" * 40] * 3, RequestLimits(max_body_bytes=64))
    with pytest.raises(PayloadTooLarge):
        await request.body()


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused_before_it_is_all_buffered() -> None:
    """The limit must stop the stream, not filter it after the fact."""
    delivered = 0

    async def receive() -> Any:
        nonlocal delivered
        delivered += 1
        return {"type": "http.request", "body": b"a" * 32, "more_body": True}

    request = Request(
        {"type": "http", "headers": []}, receive, limits=RequestLimits(max_body_bytes=64)
    )
    with pytest.raises(PayloadTooLarge):
        await request.body()
    # Refused on the chunk that crossed the limit, not after draining forever.
    assert delivered == 3


@pytest.mark.asyncio
async def test_a_multipart_form_over_the_part_limit_is_refused() -> None:
    boundary = b"B"
    body = b"".join(
        b"--B\r\nContent-Disposition: form-data; name=\"f%d\"\r\n\r\nv\r\n" % index
        for index in range(4)
    ) + b"--B--\r\n"
    request = _limited_request(
        [body],
        RequestLimits(max_parts=2),
        [(b"content-type", b"multipart/form-data; boundary=" + boundary)],
    )
    with pytest.raises(ValueError, match="more than 2 parts"):
        await request.form()


@pytest.mark.asyncio
async def test_a_multipart_part_over_the_in_memory_budget_is_refused() -> None:
    body = (
        b"--B\r\nContent-Disposition: form-data; name=\"f\"; filename=\"a.bin\"\r\n"
        b"\r\n" + b"x" * 512 + b"\r\n--B--\r\n"
    )
    request = _limited_request(
        [body],
        RequestLimits(max_part_bytes=128),
        [(b"content-type", b"multipart/form-data; boundary=B")],
    )
    with pytest.raises(ValueError, match="part exceeds 128 bytes"):
        await request.form()


@pytest.mark.asyncio
async def test_a_valid_multipart_form_still_yields_exact_bytes() -> None:
    body = (
        b"--B\r\nContent-Disposition: form-data; name=\"f\"; filename=\"a.bin\"\r\n"
        b"\r\nhello\r\n--B\r\nContent-Disposition: form-data; name=\"g\"\r\n\r\nv\r\n"
        b"--B--\r\n"
    )
    request = _limited_request(
        [body],
        RequestLimits(),
        [(b"content-type", b"multipart/form-data; boundary=B")],
    )
    form = await request.form()
    assert form.files["f"].data == b"hello"
    assert type(form.files["f"].data) is bytes
    assert form["g"] == "v"


def test_request_limits_reject_a_non_positive_value() -> None:
    with pytest.raises(ValueError, match="max_body_bytes must be positive"):
        RequestLimits(max_body_bytes=0)


@pytest.mark.asyncio
async def test_multipart_rejects_aggregate_retained_payload_limit() -> None:
    # Two parts, each within max_part_bytes and under max_parts, but together
    # over the aggregate in-memory cap. The second part crosses it.
    body = (
        b"--B\r\nContent-Disposition: form-data; name=\"f\"; filename=\"a.bin\"\r\n\r\n"
        + b"x" * 512 + b"\r\n"
        b"--B\r\nContent-Disposition: form-data; name=\"g\"; filename=\"b.bin\"\r\n\r\n"
        + b"y" * 512 + b"\r\n--B--\r\n"
    )
    request = _limited_request(
        [body],
        RequestLimits(max_form_memory_bytes=800),  # < 512 + 512
        [(b"content-type", b"multipart/form-data; boundary=B")],
    )
    with pytest.raises(PayloadTooLarge, match="form"):
        await request.form()


@pytest.mark.asyncio
async def test_multipart_at_the_aggregate_limit_is_accepted() -> None:
    body = (
        b"--B\r\nContent-Disposition: form-data; name=\"f\"; filename=\"a.bin\"\r\n\r\n"
        + b"x" * 400 + b"\r\n"
        b"--B\r\nContent-Disposition: form-data; name=\"g\"; filename=\"b.bin\"\r\n\r\n"
        + b"y" * 400 + b"\r\n--B--\r\n"
    )
    request = _limited_request(
        [body],
        RequestLimits(max_form_memory_bytes=800),  # == 400 + 400
        [(b"content-type", b"multipart/form-data; boundary=B")],
    )
    form = await request.form()
    assert form.files["f"].data == b"x" * 400
    assert form.files["g"].data == b"y" * 400


@pytest.mark.asyncio
async def test_urlencoded_form_rejects_too_many_fields() -> None:
    request = _limited_request(
        [b"a=1&b=2&c=3&d=4"],
        RequestLimits(max_form_fields=3),
        [(b"content-type", b"application/x-www-form-urlencoded")],
    )
    with pytest.raises(ValueError, match="exceeds 3 fields"):
        await request.form()


@pytest.mark.asyncio
async def test_urlencoded_form_at_the_field_limit_is_accepted() -> None:
    request = _limited_request(
        [b"a=1&b=2&c=3"],
        RequestLimits(max_form_fields=3),
        [(b"content-type", b"application/x-www-form-urlencoded")],
    )
    form = await request.form()
    assert form["a"] == "1" and form["b"] == "2" and form["c"] == "3"
