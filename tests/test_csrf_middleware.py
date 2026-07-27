from __future__ import annotations

from typing import Any

import pytest

from wreath.middleware import CSRFMiddleware, csrf_token
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
    middleware = CSRFMiddleware("s" * 32)
    safe = _request("GET")
    assert await middleware.before(safe) is None
    token = csrf_token(safe)
    response = await middleware.after(safe, Response(b"ok"))
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
    assert await middleware.before(unsafe) is None


@pytest.mark.asyncio
async def test_unsafe_requests_fail_closed_with_generic_response() -> None:
    middleware = CSRFMiddleware("s" * 32)
    for headers in (
        [(b"host", b"example.test")],
        [(b"host", b"example.test"), (b"origin", b"https://evil.test")],
    ):
        rejected = await middleware.before(_request("POST", headers))
        assert rejected.status == 403
        assert b"CSRF validation failed" in rejected.body


@pytest.mark.asyncio
async def test_valid_unsafe_request_indexes_headers_once() -> None:
    middleware = CSRFMiddleware("s" * 32)
    safe = _request("GET")
    assert await middleware.before(safe) is None
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

    assert await middleware.before(unsafe) is None
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

    middleware = CSRFMiddleware("s" * 32, exempt=explode)
    assert middleware.exempt_errors == 0

    refused = await middleware.before(_request("POST"))
    assert refused is not None
    assert refused.status == 403
    assert middleware.exempt_errors == 1

    await middleware.before(_request("POST"))
    assert middleware.exempt_errors == 2


@pytest.mark.asyncio
async def test_a_working_exempt_predicate_counts_nothing() -> None:
    """Guard against 'fixed it by counting every refusal'."""
    middleware = CSRFMiddleware("s" * 32, exempt=lambda request: True)
    assert await middleware.before(_request("POST")) is None
    assert middleware.exempt_errors == 0

    # And a predicate that simply declines still refuses without counting: the
    # request was rejected, the check was not broken.
    strict = CSRFMiddleware("s" * 32, exempt=lambda request: False)
    assert (await strict.before(_request("POST"))) is not None
    assert strict.exempt_errors == 0


def test_csrf_configuration_validation() -> None:
    with pytest.raises(ValueError):
        CSRFMiddleware("short")
    with pytest.raises(ValueError):
        CSRFMiddleware("s" * 32, same_site="none", secure=False)
