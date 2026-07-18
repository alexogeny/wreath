"""Pure-Python twin of the native HTTP/1.x request head parser."""

from __future__ import annotations

_TOKEN_CHARS = frozenset(
    b"!#$%&'*+-.^_`|~0123456789"
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)

ParsedRequest = tuple[str, bytes, int, list[tuple[bytes, bytes]], int]


def _is_token(data: bytes) -> bool:
    return len(data) > 0 and all(c in _TOKEN_CHARS for c in data)


def _parse_header_line(line: bytes) -> tuple[bytes, bytes]:
    if line[:1] in (b" ", b"\t"):
        raise ValueError("obsolete header line folding is not supported")
    name, separator, value = line.partition(b":")
    if not separator or not _is_token(name):
        raise ValueError("malformed header name")
    value = value.strip(b" \t")
    for byte in value:
        if byte != 0x09 and (byte < 0x20 or byte == 0x7F):
            raise ValueError("invalid header value byte")
    return name.lower(), value


def http_parse_request(data: bytes) -> ParsedRequest | None:
    head_end = data.find(b"\r\n\r\n")
    if head_end < 0:
        return None
    consumed = head_end + 4
    lines = data[: head_end + 2].split(b"\r\n")

    request_line = lines[0]
    fields = request_line.split(b" ")
    if len(fields) != 3:
        raise ValueError("malformed request line")
    method, target, version = fields
    if not _is_token(method):
        raise ValueError("malformed request line")
    if not target or any(c <= 0x20 or c == 0x7F for c in target):
        raise ValueError("malformed request target")
    if len(version) != 8 or not version.startswith(b"HTTP/1.") or version[7:] not in (
        b"0",
        b"1",
    ):
        raise ValueError("malformed HTTP version")
    minor_version = version[7] - 0x30

    headers: list[tuple[bytes, bytes]] = []
    for line in lines[1:-1]:
        headers.append(_parse_header_line(line))

    return method.decode("ascii"), target, minor_version, headers, consumed
