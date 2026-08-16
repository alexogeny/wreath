"""HTTP/1.x request head parsing.

`parse_request(data)` returns `None` while the head is incomplete, raises
`ValueError` on malformed input, and otherwise returns a `RequestHead`
with the header names already lowercased for ASGI.

Both tiers return a plain 5-tuple, and this wraps it in the `NamedTuple` so
callers read fields by name rather than by index. That is the only reason the
wrapper exists, and being a `NamedTuple` it is still an ordinary tuple, so the
two implementations stay interchangeable and index access keeps working.

`None` and `ValueError` mean different things and the caller must keep them
apart -- "not yet" versus "never". Collapsing them would either stall a
connection on garbage or reject a head that had merely not finished arriving.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import NamedTuple

from ._native import _core

#: `http_parse_request(data)` -- the 5-tuple `RequestHead` wraps, or `None`
#: while the head is incomplete. Annotated because a compiled function is `Any`,
#: and `parse_request`'s own return type is only as good as this.
_parse: Callable[[bytes], tuple[str, bytes, int, list[tuple[bytes, bytes]], int] | None] = (
    _core.http_parse_request
)

_HTTP_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def _is_http_token(value: str) -> bool:
    """Whether `value` is one non-empty RFC 9110 token."""
    return _HTTP_TOKEN.fullmatch(value) is not None


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
