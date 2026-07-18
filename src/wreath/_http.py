"""HTTP/1.x request head parsing.

``parse_request(data)`` returns ``None`` while the head is incomplete, raises
``ValueError`` on malformed input, and otherwise returns a ``RequestHead``
with the header names already lowercased for ASGI.
"""

from __future__ import annotations

from typing import NamedTuple

from ._native import _core

if _core is not None:
    _parse = _core.http_parse_request
else:
    from ._pure.http import http_parse_request as _parse


class RequestHead(NamedTuple):
    method: str
    target: bytes
    minor_version: int
    headers: list[tuple[bytes, bytes]]
    consumed: int


def parse_request(data: bytes) -> RequestHead | None:
    parsed = _parse(data)
    if parsed is None:
        return None
    return RequestHead(*parsed)


__all__ = ["RequestHead", "parse_request"]
