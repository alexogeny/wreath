from __future__ import annotations

from typing import Any

import pytest

from wreath.policy import CsrfPolicy, csrf_token
from wreath.request import Request
from wreath.response import Response


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _request(method: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": "/",
            "query_string": b"",
            "headers": headers or [(b"host", b"example.test")],
        },
        _receive,
    )


def _request_with_body(method: str, body: bytes, headers: list[tuple[bytes, bytes]]) -> Request:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": "/",
            "query_string": b"",
            "headers": headers,
        },
        receive,
    )


@pytest.mark.asyncio
async def test_safe_request_issues_token_and_valid_unsafe_request_passes() -> None:
    middleware = CsrfPolicy("s" * 32)
    safe = _request("GET")
    assert await middleware._ingress(safe) is None
    token = csrf_token(safe)
    response = await middleware._egress(safe, Response(b"ok"))
    cookie = next(value for name, value in response.headers if name == b"set-cookie")
    assert b"HttpOnly" not in cookie
    assert b"Secure" in cookie
    assert b"Domain=" not in cookie

    unsafe = _request(
        "POST",
        [
            (b"host", b"example.test"),
            (b"origin", b"https://example.test"),
            (b"cookie", f"wreath_csrf={token}".encode()),
            (b"x-csrf-token", token.encode()),
        ],
    )
    assert await middleware._ingress(unsafe) is None


@pytest.mark.asyncio
async def test_configured_urlencoded_form_field_can_resubmit_token() -> None:
    middleware = CsrfPolicy("s" * 32, form_field="csrf_token")
    safe = _request("GET")
    assert await middleware._ingress(safe) is None
    token = csrf_token(safe)
    unsafe = _request_with_body(
        "POST",
        f"title=hello&csrf_token={token}".encode(),
        [
            (b"host", b"example.test"),
            (b"origin", b"https://example.test"),
            (b"cookie", f"wreath_csrf={token}".encode()),
            (b"content-type", b"application/x-www-form-urlencoded"),
        ],
    )

    assert await middleware._ingress(unsafe) is None


@pytest.mark.asyncio
async def test_repeated_form_tokens_are_refused_as_ambiguous() -> None:
    middleware = CsrfPolicy("s" * 32, form_field="csrf_token")
    safe = _request("GET")
    await middleware._ingress(safe)
    token = csrf_token(safe)
    unsafe = _request_with_body(
        "POST",
        f"csrf_token={token}&csrf_token={token}".encode(),
        [
            (b"host", b"example.test"),
            (b"origin", b"https://example.test"),
            (b"cookie", f"wreath_csrf={token}".encode()),
            (b"content-type", b"application/x-www-form-urlencoded"),
        ],
    )

    refusal = await middleware._ingress(unsafe)
    assert refusal is not None and refusal.status == 403


@pytest.mark.asyncio
async def test_csrf_header_wins_without_reading_form_body() -> None:
    middleware = CsrfPolicy("s" * 32, form_field="csrf_token")
    safe = _request("GET")
    await middleware._ingress(safe)
    token = csrf_token(safe)

    async def should_not_receive() -> dict[str, Any]:
        raise AssertionError("a present CSRF header must avoid body parsing")

    unsafe = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/",
            "headers": [
                (b"host", b"example.test"),
                (b"origin", b"https://example.test"),
                (b"cookie", f"wreath_csrf={token}".encode()),
                (b"x-csrf-token", token.encode()),
                (b"content-type", b"application/x-www-form-urlencoded"),
            ],
        },
        should_not_receive,
    )

    assert await middleware._ingress(unsafe) is None


@pytest.mark.asyncio
async def test_unsafe_requests_fail_closed_with_generic_response() -> None:
    middleware = CsrfPolicy("s" * 32)
    for headers in (
        [(b"host", b"example.test")],
        [(b"host", b"example.test"), (b"origin", b"https://evil.test")],
    ):
        rejected = await middleware._ingress(_request("POST", headers))
        assert rejected.status == 403
        assert b"CSRF validation failed" in rejected.body


@pytest.mark.asyncio
async def test_valid_unsafe_request_indexes_headers_once() -> None:
    middleware = CsrfPolicy("s" * 32)
    safe = _request("GET")
    assert await middleware._ingress(safe) is None
    token = csrf_token(safe)
    unsafe = _request(
        "POST",
        [
            (b"host", b"example.test"),
            (b"origin", b"https://example.test"),
            (b"cookie", f"wreath_csrf={token}".encode()),
            (b"x-csrf-token", token.encode()),
        ],
    )

    assert await middleware._ingress(unsafe) is None
    assert unsafe._header_map is not None


@pytest.mark.asyncio
async def test_an_exempt_predicate_that_raises_refuses_and_is_counted() -> None:
    def explode(request: Request) -> bool:
        raise AttributeError("typo in the exempt predicate")

    middleware = CsrfPolicy("s" * 32, exempt=explode)
    assert middleware.exempt_errors == 0

    refused = await middleware._ingress(_request("POST"))
    assert refused is not None
    assert refused.status == 403
    assert middleware.exempt_errors == 1

    await middleware._ingress(_request("POST"))
    assert middleware.exempt_errors == 2


@pytest.mark.asyncio
async def test_a_working_exempt_predicate_counts_nothing() -> None:
    middleware = CsrfPolicy("s" * 32, exempt=lambda request: True)
    assert await middleware._ingress(_request("POST")) is None
    assert middleware.exempt_errors == 0

    # And a predicate that simply declines still refuses without counting: the
    # request was rejected, the check was not broken.
    strict = CsrfPolicy("s" * 32, exempt=lambda request: False)
    assert (await strict._ingress(_request("POST"))) is not None
    assert strict.exempt_errors == 0


def test_csrf_configuration_validation() -> None:
    with pytest.raises(ValueError):
        CsrfPolicy("short")
    with pytest.raises(ValueError):
        CsrfPolicy("s" * 32, same_site="none", secure=False)
    with pytest.raises(ValueError):
        CsrfPolicy("s" * 32, form_field="")


# `wreath mutant` reported every branch of the origin normaliser and every one
# of these refusals UNREACHED across each file that exercises CSRF: no test ever
# passed `trusted_origins=`, so the whole cross-origin allowlist -- the thing
# that lets a separate front-end POST to this API at all -- was unexercised.
# The normaliser is now shared with `WebSocketOriginPolicy` (they had a
# byte-identical copy each), so these cover both callers' input handling.


async def _admits(
    middleware: CsrfPolicy,
    *,
    origin: bytes | None = None,
    referer: bytes | None = None,
    host: bytes | None = b"example.test",
) -> bool:
    """Whether a POST carrying a *valid token* is admitted.

    The token has to be real. Both a failed origin check and a failed token
    check answer 403, so a test that posts without a token cannot tell which
    gate refused it -- it would pass with the origin check deleted entirely,
    which is the one thing these tests exist to notice.
    """
    safe = _request("GET")
    await middleware._ingress(safe)
    token = csrf_token(safe)
    headers = [
        (b"cookie", f"wreath_csrf={token}".encode()),
        (b"x-csrf-token", token.encode()),
    ]
    if host is not None:
        headers.insert(0, (b"host", host))
    if origin is not None:
        headers.append((b"origin", origin))
    if referer is not None:
        headers.append((b"referer", referer))
    return (await middleware._ingress(_request("POST", headers))) is None


async def test_a_trusted_origin_passes_the_origin_check_and_others_do_not() -> None:
    middleware = CsrfPolicy("s" * 32, trusted_origins=["https://app.example"])

    assert await _admits(middleware, origin=b"https://app.example")
    # The request's own origin is always allowed, list or no list.
    assert await _admits(middleware, origin=b"https://example.test")

    # Not on the list and not the request's own.
    assert not await _admits(middleware, origin=b"https://evil.example")
    # Near-misses a substring or suffix comparison would admit.
    assert not await _admits(middleware, origin=b"https://notapp.example")
    assert not await _admits(middleware, origin=b"https://app.example.evil")
    # Scheme is part of an origin.
    assert not await _admits(middleware, origin=b"http://app.example")


async def test_a_trusted_origin_is_matched_after_normalisation() -> None:
    middleware = CsrfPolicy(
        "s" * 32,
        trusted_origins=["https://App.Example:443", "http://other.example:8080"],
    )
    assert await _admits(middleware, origin=b"https://app.example")
    assert await _admits(middleware, origin=b"http://other.example:8080")
    # The non-default port is part of the origin and is not interchangeable.
    assert not await _admits(middleware, origin=b"http://other.example")


async def test_a_trusted_origin_also_covers_the_referer_fallback() -> None:
    middleware = CsrfPolicy("s" * 32, trusted_origins=["https://app.example"])
    assert await _admits(middleware, referer=b"https://app.example/page?x=1")
    assert not await _admits(middleware, referer=b"https://evil.example/page")


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://app.example",
        "app.example",
        "https://",
        "https://u@app.example",
        "https://u:p@app.example",
        "https://app.example/path",
        "https://app.example?q=1",
        "https://app.example#f",
        "https://app.example:notaport",
    ],
)
def test_a_trusted_origin_that_is_not_an_origin_is_refused_at_construction(
    origin: str,
) -> None:
    with pytest.raises(ValueError, match="invalid trusted origin"):
        CsrfPolicy("s" * 32, trusted_origins=[origin])


def test_the_shared_normaliser_names_the_setting_each_caller_configured() -> None:
    from wreath._webpolicy import normalize_origin

    assert normalize_origin("https://App.Example:443", label="trusted") == (b"https://app.example")
    assert normalize_origin("https://[2001:DB8::1]:443", label="trusted") == (
        b"https://[2001:db8::1]"
    )
    with pytest.raises(ValueError, match="invalid trusted origin"):
        normalize_origin("ftp://x", label="trusted")
    with pytest.raises(ValueError, match="invalid WebSocket origin"):
        normalize_origin("ftp://x", label="WebSocket")


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"cookie_name": "__Host-csrf", "secure": False}, "__Host- CSRF cookie"),
        ({"cookie_name": "__Secure-csrf", "secure": False}, "__Secure- CSRF cookie"),
        ({"max_age": 0}, "max_age must be positive"),
        ({"max_age": -1}, "max_age must be positive"),
        ({"same_site": "sometimes"}, "strict, lax, or none"),
    ],
)
def test_csrf_settings_that_cannot_work_are_refused(kwargs: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        CsrfPolicy("s" * 32, **kwargs)


async def test_trusted_hosts_constrains_the_host_the_expected_origin_is_built_from() -> None:
    middleware = CsrfPolicy("s" * 32, trusted_hosts=["example.test"])

    assert await _admits(middleware, origin=b"https://example.test")
    # Both sides forged, and both agree with each other. Refused on the Host.
    assert not await _admits(
        middleware,
        host=b"evil.test",
        origin=b"https://evil.test",
    )
    # A Host is required once the list is non-empty -- absent is not "any".
    assert not await _admits(middleware, host=None, origin=b"https://example.test")
    # Case-insensitive, as a Host comparison must be.
    assert await _admits(middleware, host=b"Example.Test", origin=b"https://example.test")
    # And a non-ASCII Host cannot slip through the decode.
    assert not await _admits(
        middleware,
        host=b"ex\xffample.test",
        origin=b"https://example.test",
    )


async def test_with_no_trusted_hosts_the_host_check_defers_to_other_middleware() -> None:
    middleware = CsrfPolicy("s" * 32)
    assert await _admits(middleware, host=b"evil.test", origin=b"https://evil.test")


def test_csrf_token_says_so_when_no_middleware_prepared_one() -> None:
    from wreath.policy import csrf_token

    with pytest.raises(RuntimeError, match="has not prepared a token"):
        csrf_token(_request("GET"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cookie_name": "bad name"},
        {"cookie_name": "bad;name"},
        {"cookie_name": ""},
        {"header_name": "bad name"},
        {"header_name": "x-csrf\ntoken"},
    ],
)
def test_cookie_and_header_names_must_be_http_tokens(kwargs: dict) -> None:
    with pytest.raises(ValueError, match="valid HTTP tokens"):
        CsrfPolicy("s" * 32, **kwargs)


@pytest.mark.asyncio
async def test_a_submitted_token_that_does_not_match_the_cookie_is_refused() -> None:
    middleware = CsrfPolicy("s" * 32)

    first, second = _request("GET"), _request("GET")
    assert await middleware._ingress(first) is None
    assert await middleware._ingress(second) is None
    cookie_token, other_token = csrf_token(first), csrf_token(second)
    assert cookie_token != other_token, "two mints must differ, or this proves nothing"

    unsafe = _request(
        "POST",
        [
            (b"host", b"example.test"),
            (b"origin", b"https://example.test"),
            (b"cookie", f"wreath_csrf={cookie_token}".encode()),
            (b"x-csrf-token", other_token.encode()),
        ],
    )
    refusal = await middleware._ingress(unsafe)
    assert refusal is not None and refusal.status == 403


@pytest.mark.asyncio
async def test_an_unsafe_request_with_a_cookie_and_no_submitted_token_is_refused() -> None:
    middleware = CsrfPolicy("s" * 32)
    safe = _request("GET")
    assert await middleware._ingress(safe) is None
    token = csrf_token(safe)

    unsafe = _request(
        "POST",
        [
            (b"host", b"example.test"),
            (b"origin", b"https://example.test"),
            (b"cookie", f"wreath_csrf={token}".encode()),
        ],
    )
    refusal = await middleware._ingress(unsafe)
    assert refusal is not None and refusal.status == 403
