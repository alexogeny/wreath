"""Pure-Python reference environment and constrained dotenv parsing."""

from __future__ import annotations

import os


def parse_dotenv(data: bytes) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(data.splitlines(), 1):
        if not raw_line:
            continue
        key, separator, value = raw_line.partition(b"=")
        if not separator or not key:
            raise ValueError(f"invalid dotenv entry on line {line_number}")
        if not (key[:1].isalpha() or key.startswith(b"_")) or not all(
            byte == 95 or 48 <= byte <= 57 or 65 <= byte <= 90 or 97 <= byte <= 122
            for byte in key
        ):
            raise ValueError(f"invalid dotenv key on line {line_number}")
        try:
            values[key.decode("ascii")] = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"invalid UTF-8 dotenv value on line {line_number}") from error
    return values


def read_osenv() -> dict[str, str]:
    return dict(os.environ)


__all__ = ["parse_dotenv", "read_osenv"]
