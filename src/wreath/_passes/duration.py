"""Reading a time budget out of a declaration.

Its own module because two range sources need it and neither may import the
other's: `wreath.passes` holds `Rows` and imports `Buckets` from
`wreath._passes.buckets`, so anything both use has to sit underneath both.
"""

from __future__ import annotations

from typing import Any

from .._duration import decimal_unit
from .keyset import PassDeclarationError

#: `d` is here so this and `wreath.series`'s compact spelling really are
#: one syntax -- `series.py` claimed they were while accepting a scale this did
#: not, so `seal(after="3d")` parsed and `Rows(within="3d")` did not.
#:
#: A day is admissible where a month is not, and the difference is not taste:
#: every caller here is an *elapsed* budget (a chunk's time, a shift's length, a
#: frontier's lateness) and a day is a fixed 86,400 seconds, whereas
#: `wreath.temporal.parse_duration` refuses months and years precisely
#: because they are not a fixed number of seconds --
#: `Series.compare(previous=Bucket)` depends on that refusal.
_SCALE = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def seconds(value: Any, *, what: str, allow_zero: bool = False) -> float:
    """Read `"2s"`, `"250ms"`, `"5m"`, `"1d"` or a plain number of seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        total = float(value)
    elif isinstance(value, str):
        parsed = decimal_unit(value)
        if parsed is None or (parsed[1] and parsed[1] not in _SCALE):
            raise PassDeclarationError(
                f"{what} must be a number of seconds or a duration like '2s', "
                f"'250ms', '5m', '1d'; got {value!r}"
            )
        number, unit = parsed
        total = float(number) * _SCALE[unit or "s"]
    else:
        raise PassDeclarationError(f"{what} must be a duration; got {value!r}")
    if total < 0 or (total == 0 and not allow_zero):
        raise PassDeclarationError(f"{what} must be positive; got {value!r}")
    return total


__all__ = ["seconds"]
