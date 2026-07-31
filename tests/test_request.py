"""Request header lookup and JSON decoding regressions.

``Request.header`` serves the first lookup with a single list scan and only
builds the header map when a second lookup proves it worthwhile; both paths
must agree, including first-value-wins duplicate handling.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath.exceptions import ClientDisconnect, PayloadTooLarge
from wreath.request import Request, RequestLimits, StreamConsumed, _multipart_boundary

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
    decoded = await request.json()
    assert decoded == {"k": [1, 2.5, None]}
    assert await request.json() is decoded


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
async def test_request_stream_yields_transport_chunks_without_materialising() -> None:
    request = _limited_request(
        [b"first", b"-second", b"-third"], RequestLimits(max_body_bytes=64)
    )

    chunks = [chunk async for chunk in request.stream()]

    assert chunks == [b"first", b"-second", b"-third"]
    with pytest.raises(StreamConsumed):
        await request.body()


@pytest.mark.asyncio
async def test_stream_replays_a_body_that_was_already_buffered() -> None:
    request = _limited_request([b"one", b"two"], RequestLimits(max_body_bytes=64))

    assert await request.body() == b"onetwo"
    assert [chunk async for chunk in request.stream()] == [b"onetwo"]


@pytest.mark.asyncio
async def test_a_second_stream_is_refused_while_the_first_is_active() -> None:
    request = _limited_request([b"one", b"two"], RequestLimits(max_body_bytes=64))
    first = request.stream()

    assert await anext(first) == b"one"
    with pytest.raises(StreamConsumed):
        await anext(request.stream())
    await first.aclose()


@pytest.mark.asyncio
async def test_stream_ignores_extension_messages_and_reports_disconnect() -> None:
    messages = iter(
        [
            {"type": "http.extension"},
            {"type": "http.disconnect"},
        ]
    )

    async def receive() -> Any:
        return next(messages)

    request = Request({"type": "http", "headers": []}, receive)
    with pytest.raises(ClientDisconnect):
        await request.body()
    assert await request.body() == b""


def test_multipart_boundary_parameter_is_strict_and_supports_quotes() -> None:
    assert _multipart_boundary(b"multipart/form-data; boundary=valid-123") == b"valid-123"
    assert _multipart_boundary(b'multipart/form-data; charset=utf-8; boundary="quoted"') == (
        b"quoted"
    )
    for invalid in (b"", b"x" * 71, b"invalid@character"):
        assert _multipart_boundary(b"multipart/form-data; boundary=" + invalid) is None
    assert _multipart_boundary(b'multipart/form-data; boundary="trailing "') is None
    assert _multipart_boundary(b"multipart/form-data; notboundary=B") is None


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
    with pytest.raises(PayloadTooLarge, match="more than 2 parts") as caught:
        await request.form()
    assert caught.value.status == 413


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
    with pytest.raises(PayloadTooLarge, match="part exceeds 128 bytes") as caught:
        await request.form()
    assert caught.value.status == 413


@pytest.mark.asyncio
async def test_a_multipart_part_header_block_over_the_limit_is_refused() -> None:
    body = (
        b"--B\r\nContent-Disposition: form-data; name=\"f\"\r\n"
        b"X-Padding: " + b"p" * 256 + b"\r\n\r\nv\r\n--B--\r\n"
    )
    request = _limited_request(
        [body],
        RequestLimits(max_part_header_bytes=64),
        [(b"content-type", b"multipart/form-data; boundary=B")],
    )
    with pytest.raises(PayloadTooLarge, match="headers exceed 64 bytes") as caught:
        await request.form()
    assert caught.value.status == 413


@pytest.mark.asyncio
async def test_a_malformed_multipart_body_is_not_a_413() -> None:
    """Only the *limits* became client refusals. A body the parser cannot read
    is a different failure and keeps raising the documented `ValueError`, so the
    message-prefix discrimination cannot quietly swallow one as the other."""
    request = _limited_request(
        [b"--B\r\nnot-a-header-line\r\n\r\nv\r\n--B--\r\n"],
        RequestLimits(),
        [(b"content-type", b"multipart/form-data; boundary=B")],
    )
    with pytest.raises(ValueError) as caught:
        await request.form()
    assert not isinstance(caught.value, PayloadTooLarge)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"--B x\r\nContent-Disposition: form-data; name=\"f\"\r\n\r\nv\r\n--B--\r\n",
        b"--B\r\nContent-Disposition: form-data; name=\"f\"\r\n\r\nunterminated",
    ],
)
async def test_multipart_rejects_bad_boundary_lines_and_unterminated_parts(
    body: bytes,
) -> None:
    request = _limited_request(
        [body],
        RequestLimits(),
        [(b"content-type", b"multipart/form-data; boundary=B")],
    )

    with pytest.raises(ValueError):
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
    assert await request.form() is form
    assert form.files["f"].data == b"hello"
    assert type(form.files["f"].data) is bytes
    assert form["g"] == "v"


@pytest.mark.asyncio
async def test_multipart_parses_incrementally_and_spools_without_buffering_body() -> None:
    body = (
        b"--B\r\nContent-Disposition: form-data; name=\"f\"; filename=\"a.bin\"\r\n"
        b"Content-Type: application/octet-stream\r\n\r\n"
        + b"0123456789" * 20
        + b"\r\n--B\r\nContent-Disposition: form-data; name=\"label\"\r\n\r\nwreath"
        b"\r\n--B--\r\n"
    )
    request = _limited_request(
        [body[index : index + 3] for index in range(0, len(body), 3)],
        RequestLimits(spool_max_bytes=32, max_body_bytes=1024),
        [(b"content-type", b"multipart/form-data; boundary=B")],
    )

    form = await request.form()

    assert form.files["f"].spooled is True
    assert b"".join(form.files["f"].chunks(17)) == b"0123456789" * 20
    assert form["label"] == "wreath"
    with pytest.raises(StreamConsumed):
        await request.body()


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
    """A 413, not a bare `ValueError`. `max_form_fields` exists to refuse
    hostile input, so it has to refuse it as a client error; the codec's
    `ValueError` reached the boundary as an unhandled 500, reporting the
    caller's fault as the server's."""
    request = _limited_request(
        [b"a=1&b=2&c=3&d=4"],
        RequestLimits(max_form_fields=3),
        [(b"content-type", b"application/x-www-form-urlencoded")],
    )
    with pytest.raises(PayloadTooLarge, match="exceeds 3 fields") as caught:
        await request.form()
    assert caught.value.status == 413


@pytest.mark.asyncio
async def test_urlencoded_form_at_the_field_limit_is_accepted() -> None:
    request = _limited_request(
        [b"a=1&b=2&c=3"],
        RequestLimits(max_form_fields=3),
        [(b"content-type", b"application/x-www-form-urlencoded")],
    )
    form = await request.form()
    assert form["a"] == "1" and form["b"] == "2" and form["c"] == "3"


# --- the urlencoded field-count limit refuses as a client error --------------


@pytest.mark.asyncio
async def test_the_field_limit_refusal_reaches_the_client_as_413() -> None:
    """End to end: the refusal has to survive the dispatch error boundary as a
    413 problem document, which is the part a bare `ValueError` could not do."""
    from wreath import Wreath
    from wreath.testing import TestClient

    app = Wreath(limits=RequestLimits(max_form_fields=2))

    @app.post("/form")
    async def endpoint(request: Any) -> Any:
        return dict(await request.form())

    async with TestClient(app) as client:
        response = await client.post(
            "/form",
            headers={"content-type": "application/x-www-form-urlencoded"},
            content=b"a=1&b=2&c=3",
        )

    assert response.status == 413
    assert response.header("content-type") == "application/problem+json"
    assert response.json()["status"] == 413
