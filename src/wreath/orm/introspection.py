"""Startup schema validation.

Wreath never creates, alters, or drops database objects. It reads
``pg_catalog`` once at startup and reports, deterministically, where the
database and the compiled models disagree.

``information_schema`` is deliberately not used: it reports SQL-standard type
names and loses the type OIDs and array element identity this comparison
depends on.

**Every catalog column is cast to a type the driver has a codec for.** Wreath's
PostgreSQL driver decodes a deliberately small set -- bool, the int and float
widths, text/varchar, bytea, uuid, the date and timestamp types, numeric, json
and jsonb -- and hands anything else back as the raw wire bytes. The catalog is
made almost entirely of types outside that set: ``nspname``, ``relname`` and
``attname`` are ``name``, ``atttypid`` and ``typelem`` are ``oid``, ``contype``
is ``"char"``, ``conkey`` is ``int2[]`` and ``indkey`` is ``int2vector``. Read
uncast, a column name arrives as ``b'id'`` and matches no declared column, so
the validator reported every column of every table missing.

The casts are not cosmetic, and they are not only about arrays. The raw bytes
also differ between the two result formats -- ``atttypid`` is ``b'20'`` on a
cold operation (text) and ``b'\\x00\\x00\\x00\\x14'`` once the plan is cached
(binary) -- so an uncast read gives a different wrong answer on the first call
than on the second. Casting to ``text``/``bigint`` makes both formats decode to
the same Python value, which is why this file does not need to care whether the
statement is cold or warm.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

from .errors import SchemaMismatchError
from .schema import ModelSpec

#: One row per column of every table a registry maps.
_COLUMNS_SQL = """
SELECT
    n.nspname::text AS schema_name,
    c.relname::text AS table_name,
    a.attname::text AS column_name,
    a.attnum::int AS position,
    a.atttypid::bigint AS type_oid,
    a.attnotnull AS not_null,
    COALESCE(t.typelem, 0)::bigint AS element_oid,
    COALESCE(pg_get_expr(d.adbin, d.adrelid), '')::text AS column_default
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND a.attnum > 0
  AND NOT a.attisdropped
  AND n.nspname = $1::text
  AND c.relname = $2::text
ORDER BY a.attnum
"""

#: Primary key, unique, and foreign-key constraints for one table.
_CONSTRAINTS_SQL = """
SELECT
    con.contype::text AS kind,
    con.conkey::text AS local_positions,
    con.confkey::text AS remote_positions,
    COALESCE(fn.nspname::text, '') AS remote_schema,
    COALESCE(fc.relname::text, '') AS remote_table
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_class fc ON fc.oid = con.confrelid
LEFT JOIN pg_catalog.pg_namespace fn ON fn.oid = fc.relnamespace
WHERE con.contype IN ('p', 'u', 'f')
  AND n.nspname = $1::text
  AND c.relname = $2::text
ORDER BY con.contype, con.conname
"""

#: Unique indexes, which satisfy a declared unique= without a named constraint.
_INDEXES_SQL = """
SELECT i.indkey::text AS positions
FROM pg_catalog.pg_index i
JOIN pg_catalog.pg_class c ON c.oid = i.indrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE i.indisunique
  AND n.nspname = $1::text
  AND c.relname = $2::text
"""


@dataclass(frozen=True, slots=True, order=True)
class SchemaIssue:
    """One disagreement, ordered so a diff is stable across runs."""

    schema: str
    table: str
    column: str
    issue_code: str
    detail: str

    def __str__(self) -> str:
        location = f"{self.schema}.{self.table}"
        if self.column:
            location += f".{self.column}"
        return f"{location}: {self.issue_code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class SchemaDiff:
    issues: tuple[SchemaIssue, ...]

    def __bool__(self) -> bool:
        return bool(self.issues)

    def report(self, limit: int = 50) -> str:
        shown = self.issues[:limit]
        lines = [f"  - {item}" for item in shown]
        if len(self.issues) > limit:
            lines.append(f"  ... and {len(self.issues) - limit} more")
        return "\n".join(lines)


async def validate_registry(registry: Any) -> SchemaDiff:
    """Compare a registry's models to the live catalog.

    Runs on the registry's read pool when it has one, since validation is a
    read. Returns the diff; raises or warns according to ``validate_schema``.
    """
    mode = registry.validate_schema
    if mode == "off":
        return SchemaDiff(())
    workload = "read" if _has_workload(registry.database, "read") else "write"
    connection = await registry.database.acquire(workload)
    try:
        issues: list[SchemaIssue] = []
        for spec in registry.specs:
            issues.extend(await _validate_model(connection, spec))
    finally:
        await registry.database.release(workload, connection)
    diff = SchemaDiff(tuple(sorted(issues)))
    if not diff:
        return diff
    summary = (
        f"the {registry.database.name!r} database does not match "
        f"{len(registry.specs)} compiled model(s):\n{diff.report()}"
    )
    if mode == "error":
        raise SchemaMismatchError(summary, diff.issues)
    warnings.warn(summary, RuntimeWarning, stacklevel=2)
    return diff


def _has_workload(database: Any, workload: str) -> bool:
    from ..postgres import InterfaceError

    try:
        database.pool(workload)
    except KeyError:
        return False
    except InterfaceError:
        # `_configured_pool` raises this for a workload that is declared but not
        # started -- which answers the question being asked. Those two are the
        # only outcomes `Database.pool` produces, so anything else (a bad
        # workload name, a `database` that is not one) is a caller bug and is
        # left to propagate.
        return True
    return True


async def _validate_model(connection: Any, spec: ModelSpec) -> list[SchemaIssue]:
    rows = await connection.fetch(_COLUMNS_SQL, spec.schema, spec.table)
    if not rows:
        return [
            SchemaIssue(
                spec.schema, spec.table, "", "missing_table",
                f"{spec.model_type.__name__} maps {spec.qualified_name}, which does "
                "not exist or has no readable columns",
            )
        ]
    issues: list[SchemaIssue] = []
    actual = {str(row[2]): row for row in rows}
    by_position = {int(row[3]): str(row[2]) for row in rows}

    for column in spec.columns:
        row = actual.get(column.database_name)
        if row is None:
            issues.append(
                SchemaIssue(
                    spec.schema, spec.table, column.database_name, "missing_column",
                    f"{spec.model_type.__name__}.{column.python_name} has no matching "
                    "database column",
                )
            )
            continue
        type_oid = int(row[4])
        if type_oid != column.oid:
            issues.append(
                SchemaIssue(
                    spec.schema, spec.table, column.database_name, "type_mismatch",
                    f"declared {column.pg_type.name} (OID {column.oid}) but the "
                    f"database has OID {type_oid}",
                )
            )
        not_null = bool(row[5])
        if not_null == column.nullable:
            issues.append(
                SchemaIssue(
                    spec.schema, spec.table, column.database_name, "nullability_mismatch",
                    f"declared {'nullable' if column.nullable else 'not null'} but the "
                    f"database is {'not null' if not_null else 'nullable'}",
                )
            )
        if column.server_default is not None:
            database_default = str(row[7])
            if _normalize_default(database_default) != _normalize_default(
                column.server_default
            ):
                issues.append(
                    SchemaIssue(
                        spec.schema, spec.table, column.database_name,
                        "server_default_mismatch",
                        f"declared server_default {column.server_default!r} but the "
                        f"database has {database_default!r}",
                    )
                )

    issues.extend(await _validate_constraints(connection, spec, by_position))
    return issues


async def _validate_constraints(
    connection: Any, spec: ModelSpec, by_position: dict[int, str]
) -> list[SchemaIssue]:
    issues: list[SchemaIssue] = []
    rows = await connection.fetch(_CONSTRAINTS_SQL, spec.schema, spec.table)
    primary: tuple[str, ...] = ()
    unique: set[tuple[str, ...]] = set()
    foreign: set[tuple[tuple[str, ...], str, str, tuple[int, ...]]] = set()
    for row in rows:
        kind = _text(row[0])
        local = _names(row[1], by_position)
        if kind == "p":
            primary = local
        elif kind == "u":
            unique.add(local)
        elif kind == "f":
            foreign.add((local, _text(row[3]), _text(row[4]), tuple(_positions(row[2]))))

    for row in await connection.fetch(_INDEXES_SQL, spec.schema, spec.table):
        unique.add(_names(row[0], by_position))

    declared_primary = tuple(item.database_name for item in spec.primary_key)
    if primary != declared_primary:
        issues.append(
            SchemaIssue(
                spec.schema, spec.table, "", "primary_key_mismatch",
                f"declared primary key ({', '.join(declared_primary)}) but the "
                f"database has ({', '.join(primary) or 'none'})",
            )
        )
    for column in spec.columns:
        if column.unique and (column.database_name,) not in unique:
            issues.append(
                SchemaIssue(
                    spec.schema, spec.table, column.database_name, "missing_unique",
                    "declared unique=True but the database has no unique constraint "
                    "or index on it",
                )
            )
    # Index corresponding local/remote column pairs so each declared reference
    # validates both ends with one lookup. Flattening composite constraints here
    # preserves O(C + F) behavior while checking each paired target position.
    foreign_keys = {
        (local_name, schema, table, remote_position)
        for local_names, schema, table, remote_positions in foreign
        for local_name, remote_position in zip(local_names, remote_positions, strict=True)
    }
    for column in spec.columns:
        reference = column.reference
        if reference is None:
            continue
        if (
            column.database_name,
            reference.schema,
            reference.table,
            reference.position,
        ) not in foreign_keys:
            issues.append(
                SchemaIssue(
                    spec.schema, spec.table, column.database_name, "missing_foreign_key",
                    f"declared references {reference.schema}.{reference.table}."
                    f"{reference.column} but the database has no such foreign key",
                )
            )
    return issues


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _positions(value: Any) -> list[int]:
    """Read a column position list out of a catalog vector.

    The SQL casts both spellings to ``text``, and they render differently:
    ``conkey`` is an ``int2[]`` and arrives as ``{1,2}``, while ``indkey`` is an
    ``int2vector`` and arrives as ``1 2``. ``confkey`` is NULL on every
    constraint that is not a foreign key. All three are handled here so the
    callers do not branch on which catalog column they came from.

    The list branch stays because a driver that grows an array codec would
    surface these as sequences; it is not currently reachable.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [int(item) for item in value]
    text = _text(value).strip().strip("{}")
    if not text:
        return []
    return [int(item) for item in text.replace(",", " ").split()]


def _names(value: Any, by_position: dict[int, str]) -> tuple[str, ...]:
    return tuple(
        by_position[item] for item in _positions(value) if item in by_position
    )


def _normalize_default(value: str) -> str:
    """Compare server defaults ignoring whitespace and PostgreSQL's casts.

    Peels matched outer ``(...)`` pairs (stripping whitespace between them) using
    indices, so an input with N nested pairs copies the string once, not O(N^2).
    Semantics match the previous strip-and-reslice loop, including that it does
    not validate overall parenthesis balance.
    """
    text = " ".join(value.split()).strip()
    left, right = 0, len(text)
    while True:
        while left < right and text[left] == " ":
            left += 1
        while right > left and text[right - 1] == " ":
            right -= 1
        if left < right and text[left] == "(" and text[right - 1] == ")":
            left += 1
            right -= 1
        else:
            break
    return text[left:right].lower()


__all__ = ["SchemaDiff", "SchemaIssue", "validate_registry"]
