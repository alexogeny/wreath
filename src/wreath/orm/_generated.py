"""Render a generated column's expression in PostgreSQL's own normal form.

The same problem `wreath.orm._index_predicate` solves, one column over. A
`GENERATED ALWAYS AS (...) STORED` expression is not stored as you wrote it:
PostgreSQL parses it, and `pg_get_expr` deparses the parse tree back to
canonical text. So

```sql
to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))
```

comes back out of the catalog as

```sql
to_tsvector('english'::regconfig,
            ((COALESCE(title, ''::text) || ' '::text) || COALESCE(body, ''::text)))
```

(on one line, in the catalog).

`detect` compares the ORM's intent against the catalog by string. If the two
spellings disagree, every run reports drift on a column it just created --
forever, and with nothing actually wrong. So this module emits the normal form
directly, and the declaration vocabulary is deliberately small enough that it
can.

Measured against PostgreSQL 17, not inferred:

* the configuration is cast: `to_tsvector('english'::regconfig, ...)`
* each source is `COALESCE(<ident>, ''::text)`, and `''` is a `text` literal
* concatenation is left-associative and fully parenthesised, with a `' '::text`
  separator of its own between each pair -- one source therefore has *no*
  wrapping parentheses at all
* an identifier is quoted only when it has to be, by the same rule
  `quote_ident` uses -- which is `_index_predicate.quote_identifier`

Only `text` sources are accepted. A `varchar` source deparses as
`(COALESCE(vch, ''::character varying))::text`, which is a different shape for
every width; refusing it is cheaper than predicting it, and the refusal happens
at startup rather than on a deploy.
"""

from __future__ import annotations

from typing import Any

from ._index_predicate import quote_identifier
from .errors import DeclarationError
from .types import GeneratedType, TsVectorType


def render_generation(pg_type: GeneratedType, spec_columns: dict, model: str) -> str:
    """Render `pg_type`'s expression as PostgreSQL will deparse it back.

    Args:
        pg_type: The generated column's type.
        spec_columns: The model's columns, keyed by database name.
        model: The model's name, for messages.

    Returns:
        The expression text, without the surrounding `GENERATED ALWAYS AS (...)`.

    Raises:
        DeclarationError: A source column is missing, is not `text`, or is
            itself generated.
    """
    if not isinstance(pg_type, TsVectorType):
        raise DeclarationError(
            f"{model} declares a generated column of type {pg_type.name}, which "
            "wreath cannot render an expression for"
        )
    terms = [
        f"COALESCE({quote_identifier(_source(pg_type, name, spec_columns, model))}, ''::text)"
        for name in pg_type.sources
    ]
    expression = terms[0]
    for term in terms[1:]:
        # Left-associative and fully parenthesised, one pair per `||`, which is
        # exactly what the deparser emits for `a || ' ' || b`.
        expression = f"({expression} || ' '::text)"
        expression = f"({expression} || {term})"
    return f"to_tsvector('{pg_type.config}'::regconfig, {expression})"


def _source(pg_type: Any, name: str, spec_columns: dict, model: str) -> str:
    """The database name of one validated source column."""
    item = spec_columns.get(name)
    if item is None:
        raise DeclarationError(
            f"{model} declares a TsVector over unknown column {name!r}; declare it "
            "as a column first"
        )
    if isinstance(item.pg_type, GeneratedType):
        raise DeclarationError(
            f"{model}.{name} is itself a generated column, so a TsVector cannot be "
            "derived from it; name the columns it reads instead"
        )
    if item.pg_type.name != "text":
        raise DeclarationError(
            f"{model} declares a TsVector over {name!r}, whose type is "
            f"{item.pg_type.name}; only text columns are supported, because "
            "PostgreSQL casts anything else inside the expression and the cast it "
            "writes back cannot be predicted -- which would report the column as "
            "drifted on every migration run"
        )
    return item.database_name


__all__ = ["render_generation"]
