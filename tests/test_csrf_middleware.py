from __future__ import annotations

from typing import Any

import pytest

from wreath.policy import CsrfPolicy, csrf_token
from wreath.policy.csrf import _referer_origin, _request_origin
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
async def test_query_is_safe_and_needs_no_cross_site_csrf_token() -> None:
    middleware = CsrfPolicy("s" * 32)
    request = _request(
        "QUERY",
        [(b"host", b"example.test"), (b"sec-fetch-site", b"cross-site")],
    )

    assert await middleware._ingress(request) is None


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
async def test_header_only_policy_never_reads_a_form_body() -> None:
    async def should_not_receive() -> dict[str, Any]:
        raise AssertionError("a header-only CSRF policy must not read the body")

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/",
            "headers": [
                (b"host", b"example.test"),
                (b"origin", b"https://example.test"),
                (b"content-type", b"application/x-www-form-urlencoded"),
            ],
        },
        should_not_receive,
    )

    refusal = await CsrfPolicy("s" * 32)._ingress(request)
    assert refusal is not None and refusal.status == 403


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


def test_csrf_configuration_accepts_bytes_and_secure_cookie_modes() -> None:
    CsrfPolicy(b"s" * 32)
    CsrfPolicy("s" * 32, cookie_name="__Secure-csrf", secure=True)
    CsrfPolicy("s" * 32, same_site="none", secure=True)


def test_csrf_configuration_refuses_a_non_string_form_field() -> None:
    with pytest.raises(ValueError, match="form_field must be a non-empty string or None"):
        CsrfPolicy("s" * 32, form_field=1)


def test_request_origin_requires_http_or_https_and_a_host() -> None:
    non_http = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "ftp",
            "path": "/",
            "query_string": b"",
            "headers": [(b"host", b"example.test")],
        },
        _receive,
    )
    assert _request_origin(non_http, {b"host": b"example.test"}) is None
    assert _request_origin(_request("POST"), {}) is None


def test_csrf_token_prefers_the_native_request_context() -> None:
    class Context:
        policy_csrf_token = "native-token"

    class NativeRequest:
        _context = Context()

    assert csrf_token(NativeRequest()) == "native-token"


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


async def test_exact_request_origin_skips_origin_normalisation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wreath.policy import csrf as csrf_module

    def unexpected_normalisation(*_args: object) -> bool:
        raise AssertionError("an exact origin does not need normalisation")

    monkeypatch.setattr(csrf_module, "origin_matches", unexpected_normalisation)

    assert await _admits(CsrfPolicy("s" * 32), origin=b"https://example.test")
    assert await _admits(CsrfPolicy("s" * 32), referer=b"https://example.test/path")


async def test_exact_but_invalid_request_origin_is_still_refused() -> None:
    assert not await _admits(
        CsrfPolicy("s" * 32),
        host=b"example.test/path",
        origin=b"https://example.test/path",
    )


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
    "referer",
    [b"ftp://example.test/page", b"https:///page"],
    ids=["scheme", "hostname"],
)
async def test_an_invalid_referer_is_refused(referer: bytes) -> None:
    assert _referer_origin(referer.decode("ascii")) is None
    assert not await _admits(CsrfPolicy("s" * 32), referer=referer)


@pytest.mark.parametrize(
    ("referer", "origin"),
    [
        ("http://example.test:80/page", b"http://example.test"),
        ("https://example.test:443/page", b"https://example.test"),
        ("http://example.test:8080/page", b"http://example.test:8080"),
        ("https://example.test:8443/page", b"https://example.test:8443"),
        ("https://[2001:DB8::1]:443/page", b"https://[2001:db8::1]"),
    ],
)
def test_referer_origin_normalizes_authority(referer: str, origin: bytes) -> None:
    assert _referer_origin(referer) == origin


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


@pytest.mark.parametrize("max_age", [float("nan"), float("inf")])
def test_csrf_refuses_non_finite_expiry_windows(max_age: float) -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        CsrfPolicy("s" * 32, max_age=max_age)


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


@pytest.mark.parametrize("duplicate", [b"host", b"origin", b"x-csrf-token", b"sec-fetch-site"])
async def test_csrf_refuses_duplicate_security_boundary_headers(duplicate: bytes) -> None:
    middleware = CsrfPolicy("s" * 32, trusted_hosts=["example.test"])
    safe = _request("GET")
    await middleware._ingress(safe)
    token = csrf_token(safe).encode()
    headers = [
        (b"host", b"example.test"),
        (b"origin", b"https://example.test"),
        (b"cookie", b"wreath_csrf=" + token),
        (b"x-csrf-token", token),
    ]
    conflicting = {
        b"host": b"evil.test",
        b"origin": b"https://evil.test",
        b"x-csrf-token": b"invalid",
        b"sec-fetch-site": b"cross-site",
    }
    if duplicate == b"sec-fetch-site":
        headers.insert(0, (duplicate, b"same-origin"))
    position = next(index for index, (name, _) in enumerate(headers) if name == duplicate)
    headers.insert(position + 1, (duplicate, conflicting[duplicate]))

    assert await middleware._ingress(_request("POST", headers)) is not None


async def test_with_no_trusted_hosts_the_host_check_defers_to_other_middleware() -> None:
    middleware = CsrfPolicy("s" * 32)
    assert await _admits(middleware, host=b"evil.test", origin=b"https://evil.test")


async def test_an_origin_cannot_be_valid_without_a_request_origin() -> None:
    middleware = CsrfPolicy("s" * 32)
    request = _request("POST", [(b"origin", b"https://example.test")])
    assert not middleware._origin_valid(request, {b"origin": b"https://example.test"})


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


@pytest.mark.asyncio
async def test_an_unsafe_request_with_a_submitted_token_and_no_cookie_is_refused() -> None:
    middleware = CsrfPolicy("s" * 32)
    safe = _request("GET")
    assert await middleware._ingress(safe) is None
    token = csrf_token(safe)
    unsafe = _request(
        "POST",
        [
            (b"host", b"example.test"),
            (b"origin", b"https://example.test"),
            (b"x-csrf-token", token.encode()),
        ],
    )
    refusal = await middleware._ingress(unsafe)
    assert refusal is not None and refusal.status == 403


@pytest.mark.parametrize(("now", "renew"), [(1020, False), (1080, True)])
def test_a_valid_submission_renews_only_near_expiry(now: int, renew: bool) -> None:
    middleware = CsrfPolicy("s" * 32, max_age=100)
    token = middleware._new_token(1000)
    request = _request(
        "POST",
        [
            (b"host", b"example.test"),
            (b"origin", b"https://example.test"),
            (b"cookie", f"wreath_csrf={token}".encode()),
        ],
    )
    assert (
        middleware._validate_submission(request, request._index_headers(), token.encode(), now)
        is None
    )
    prepared = csrf_token(request)
    assert (prepared != token) is renew
    assert request.state.get("_wreath_csrf_issue") is renew


@pytest.mark.asyncio
@pytest.mark.parametrize(("now", "renew"), [(1020, False), (1080, True)])
async def test_a_safe_request_reuses_only_a_fresh_cookie(
    monkeypatch: pytest.MonkeyPatch,
    now: int,
    renew: bool,
) -> None:
    middleware = CsrfPolicy("s" * 32, max_age=100)
    token = middleware._new_token(1000)
    monkeypatch.setattr("wreath.policy.csrf.time.time", lambda: now)
    request = _request(
        "GET",
        [(b"host", b"example.test"), (b"cookie", f"wreath_csrf={token}".encode())],
    )
    assert await middleware._ingress(request) is None
    prepared = csrf_token(request)
    assert (prepared != token) is renew
    response = await middleware._egress(request, Response(b"ok"))
    has_cookie = any(name == b"set-cookie" for name, _value in response.headers)
    assert has_cookie is renew


@pytest.mark.asyncio
async def test_a_safe_request_replaces_an_invalid_cookie_at_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = CsrfPolicy("s" * 32, max_age=100)
    monkeypatch.setattr("wreath.policy.csrf.time.time", lambda: 0)
    request = _request(
        "GET",
        [(b"host", b"example.test"), (b"cookie", b"wreath_csrf=invalid")],
    )
    assert await middleware._ingress(request) is None
    assert csrf_token(request) != "invalid"
    response = await middleware._egress(request, Response(b"ok"))
    assert any(name == b"set-cookie" for name, _value in response.headers)


@pytest.mark.asyncio
async def test_a_broken_sync_exemption_returns_before_submission_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(_request: Request) -> bool:
        raise RuntimeError("broken exemption")

    def unexpected(*_args: object):
        raise AssertionError("an exemption error already decided the request")

    monkeypatch.setattr(CsrfPolicy, "_validate_submission", unexpected)
    response = await CsrfPolicy("s" * 32, exempt=explode)._ingress(_request("POST"))
    assert response is not None and response.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("exemption", ["true", "error"])
async def test_a_form_exemption_returns_before_body_parsing(exemption: str) -> None:
    async def unexpected_receive() -> dict[str, Any]:
        raise AssertionError("an exempt request must not parse its body")

    def exempt(_request: Request) -> bool:
        if exemption == "error":
            raise RuntimeError("broken exemption")
        return True

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/",
            "query_string": b"",
            "headers": [
                (b"host", b"example.test"),
                (b"content-type", b"application/x-www-form-urlencoded"),
            ],
        },
        unexpected_receive,
    )
    response = await CsrfPolicy("s" * 32, form_field="csrf", exempt=exempt)._ingress(request)
    if exemption == "true":
        assert response is None
    else:
        assert response is not None and response.status == 403


@pytest.mark.asyncio
async def test_a_form_policy_does_not_parse_a_non_form_body() -> None:
    async def unexpected_receive() -> dict[str, Any]:
        raise AssertionError("a non-form media type must not be parsed as a form")

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/",
            "query_string": b"",
            "headers": [(b"host", b"example.test"), (b"content-type", b"application/json")],
        },
        unexpected_receive,
    )
    response = await CsrfPolicy("s" * 32, form_field="csrf")._ingress(request)
    assert response is not None and response.status == 403


@pytest.mark.asyncio
async def test_egress_with_an_issue_marker_but_no_token_is_a_noop() -> None:
    middleware = CsrfPolicy("s" * 32)
    request = _request("GET")
    request.state._wreath_csrf_issue = True
    response = Response(b"ok")
    assert await middleware._egress(request, response) is response
    assert all(name != b"set-cookie" for name, _value in response.headers)
