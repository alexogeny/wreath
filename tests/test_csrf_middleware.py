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


def test_csrf_configuration_validation() -> None:
    with pytest.raises(ValueError):
        CSRFMiddleware("short")
    with pytest.raises(ValueError):
        CSRFMiddleware("s" * 32, same_site="none", secure=False)
