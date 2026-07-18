"""Pure-Python security matcher twins."""

from __future__ import annotations

import base64
import hmac
import os
import re

_TOKEN_COMPONENT = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TOKEN_VERSION = "v1"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def csrf_sign(secret: bytes, issued: int, nonce: str) -> str:
    """Sign one CSRF token body. See ADR 0018 and the C twin in security.c."""
    if len(nonce) != 43:
        raise ValueError("csrf nonce must be 43 characters")
    message = f"{_TOKEN_VERSION}.{issued}.{nonce}"
    signature = hmac.digest(secret, message.encode("ascii"), "sha256")
    return f"{message}.{_b64encode(signature)}"


def csrf_new_token(secret: bytes, issued: int) -> str:
    return csrf_sign(secret, issued, _b64encode(os.urandom(32)))


def csrf_validate(secret: bytes, token: str, now: int, max_age: int) -> tuple[bool, int]:
    """Return (signature is valid and fresh, issued time).

    The issued time is reported even when the token has expired, so the caller
    can renew rather than reject.
    """
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != _TOKEN_VERSION:
        return False, 0
    try:
        issued = int(parts[1])
    except ValueError:
        return False, 0
    # Python's int is arbitrary precision and C's strtoll is not, so a token
    # claiming a 26-digit issue time would be rejected by both twins but with
    # different `issued` values. No caller reads `issued` from a rejected
    # token, but the twins must still agree exactly, and no real timestamp
    # lives outside int64.
    if not -(2**63) <= issued < 2**63:
        return False, 0
    if not _TOKEN_COMPONENT.fullmatch(parts[2]) or not _TOKEN_COMPONENT.fullmatch(parts[3]):
        return False, 0
    if issued > now + 60 or now - issued > max_age:
        return False, issued
    message = f"{_TOKEN_VERSION}.{issued}.{parts[2]}".encode("ascii")
    expected = _b64encode(hmac.digest(secret, message, "sha256"))
    return hmac.compare_digest(expected, parts[3]), issued


def host_allowed(host: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        if pattern == "*" or host == pattern:
            return True
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
    return False


__all__ = ["csrf_new_token", "csrf_sign", "csrf_validate", "host_allowed"]
