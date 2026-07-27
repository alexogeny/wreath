"""Reading a time budget out of a declaration.

Its own module because two range sources need it and neither may import the
other's: :mod:`wreath.passes` holds ``Rows`` and imports ``Buckets`` from
:mod:`wreath._passes.buckets`, so anything both use has to sit underneath both.
"""

from __future__ import annotations

import re
from typing import Any

from .keyset import PassDeclarationError

_DURATION = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(ms|s|m|h)?\s*$")
_SCALE = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def seconds(value: Any, *, what: str, allow_zero: bool = False) -> float:
    """Read ``"2s"``, ``"250ms"``, ``"5m"`` or a plain number of seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        total = float(value)
    elif isinstance(value, str):
        match = _DURATION.fullmatch(value)
        if match is None:
            raise PassDeclarationError(
                f"{what} must be a number of seconds or a duration like '2s', "
                f"'250ms', '5m'; got {value!r}"
            )
        total = float(match.group(1)) * _SCALE[match.group(2) or "s"]
    else:
        raise PassDeclarationError(f"{what} must be a duration; got {value!r}")
    if total < 0 or (total == 0 and not allow_zero):
        raise PassDeclarationError(f"{what} must be positive; got {value!r}")
    return total


__all__ = ["seconds"]
