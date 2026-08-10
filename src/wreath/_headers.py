"""Header-list helpers over ASGI-style `list[tuple[bytes, bytes]]`.

Names are expected lowercase, as the ASGI spec guarantees for servers.
"""

from __future__ import annotations

from ._native import _core

find_header = _core.find_header
build_header_map = _core.build_header_map

__all__ = ["build_header_map", "find_header"]
