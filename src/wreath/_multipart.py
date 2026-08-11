"""multipart/form-data parsing for complete request bodies.

The hot loop (boundary scanning, header splitting) runs in C when available;
content-disposition unpacking happens here since it touches a few dozen bytes
per part.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

from ._headers import find_header
from ._native import _core

#: `multipart_parse(body, boundary, max_parts, max_part_header_bytes,
#: max_part_bytes, factory)` -- the scanner, which calls `factory` per part
#: while it still owns the body view. `...` rather than a spelled argument list
#: because the last argument decides the element type, so the two cannot be
#: stated independently; what is worth pinning is that the result is a list of
#: whatever `factory` returned.
_raw_parse: Callable[..., list[Part]] = _core.multipart_parse


class Part(NamedTuple):
    name: str | None
    filename: str | None
    headers: list[tuple[bytes, bytes]]
    data: bytes


def _disposition_param(value: bytes, param: bytes) -> str | None:
    for fragment in value.split(b";"):
        key, sep, raw = fragment.strip(b" \t").partition(b"=")
        if not sep or key.strip(b" \t").lower() != param:
            continue
        raw = raw.strip(b" \t")
        if len(raw) >= 2 and raw.startswith(b'"') and raw.endswith(b'"'):
            raw = raw[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
        return raw.decode("utf-8", "replace")
    return None


def _make_part(headers: list[tuple[bytes, bytes]], data: Any) -> Part:
    disposition = find_header(headers, b"content-disposition")
    name = filename = None
    if disposition is not None:
        name = _disposition_param(disposition, b"name")
        filename = _disposition_param(disposition, b"filename")
    return Part(name, filename, headers, bytes(data))


def parse(
    body: bytes,
    boundary: bytes,
    max_parts: int = -1,
    max_part_header_bytes: int = -1,
    max_part_bytes: int = -1,
) -> list[Part]:
    """Split a complete multipart body into parts. A negative limit means none.

    The limits are enforced inside the parser, before an over-budget part is
    copied out of the body, and behave identically in the C and Python
    parsers.
    """
    # Convert each part while the low-level scanner owns it. This avoids a
    # second list of body views and a second Python pass over all parts.
    return _raw_parse(
        body,
        boundary,
        max_parts,
        max_part_header_bytes,
        max_part_bytes,
        _make_part,
    )


__all__ = ["Part", "parse"]
