"""Reads against PostgreSQL's system catalogs, written once.

Two subsystems ship a database table that grew a `trace_context` column in a
later version, and both must answer "has this deployment's table got it yet?"
before they write. `wreath.jobs` asked about `jobs` and `wreath._passes.ledger`
asked about `passes`, with the same five-line catalog join, the same `::text`
cast, and the same comment explaining the cast -- copied along with the query,
which is how you can tell.

The question generalises and the answer should not be re-derived per table. It
is a *precondition callers check* rather than an error they catch, and that is
the part worth keeping intact wherever it is used: a broad `except` around the
write would swallow a revoked grant and a driver fault alongside the one case it
means to survive, and in `_passes` the seed runs inside the shift, where
poisoning the connection would take the whole walk with it.
"""

from __future__ import annotations

from typing import Any

#: `nspname`, `relname` and `attname` are all `name`, not `text`. Without the
#: casts PostgreSQL infers the parameters as `name` too, which the driver cannot
#: encode -- `wreath-sql-lint` SQL002. Written here once instead of beside every
#: caller's copy of the query.
_COLUMN_EXISTS = (
    "SELECT true FROM pg_attribute a "
    "JOIN pg_class k ON k.oid = a.attrelid "
    "JOIN pg_namespace n ON n.oid = k.relnamespace "
    "WHERE n.nspname = $1::text AND k.relname = $2::text "
    "AND a.attname = $3::text "
    "AND a.attnum > 0 AND NOT a.attisdropped"
)


async def column_exists(
    executor: Any, *, schema: str, table: str, column: str
) -> bool:
    """Whether `schema.table` currently has `column`.

    `attnum > 0` excludes the system columns and `NOT attisdropped` excludes one
    that has been dropped but whose catalog row survives -- a dropped column
    still occupies an `attnum`, so omitting that clause answers True forever
    after a `DROP COLUMN`.

    Args:
        executor: anything with `fetchval(sql, *params)` -- a connection, a
            pool handle, or a transaction.
        schema: the namespace, unquoted and passed as a parameter.
        table: the relation name, unquoted and passed as a parameter.
        column: the attribute name, unquoted and passed as a parameter.

    Returns:
        True when the column is present and live.
    """
    return bool(await executor.fetchval(_COLUMN_EXISTS, schema, table, column))


__all__ = ["column_exists"]
