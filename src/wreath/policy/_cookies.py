from __future__ import annotations

from collections.abc import Iterable


def cookie_name_is_ambiguous(headers: Iterable[tuple[bytes, bytes]], name: str) -> bool:
    wanted = name.encode("ascii")
    seen = False
    for raw_name, value in headers:
        if raw_name.lower() != b"cookie":
            continue
        start = 0
        size = len(value)
        while start <= size:
            end = value.find(b";", start)
            if end < 0:
                end = size
            low = start
            while low < end and value[low] in (32, 9):
                low += 1
            high = end
            while high > low and value[high - 1] in (32, 9):
                high -= 1
            equals = value.find(b"=", low, high)
            if equals >= 0:
                name_high = equals
                while name_high > low and value[name_high - 1] in (32, 9):
                    name_high -= 1
                if name_high - low == len(wanted) and value.startswith(wanted, low, name_high):
                    if seen:
                        return True
                    seen = True
            if end == size:
                break
            start = end + 1
    return False
