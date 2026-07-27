"""Compact JSON with a native encoder and decoder.

``dumps`` returns UTF-8 bytes (orjson-style) and is stricter than stdlib
``json.dumps``: object keys must be str and non-finite floats raise
ValueError. ``loads`` matches stdlib ``json.loads`` semantics (including the
NaN/Infinity constants and lone surrogate escapes) and accepts str, bytes,
or bytearray.

Dates, times, datetimes, and durations encode as ISO-8601 strings, so a handler
never writes ``.isoformat()`` by hand and two endpoints cannot spell the same
moment differently. That happens on a **retry**, not on the way in: the encoder
is tried as-is first, and only a ``TypeError`` triggers the walk that rewrites
temporal values. A payload with no temporal values therefore pays no walk at
all — the cost lands only on the payloads that need it.

What it does cost every JSON response is **one Python frame**, because this
facade is now a function rather than a direct binding to the encoder. It adds
no Python/native boundary crossing (``wreath-request-trace`` is unchanged), and
a frame is small against a serialization measured in microseconds — but that
last part is an expectation, not a measurement, and AGENTS.md does not let it
be stated as one. If ``wreath-decomp`` ever attributes a response-path delta
here, the way to remove the frame is to teach the encoders about temporal
values directly, which means changing ``_native/json.c`` and its pure twin
together and byte-for-byte.
"""

from __future__ import annotations

from typing import Any

from ._native import _core

if _core is not None:
    _dumps = _core.json_dumps
    loads = _core.json_loads
else:
    from ._pure.json import json_dumps as _dumps
    from ._pure.json import json_loads as loads


def dumps(obj: Any) -> bytes:
    """Encode ``obj`` as compact UTF-8 JSON, rendering temporal values as ISO-8601."""
    try:
        return _dumps(obj)
    except TypeError:
        # Either a temporal value the encoders do not know, or something
        # genuinely unserializable. `jsonable` rewrites the first and leaves the
        # second alone, so the retry re-raises the encoder's own error for
        # anything it could not help with -- the message stays accurate.
        from .temporal import jsonable

        return _dumps(jsonable(obj))


__all__ = ["dumps", "loads"]
