"""Pure-Python twins for browser response/request policy helpers."""

from __future__ import annotations

from urllib.parse import urlsplit

NO_TRANSFORM = 1
NO_STORE = 2
PRIVATE = 4
PUBLIC = 8


def _quality(parameters: list[bytes]) -> float:
    quality = 1.0
    for parameter in parameters:
        name, separator, value = parameter.strip().partition(b"=")
        if name.lower() != b"q":
            continue
        if not separator:
            return 0.0
        try:
            text = value.decode("ascii")
            if text not in {"0", "1"}:
                whole, dot, fraction = text.partition(".")
                if not dot or whole not in {"0", "1"} or not 1 <= len(fraction) <= 3:
                    return 0.0
                if not fraction.isdigit() or (whole == "1" and any(c != "0" for c in fraction)):
                    return 0.0
            quality = float(text)
        except (UnicodeDecodeError, ValueError):
            return 0.0
    return quality


def select_content_encoding(accept_encoding: bytes) -> str | None:
    gzip_quality: float | None = None
    wildcard_quality: float | None = None
    for raw_item in accept_encoding.split(b","):
        pieces = raw_item.split(b";")
        coding = pieces[0].strip().lower()
        if not coding:
            continue
        quality = _quality(pieces[1:])
        if coding == b"gzip":
            gzip_quality = quality
        elif coding == b"*":
            wildcard_quality = quality
    if gzip_quality is not None:
        return "gzip" if gzip_quality > 0 else None
    return "gzip" if wildcard_quality is not None and wildcard_quality > 0 else None


def is_compressible_content_type(content_type: bytes) -> bool:
    media_type = content_type.partition(b";")[0].strip().lower()
    return (
        media_type.startswith(b"text/")
        or media_type
        in {
            b"application/json",
            b"application/problem+json",
            b"application/javascript",
            b"application/xml",
            b"image/svg+xml",
        }
        or media_type.startswith(b"application/")
        and (media_type.endswith(b"+json") or media_type.endswith(b"+xml"))
    )


def cache_control_flags(value: bytes) -> int:
    flags = 0
    for raw in value.split(b","):
        directive = raw.strip().split(b"=", 1)[0].lower()
        if directive == b"no-transform":
            flags |= NO_TRANSFORM
        elif directive == b"no-store":
            flags |= NO_STORE
        elif directive == b"private":
            flags |= PRIVATE
        elif directive == b"public":
            flags |= PUBLIC
    return flags


def _normalized_origin(value: bytes) -> bytes | None:
    if value == b"null":
        return value
    try:
        text = value.decode("ascii")
        parsed = urlsplit(text)
        port = parsed.port
    except (UnicodeDecodeError, ValueError):
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return None
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = 80 if scheme == "http" else 443
    authority = host if port is None or port == default else f"{host}:{port}"
    return f"{scheme}://{authority}".encode("ascii")


def origin_matches(origin: bytes, allowed: tuple[bytes, ...]) -> bool:
    normalized = _normalized_origin(origin)
    return normalized is not None and normalized in allowed


def _validated_headers(headers: list[tuple[bytes, bytes]]) -> None:
    for pair in headers:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], bytes)
            or not isinstance(pair[1], bytes)
        ):
            raise TypeError("response headers must be two-item bytes tuples")


def find_response_header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    target = name.lower()
    for key, value in headers:
        if key.lower() == target:
            return value
    return None


def append_missing_headers(
    headers: list[tuple[bytes, bytes]], additions: tuple[tuple[bytes, bytes], ...]
) -> None:
    _validated_headers(headers)
    items = tuple(additions)
    for pair in items:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], bytes)
            or not isinstance(pair[1], bytes)
        ):
            raise TypeError("header additions must be two-item bytes tuples")
    existing = {name.lower() for name, _value in headers}
    for name, value in items:
        normalized = name.lower()
        if normalized not in existing:
            headers.append((name, value))
            existing.add(normalized)


def append_vary(headers: list[tuple[bytes, bytes]], token: bytes) -> None:
    _validated_headers(headers)
    target = token.strip().lower()
    if not target:
        raise ValueError("Vary token must not be empty")
    vary_indexes: list[int] = []
    tokens: list[bytes] = []
    seen: set[bytes] = set()
    for index, (name, value) in enumerate(headers):
        if name.lower() != b"vary":
            continue
        vary_indexes.append(index)
        for item in value.split(b","):
            normalized = item.strip().lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                tokens.append(normalized)
    if b"*" in seen:
        merged = b"*"
    else:
        if target not in seen:
            tokens.append(target)
        merged = b", ".join(tokens)
    if vary_indexes:
        first = vary_indexes[0]
        headers[first] = (b"vary", merged)
        for index in reversed(vary_indexes[1:]):
            del headers[index]
    else:
        headers.append((b"vary", merged))


def replace_content_length(headers: list[tuple[bytes, bytes]], length: int | None) -> None:
    _validated_headers(headers)
    if length is not None and length < 0:
        raise ValueError("content length must be non-negative")
    headers[:] = [(key, value) for key, value in headers if key.lower() != b"content-length"]
    if length is not None:
        headers.append((b"content-length", str(length).encode("ascii")))


def replace_response_header(
    headers: list[tuple[bytes, bytes]], name: bytes, value: bytes
) -> None:
    """Replace every instance of one response header with one current value."""
    _validated_headers(headers)
    target = name.lower()
    headers[:] = [(key, item) for key, item in headers if key.lower() != target]
    headers.append((name, value))


def replace_cookie(
    headers: list[tuple[bytes, bytes]], prefix: bytes, value: bytes
) -> None:
    """Replace one cookie's Set-Cookie line, preserving every other cookie."""
    _validated_headers(headers)
    headers[:] = [
        (key, item)
        for key, item in headers
        if not (key.lower() == b"set-cookie" and item.startswith(prefix))
    ]
    headers.append((b"set-cookie", value))


def replace_server_timing(
    headers: list[tuple[bytes, bytes]], metric: bytes, value: bytes
) -> None:
    """Replace one Server-Timing metric while retaining every other metric."""
    _validated_headers(headers)
    retained: list[bytes] = []
    target = metric.strip().lower()
    for key, item in headers:
        if key.lower() != b"server-timing":
            continue
        for entry in item.split(b","):
            entry = entry.strip()
            name = entry.partition(b";")[0].strip().lower()
            if entry and name != target:
                retained.append(entry)
    headers[:] = [
        (key, item) for key, item in headers if key.lower() != b"server-timing"
    ]
    retained.append(value)
    headers.append((b"server-timing", b", ".join(retained)))


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
    "origin_matches",
    "replace_cookie",
    "replace_content_length",
    "replace_response_header",
    "replace_server_timing",
    "select_content_encoding",
]
