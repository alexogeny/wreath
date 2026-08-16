"""PostgreSQL's bounded identifier vocabulary, stated once."""

from __future__ import annotations

import re
from typing import Final

MAX_IDENTIFIER_BYTES: Final = 63
# Unquoted names fold to lower case. `fullmatch` is deliberate: `$` also
# matches immediately before a trailing newline, so anchored `^...$` once
# accepted a declaration whose emitted DDL named a different object.
_UNQUOTED_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_$]*")


def validate_identifier(value: str, kind: str) -> str:
    """Validate the quoted identifier spelling used for channels and queues."""
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 63:
        raise ValueError(f"{kind} must be 1..63 bytes: {value!r}")
    for character in value:
        if not (character.isascii() and (character.isalnum() or character in "_$")):
            raise ValueError(f"invalid {kind} character {character!r} in {value!r}")
    return value


def validate_unquoted_identifier(
    value: str,
    kind: str,
    *,
    error: type[Exception] = ValueError,
) -> str:
    """Validate the lower-case, unquoted identifier form used by ORM DDL."""
    if not isinstance(value, str) or not _UNQUOTED_IDENTIFIER.fullmatch(value):
        raise error(
            f"{kind} {value!r} is not a plain SQL identifier: use a lower-case "
            "unquoted PostgreSQL identifier made of letters, digits, underscores, "
            "and dollar signs, starting with a letter or underscore"
        )
    if len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
        raise error(
            f"{kind} {value!r} exceeds PostgreSQL's {MAX_IDENTIFIER_BYTES}-byte limit"
        )
    return value


__all__ = [
    "MAX_IDENTIFIER_BYTES",
    "validate_identifier",
    "validate_unquoted_identifier",
]
