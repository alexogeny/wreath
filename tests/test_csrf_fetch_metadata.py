"""Fetch Metadata is the primary CSRF check; the token is the fallback.

These tests are written around the *refusals*. A suite that only shows the fast
path passing is satisfied by deleting the check entirely, which is the shape
this repository keeps finding -- so every allow here has a matching deny, and
`test_the_refusals_are_not_vacuous` fails if the header check stops rejecting.
"""

from __future__ import annotations

from typing import Any

import pytest

from wreath.middleware import CSRFMiddleware, csrf_token
from wreath.request import Request
from wreath.response import Response

SECRET = "s" * 32


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


def _unsafe(site: bytes | None) -> Request:
    headers = [(b"host", b"example.test")]
    if site is not None:
        headers.append((b"sec-fetch-site", site))
    return _request("POST", headers)


def _set_cookie(response: Response) -> bytes | None:
    return next((v for n, v in response.headers if n == b"set-cookie"), None)


def _vary(response: Response) -> bytes | None:
    return next((v for n, v in response.headers if n == b"vary"), None)


# --- the refusals ------------------------------------------------------------


@pytest.mark.parametrize("site", [b"cross-site", b"same-site"])
@pytest.mark.asyncio
async def test_an_unsafe_request_from_another_site_is_refused(site: bytes) -> None:
    """`same-site` is refused as firmly as `cross-site`.

    It means a *different subdomain*, which is a different security origin --
    and a sibling-subdomain takeover is exactly the attack that distinction
    exists to stop. Go's `CrossOriginProtection` refuses it too. Accepting it
    would make the header check weaker than the token check it fronts.
    """
    middleware = CSRFMiddleware(SECRET)
    response = await middleware.before(_unsafe(site))
    assert response is not None
    assert response.status == 403
    assert middleware.cross_site_refusals == 1


@pytest.mark.asyncio
async def test_a_garbage_sec_fetch_site_is_refused_not_ignored() -> None:
    """An unrecognised value must not fall through to the token path.

    Falling through would let an attacker who can set one header downgrade the
    check to whichever path they prefer.
    """
    middleware = CSRFMiddleware(SECRET)
    response = await middleware.before(_unsafe(b"same-orig"))
    assert response is not None and response.status == 403


@pytest.mark.parametrize("site", [b"same-origin", b"none"])
@pytest.mark.asyncio
async def test_an_unsafe_same_origin_request_passes_without_any_token(site: bytes) -> None:
    """The saving: no cookie, no header, no HMAC, and it is allowed."""
    middleware = CSRFMiddleware(SECRET)
    assert await middleware.before(_unsafe(site)) is None
    assert middleware.cross_site_refusals == 0


# --- the fallback still works, unchanged -------------------------------------


@pytest.mark.asyncio
async def test_a_client_sending_no_header_still_needs_a_valid_token() -> None:
    middleware = CSRFMiddleware(SECRET)
    response = await middleware.before(_request("POST", [(b"host", b"example.test")]))
    assert response is not None and response.status == 403
    # Refused by the token path, not the header path.
    assert middleware.cross_site_refusals == 0


@pytest.mark.asyncio
async def test_a_legacy_client_with_a_valid_token_is_admitted() -> None:
    middleware = CSRFMiddleware(SECRET)
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


# --- the safe path, where the cost was ---------------------------------------


@pytest.mark.asyncio
async def test_a_safe_request_with_the_header_mints_nothing() -> None:
    middleware = CSRFMiddleware(SECRET)
    safe = _request("GET", [(b"host", b"example.test"), (b"sec-fetch-site", b"same-origin")])
    assert await middleware.before(safe) is None
    response = await middleware.after(safe, Response(b"ok"))
    assert _set_cookie(response) is None


@pytest.mark.asyncio
async def test_csrf_token_still_works_for_a_modern_browser() -> None:
    """The public API is unchanged: a handler that asks gets a token.

    Minting moved to the caller that wanted one, instead of being paid by every
    request that did not. The cookie is still written.
    """
    middleware = CSRFMiddleware(SECRET)
    safe = _request("GET", [(b"host", b"example.test"), (b"sec-fetch-site", b"same-origin")])
    assert await middleware.before(safe) is None

    token = csrf_token(safe)
    assert token.startswith("v1.")
    assert csrf_token(safe) == token  # minted once, not per call

    response = await middleware.after(safe, Response(b"ok"))
    cookie = _set_cookie(response)
    assert cookie is not None and token.encode() in cookie


@pytest.mark.asyncio
async def test_a_token_minted_on_demand_is_accepted_by_the_fallback() -> None:
    """End to end: the lazily minted token really works as a token."""
    middleware = CSRFMiddleware(SECRET)
    safe = _request("GET", [(b"host", b"example.test"), (b"sec-fetch-site", b"same-origin")])
    await middleware.before(safe)
    token = csrf_token(safe)

    legacy = _request(
        "POST",
        [
            (b"host", b"example.test"),
            (b"origin", b"https://example.test"),
            (b"cookie", f"wreath_csrf={token}".encode()),
            (b"x-csrf-token", token.encode()),
        ],
    )
    assert await middleware.before(legacy) is None


# --- Vary --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_response_whose_cookie_turned_on_the_header_varies_on_it() -> None:
    middleware = CSRFMiddleware(SECRET)
    safe = _request("GET", [(b"host", b"example.test"), (b"sec-fetch-site", b"same-origin")])
    await middleware.before(safe)
    response = await middleware.after(safe, Response(b"ok"))
    assert _vary(response) is not None
    assert b"sec-fetch-site" in _vary(response).lower()


@pytest.mark.asyncio
async def test_vary_is_merged_not_overwritten() -> None:
    """A `Vary` another middleware set must survive.

    `cors.py` shipped the mirror-image defect today -- appending only when there
    was no `Vary` at all -- so this is pinned rather than assumed.
    """
    middleware = CSRFMiddleware(SECRET)
    safe = _request("GET", [(b"host", b"example.test"), (b"sec-fetch-site", b"same-origin")])
    await middleware.before(safe)
    response = Response(b"ok")
    response.headers.append((b"vary", b"accept-encoding"))
    response = await middleware.after(safe, response)
    vary = _vary(response).lower()
    assert b"accept-encoding" in vary
    assert b"sec-fetch-site" in vary


@pytest.mark.asyncio
async def test_a_legacy_response_does_not_vary_on_a_header_it_never_read() -> None:
    middleware = CSRFMiddleware(SECRET)
    safe = _request("GET")
    await middleware.before(safe)
    response = await middleware.after(safe, Response(b"ok"))
    assert _vary(response) is None


# --- the guard on the guard --------------------------------------------------


@pytest.mark.asyncio
async def test_the_refusals_are_not_vacuous() -> None:
    """Prove a refusal test can fail, by widening the accepted set.

    Without this, every deny above would still pass if `_TRUSTED_SITES` grew to
    admit everything -- the check would be gone and the suite green.
    """
    from wreath.middleware import csrf as csrf_module

    middleware = CSRFMiddleware(SECRET)
    assert (await middleware.before(_unsafe(b"cross-site"))) is not None

    original = csrf_module._TRUSTED_SITES
    csrf_module._TRUSTED_SITES = frozenset({"same-origin", "none", "cross-site"})
    try:
        assert (await middleware.before(_unsafe(b"cross-site"))) is None, (
            "widening the accepted set did not change the outcome, so these "
            "tests are not exercising the check they claim to"
        )
    finally:
        csrf_module._TRUSTED_SITES = original

    assert (await middleware.before(_unsafe(b"cross-site"))) is not None


@pytest.mark.asyncio
async def test_a_clean_run_leaves_the_refusal_counter_at_zero() -> None:
    """Otherwise "the counter moved" proves nothing."""
    middleware = CSRFMiddleware(SECRET)
    await middleware.before(_unsafe(b"same-origin"))
    await middleware.before(_request("GET", [(b"sec-fetch-site", b"same-origin")]))
    assert middleware.cross_site_refusals == 0
