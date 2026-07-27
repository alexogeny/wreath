"""Pure-Python twin of the native web codecs."""

from __future__ import annotations

_HEX = "0123456789abcdefABCDEF"


def percent_decode(data: bytes, plus_as_space: bool = False) -> bytes:
    if b"%" not in data:
        return data.replace(b"+", b" ") if plus_as_space else bytes(data)
    out = bytearray()
    i = 0
    length = len(data)
    while i < length:
        c = data[i]
        if c == 0x25 and i + 2 < length:
            hi = chr(data[i + 1])
            lo = chr(data[i + 2])
            if hi in _HEX and lo in _HEX:
                out.append(int(hi + lo, 16))
                i += 3
                continue
        if plus_as_space and c == 0x2B:
            out.append(0x20)
        else:
            out.append(c)
        i += 1
    return bytes(out)


def parse_qs(query: bytes, max_fields: int = 0) -> list[tuple[str, str]]:
    """Decode `a=1&b=2` pairs. `max_fields` > 0 bounds the field count,
    rejected while scanning before the offending field is decoded (0 = no bound).
    """
    pairs: list[tuple[str, str]] = []
    start = 0
    length = len(query)
    index = 0
    while index <= length:
        if index == length or query[index] == 0x26:  # b"&"
            if index > start:
                if max_fields and len(pairs) >= max_fields:
                    raise ValueError(f"urlencoded data exceeds {max_fields} fields")
                field = query[start:index]
                key, sep, value = field.partition(b"=")
                decoded_key = percent_decode(key, plus_as_space=True).decode(
                    "utf-8", "replace"
                )
                decoded_value = (
                    percent_decode(value, plus_as_space=True).decode("utf-8", "replace")
                    if sep
                    else ""
                )
                pairs.append((decoded_key, decoded_value))
            start = index + 1
        index += 1
    return pairs


def parse_cookies(header: bytes) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for fragment in header.split(b";"):
        fragment = fragment.strip(b" \t")
        if not fragment:
            continue
        name, sep, value = fragment.partition(b"=")
        if not sep or not name:
            continue
        decoded_name = name.decode("latin-1")
        if decoded_name not in cookies:
            cookies[decoded_name] = value.decode("latin-1")
    return cookies
