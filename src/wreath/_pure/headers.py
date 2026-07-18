"""Pure-Python twin of the native header helpers."""

from __future__ import annotations

from collections.abc import Iterable

HeaderList = Iterable[tuple[bytes, bytes]]


def _check_pair(pair: object) -> tuple[bytes, bytes]:
    if not isinstance(pair, (tuple, list)) or len(pair) != 2:
        raise TypeError("header entries must be two-item tuples")
    key, value = pair
    if not isinstance(key, bytes) or not isinstance(value, bytes):
        raise TypeError("header names and values must be bytes")
    return key, value


def find_header(headers: HeaderList, name: bytes) -> bytes | None:
    for pair in headers:
        key, value = _check_pair(pair)
        if key == name:
            return value
    return None


def build_header_map(headers: HeaderList) -> dict[bytes, bytes]:
    header_map: dict[bytes, bytes] = {}
    for pair in headers:
        key, value = _check_pair(pair)
        if key not in header_map:
            header_map[key] = value
    return header_map
