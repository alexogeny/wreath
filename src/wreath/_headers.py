"""Header-list helpers over ASGI-style `list[tuple[bytes, bytes]]`.

Names are expected lowercase, as the ASGI spec guarantees for servers.
"""

from __future__ import annotations

from collections.abc import Callable

from ._native import _core

#: The annotations are the point. A compiled function is `Any` to a type
#: checker, so a facade that only rebinds tells its callers nothing and they
#: infer `Any` onward -- this module is where `request.py` learned that a header
#: map is `dict[str, str]` and not `dict[Any, None | Any]`.

#: `find_header(headers, name)` -- the first value for a lowercase name, or
#: `None`. Repeated fields keep their order; the first wins.
find_header: Callable[[list[tuple[bytes, bytes]], bytes], bytes | None] = _core.find_header

#: `build_header_map(headers)` -- one pass to a name-to-value index, first
#: occurrence winning. **Bytes on both sides, not decoded**: the caller decodes
#: latin-1 at the point it hands a value out, so the index costs no decode for
#: the headers nobody reads.
build_header_map: Callable[[list[tuple[bytes, bytes]]], dict[bytes, bytes]] = _core.build_header_map

validate_response_headers: Callable[
    [list[tuple[bytes, bytes]]], tuple[bool, bytes | None]
] = _core.validate_response_headers

__all__ = ["build_header_map", "find_header", "validate_response_headers"]
