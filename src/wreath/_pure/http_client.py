"""Pure-Python HTTP/1.1 client codec primitives.

This starts as the executable specification for the native outbound client. It
owns no sockets or pool policy: callers provide validated origin information and
receive or send complete byte fragments.
"""

from __future__ import annotations

from collections.abc import Iterable

from .http import _is_token, _parse_header_line

ParsedResponseHead = tuple[int, int, bytes, list[tuple[bytes, bytes]], int]


def _field_value_is_valid(value: bytes) -> bool:
    return all(c == 0x09 or 0x20 <= c < 0x7F for c in value)


def _validated_method(method: str) -> bytes:
    try:
        encoded = method.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("invalid HTTP method") from error
    if not _is_token(encoded):
        raise ValueError("invalid HTTP method")
    return encoded


def _validate_target(target: bytes) -> None:
    if not target or (not target.startswith(b"/") and target != b"*"):
        raise ValueError("invalid request target")
    if any(c <= 0x20 or c == 0x7F for c in target):
        raise ValueError("invalid request target")


def serialize_request(
    method: str,
    target: bytes,
    host: bytes,
    *,
    headers: Iterable[tuple[bytes, bytes]] = (),
    body: bytes | bytearray | memoryview = b"",
) -> bytes:
    """Serialize one fixed-length HTTP/1.1 request.

    Streaming/chunked requests belong to the connection protocol. This helper
    deliberately owns ``Host`` and body framing so callers cannot create an
    ambiguous request accidentally.
    """

    encoded_method = _validated_method(method)
    _validate_target(target)
    if not host or not _field_value_is_valid(host):
        raise ValueError("invalid host")

    normalized_headers: list[tuple[bytes, bytes]] = []
    for name, value in headers:
        if not _is_token(name):
            raise ValueError("invalid header name")
        if not _field_value_is_valid(value):
            raise ValueError("invalid header value")
        lowered = name.lower()
        if lowered == b"host":
            raise ValueError("host header is owned by the client")
        if lowered == b"content-length":
            raise ValueError("content-length is owned by the client")
        if lowered == b"transfer-encoding":
            raise ValueError("transfer-encoding requires streaming mode")
        normalized_headers.append((lowered, value))

    payload = bytes(body)
    parts = [encoded_method, b" ", target, b" HTTP/1.1\r\nhost: ", host, b"\r\n"]
    for name, value in normalized_headers:
        parts.extend((name, b": ", value, b"\r\n"))
    if payload:
        parts.extend((b"content-length: ", str(len(payload)).encode("ascii"), b"\r\n"))
    parts.extend((b"\r\n", payload))
    return b"".join(parts)


def parse_response_head(data: bytes) -> ParsedResponseHead | None:
    """Parse one HTTP/1.x response head from ``data``.

    Return ``None`` while incomplete. The consumed offset points immediately
    after the header terminator; body framing is a separate protocol concern.
    """

    head_end = data.find(b"\r\n\r\n")
    if head_end < 0:
        return None
    consumed = head_end + 4
    lines = data[:head_end].split(b"\r\n")
    if not lines:
        raise ValueError("malformed response status line")

    fields = lines[0].split(b" ", 2)
    if len(fields) < 2:
        raise ValueError("malformed response status line")
    version, status_data = fields[:2]
    reason = fields[2] if len(fields) == 3 else b""
    if len(version) != 8 or not version.startswith(b"HTTP/1.") or version[7:] not in (
        b"0",
        b"1",
    ):
        raise ValueError("malformed HTTP version")
    if len(status_data) != 3 or not status_data.isdigit():
        raise ValueError("malformed response status")
    status = int(status_data)
    if status < 100:
        raise ValueError("malformed response status")
    if not _field_value_is_valid(reason):
        raise ValueError("invalid response reason")

    headers: list[tuple[bytes, bytes]] = []
    for line in lines[1:]:
        headers.append(_parse_header_line(line))

    return version[7] - 0x30, status, reason, headers, consumed


def response_framing(
    method: str,
    status: int,
    headers: list[tuple[bytes, bytes]],
) -> tuple[str, int]:
    """Classify one final response body as none/chunked/length/close."""
    if method == "HEAD" or status in (204, 304):
        return "none", 0
    lengths = [value for name, value in headers if name == b"content-length"]
    encodings = [value for name, value in headers if name == b"transfer-encoding"]
    if encodings and lengths:
        raise ValueError("response has conflicting transfer-encoding and content-length")
    if encodings:
        tokens = [
            token.strip().lower()
            for value in encodings
            for token in value.split(b",")
        ]
        if not tokens or tokens[-1] != b"chunked" or tokens.count(b"chunked") != 1:
            raise ValueError("unsupported response transfer-encoding")
        return "chunked", -1
    if lengths:
        if any(value != lengths[0] for value in lengths[1:]):
            raise ValueError("response has conflicting content-length values")
        if not lengths[0] or not lengths[0].isdigit():
            raise ValueError("invalid response content-length")
        return "length", int(lengths[0])
    return "close", -1


def response_keeps_alive(
    minor: int,
    headers: list[tuple[bytes, bytes]],
    framed: bool,
) -> bool:
    """Decide whether a framed HTTP/1.x response connection is reusable."""
    connection_tokens = {
        token.strip().lower()
        for name, value in headers
        if name == b"connection"
        for token in value.split(b",")
    }
    reusable = framed and b"close" not in connection_tokens
    if minor == 0:
        reusable = reusable and b"keep-alive" in connection_tokens
    return reusable


__all__ = [
    "ParsedResponseHead",
    "parse_response_head",
    "response_framing",
    "response_keeps_alive",
    "serialize_request",
]
