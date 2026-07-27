"""Keyset ranges over one ordered domain, and the refusals that keep them sound.

A chunked pass moves through a table by remembering the last key it finished and
asking for the next few rows *after* it. That is keyset paging, and it is the
whole reason a pass stays fast at ten million rows: `LIMIT c OFFSET k` must
produce and discard `k` rows before it returns any, so a walk of `N` rows in
chunks of `c` touches `N**2 / (2c)` rows in total, while ``WHERE key > $1
ORDER BY key LIMIT c` is an index descent plus `c`` rows per chunk --
`(N/c)*log N + N`. At ten million rows in ten-thousand-row chunks that is
5e12 against 1e7. This is a complexity argument and it needs no benchmark; what
it does need are two correctness conditions, and they are the refusals below.

**The index must exist.** Without one the database sorts the whole table once
per chunk, which is `N/c` sorts of `N` rows -- worse than the `OFFSET` this
was avoiding. So a key whose leading column has no index is refused rather than
silently degraded.

**A composite key is one row comparison.** `(herd_id, id) > ($1, $2)` is
answered by a single index scan on `(herd_id, id)`. The hand-expanded
`herd_id > $1 OR (herd_id = $1 AND id > $2)` means the same thing and is
planned as a bitmap-or over two scans followed by a sort. So the row-comparison
form is the only one this module emits. A row comparison also has no
mixed-direction form, so `(a ASC, b DESC)` is refused with that as the reason.

**The boundary must identify one row.** The cursor stores a key value, so if two
rows share the value that lands on a chunk boundary then `>` skips the
siblings (silent data loss whose counters still add up) and `>=` re-processes
them forever once one value has more rows than the chunk limit. There is no
third option, so a key that cannot be proven unique is refused, and the message
names the fix, which is always the same: append the primary key as a tiebreaker.

This module is deliberately free of any pass machinery -- no ledger, no
transaction, no job runner -- because `wreath.pagination` will want exactly
this when cursor pages land (design 20 §5.6), and it should inherit the
uniqueness refusal and the row-comparison rule rather than rediscover them.
"""

from __future__ import annotations

import datetime
import re
import uuid
from dataclasses import dataclass
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQL_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*$")

#: Column defaults the registry can prove assign values in increasing order, so
#: a row written after a ceiling was captured cannot land beneath it.
_MONOTONE_DEFAULTS = ("now()", "clock_timestamp()", "current_timestamp", "nextval(")

#: SQL type names whose values are timestamps, which is what a frontier measured
#: against the database clock needs its leading column to be.
_CLOCK_TYPES = frozenset(
    {"timestamptz", "timestamp", "timestamp with time zone", "timestamp without time zone"}
)

#: SQL type names a cursor does not yet survive.
#:
#: The original reason was that nothing could read a decimal back, so a cursor
#: returned through `float()`: `1.0000000000000000001` and `...002` both
#: decoded to `1.0`, two adjacent boundaries became one, and `>` skipped
#: every row between them. **That reason expired when the numeric codec landed**
#: -- `orm.types.BY_OID` carries one now -- and the ordering property was
#: swept over the real ledger path (`Decimal` -> `str` -> jsonb ->
#: `Decimal`): 403 values, 81,001 ordered pairs, zero value failures and zero
#: ordering failures, against 229 value failures and 5 collapsed pairs for the
#: float path it replaces.
#:
#: It stays refused for a different reason, found while measuring that one.
#: `wreath._passes.progress.position` places a key value on a line for
#: `progress=Keyspace()` and handles `int` and `float` but not `Decimal`,
#: while `_EXAMPLE` already maps `numeric` to `0.0`. So a numeric key would
#: pass `Keyspace.refuse` at declaration and then silently measure nothing at
#: runtime -- a check with nothing to check, which is worse than the refusal.
#:
#: Lifting this needs three coordinated changes, not one: empty this set, route
#: `numeric` to `Decimal` in `_decode_one`, and teach `position` about
#: `Decimal` *including the non-finites*. PostgreSQL orders `NaN` above every
#: other numeric and `Decimal("NaN") > x` **raises** rather than returning
#: `False`, so `float(Decimal("NaN"))` would hand the percentage arithmetic a
#: `nan` instead of an error. The walk itself is unaffected -- it never orders
#: in Python, and a NaN cursor correctly leaves zero rows remaining -- but the
#: progress path is, and that is the piece to design rather than bolt on.
_INEXACT_TYPES = frozenset({"numeric", "decimal"})


class PassDeclarationError(ValueError):
    """A pass that cannot be made correct, refused where it was declared.

    Every rule this module enforces is a data-loss or a never-terminates bug in
    disguise, and every one of them is visible from the declaration. Raising here
    -- at import time, with the column name and the fix in the message -- costs a
    failed start; raising at 3am costs a table.
    """


@dataclass(frozen=True, slots=True)
class Key:
    """One column of a pass's ordering key, with the facts the refusals need.

    The ORM fills these in from the model's own declaration. For a table the ORM
    does not own -- one of Wreath's own store tables, or a legacy table -- the
    caller states them, which is the honest arrangement: the pass cannot read a
    catalog it has not been given, and a rule you can only ask for should still
    be asked for out loud.

    Args:
        name: the column. Interpolated into SQL, so it must be a plain identifier.
        type: its SQL type, used to decode a cursor read back out of the ledger.
        indexed: an index leads with this column. Only the first key column is
            asked, because that is the one the range scan descends.
        unique: the key columns up to and including this one identify a row.
            For a single-column key that is the column alone; for
            `(stamp, id)` it is set on `id`, because the pair is what the
            boundary carries. A table whose primary key is itself composite
            sets it on the *last* of those columns -- `(retention_until,
            source, message_id)` marks `message_id`, because `(source,
            message_id)` is the key and the walk's boundary is the whole
            tuple. Setting it on a column that does not complete a unique
            constraint is the one declaration a pass cannot check and cannot
            survive: the boundary silently skips the row's siblings.
        monotone: values are assigned in increasing order, so a row inserted
            after a fixed ceiling was captured cannot land beneath it.
        descending: the walk runs from high to low on this column.
    """

    name: str
    type: str = "text"
    indexed: bool = False
    unique: bool = False
    monotone: bool = False
    descending: bool = False

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.name):
            raise PassDeclarationError(
                f"key column {self.name!r} must be a plain SQL identifier"
            )
        if not _SQL_TYPE.fullmatch(self.type):
            raise PassDeclarationError(
                f"key column {self.name!r} has type {self.type!r}, which is not a "
                "plain SQL type name"
            )

    @property
    def is_clock(self) -> bool:
        """Whether this column holds a database timestamp."""
        return self.type.lower() in _CLOCK_TYPES


def key_from_column(expression: Any) -> Key:
    """Read a `Key` off an ORM column expression, or an `.asc()`/`.desc()`.

    Everything the refusals ask about is already in the model declaration, so a
    caller writing `key=Trek.id` never restates it.
    """
    direction = False
    node = expression
    if hasattr(node, "direction") and hasattr(node, "expression"):
        direction = str(node.direction).upper() == "DESC"
        node = node.expression
    column = getattr(node, "column", None)
    if column is None or not hasattr(column, "pg_type"):
        raise PassDeclarationError(
            "a Rows key must be a model column (Trek.id), a Key(...) for a table "
            f"the ORM does not own, or one of those ordered with .asc()/.desc(); got {expression!r}"
        )
    return Key(
        name=column.database_name,
        type=column.pg_type.name,
        # A primary key always has an index; a unique column always has one too.
        indexed=bool(column.indexed or column.primary_key or column.unique),
        unique=bool(column.primary_key or column.unique),
        monotone=_monotone(column),
        descending=direction,
    )


def _monotone(column: Any) -> bool:
    default = getattr(column, "server_default", None)
    if isinstance(default, str):
        lowered = default.lower()
        return any(token in lowered for token in _MONOTONE_DEFAULTS)
    return False


def normalise(key: Any) -> tuple[Key, ...]:
    """Turn whatever a caller passed as `key=` into an ordered tuple of keys."""
    items = key if isinstance(key, (tuple, list)) else (key,)
    if not items:
        raise PassDeclarationError("a Rows key needs at least one column")
    return tuple(item if isinstance(item, Key) else key_from_column(item) for item in items)


def refuse_unsound_key(keys: tuple[Key, ...], *, table: str) -> None:
    """Refuse a key that cannot walk a table correctly. See this module's docstring."""
    names = ", ".join(item.name for item in keys)
    directions = {item.descending for item in keys}
    if len(directions) > 1:
        ascending = ", ".join(item.name for item in keys if not item.descending)
        descending = ", ".join(item.name for item in keys if item.descending)
        raise PassDeclarationError(
            f"key ({names}) on {table} mixes directions (ascending: {ascending}; "
            f"descending: {descending}). A row comparison has no mixed-direction "
            "form, and expanding it into ORs costs the single index scan that "
            "makes a keyset walk cheap -- order every key column the same way."
        )
    if len(set(item.name for item in keys)) != len(keys):
        raise PassDeclarationError(f"key ({names}) on {table} repeats a column")
    if not keys[0].indexed:
        raise PassDeclarationError(
            f"key ({names}) on {table} leads with {keys[0].name!r}, which has no "
            "index. Without one the database sorts the whole table once per "
            "chunk -- N/c sorts of N rows, which is worse than the OFFSET paging "
            "a keyset walk exists to avoid. Declare an index on it."
        )
    if not any(item.unique for item in keys):
        raise PassDeclarationError(
            f"key ({names}) on {table} is not unique, so a value landing on a "
            "chunk boundary either skips its siblings (silent data loss) or "
            "re-reads them forever. Append the primary key as a tiebreaker -- "
            f"key=({names}, <primary key>) -- which stays one index scan. A "
            "composite primary key appends every one of its columns, and "
            "`unique=True` goes on the last of them."
        )
    for item in keys:
        if item.type.lower() in _INEXACT_TYPES:
            raise PassDeclarationError(
                f"key ({names}) on {table} includes {item.name!r}, which is "
                f"{item.type}. A cursor round-trips through the ledger's jsonb, "
                "and there is no decimal codec to read it back with, so the value "
                "returns as a float: two boundaries a decimal place apart become "
                "the same number and the walk skips every row between them. Walk "
                "on an exact column instead -- the primary key is the usual "
                f"answer -- and put {item.name!r} in the chunk's work, not its key."
            )


def refuse_unmonotone_key(keys: tuple[Key, ...], *, table: str, reason: str | None) -> None:
    """Refuse a fixed ceiling over a key whose values are not assigned in order.

    A ceiling captured at launch is only sound when a row written afterwards
    cannot land beneath it. With an identity primary key or a `now()` default
    that holds; with `gen_random_uuid()` a new row can land anywhere, including
    behind the cursor, where the pass will never see it.

    *reason* is the escape, and it is a sentence rather than a flag on purpose:
    ULIDs and UUIDv7 really are monotone, and nothing in the model declaration
    can see that when the value is assigned by the application.
    """
    if keys[0].monotone or reason:
        return
    raise PassDeclarationError(
        f"Ceiling.at_launch() over {keys[0].name!r} on {table} needs values "
        "assigned in increasing order: a row inserted behind the cursor after "
        "launch is one the pass will never see. Nothing in the declaration of "
        f"{keys[0].name!r} proves that. If it is true anyway -- ULIDs or UUIDv7 "
        'from the application -- say why: Ceiling.at_launch(monotone="...").'
    )


def refuse_unclocked_key(keys: tuple[Key, ...], *, table: str) -> None:
    """Refuse a clock-derived frontier over a key that does not hold a timestamp."""
    if keys[0].is_clock:
        return
    raise PassDeclarationError(
        f"Sealed(after=...) measures a frontier against the database clock, so "
        f"the leading key column must be a timestamp; {keys[0].name!r} on {table} "
        f"is {keys[0].type}."
    )


def order_clause(keys: tuple[Key, ...], *, reverse: bool = False) -> str:
    """`ORDER BY` for this key, in the one direction it is allowed to have.

    *reverse* walks the same index from the other end, which is how the last key
    still inside a range is found in one descent.
    """
    descending = keys[0].descending != reverse
    suffix = " DESC" if descending else ""
    return ", ".join(f"{item.name}{suffix}" for item in keys)


def row_reference(keys: tuple[Key, ...]) -> str:
    """The key as a row constructor: `(herd_id, id)`, or `id` for one column."""
    if len(keys) == 1:
        return keys[0].name
    return "(" + ", ".join(item.name for item in keys) + ")"


def row_comparison(keys: tuple[Key, ...], operator: str, placeholders: list[str]) -> str:
    """One row comparison against a bound key: `(a, b) > ($1, $2)`.

    Never the hand-expanded `a > $1 OR (a = $1 AND b > $2)`. Both mean the same
    thing; only this one is reliably a single index scan.
    """
    if len(placeholders) != len(keys):
        raise PassDeclarationError("a keyset comparison needs one bind per key column")
    if len(keys) == 1:
        return f"{keys[0].name} {operator} {placeholders[0]}"
    return f"({', '.join(item.name for item in keys)}) {operator} ({', '.join(placeholders)})"


def after_operator(keys: tuple[Key, ...]) -> str:
    """The comparison that means "past the cursor" for this key's direction."""
    return "<" if keys[0].descending else ">"


def upto_operator(keys: tuple[Key, ...]) -> str:
    """The comparison that means "not past the ceiling" for this key's direction."""
    return ">=" if keys[0].descending else "<="


def encode_cursor(keys: tuple[Key, ...], values: tuple[Any, ...]) -> list[Any]:
    """A JSON-safe encoding of one key value, for the ledger's `jsonb` cursor."""
    if len(values) != len(keys):
        raise PassDeclarationError("a cursor carries one value per key column")
    return [_encode_one(value) for value in values]


def _encode_one(value: Any) -> Any:
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


def decode_cursor(keys: tuple[Key, ...], encoded: Any) -> tuple[Any, ...] | None:
    """Read a ledger cursor back into values the driver can bind.

    The placeholders carry no `::cast`, so the value handed to the driver has
    to be the right Python type; the key's declared SQL type is what says which.

    **A timestamp comes back on a fixed offset, not the zone it went in on.**
    `isoformat` writes `+13:00`, not `Pacific/Auckland`, so the instant is
    exact and the ordering is exact but the `tzinfo` object is not the original.
    Nothing here depends on it: PostgreSQL compares absolute instants, the
    compare-and-swap compares the encoded JSON rather than the decoded value, and
    `Buckets.advance` re-anchors on its own declared zone before doing any
    calendar arithmetic. Do not start depending on `tzinfo` identity, and do not
    "fix" this by dropping the offset -- that would lose the instant.

    One consequence worth knowing: inside an ambiguous local hour the decoded
    value compares *unequal* to the original under `==` while naming the same
    instant, because PEP 495 ignores `fold` in interzone comparison. Compare
    instants (`astimezone(utc)`), not datetimes, if you ever need to.
    """
    if encoded is None:
        return None
    if not isinstance(encoded, (list, tuple)) or len(encoded) != len(keys):
        raise PassDeclarationError(
            f"ledger cursor {encoded!r} does not match a {len(keys)}-column key"
        )
    return tuple(_decode_one(key, value) for key, value in zip(keys, encoded, strict=True))


def _decode_one(key: Key, value: Any) -> Any:
    if value is None:
        return None
    name = key.type.lower()
    if name in _CLOCK_TYPES and isinstance(value, str):
        return datetime.datetime.fromisoformat(value)
    if name == "date" and isinstance(value, str):
        return datetime.date.fromisoformat(value)
    if name == "uuid" and isinstance(value, str):
        return uuid.UUID(value)
    if name in ("int2", "int4", "int8", "smallint", "integer", "bigint"):
        return int(value)
    # `numeric` is deliberately absent: it is refused as a key type (see
    # `_INEXACT_TYPES`), and leaving it here would quietly do the lossy thing for
    # anything that reached this function by another route.
    if name in ("float4", "float8", "real", "double precision"):
        return float(value)
    if name == "bytea" and isinstance(value, str):
        return bytes.fromhex(value)
    return value


__all__ = [
    "Key",
    "PassDeclarationError",
    "after_operator",
    "decode_cursor",
    "encode_cursor",
    "key_from_column",
    "normalise",
    "order_clause",
    "refuse_unclocked_key",
    "refuse_unmonotone_key",
    "refuse_unsound_key",
    "row_comparison",
    "row_reference",
    "upto_operator",
]
