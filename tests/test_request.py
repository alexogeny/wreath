from __future__ import annotations

from typing import Any, cast

import pytest

from wreath._native import _core
from wreath.exceptions import (
    ClientDisconnect,
    PayloadTooLarge,
    RequestHeaderFieldsTooLarge,
)
from wreath.request import (
    _REQUEST_LAYOUT,
    DEFAULT_LIMITS,
    Request,
    RequestLimits,
    StreamConsumed,
    _multipart_boundary,
    _valid_boundary,
)

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


@pytest.mark.parametrize("scope", [{"type": "http"}, object()])
def test_native_activation_request_matches_public_constructor(scope: Any) -> None:
    params = {"item": "7"}
    app = object()
    public = Request(scope, _no_receive, params, DEFAULT_LIMITS, app=app)
    activated = _core.request_new(_REQUEST_LAYOUT, scope, _no_receive, params, DEFAULT_LIMITS, app)

    assert type(activated) is Request
    missing = object()
    for slot in Request.__slots__:
        activated_value = getattr(activated, slot, missing)
        public_value = getattr(public, slot, missing)
        if slot in {"_client_source", "_policy_mask"}:
            assert activated_value == public_value
        else:
            assert activated_value is public_value


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


def test_present_scope_headers_need_no_eager_fallback() -> None:
    class Scope(dict[str, Any]):
        def get(self, key: str, default: Any = None) -> Any:
            if key == "headers" and isinstance(default, list):
                raise AssertionError("headers allocated an unused fallback list")
            return super().get(key, default)

    headers = [(b"host", b"example")]
    request = Request(Scope(type="http", headers=headers), _no_receive)
    assert request.headers is headers


@pytest.mark.parametrize(
    "attribute",
    ["scope", "method", "path", "client", "scheme", "query_string", "headers"],
)
def test_an_unbacked_request_refuses_scope_dependent_properties(
    attribute: str,
) -> None:
    request = Request(None, _no_receive)

    with pytest.raises(RuntimeError):
        getattr(request, attribute)


def test_an_unbacked_request_refuses_scope_dependent_writes() -> None:
    request = Request(None, _no_receive)

    with pytest.raises(RuntimeError, match="scope is unavailable"):
        request._set_client(("127.0.0.1", 80))
    with pytest.raises(RuntimeError, match="scope is unavailable"):
        request._set_scheme("https")


def test_reverse_urls_require_an_attached_application() -> None:
    request = _request()

    with pytest.raises(RuntimeError, match="not attached"):
        request.url_path_for("missing")
    with pytest.raises(RuntimeError, match="not attached"):
        request.url_for("missing")


def test_an_absolute_reverse_url_requires_a_host_or_server_address() -> None:
    from wreath import Wreath

    app = Wreath()

    @app.get("/items", name="items")
    async def items(request: Any) -> None:
        pass

    request = Request(
        {"type": "http", "scheme": "http", "headers": []},
        _no_receive,
        app=app,
    )

    with pytest.raises(RuntimeError, match="without a host"):
        request.url_for("items")


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
    request = _request([(b"host", b"x"), (b"cookie", b"a=1; b=2")])
    first = request.cookies
    assert request.header("host") == "x"
    assert request.header("cookie") == "a=1; b=2"
    _ = request.headers
    assert request.cookies is first


def test_split_cookie_headers_enforce_the_aggregate_limit() -> None:
    request = Request(
        {
            "type": "http",
            "headers": [(b"cookie", b"a=1"), (b"cookie", b"b=22")],
        },
        _no_receive,
        limits=RequestLimits(max_cookie_bytes=8),
    )

    with pytest.raises(RequestHeaderFieldsTooLarge, match="limit is 8"):
        _ = request.cookies


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
    request = _limited_request([b"a" * 40] * 3, RequestLimits(max_body_bytes=64))
    with pytest.raises(PayloadTooLarge):
        await request.body()


@pytest.mark.asyncio
async def test_request_stream_yields_transport_chunks_without_materialising() -> None:
    request = _limited_request([b"first", b"-second", b"-third"], RequestLimits(max_body_bytes=64))

    chunks = [chunk async for chunk in request.stream()]

    assert chunks == [b"first", b"-second", b"-third"]
    with pytest.raises(StreamConsumed):
        await request.body()


@pytest.mark.asyncio
async def test_stream_refuses_a_chunk_over_the_body_limit() -> None:
    request = _limited_request([b"a" * 65], RequestLimits(max_body_bytes=64))

    with pytest.raises(PayloadTooLarge, match="exceeds 64 bytes"):
        _ = [chunk async for chunk in request.stream()]


@pytest.mark.asyncio
async def test_stream_replays_a_body_that_was_already_buffered() -> None:
    request = _limited_request([b"one", b"two"], RequestLimits(max_body_bytes=64))

    assert await request.body() == b"onetwo"
    assert [chunk async for chunk in request.stream()] == [b"onetwo"]


@pytest.mark.asyncio
async def test_a_second_stream_is_refused_while_the_first_is_active() -> None:
    request = _limited_request([b"one", b"two"], RequestLimits(max_body_bytes=64))
    first = cast(Any, request.stream())

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


def test_multipart_boundary_validation_matches_the_rfc_octet_set() -> None:
    allowed = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'()+_,-./:=? "
    for byte in range(256):
        assert _valid_boundary(b"A" + bytes((byte,)) + b"B") is (byte in allowed)


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused_before_it_is_all_buffered() -> None:
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
    body = (
        b"".join(
            b'--B\r\nContent-Disposition: form-data; name="f%d"\r\n\r\nv\r\n' % index
            for index in range(4)
        )
        + b"--B--\r\n"
    )
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
        b'--B\r\nContent-Disposition: form-data; name="f"; filename="a.bin"\r\n'
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
        b'--B\r\nContent-Disposition: form-data; name="f"\r\n'
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
async def test_an_unterminated_multipart_header_over_the_limit_is_refused() -> None:
    body = b"--B\r\nX-Padding: " + b"p" * 65
    request = _limited_request(
        [body],
        RequestLimits(max_part_header_bytes=64),
        [(b"content-type", b"multipart/form-data; boundary=B")],
    )

    with pytest.raises(PayloadTooLarge, match="headers exceed 64 bytes"):
        await request.form()


@pytest.mark.asyncio
async def test_multipart_content_type_requires_a_boundary() -> None:
    request = _limited_request(
        [b""],
        RequestLimits(),
        [(b"content-type", b"multipart/form-data")],
    )

    with pytest.raises(ValueError, match="without a boundary"):
        await request.form()


@pytest.mark.asyncio
async def test_a_malformed_multipart_body_is_not_a_413() -> None:
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
        b'--B x\r\nContent-Disposition: form-data; name="f"\r\n\r\nv\r\n--B--\r\n',
        b'--B\r\nContent-Disposition: form-data; name="f"\r\n\r\nunterminated',
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
        b'--B\r\nContent-Disposition: form-data; name="f"; filename="a.bin"\r\n'
        b'\r\nhello\r\n--B\r\nContent-Disposition: form-data; name="g"\r\n\r\nv\r\n'
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
        b'--B\r\nContent-Disposition: form-data; name="f"; filename="a.bin"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
        + b"0123456789"
        * 20
        + b'\r\n--B\r\nContent-Disposition: form-data; name="label"\r\n\r\nwreath'
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
        b'--B\r\nContent-Disposition: form-data; name="f"; filename="a.bin"\r\n\r\n'
        + b"x" * 512
        + b"\r\n"
        b'--B\r\nContent-Disposition: form-data; name="g"; filename="b.bin"\r\n\r\n'
        + b"y" * 512
        + b"\r\n--B--\r\n"
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
        b'--B\r\nContent-Disposition: form-data; name="f"; filename="a.bin"\r\n\r\n'
        + b"x" * 400
        + b"\r\n"
        b'--B\r\nContent-Disposition: form-data; name="g"; filename="b.bin"\r\n\r\n'
        + b"y" * 400
        + b"\r\n--B--\r\n"
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


@pytest.mark.asyncio
async def test_the_field_limit_refusal_reaches_the_client_as_413() -> None:
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
