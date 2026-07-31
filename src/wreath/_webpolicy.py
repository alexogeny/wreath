"""Browser policy helpers with optional native acceleration."""

from __future__ import annotations

from urllib.parse import urlsplit

from ._native import _core


def normalize_origin(value: str, *, label: str) -> bytes:
    """One trusted origin, as the exact bytes a browser will send.

    `origin_matches` compares bytes, so this is what makes that comparison
    correct: the scheme and host are case-folded, an IPv6 host is rebracketed,
    and a port is dropped when it is the scheme's default -- because a browser
    sends `https://app.example`, never `https://app.example:443`.

    Anything that is not a browser origin is refused rather than normalised:
    another scheme, userinfo, a path beyond `/`, a query, a fragment, or a port
    that is not a number. An entry that no browser can ever send would sit in an
    allowlist matching nothing while reading, to whoever wrote it, like cover.

    Shared by `middleware.csrf` and `middleware.security` because they defend
    the same thing -- a request arriving from an origin that is not yours -- and
    they had a byte-identical copy each, differing only in the noun in the error
    message. Two copies of a normaliser feeding an exact-bytes comparison is one
    fix away from a hole in whichever copy nobody remembered.

    Args:
        value: the configured origin, e.g. `https://app.example`.
        label: the noun for the error message ("trusted", "WebSocket"), so each
            caller's refusal still names the setting the reader was editing.

    Raises:
        ValueError: `value` is not a browser origin.
    """
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"invalid {label} origin: {value!r}")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"invalid {label} origin: {value!r}") from error
    default = 80 if scheme == "http" else 443
    authority = host if port is None or port == default else f"{host}:{port}"
    return f"{scheme}://{authority}".encode("ascii")

if _core is not None and hasattr(_core, "select_content_encoding"):
    NO_TRANSFORM = _core.NO_TRANSFORM
    NO_STORE = _core.NO_STORE
    PRIVATE = _core.PRIVATE
    PUBLIC = _core.PUBLIC
    append_missing_headers = _core.append_missing_headers
    append_vary = _core.append_vary
    cache_control_flags = _core.cache_control_flags
    find_response_header = _core.find_response_header
    is_compressible_content_type = _core.is_compressible_content_type
    origin_matches = _core.origin_matches
    replace_cookie = _core.replace_cookie
    replace_content_length = _core.replace_content_length
    replace_response_header = _core.replace_response_header
    replace_server_timing = _core.replace_server_timing
    select_content_encoding = _core.select_content_encoding
else:
    from ._pure.webpolicy import (
        NO_STORE,
        NO_TRANSFORM,
        PRIVATE,
        PUBLIC,
        append_missing_headers,
        append_vary,
        cache_control_flags,
        find_response_header,
        is_compressible_content_type,
        origin_matches,
        replace_content_length,
        replace_cookie,
        replace_response_header,
        replace_server_timing,
        select_content_encoding,
    )

__all__ = [
    "NO_STORE",
    "NO_TRANSFORM",
    "PRIVATE",
    "PUBLIC",
    "append_missing_headers",
    "append_vary",
    "cache_control_flags",
    "find_response_header",
    "is_compressible_content_type",
    "normalize_origin",
    "origin_matches",
    "replace_cookie",
    "replace_content_length",
    "replace_response_header",
    "replace_server_timing",
    "select_content_encoding",
]
