"""Browser policy helpers with optional native acceleration."""

from __future__ import annotations

from ._native import _core

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
    replace_content_length = _core.replace_content_length
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
    "origin_matches",
    "replace_content_length",
    "select_content_encoding",
]
