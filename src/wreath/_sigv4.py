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
_MAX_PRESIGN_SECONDS = 7 * 24 * 60 * 60
_PRESIGN_AUTH_PARAMS = frozenset(
    {
        "x-amz-algorithm",
        "x-amz-credential",
        "x-amz-date",
        "x-amz-expires",
        "x-amz-security-token",
        "x-amz-signature",
        "x-amz-signedheaders",
    }
)
_HTTP_FIELD_NAME = frozenset(
    "!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)

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


def _check_header_field(name: str, value: str) -> None:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{name} must not contain control characters")


def _normalize_headers(values: dict[str, str], owner: str) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in values.items():
        if not name or any(character not in _HTTP_FIELD_NAME for character in name):
            raise ValueError(f"{owner} contains invalid HTTP field name {name!r}")
        normalized_name = name.lower()
        if normalized_name == "host":
            raise ValueError(f"{owner} must not redefine host; pass it with the host parameter")
        if normalized_name in normalized:
            raise ValueError(f"{owner} contains duplicate case-insensitive name {name!r}")
        if "\r" in value or "\n" in value:
            raise ValueError(f"{owner} contains control characters in {name!r}")
        normalized[normalized_name] = value
    return normalized


def _check_host(host: str) -> None:
    if (
        not host
        or len(host) > 1024
        or any(character.isspace() or character in "/\\?#@" for character in host)
    ):
        raise ValueError("host must be an HTTP authority without userinfo, paths, or controls")
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("host must be an HTTP authority encoded as ASCII") from None


def uri_encode(value: str, *, encode_slash: bool = True) -> str:
    """RFC 3986 percent-encoding with AWS's unreserved set (`A-Za-z0-9-._~`)."""
    return quote(value, safe="" if encode_slash else "/")


def signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


class _SigningKeyCache:
    __slots__ = ("_entry",)

    def __init__(self) -> None:
        self._entry: tuple[tuple[str, str, str, str], bytes] | None = None

    def get(self, secret: str, date_stamp: str, region: str, service: str) -> bytes:
        scope = (secret, date_stamp, region, service)
        entry = self._entry
        if entry is not None and entry[0] == scope:
            return entry[1]
        key = signing_key(secret, date_stamp, region, service)
        self._entry = (scope, key)
        return key


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
    _prevalidated: bool = False,
    _key_cache: _SigningKeyCache | None = None,
) -> dict[str, str]:
    """Return the headers to add for a header-auth SigV4 request (`Authorization` etc.)."""
    if not _prevalidated:
        _check_host(host)
        for name, value in (
            ("access_key", access_key),
            ("region", region),
            ("service", service),
            ("amz_date", amz_date),
            ("session_token", session_token or ""),
            ("payload_hash", payload_hash or ""),
        ):
            _check_header_field(name, value)
    date_stamp = amz_date[:8]
    normalized_headers = (
        {name.lower(): value for name, value in (headers or {}).items()}
        if _prevalidated
        else _normalize_headers(headers or {}, "headers")
    )
    normalized_headers["host"] = host
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
        signing_key(secret_key, date_stamp, region, service)
        if _key_cache is None
        else _key_cache.get(secret_key, date_stamp, region, service),
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
    _key_cache: _SigningKeyCache | None = None,
) -> str:
    """Return a fully-formed SigV4 presigned URL (auth in the query string, no network)."""
    if type(expires) is not int or not 1 <= expires <= _MAX_PRESIGN_SECONDS:
        raise ValueError("expires must be an integer from 1 through 604800")
    if scheme not in ("http", "https"):
        raise ValueError("scheme must be 'http' or 'https'")
    _check_host(host)
    normalized_signed_headers = _normalize_headers(signed_headers or {}, "signed_headers")
    date_stamp = amz_date[:8]
    scope = _scope(date_stamp, region, service)
    headers = {
        "host": host,
        **normalized_signed_headers,
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
        for name, value in extra_params:
            if name.lower() in _PRESIGN_AUTH_PARAMS:
                raise ValueError(f"extra_params contains reserved SigV4 query parameter {name!r}")
            params.append((name, value))
    canonical, _ = canonical_request(method, path, params, headers, payload_hash)
    signing_value = string_to_sign(amz_date, scope, canonical)
    signature = hmac.new(
        signing_key(secret_key, date_stamp, region, service)
        if _key_cache is None
        else _key_cache.get(secret_key, date_stamp, region, service),
        signing_value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    # The signature is appended last (not part of the signed query), per AWS convention.
    query = "&".join(f"{uri_encode(name)}={uri_encode(value)}" for name, value in sorted(params))
    query += f"&X-Amz-Signature={signature}"
    return f"{scheme}://{host}{uri_encode(path, encode_slash=False)}?{query}"
