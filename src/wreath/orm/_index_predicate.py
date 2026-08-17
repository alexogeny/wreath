"""Render a partial-index predicate in PostgreSQL's own normal form.

A predicate is not stored as you wrote it. PostgreSQL parses it to a node tree,
and `pg_get_expr` deparses that tree back to canonical text -- so
`status = 'ready'`, `(status = 'ready')`, `status='ready'` and
`status = 'ready'::text` all come back as `(status = 'ready'::text)`.

That matters because `detect` compares the ORM's intent against the catalog by
string. If the two spellings disagree, every `detect` run reports drift on an
index it just created, forever -- a far worse failure than a crash, because it
never resolves and nothing is actually wrong.

So this module emits the normal form directly, and the declaration vocabulary is
deliberately small enough that it can. The rules below were measured against
PostgreSQL 17.10, not inferred:

* the whole predicate is parenthesised, and each `AND` operand keeps its own
  parentheses -- `((status = 'ready'::text) AND (tries = 0))`
* a text literal gains `::text`; integers and booleans gain nothing
* `IN` is deparsed as `= ANY (ARRAY[...])`, never as `IN`
* an identifier is quoted only when it has to be, by the same rule
  `quote_ident` uses

Types outside `_LITERAL` are refused rather than guessed at **wherever a
literal is rendered**, because their normal forms are not simply predictable: a
`varchar` comparison casts the *column* (`((vch)::text = 'x'::text)`),
`double precision` parenthesises and casts the literal, and `timestamptz`
rewrites the literal's format. Each would be a permanent-drift bug. Widening
`_LITERAL` means measuring the new type's normal form first and pinning it
in `tests/postgres/test_partial_index_roundtrip.py`.

`IS NULL` and `IS NOT NULL` render **no literal**, and so accept any declared
column type. That branch used to compute the type kind and then throw it away,
which meant it refused `timestamptz` for a reason that only applies to the
comparison branches -- and `retired_at IS NULL` is the archetypal partial index.
Measured against PostgreSQL 17.10: every one of the 32 types
`wreath.orm.types.BY_OID` can declare (16 scalars and their array forms), in
both polarities, deparses to exactly `(<ident> IS [NOT] NULL)`. A `NullTest`
node carries no operand to coerce, so there is nothing for the type to change.
"""

from __future__ import annotations

import re
from typing import Any

from .._pgname import quote_identifier as _quote_identifier
from .errors import DeclarationError
from .table import AllOf, Eq, InValues, IsNull

#: Identifiers PostgreSQL always quotes because they are reserved words, from
#: `pg_get_keywords()` where `catcode IN ('R','T')`. Pinned by a test that
#: re-reads them from the live server, so a version bump that adds one is caught
#: rather than silently producing an unquoted identifier the catalog quotes.
RESERVED_WORDS = frozenset(
    """
    all analyse analyze and any array as asc asymmetric authorization binary both
    case cast check collate collation column concurrently constraint create cross
    current_catalog current_date current_role current_schema current_time
    current_timestamp current_user default deferrable desc distinct do else end
    except false fetch for foreign freeze from full grant group having ilike in
    initially inner intersect into is isnull join lateral leading left like limit
    localtime localtimestamp natural not notnull null offset on only or order outer
    overlaps placing primary references returning right select session_user similar
    some symmetric system_user table tablesample then to trailing true union unique
    user using variadic verbose when where window with
    """.split()
)

#: Matched with `fullmatch`, never `match`. This one decides *quoting* rather
#: than acceptance, so `^...$` was worse than lax: `$` matches immediately before
#: a trailing newline, so `"embedding\n"` was judged bare and emitted unquoted --
#: the one answer `quote_ident` never gives.
_BARE = re.compile(r"[a-z_][a-z0-9_$]*")

#: Column types whose normal form this module reproduces exactly. Each maps to a
#: literal renderer and the Python types it accepts.
_LITERAL = {
    "text": "text",
    "int2": "integer",
    "int4": "integer",
    "int8": "integer",
    "bool": "boolean",
}


def quote_identifier(name: str) -> str:
    """Quote *name* exactly when PostgreSQL's `quote_ident` would."""
    return _quote_identifier(name, bare=_BARE, reserved=RESERVED_WORDS)


def _literal(value: object, kind: str, column: str, model: str) -> str:
    if kind == "text":
        if not isinstance(value, str):
            raise DeclarationError(
                f"{model} index predicate on text column {column!r} needs a str, "
                f"got {value!r}"
            )
        return "'" + value.replace("'", "''") + "'::text"
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise DeclarationError(
                f"{model} index predicate on integer column {column!r} needs an int, "
                f"got {value!r}"
            )
        return str(value)
    if not isinstance(value, bool):
        raise DeclarationError(
            f"{model} index predicate on boolean column {column!r} needs True or "
            f"False, got {value!r}"
        )
    return "true" if value else "false"


def _column(column: str, spec_columns: dict, model: str) -> Any:
    """The declared column *column* names, whatever its type."""
    item = spec_columns.get(column)
    if item is None:
        raise DeclarationError(
            f"{model} index predicate names unknown column {column!r}; "
            "declare it as a column first"
        )
    return item


def _kind(column: str, spec_columns: dict, model: str) -> tuple[str, str]:
    """The literal renderer for *column*, for the branches that render one."""
    item = _column(column, spec_columns, model)
    type_name = item.pg_type.name
    kind = _LITERAL.get(type_name)
    if kind is None:
        raise DeclarationError(
            f"{model} index predicate compares column {column!r}, whose type "
            f"{type_name} wreath cannot render as a literal in PostgreSQL's "
            "normal form -- eq() and one_of() support "
            f"{', '.join(sorted(_LITERAL))}. is_null() and is_not_null() render "
            "no literal and take any type; otherwise declare the index without "
            "where= and manage the predicate outside migrations."
        )
    return kind, item.database_name


def _render_one(predicate: object, spec_columns: dict, model: str) -> str:
    if isinstance(predicate, Eq):
        kind, name = _kind(predicate.column, spec_columns, model)
        literal = _literal(predicate.value, kind, predicate.column, model)
        return f"({quote_identifier(name)} = {literal})"
    if isinstance(predicate, IsNull):
        # No literal is rendered, so no type can change the normal form; the
        # column only has to exist. See this module's docstring for the
        # measurement that says so.
        name = _column(predicate.column, spec_columns, model).database_name
        test = "IS NOT NULL" if predicate.negated else "IS NULL"
        return f"({quote_identifier(name)} {test})"
    if isinstance(predicate, InValues):
        kind, name = _kind(predicate.column, spec_columns, model)
        if kind != "text":
            raise DeclarationError(
                f"{model} one_of({predicate.column!r}, ...) is supported for text "
                "columns only; PostgreSQL casts each element of a non-text ARRAY "
                "and the cast depends on the column's width, so the catalog form "
                "cannot be predicted"
            )
        items = ", ".join(
            _literal(value, kind, predicate.column, model) for value in predicate.values
        )
        return f"({quote_identifier(name)} = ANY (ARRAY[{items}]))"
    raise DeclarationError(f"{model} index predicate has unsupported shape {predicate!r}")


def render_predicate(predicate: object, spec_columns: dict, model: str) -> str:
    """Render *predicate* as PostgreSQL would deparse it from its own catalog."""
    if isinstance(predicate, AllOf):
        parts = " AND ".join(
            _render_one(operand, spec_columns, model) for operand in predicate.operands
        )
        return f"({parts})"
    return _render_one(predicate, spec_columns, model)


__all__ = ["RESERVED_WORDS", "quote_identifier", "render_predicate"]
