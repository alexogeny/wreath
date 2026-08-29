"""AWS Signature Version 4 signing."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from urllib.parse import quote

from ._native import _core

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

__all__ = [
    "ALGORITHM",
    "UNSIGNED_PAYLOAD",
    "EMPTY_SHA256",
    "sha256_hex",
    "uri_encode",
    "signing_key",
    "canonical_request",
    "string_to_sign",
    "sign",
    "presign",
]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def uri_encode(value: str, *, encode_slash: bool = True) -> str:
    """RFC 3986 percent-encoding with AWS's unreserved set (`A-Za-z0-9-._~`)."""
    return quote(value, safe="" if encode_slash else "/")


def signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def _canonical_headers(headers: dict[str, str]) -> tuple[str, str]:
    return _core.sigv4_headers(headers)


def _canonical_query(params: Iterable[tuple[str, str]]) -> str:
    encoded = sorted((uri_encode(name), uri_encode(value)) for name, value in params)
    return "&".join(f"{name}={value}" for name, value in encoded)


def canonical_request(
    method: str,
    path: str,
    params: Iterable[tuple[str, str]],
    headers: dict[str, str],
    payload_hash: str,
) -> tuple[str, str]:
    return _core.sigv4_canonical(method, path, params, headers, payload_hash)


def _scope(date_stamp: str, region: str, service: str) -> str:
    return f"{date_stamp}/{region}/{service}/aws4_request"


def string_to_sign(amz_date: str, scope: str, canonical: str) -> str:
    return "\n".join([ALGORITHM, amz_date, scope, sha256_hex(canonical.encode("utf-8"))])


def sign(
    *,
    method: str,
    host: str,
    path: str,
    region: str,
    service: str,
    access_key: str,
    secret_key: str,
    amz_date: str,
    params: Iterable[tuple[str, str]] | None = None,
    headers: dict[str, str] | None = None,
    payload_hash: str | None = None,
    session_token: str | None = None,
) -> dict[str, str]:
    """Return the headers to add for a header-auth SigV4 request (`Authorization` etc.)."""
    date_stamp = amz_date[:8]
    normalized_headers: dict[str, str] = {
        name.lower(): value for name, value in (headers or {}).items()
    }
    normalized_headers.setdefault("host", host)
    normalized_headers["x-amz-date"] = amz_date
    if payload_hash is None:
        payload_hash = EMPTY_SHA256
    normalized_headers["x-amz-content-sha256"] = payload_hash
    if session_token:
        normalized_headers["x-amz-security-token"] = session_token
    canonical, signed_headers = canonical_request(
        method, path, params or [], normalized_headers, payload_hash
    )
    scope = _scope(date_stamp, region, service)
    signing_value = string_to_sign(amz_date, scope, canonical)
    signature = hmac.new(
        signing_key(secret_key, date_stamp, region, service),
        signing_value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    output = {
        "Authorization": (
            f"{ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    if session_token:
        output["x-amz-security-token"] = session_token
    return output


def presign(
    *,
    method: str,
    host: str,
    path: str,
    region: str,
    service: str,
    access_key: str,
    secret_key: str,
    amz_date: str,
    expires: int,
    signed_headers: dict[str, str] | None = None,
    extra_params: Iterable[tuple[str, str]] | None = None,
    session_token: str | None = None,
    payload_hash: str = UNSIGNED_PAYLOAD,
    scheme: str = "https",
) -> str:
    """Return a fully-formed SigV4 presigned URL (auth in the query string, no network)."""
    date_stamp = amz_date[:8]
    scope = _scope(date_stamp, region, service)
    headers = {
        "host": host,
        **{name.lower(): value for name, value in (signed_headers or {}).items()},
    }
    _, canonical_header_names = _canonical_headers(headers)
    params: list[tuple[str, str]] = [
        ("X-Amz-Algorithm", ALGORITHM),
        ("X-Amz-Credential", f"{access_key}/{scope}"),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", str(expires)),
        ("X-Amz-SignedHeaders", canonical_header_names),
    ]
    if session_token:
        params.append(("X-Amz-Security-Token", session_token))
    if extra_params:
        params.extend(extra_params)
    canonical, _ = canonical_request(method, path, params, headers, payload_hash)
    signing_value = string_to_sign(amz_date, scope, canonical)
    signature = hmac.new(
        signing_key(secret_key, date_stamp, region, service),
        signing_value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    # The signature is appended last (not part of the signed query), per AWS convention.
    query = "&".join(f"{uri_encode(name)}={uri_encode(value)}" for name, value in sorted(params))
    query += f"&X-Amz-Signature={signature}"
    return f"{scheme}://{host}{uri_encode(path, encode_slash=False)}?{query}"
