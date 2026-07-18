"""Compact JSON with a native encoder and decoder.

``dumps`` returns UTF-8 bytes (orjson-style) and is stricter than stdlib
``json.dumps``: object keys must be str and non-finite floats raise
ValueError. ``loads`` matches stdlib ``json.loads`` semantics (including the
NaN/Infinity constants and lone surrogate escapes) and accepts str, bytes,
or bytearray.
"""

from __future__ import annotations

from ._native import _core

if _core is not None:
    dumps = _core.json_dumps
    loads = _core.json_loads
else:
    from ._pure.json import json_dumps as dumps
    from ._pure.json import json_loads as loads

__all__ = ["dumps", "loads"]
