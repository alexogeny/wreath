"""HTTP compliance, codified. Each test names the clause it enforces.

Framing tests drive the real native HTTP/1 protocol (see conftest); response and
cookie tests exercise the framework primitives a handler uses directly.
"""
from __future__ import annotations

import pytest

from wreath.exceptions import MethodNotAllowed, TooManyRequests, Unauthorized
from wreath.http_client import _parse_retry_after
from wreath.response import Response

from .conftest import drive_request, header_block, status_of

# --- RFC 9112 message framing (request) -------------------------------------


def test_transfer_encoding_and_content_length_together_is_400() -> None:
    # RFC 9112 §6.1 — a request-smuggling vector; the server must reject it.
    r = drive_request(
        b"POST / HTTP/1.1\r\nHost: x\r\n"
        b"Transfer-Encoding: chunked\r\nContent-Length: 5\r\n\r\n"
    )
    assert status_of(r) == 400


def test_conflicting_duplicate_content_length_is_400() -> None:
    # RFC 9110 §8.6 — differing duplicate Content-Length values are invalid.
    r = drive_request(
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\n"
    )
    assert status_of(r) == 400


def test_transfer_encoding_not_ending_in_chunked_is_400() -> None:
    # RFC 9112 §6.1 — the final coding of a request TE must be chunked.
    r = drive_request(b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: gzip\r\n\r\n")
    assert status_of(r) == 400


def test_missing_host_on_http_1_1_is_400() -> None:
    # RFC 9112 §3.2 — an HTTP/1.1 request without Host must be rejected.
    assert status_of(drive_request(b"GET / HTTP/1.1\r\n\r\n")) == 400


def test_multiple_host_headers_is_400() -> None:
    # RFC 9112 §3.2 / RFC 9110 §7.2 — exactly one Host is allowed.
    assert status_of(drive_request(b"GET / HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n")) == 400


# --- RFC 9110 response requirements -----------------------------------------


def test_date_header_is_sent_by_default() -> None:
    # RFC 9110 §6.6.1 (MUST) — an origin server with a clock sends Date.
    headers = header_block(drive_request(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"))
    assert b"date" in headers


def test_405_carries_allow() -> None:
    # RFC 9110 §15.5.6 (MUST) — a 405 lists the supported methods in Allow.
    exc = MethodNotAllowed(allow=["GET", "POST"])
    assert exc.status == 405
    assert (b"allow", b"GET, POST") in exc.headers


def test_401_carries_www_authenticate_by_default() -> None:
    # RFC 9110 §15.5.2 (MUST) — a 401 carries a WWW-Authenticate challenge.
    assert (b"www-authenticate", b"Bearer") in Unauthorized().headers
    assert (b"www-authenticate", b'Basic realm="x"') in Unauthorized(
        challenge='Basic realm="x"').headers


def test_429_may_carry_retry_after() -> None:
    # RFC 9110 §10.2.3 — Retry-After tells the client how long to wait.
    assert (b"retry-after", b"30") in TooManyRequests(retry_after=30).headers


def test_frameworks_own_oauth2_401_carries_a_bearer_challenge() -> None:
    # RFC 9110 §15.5.2 / RFC 6750 §3 — wreath's OAuth2 callback must not emit a
    # bare 401 (regression guard for the framework-internal fix).
    from wreath._auth.oauth2 import _bearer_401

    resp = _bearer_401("invalid_id_token")
    assert resp.status == 401
    assert any(name == b"www-authenticate" and b"Bearer" in value
               for name, value in resp.headers)


@pytest.mark.parametrize("status", [204, 304])
def test_bodyless_statuses_omit_content_length(status: int) -> None:
    # RFC 9110 §6.4.1 / §15.4.5 — 204 and 304 carry no message body framing.
    names = {name for name, _ in Response(b"", status=status).headers}
    assert b"content-length" not in names


# --- RFC 9111 cache-control -------------------------------------------------


def test_cache_control_renders_directives() -> None:
    from wreath.cache_control import CacheControl

    header = CacheControl(no_store=True).to_header()
    assert b"no-store" in header


# --- RFC 9110 §10.2.3 Retry-After parsing (client) --------------------------


def test_retry_after_accepts_delta_seconds_and_http_date() -> None:
    assert _parse_retry_after(b"120") == 120.0
    assert _parse_retry_after(b"Wed, 21 Oct 2099 07:28:00 GMT") > 1_000_000
    assert _parse_retry_after(b"Wed, 21 Oct 1999 07:28:00 GMT") == 0.0
    assert _parse_retry_after(b"garbage") is None


# --- RFC 6265bis Set-Cookie -------------------------------------------------


def test_samesite_none_requires_secure() -> None:
    # RFC 6265bis §5.4.7 — browsers drop a SameSite=None cookie without Secure.
    with pytest.raises(ValueError):
        Response(b"").set_cookie("sid", "x", samesite="none")
    Response(b"").set_cookie("sid", "x", samesite="none", secure=True)  # ok


def test_host_prefix_requires_secure_root_no_domain() -> None:
    # RFC 6265bis §4.1.3.
    with pytest.raises(ValueError):
        Response(b"").set_cookie("__Host-sid", "x", secure=True, domain="example.com")
    Response(b"").set_cookie("__Host-sid", "x", secure=True)  # Path=/ + no Domain by default


def test_secure_prefix_requires_secure() -> None:
    with pytest.raises(ValueError):
        Response(b"").set_cookie("__Secure-sid", "x")  # secure defaults False


def test_cookie_rejects_control_characters() -> None:
    # RFC 6265 §4.1 — CR/LF in a cookie is header injection.
    with pytest.raises(ValueError):
        Response(b"").set_cookie("sid", "x\r\nInjected: 1")
