"""Pure-Python security matcher twins."""

from __future__ import annotations

import base64
import hmac
import os
import re

_TOKEN_COMPONENT = re.compile(r"^[A-Za-z0-9_-]{43}$")
#: Exactly what `strtoll(number, &end, 10)` in the C twin consumes whole: C
#: locale whitespace, an optional sign, and ASCII digits. `int()` is wider than
#: that -- it also takes Unicode digits (`int("\N{ARABIC-INDIC DIGIT ONE}")`),
#: `_` separators (`int("1_0")`), and trailing whitespace -- so parsing with it
#: unguarded made the twins report different `issued` values for the same
#: rejected token.
_TOKEN_STAMP = re.compile(r"[ \t\n\v\f\r]*[+-]?[0-9]+\Z")
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
    # The C twin copies this field into a 24-byte buffer before `strtoll`, so it
    # refuses anything that would not fit. Without the same bound, a stamp
    # padded to 24+ leading zeros parsed here to the value the padding hides and
    # was accepted where the twin rejected it.
    if not 0 < len(parts[1]) < 24 or _TOKEN_STAMP.fullmatch(parts[1]) is None:
        return False, 0
    issued = int(parts[1])
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
    # The native twin parses this argument as `str` and raises TypeError for
    # anything else. Keep that boundary identical: callers rely on an outer
    # malformed-host guard, and silently treating its accidental removal as a
    # normal non-match would make the pure and native configurations disagree.
    if not isinstance(host, str):
        raise TypeError("host must be str")
    for pattern in patterns:
        if pattern == "*" or host == pattern:
            return True
        if pattern.startswith("*."):
            suffix = pattern[1:]
            # The wildcard stands for at least one label, so the host must be
            # strictly longer than the suffix. Comparing against `suffix[1:]`
            # instead only excluded the bare parent (`example` for
            # `*.example`) and still admitted the empty-label host `.example`,
            # which the C twin rejects.
            if len(host) > len(suffix) and host.endswith(suffix):
                return True
    return False


__all__ = ["csrf_new_token", "csrf_sign", "csrf_validate", "host_allowed"]
