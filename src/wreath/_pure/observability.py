"""Pure-Python request-correlation and timing twins."""

from __future__ import annotations

_ID_CHARS = frozenset(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)


def request_id_valid(value: bytes, max_len: int) -> bool:
    data = bytes(value)
    if not data or len(data) > max_len:
        return False
    return _ID_CHARS.issuperset(data)


def format_server_timing(name: bytes, seconds: float) -> bytes:
    metric = bytes(name)
    if not 1 <= len(metric) <= 64:
        raise ValueError("metric name must be 1-64 bytes")
    return metric + b";dur=" + f"{seconds * 1000.0:.3f}".encode("ascii")


__all__ = ["format_server_timing", "request_id_valid"]
