"""multipart/form-data parsing for complete request bodies."""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from ._native import _core

#: `multipart_parse(body, boundary, max_parts, max_part_header_bytes,
#: max_part_bytes, factory, part_type)` -- the scanner and materializer.
_raw_parse: Callable[..., list[Part]] = _core.multipart_parse


class Part(NamedTuple):
    name: str | None
    filename: str | None
    headers: list[tuple[bytes, bytes]]
    data: bytes


def parse(
    body: bytes,
    boundary: bytes,
    max_parts: int = -1,
    max_part_header_bytes: int = -1,
    max_part_bytes: int = -1,
) -> list[Part]:
    """Split a complete multipart body into parts. A negative limit means none.

    The limits are enforced inside the parser, before an over-budget part is
    copied out of the body, and behave identically in the C and Python
    parsers.
    """
    return _raw_parse(
        body,
        boundary,
        max_parts,
        max_part_header_bytes,
        max_part_bytes,
        None,
        Part,
    )


__all__ = ["Part", "parse"]
