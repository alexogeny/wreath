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
    """Failing closed is right; failing closed *silently* is not.

    A broken predicate refuses every unsafe request forever, and a wall of 403s
    reads exactly like a site under attack rather than one that is misconfigured.
    The count is what tells those two apart.
    """
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
    """Guard against 'fixed it by counting every refusal'."""
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


# --- trusted origins, and the config refusals -------------------------------
#
# `wreath mutant` reported every branch of the origin normaliser and every one
# of these refusals UNREACHED across each file that exercises CSRF: no test ever
# passed `trusted_origins=`, so the whole cross-origin allowlist -- the thing
# that lets a separate front-end POST to this API at all -- was unexercised.
# The normaliser is now shared with `WebSocketOriginPolicy` (they had a
# byte-identical copy each), so these cover both callers' input handling.


async def _admits(
    middleware: CsrfPolicy, *, origin: bytes | None = None,
    referer: bytes | None = None, host: bytes | None = b"example.test",
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
    """The allowlist is what lets a separate front-end origin POST here at all."""
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
    """Comparison is exact bytes, so normalisation is the whole correctness story."""
    middleware = CsrfPolicy(
        "s" * 32, trusted_origins=["https://App.Example:443", "http://other.example:8080"],
    )
    assert await _admits(middleware, origin=b"https://app.example")
    assert await _admits(middleware, origin=b"http://other.example:8080")
    # The non-default port is part of the origin and is not interchangeable.
    assert not await _admits(middleware, origin=b"http://other.example")


async def test_a_trusted_origin_also_covers_the_referer_fallback() -> None:
    """A browser that sends `Referer` and no `Origin` gets the same allowlist."""
    middleware = CsrfPolicy("s" * 32, trusted_origins=["https://app.example"])
    assert await _admits(middleware, referer=b"https://app.example/page?x=1")
    assert not await _admits(middleware, referer=b"https://evil.example/page")


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://app.example", "app.example", "https://", "https://u@app.example",
        "https://u:p@app.example", "https://app.example/path",
        "https://app.example?q=1", "https://app.example#f",
        "https://app.example:notaport",
    ],
)
def test_a_trusted_origin_that_is_not_an_origin_is_refused_at_construction(
    origin: str,
) -> None:
    """A value no browser can send would sit in the list matching nothing."""
    with pytest.raises(ValueError, match="invalid trusted origin"):
        CsrfPolicy("s" * 32, trusted_origins=[origin])


def test_the_shared_normaliser_names_the_setting_each_caller_configured() -> None:
    """One implementation, two nouns: the message still points at the right knob."""
    from wreath._webpolicy import normalize_origin

    assert normalize_origin("https://App.Example:443", label="trusted") == (
        b"https://app.example"
    )
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
    """The cookie-prefix ones are the sharp pair.

    A browser enforces `__Host-` and `__Secure-` by *dropping* the cookie, so a
    prefix without `secure=True` is a CSRF cookie that silently never arrives --
    which looks exactly like a working deployment until a POST is refused.
    """
    with pytest.raises(ValueError, match=match):
        CsrfPolicy("s" * 32, **kwargs)


async def test_trusted_hosts_constrains_the_host_the_expected_origin_is_built_from() -> None:
    """Without this, the attacker supplies both sides of the comparison.

    The expected origin is derived from the request's own `Host` header, which
    the client sends. An attacker who can set `Host: evil.test` and
    `Origin: https://evil.test` therefore matches themselves -- the check
    compares two values they control. `trusted_hosts` is what closes that, and
    it had no test: `wreath mutant` could delete the whole branch and nothing
    noticed.
    """
    middleware = CsrfPolicy("s" * 32, trusted_hosts=["example.test"])

    assert await _admits(middleware, origin=b"https://example.test")
    # Both sides forged, and both agree with each other. Refused on the Host.
    assert not await _admits(
        middleware, host=b"evil.test", origin=b"https://evil.test",
    )
    # A Host is required once the list is non-empty -- absent is not "any".
    assert not await _admits(middleware, host=None, origin=b"https://example.test")
    # Case-insensitive, as a Host comparison must be.
    assert await _admits(middleware, host=b"Example.Test", origin=b"https://example.test")
    # And a non-ASCII Host cannot slip through the decode.
    assert not await _admits(
        middleware, host=b"ex\xffample.test", origin=b"https://example.test",
    )


async def test_with_no_trusted_hosts_the_host_check_defers_to_other_middleware() -> None:
    """Empty means "TrustedHostPolicy is mounted", not "nothing is checked".

    Pinned because the two configurations differ only in whether one branch
    runs, and the permissive one is the default.
    """
    middleware = CsrfPolicy("s" * 32)
    assert await _admits(middleware, host=b"evil.test", origin=b"https://evil.test")


def test_csrf_token_says_so_when_no_middleware_prepared_one() -> None:
    """A `RuntimeError` naming the cause, not a `None` that renders as a blank field.

    The template that calls this is building a form. Returning nothing would
    ship a form with an empty token that fails on submit; the refusal names the
    two ways it happens -- middleware not installed, or `exempt` excused it.
    """
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
    """A name with a separator in it is a header-splitting or cookie-parsing bug."""
    with pytest.raises(ValueError, match="valid HTTP tokens"):
        CsrfPolicy("s" * 32, **kwargs)
