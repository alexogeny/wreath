"""Pure-Python twins of the native JSON encoder and decoder.

The encoder is built on the stdlib encoder with the same strictness as the C
version: bytes output, str-only object keys, and non-finite floats rejected.
The decoder twin is stdlib ``json.loads`` itself — the native decoder is
written to match its observable behaviour exactly.
"""

from __future__ import annotations

import json
import math
from typing import Any

#: Set once by :mod:`wreath._json`, mirroring ``_core.json_configure``. Both stay
#: None until then, and the encoder raises its ordinary TypeError in that case.
_temporal_types: tuple[type, ...] | None = None
_format_iso: Any = None


def json_configure(temporal_types: tuple[type, ...], format_iso: Any) -> None:
    """Teach the encoder to render temporal values inline, as ``json.c`` does."""
    global _temporal_types, _format_iso
    _temporal_types = temporal_types
    _format_iso = format_iso


def _default(value: Any) -> Any:
    """Render a temporal value, or re-raise the encoder's own TypeError."""
    if _temporal_types is not None and isinstance(value, _temporal_types):
        return _format_iso(value)
    raise TypeError(
        f"object of type {type(value).__name__} is not JSON serializable"
    )


_encode = json.JSONEncoder(
    ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=_default
).encode

json_loads = json.loads


def json_dumps(obj: Any) -> bytes:
    _check(obj)
    return _encode(obj).encode("utf-8")


def _check(obj: Any) -> None:
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("JSON values must be finite numbers")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"JSON object keys must be str, got {type(key).__name__}"
                )
            _check(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _check(item)
    # Anything else -- including a temporal value -- is left to `_default`.
