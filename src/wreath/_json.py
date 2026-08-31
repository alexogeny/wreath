"""Compact JSON with a native encoder and decoder.

`dumps` returns UTF-8 bytes (orjson-style) and is stricter than stdlib
`json.dumps`: object keys must be str and non-finite floats raise
ValueError. `loads` matches stdlib `json.loads` semantics (including the
NaN/Infinity constants and lone surrogate escapes) and accepts str, bytes,
or bytearray.

Dates, times, datetimes, and durations encode as ISO-8601 strings, so a handler
never writes `.isoformat()` by hand and two endpoints cannot spell the same
moment differently. An object that defines `__jsonable__` is asked how it
would like to be encoded, which is how a result type goes back from a handler
without the caller unwrapping it first. Both are consumed at the native encoder
boundary: no failed first pass and no recursively rebuilt Python document.

What it does cost every JSON response is **one Python frame**, because this
facade is now a function rather than a direct binding to the encoder. It adds
no Python/native boundary crossing (`wreath-request-trace` is unchanged), and
a frame is small against a serialization measured in microseconds — but that
last part is an expectation, not a measurement, and AGENTS.md does not let it
be stated as one. If `wreath-decomp` ever attributes a response-path delta
here, the way to remove the frame is to teach the encoders about temporal
values directly, in `_native/json.c`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ._native import _core

#: `json_dumps(obj)` -- compact UTF-8 bytes, separators `,`/`:`, no space.
_dumps: Callable[[object], bytes] = _core.json_dumps

#: `json_loads(data)` -- `str`, `bytes` or `bytearray` to Python objects. The
#: return is `Any` on purpose and only here: a decoder's output type is the
#: caller's claim about the document, not something this can know.
loads: Callable[[str | bytes | bytearray], Any] = _core.json_loads

#: Execute the immutable instruction tuples produced by ``wreath.jsonpath``.
#: The mutable nodelist, Nothing sentinel, and visit budget belong to one call.
jsonpath_find: Callable[..., list[tuple[Any, tuple[str | int, ...]]]] = _core.jsonpath_find

#: `json_configure(temporal_types, format_iso)` -- installs the temporal hook.
_configure: Callable[[tuple[type, ...], Callable[[Any], str]], None] = _core.json_configure


def _install_temporal() -> None:
    """Let whichever encoder is selected render temporal values inline.

    Deferred to first use rather than done at import: `wreath.temporal` imports
    from this module's neighbours, and binding it eagerly here would order the
    two packages against each other.
    """
    global _temporal_installed
    from .temporal import _TEMPORAL, format_iso

    _configure(_TEMPORAL, format_iso)
    _temporal_installed = True


_temporal_installed = False


def dumps(obj: Any) -> bytes:
    """Encode `obj` as compact UTF-8 JSON, rendering temporal values as ISO-8601."""
    if not _temporal_installed:
        _install_temporal()
    return _dumps(obj)


__all__ = ["dumps", "jsonpath_find", "loads"]
