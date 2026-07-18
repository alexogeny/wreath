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

_encode = json.JSONEncoder(
    ensure_ascii=False, separators=(",", ":"), allow_nan=False
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
