"""Pure-Python twin of the native multipart/form-data parser."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

RawPart = tuple[list[tuple[bytes, bytes]], bytes]
PartFactory = Callable[[list[tuple[bytes, bytes]], bytes], Any]


def _parse_part_headers(block: bytes) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    if not block:
        return headers
    for line in block.split(b"\r\n"):
        name, sep, value = line.partition(b":")
        if not sep or not name:
            raise ValueError("malformed multipart header line")
        headers.append((name.lower(), value.strip(b" \t")))
    return headers


def multipart_parse(
    body: bytes,
    boundary: bytes,
    max_parts: int = -1,
    max_part_header_bytes: int = -1,
    max_part_bytes: int = -1,
    part_factory: PartFactory | None = None,
) -> list[Any]:
    """Split a complete multipart body. A negative limit means no limit.

    The limits must reject exactly what the native parser rejects, with the
    same exception type and message: `tests/test_native_parity.py` compares the
    two parsers directly.
    """
    if not 1 <= len(boundary) <= 70:
        raise ValueError("boundary must be 1-70 bytes")
    delimiter = b"\r\n--" + boundary

    if body.startswith(b"--" + boundary):
        pos = len(boundary) + 2
    else:
        first = body.find(delimiter)
        if first < 0:
            raise ValueError("multipart boundary not found")
        pos = first + len(delimiter)

    parts: list[Any] = []
    while True:
        if body[pos : pos + 2] == b"--":
            break
        while pos < len(body) and body[pos] in b" \t":
            pos += 1
        if body[pos : pos + 2] != b"\r\n":
            raise ValueError("malformed multipart boundary line")
        pos += 2

        # Locate the header block and the part body without copying either yet:
        # the limits below must reject an oversized part before it is sliced.
        if body[pos : pos + 2] == b"\r\n":
            headers_end = pos
            body_start = pos + 2
        else:
            headers_end = body.find(b"\r\n\r\n", pos)
            if headers_end < 0:
                raise ValueError("unterminated multipart headers")
            body_start = headers_end + 4

        next_boundary = body.find(delimiter, body_start)
        if next_boundary < 0:
            raise ValueError("unterminated multipart part")

        # Checked in the same order as the native parser, so that a body over
        # several limits at once fails with the same error in both.
        if 0 <= max_parts <= len(parts):
            raise ValueError(f"multipart form has more than {max_parts} parts")
        if 0 <= max_part_header_bytes < headers_end - pos:
            raise ValueError(
                f"multipart part headers exceed {max_part_header_bytes} bytes"
            )
        if 0 <= max_part_bytes < next_boundary - body_start:
            raise ValueError(f"multipart part exceeds {max_part_bytes} bytes")

        header_block = body[pos:headers_end]
        headers = _parse_part_headers(header_block)
        content = body[body_start:next_boundary]
        parts.append(
            (headers, content) if part_factory is None else part_factory(headers, content)
        )
        pos = next_boundary + len(delimiter)
    return parts
