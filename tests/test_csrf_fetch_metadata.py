from __future__ import annotations

from typing import Any

import pytest

from wreath.policy import CsrfPolicy, csrf_token
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


@pytest.mark.parametrize(
    ("method", "headers"),
    [
        ("GET", [(b"host", b"example.test")]),
        (
            "POST",
            [(b"host", b"example.test"), (b"sec-fetch-site", b"same-origin")],
        ),
    ],
    ids=["safe-absent", "unsafe-present"],
)
@pytest.mark.asyncio
async def test_form_enabled_policy_reads_fetch_metadata_once(
    method: str,
    headers: list[tuple[bytes, bytes]],
) -> None:
    class CountingRequest(Request):
        fetch_metadata_reads = 0

        def header(self, name: str | bytes, default: str | None = None) -> str | None:
            if name == b"sec-fetch-site":
                self.fetch_metadata_reads += 1
            return super().header(name, default)

    request = CountingRequest(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": "/",
            "query_string": b"",
            "headers": headers,
        },
        _receive,
    )

    assert await CsrfPolicy(SECRET, form_field="csrf_token")._ingress(request) is None
    assert request.fetch_metadata_reads == 1


@pytest.mark.parametrize("site", [b"cross-site", b"same-site"])
@pytest.mark.asyncio
async def test_an_unsafe_request_from_another_site_is_refused(site: bytes) -> None:
    middleware = CsrfPolicy(SECRET)
    response = await middleware._ingress(_unsafe(site))
    assert response is not None
    assert response.status == 403
    assert middleware.cross_site_refusals == 1


@pytest.mark.asyncio
async def test_a_garbage_sec_fetch_site_is_refused_not_ignored() -> None:
    middleware = CsrfPolicy(SECRET)
    response = await middleware._ingress(_unsafe(b"same-orig"))
    assert response is not None and response.status == 403


@pytest.mark.parametrize("site", [b"same-origin", b"none"])
@pytest.mark.asyncio
async def test_an_unsafe_same_origin_request_passes_without_any_token(site: bytes) -> None:
    middleware = CsrfPolicy(SECRET)
    assert await middleware._ingress(_unsafe(site)) is None
    assert middleware.cross_site_refusals == 0


@pytest.mark.asyncio
async def test_a_client_sending_no_header_still_needs_a_valid_token() -> None:
    middleware = CsrfPolicy(SECRET)
    response = await middleware._ingress(_request("POST", [(b"host", b"example.test")]))
    assert response is not None and response.status == 403
    # Refused by the token path, not the header path.
    assert middleware.cross_site_refusals == 0


@pytest.mark.asyncio
async def test_a_legacy_client_with_a_valid_token_is_admitted() -> None:
    middleware = CsrfPolicy(SECRET)
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


@pytest.mark.asyncio
async def test_a_safe_request_with_the_header_mints_nothing() -> None:
    middleware = CsrfPolicy(SECRET)
    safe = _request("GET", [(b"host", b"example.test"), (b"sec-fetch-site", b"same-origin")])
    assert await middleware._ingress(safe) is None
    response = await middleware._egress(safe, Response(b"ok"))
    assert _set_cookie(response) is None


@pytest.mark.asyncio
async def test_csrf_token_still_works_for_a_modern_browser() -> None:
    middleware = CsrfPolicy(SECRET)
    safe = _request("GET", [(b"host", b"example.test"), (b"sec-fetch-site", b"same-origin")])
    assert await middleware._ingress(safe) is None

    token = csrf_token(safe)
    assert token.startswith("v1.")
    assert csrf_token(safe) == token  # minted once, not per call

    response = await middleware._egress(safe, Response(b"ok"))
    cookie = _set_cookie(response)
    assert cookie is not None and token.encode() in cookie


@pytest.mark.asyncio
async def test_a_token_minted_on_demand_is_accepted_by_the_fallback() -> None:
    middleware = CsrfPolicy(SECRET)
    safe = _request("GET", [(b"host", b"example.test"), (b"sec-fetch-site", b"same-origin")])
    await middleware._ingress(safe)
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
    assert await middleware._ingress(legacy) is None


@pytest.mark.asyncio
async def test_a_response_whose_cookie_turned_on_the_header_varies_on_it() -> None:
    middleware = CsrfPolicy(SECRET)
    safe = _request("GET", [(b"host", b"example.test"), (b"sec-fetch-site", b"same-origin")])
    await middleware._ingress(safe)
    response = await middleware._egress(safe, Response(b"ok"))
    vary = _vary(response)
    assert vary is not None
    assert b"sec-fetch-site" in vary.lower()


@pytest.mark.asyncio
async def test_vary_is_merged_not_overwritten() -> None:
    middleware = CsrfPolicy(SECRET)
    safe = _request("GET", [(b"host", b"example.test"), (b"sec-fetch-site", b"same-origin")])
    await middleware._ingress(safe)
    response = Response(b"ok")
    response.headers.append((b"vary", b"accept-encoding"))
    response = await middleware._egress(safe, response)
    vary_header = _vary(response)
    assert vary_header is not None
    vary = vary_header.lower()
    assert b"accept-encoding" in vary
    assert b"sec-fetch-site" in vary


@pytest.mark.asyncio
async def test_a_legacy_response_does_not_vary_on_a_header_it_never_read() -> None:
    middleware = CsrfPolicy(SECRET)
    safe = _request("GET")
    await middleware._ingress(safe)
    response = await middleware._egress(safe, Response(b"ok"))
    assert _vary(response) is None


@pytest.mark.asyncio
async def test_the_refusals_are_not_vacuous() -> None:
    from wreath.policy import csrf as csrf_module

    middleware = CsrfPolicy(SECRET)
    assert (await middleware._ingress(_unsafe(b"cross-site"))) is not None

    original = csrf_module._TRUSTED_SITES
    csrf_module._TRUSTED_SITES = frozenset({"same-origin", "none", "cross-site"})
    try:
        assert (await middleware._ingress(_unsafe(b"cross-site"))) is None, (
            "widening the accepted set did not change the outcome, so these "
            "tests are not exercising the check they claim to"
        )
    finally:
        csrf_module._TRUSTED_SITES = original

    assert (await middleware._ingress(_unsafe(b"cross-site"))) is not None


@pytest.mark.asyncio
async def test_a_clean_run_leaves_the_refusal_counter_at_zero() -> None:
    middleware = CsrfPolicy(SECRET)
    await middleware._ingress(_unsafe(b"same-origin"))
    await middleware._ingress(_request("GET", [(b"sec-fetch-site", b"same-origin")]))
    assert middleware.cross_site_refusals == 0


@pytest.mark.parametrize("site", [b"same-origin", b"none"])
@pytest.mark.asyncio
async def test_a_handler_can_still_mint_a_token_on_a_trusted_unsafe_request(
    site: bytes,
) -> None:
    middleware = CsrfPolicy(SECRET)
    request = _unsafe(site)
    assert await middleware._ingress(request) is None
    assert csrf_token(request).startswith("v1.")


@pytest.mark.asyncio
async def test_a_refused_cross_site_request_prepares_no_minter() -> None:
    middleware = CsrfPolicy(SECRET)
    request = _unsafe(b"cross-site")
    assert (await middleware._ingress(request)) is not None
    with pytest.raises(RuntimeError, match="has not prepared a token"):
        csrf_token(request)
