"""Startup schema validation.

Wreath never creates, alters, or drops database objects. It reads
`pg_catalog` once at startup and reports, deterministically, where the
database and the compiled models disagree.

`information_schema` is deliberately not used: it reports SQL-standard type
names and loses the type OIDs and array element identity this comparison
depends on.

**Every catalog column is cast to a type the driver has a codec for.** Wreath's
PostgreSQL driver decodes a deliberately small set -- bool, the int and float
widths, text/varchar, bytea, uuid, the date and timestamp types, numeric, json
and jsonb -- and hands anything else back as the raw wire bytes. The catalog is
made almost entirely of types outside that set: `nspname`, `relname` and
`attname` are `name`, `atttypid` and `typelem` are `oid`, `contype`
is `"char"`, `conkey` is `int2[]` and `indkey` is `int2vector`. Read
uncast, a column name arrives as `b'id'` and matches no declared column, so
the validator reported every column of every table missing.

The casts are not cosmetic, and they are not only about arrays. The raw bytes
also differ between the two result formats -- `atttypid` is `b'20'` on a
cold operation (text) and `b'\\x00\\x00\\x00\\x14'` once the plan is cached
(binary) -- so an uncast read gives a different wrong answer on the first call
than on the second. Casting to `text`/`bigint` makes both formats decode to
the same Python value, which is why this file does not need to care whether the
statement is cold or warm.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any

from .errors import ExtensionNotInstalledError, SchemaMismatchError
from .fields import _IMPLICIT_OPCLASS_METHODS
from .model import rebind_storage_oid
from .schema import ModelSpec
from .types import ExtensionType, bind_extension_oid

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

_BATCH_COLUMNS_SQL = """
WITH wanted AS (
    SELECT schema_name, table_name
    FROM jsonb_to_recordset($1::jsonb) AS item(schema_name text, table_name text)
)
SELECT
    n.nspname::text AS schema_name,
    c.relname::text AS table_name,
    a.attname::text AS column_name,
    a.attnum::int AS position,
    a.atttypid::bigint AS type_oid,
    a.attnotnull AS not_null,
    COALESCE(t.typelem, 0)::bigint AS element_oid,
    COALESCE(pg_get_expr(d.adbin, d.adrelid), '')::text AS column_default
FROM wanted w
JOIN pg_catalog.pg_namespace n ON n.nspname = w.schema_name
JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relname = w.table_name
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY n.nspname, c.relname, a.attnum
"""

_BATCH_CONSTRAINTS_SQL = """
WITH wanted AS (
    SELECT schema_name, table_name
    FROM jsonb_to_recordset($1::jsonb) AS item(schema_name text, table_name text)
)
SELECT
    n.nspname::text AS schema_name,
    c.relname::text AS table_name,
    con.contype::text AS kind,
    con.conkey::text AS local_positions,
    con.confkey::text AS remote_positions,
    COALESCE(fn.nspname::text, '') AS remote_schema,
    COALESCE(fc.relname::text, '') AS remote_table
FROM wanted w
JOIN pg_catalog.pg_namespace n ON n.nspname = w.schema_name
JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relname = w.table_name
JOIN pg_catalog.pg_constraint con ON con.conrelid = c.oid
LEFT JOIN pg_catalog.pg_class fc ON fc.oid = con.confrelid
LEFT JOIN pg_catalog.pg_namespace fn ON fn.oid = fc.relnamespace
WHERE con.contype IN ('p', 'u', 'f')
ORDER BY n.nspname, c.relname, con.contype, con.conname
"""

_BATCH_INDEXES_SQL = """
WITH wanted AS (
    SELECT schema_name, table_name
    FROM jsonb_to_recordset($1::jsonb) AS item(schema_name text, table_name text)
)
SELECT
    n.nspname::text AS schema_name,
    c.relname::text AS table_name,
    i.indkey::text AS positions
FROM wanted w
JOIN pg_catalog.pg_namespace n ON n.nspname = w.schema_name
JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relname = w.table_name
JOIN pg_catalog.pg_index i ON i.indrelid = c.oid
WHERE i.indisunique
ORDER BY n.nspname, c.relname
"""


#: One extension type's OID, resolved by name against the connection's own
#: `search_path`. `to_regtype` returns NULL rather than raising for a type that
#: does not exist, which is the answer this needs -- an absent extension is a
#: readiness fact to report, not an error to catch. The extension and schema
#: come back alongside it so the failure message can name where wreath looked.
_EXTENSION_TYPE_SQL = """
WITH wanted AS (
    SELECT type_name
    FROM jsonb_to_recordset($1::jsonb) AS item(type_name text)
)
SELECT
    wanted.type_name,
    COALESCE(pg_catalog.to_regtype(wanted.type_name)::oid::bigint, 0) AS type_oid,
    COALESCE((
        SELECT n.nspname
        FROM pg_catalog.pg_type t
        JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
        WHERE t.oid = pg_catalog.to_regtype(wanted.type_name)::oid
    ), '')::text AS type_schema,
    pg_catalog.current_schema()::text AS current_schema
FROM wanted
ORDER BY wanted.type_name
"""


#: Every default operator class belonging to one of the named access methods.
#:
#: `opcdefault` is a property of a *pair* -- an access method and the type it
#: indexes -- not of a method alone, because one method indexes several types
#: and each of them has its own default. `ivfflat` over `vector` defaults to
#: `vector_l2_ops`; `ivfflat` over `halfvec` defaults to something else. The key
#: is therefore `(amname, opcintype)`, which is exactly the pair the catalog
#: read in `wreath.migrations` blanks a default for.
#:
#: The method list arrives as one comma-joined text rather than an array because
#: the driver decodes a deliberately small set of types and `text[]` is not in
#: it -- the same reason every column here is cast.
_DEFAULT_OPCLASS_SQL = """
SELECT
    am.amname::text AS access_method,
    op.opcintype::bigint AS type_oid,
    op.opcname::text AS operator_class
FROM pg_catalog.pg_opclass op
JOIN pg_catalog.pg_am am ON am.oid = op.opcmethod
WHERE op.opcdefault
  AND am.amname = ANY(pg_catalog.string_to_array($1::text, ','))
ORDER BY am.amname, op.opcintype
"""


@dataclass(frozen=True, slots=True, order=True)
class ExtensionTypeResolution:
    """Where one extension type's OID came from, or that it has none."""

    type_name: str
    extension: str
    oid: int
    schema: str
    #: The schema the connection would have created the extension into, so a
    #: failure can say where wreath looked rather than only what it wanted.
    current_schema: str

    @property
    def installed(self) -> bool:
        return self.oid != 0


def declared_extension_columns(registry: Any) -> tuple[tuple[Any, Any], ...]:
    """Every `(spec, column)` in `registry` whose type comes from an extension."""
    return tuple(
        (spec, column)
        for spec in registry.specs
        for column in spec.columns
        if isinstance(column.pg_type, ExtensionType)
    )


async def probe_extension_types(
    connection: Any, wanted: dict[str, str]
) -> tuple[ExtensionTypeResolution, ...]:
    """Read one OID per `{type_name: extension}` entry, without deciding anything.

    Split out from `resolve_extension_types` because `wreath.doctor` needs the
    same reading without the startup failure: a readiness report that raised
    would be a worse tool than the startup check it duplicates.
    """
    if not wanted:
        return ()
    payload = _extension_payload(sorted(wanted))
    fetch = getattr(connection, "fetch", None)
    if callable(fetch):
        rows = await fetch(_EXTENSION_TYPE_SQL, payload)
    else:
        rows = []
        for name in sorted(wanted):
            row = await connection.fetchrow(_EXTENSION_TYPE_SQL, _extension_payload((name,)))
            if row is not None:
                rows.append(row)
    return tuple(
        ExtensionTypeResolution(
            type_name=_text(row[0]),
            extension=wanted[_text(row[0])],
            oid=int(row[1]),
            schema=_text(row[2]),
            current_schema=_text(row[3]),
        )
        for row in rows
    )


def _extension_payload(names: Any) -> str:
    from .._json import dumps

    return dumps([{"type_name": name} for name in names]).decode("utf-8")


async def resolve_extension_types(registry: Any) -> tuple[ExtensionTypeResolution, ...]:
    """Give this registry's extension-typed columns the OIDs this database uses.

    Runs at startup, before anything binds a value. `pgvector`'s `vector` is not
    a compile-time OID the way `bigint` is -- `CREATE EXTENSION` assigns it, and
    a different database assigns a different one -- so the OID is read here once
    and handed to the driver's codec table. Nothing resolves a type at request
    time; that is the whole point of doing it here.

    A registry that declares no extension type does no I/O at all.

    Raises:
        ExtensionNotInstalledError: a declared type has no OID in this database.
            The message names the extension, the schema wreath looked in, and
            the first column that wanted it, because the alternative is an
            unrecognised-OID error at the first query with none of that in it.
    """
    columns = declared_extension_columns(registry)
    if not columns:
        return ()
    wanted: dict[str, str] = {}
    first_use: dict[str, str] = {}
    for spec, column in columns:
        pg_type = column.pg_type
        wanted[pg_type.type_name] = pg_type.extension
        first_use.setdefault(
            pg_type.type_name,
            f"{spec.model_type.__name__}.{column.python_name} "
            f"({spec.qualified_name}.{column.database_name})",
        )
    workload = "read" if _has_workload(registry.database, "read") else "write"
    connection = await registry.database.acquire(workload)
    try:
        found = await probe_extension_types(connection, wanted)
    finally:
        await registry.database.release(workload, connection)
    for item in found:
        if not item.installed:
            raise ExtensionNotInstalledError(
                f"{first_use[item.type_name]} declares the {item.type_name!r} type, "
                f"which the {registry.database.name!r} database does not provide: the "
                f"{item.extension!r} extension is not installed on the search path "
                f"(current schema {item.current_schema or '?'!r}). Run "
                f"CREATE EXTENSION IF NOT EXISTS {item.extension} in that database -- "
                "some managed PostgreSQL tiers restrict who may do that, in which "
                "case it has to be enabled by the provider.",
                extension=item.extension,
                schema=item.current_schema,
            )
        bind_extension_oid(item.type_name, item.oid)
    # The types now know their OID; the compiled model storage still does not.
    # It baked in 0 when the class was defined, and the native hydrate plan
    # validates every result column against that -- so without this a vector
    # query would fail on its first row with a type mismatch against OID 0.
    for spec, column in columns:
        rebind_storage_oid(spec.model_type, column.index, column.pg_type.oid)
    return found


def declared_index_methods(registry: Any) -> tuple[str, ...]:
    """The access methods this registry declares that spell an operator class.

    btree and GIN are excluded because their index descriptor carries no
    operator-class field at all, so nothing about them can disagree with the
    catalog over one. Sorted, so the probe below asks the same question in the
    same order on every run.
    """
    return tuple(
        sorted(
            {
                column.index_method
                for spec in registry.specs
                for column in spec.columns
                if column.indexed
                and column.index_method
                and column.index_method not in _IMPLICIT_OPCLASS_METHODS
            }
        )
    )


async def probe_default_opclasses(
    connection: Any, methods: tuple[str, ...]
) -> dict[tuple[str, int], str]:
    """Read `{(access method, indexed type OID): default operator class}`.

    Split from `resolve_default_opclasses` for the same reason
    `probe_extension_types` is split from `resolve_extension_types`: the reading
    is useful without the binding, and a test that wants to know what this
    server's defaults *are* should not have to build a registry to find out.

    An access method with no default operator class -- and a method this server
    has never heard of -- simply contributes no rows. There is nothing to raise
    about: it means every declared operator class on that method is explicit,
    which is the case the catalog already records verbatim.
    """
    if not methods:
        return {}
    rows = await connection.fetch(_DEFAULT_OPCLASS_SQL, ",".join(methods))
    return {(_text(row[0]), int(row[1])): _text(row[2]) for row in rows}


async def resolve_default_opclasses(
    registry: Any, connection: Any | None = None
) -> dict[tuple[str, int], str]:
    """Give `registry` this database's default operator class per index method.

    An index declared `index_ops="vector_l2_ops"` on `ivfflat` names pgvector's
    *default* operator class for that method, and PostgreSQL does not remember
    that it was named: `pg_get_indexdef` deparses it away, and wreath's catalog
    read deliberately records a default as the empty string so that an index
    declared without an operator class is not reported as drifted. Without this,
    the desired descriptor keeps saying `vector_l2_ops` and the catalog keeps
    saying nothing, so `detect` rediscovers drift on every run and `generate`
    emits a `MANUAL` forever for an index that is already exactly right.

    The answer belongs to the database, so it is read from one, once per
    registry, and cached on it. A registry that declares no such index does no
    I/O at all. `connection` is for the migration entry points, which already
    hold one and are the only thing that builds a desired descriptor; passing
    none borrows from the registry's own pool instead.
    """
    resolved = registry.default_opclasses
    if resolved is not None:
        return resolved
    methods = declared_index_methods(registry)
    if not methods:
        registry.default_opclasses = {}
        return registry.default_opclasses
    if connection is not None:
        found = await probe_default_opclasses(connection, methods)
    else:
        workload = "read" if _has_workload(registry.database, "read") else "write"
        borrowed = await registry.database.acquire(workload)
        try:
            found = await probe_default_opclasses(borrowed, methods)
        finally:
            await registry.database.release(workload, borrowed)
    registry.default_opclasses = found
    return found


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
    read. Returns the diff; raises or warns according to `validate_schema`.
    """
    mode = registry.validate_schema
    if mode == "off":
        return SchemaDiff(())
    if not registry.specs:
        return SchemaDiff(())
    workload = "read" if _has_workload(registry.database, "read") else "write"
    connection = await registry.database.acquire(workload)
    try:
        issues: list[SchemaIssue] = []
        table_keys = {
            (spec.schema, spec.table)
            for spec in registry.specs
        }
        table_keys.update(
            (str(column.reference.schema), column.reference.table)
            for spec in registry.specs
            for column in spec.columns
            if column.reference is not None
        )
        from .._json import dumps

        payload = dumps(
            [
                {"schema_name": schema, "table_name": table}
                for schema, table in sorted(table_keys)
            ]
        ).decode("utf-8")
        all_column_rows = await connection.fetch(_BATCH_COLUMNS_SQL, payload)
        all_constraint_rows = await connection.fetch(_BATCH_CONSTRAINTS_SQL, payload)
        all_index_rows = await connection.fetch(_BATCH_INDEXES_SQL, payload)
        column_rows: dict[tuple[str, str], list[Any]] = {}
        constraint_rows: dict[tuple[str, str], list[Any]] = {}
        index_rows: dict[tuple[str, str], list[Any]] = {}
        for row in all_column_rows:
            column_rows.setdefault((_text(row[0]), _text(row[1])), []).append(row)
        for row in all_constraint_rows:
            constraint_rows.setdefault((_text(row[0]), _text(row[1])), []).append(
                tuple(row[index] for index in range(2, 7))
            )
        for row in all_index_rows:
            index_rows.setdefault((_text(row[0]), _text(row[1])), []).append((row[2],))
        columns = {
            key: {int(row[3]): _text(row[2]) for row in rows}
            for key, rows in column_rows.items()
        }
        for spec in registry.specs:
            key = (spec.schema, spec.table)
            issues.extend(
                await _validate_model(
                    connection,
                    spec,
                    columns,
                    rows=column_rows.get(key, []),
                    constraint_rows=constraint_rows.get(key, []),
                    index_rows=index_rows.get(key, []),
                )
            )
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


async def _validate_model(
    connection: Any,
    spec: ModelSpec,
    columns: dict[tuple[str, str], dict[int, str]] | None = None,
    *,
    rows: Any = None,
    constraint_rows: Any = None,
    index_rows: Any = None,
) -> list[SchemaIssue]:
    if columns is None:
        columns = {}
    if rows is None:
        rows = await connection.fetch(_COLUMNS_SQL, spec.schema, spec.table)
    if not rows:
        return [
            SchemaIssue(
                spec.schema,
                spec.table,
                "",
                "missing_table",
                f"{spec.model_type.__name__} maps {spec.qualified_name}, which does "
                "not exist or has no readable columns",
            )
        ]
    issues: list[SchemaIssue] = []
    actual = {str(row[2]): row for row in rows}
    by_position = {int(row[3]): str(row[2]) for row in rows}
    columns[(spec.schema, spec.table)] = by_position

    for column in spec.columns:
        row = actual.get(column.database_name)
        if row is None:
            issues.append(
                SchemaIssue(
                    spec.schema,
                    spec.table,
                    column.database_name,
                    "missing_column",
                    f"{spec.model_type.__name__}.{column.python_name} has no matching "
                    "database column",
                )
            )
            continue
        type_oid = int(row[4])
        if type_oid != column.oid:
            issues.append(
                SchemaIssue(
                    spec.schema,
                    spec.table,
                    column.database_name,
                    "type_mismatch",
                    f"declared {column.pg_type.name} (OID {column.oid}) but the "
                    f"database has OID {type_oid}",
                )
            )
        not_null = bool(row[5])
        if not_null == column.nullable:
            issues.append(
                SchemaIssue(
                    spec.schema,
                    spec.table,
                    column.database_name,
                    "nullability_mismatch",
                    f"declared {'nullable' if column.nullable else 'not null'} but the "
                    f"database is {'not null' if not_null else 'nullable'}",
                )
            )
        if column.server_default is not None:
            database_default = str(row[7])
            if _normalize_default(database_default) != _normalize_default(column.server_default):
                issues.append(
                    SchemaIssue(
                        spec.schema,
                        spec.table,
                        column.database_name,
                        "server_default_mismatch",
                        f"declared server_default {column.server_default!r} but the "
                        f"database has {database_default!r}",
                    )
                )

    issues.extend(
        await _validate_constraints(
            connection,
            spec,
            by_position,
            columns,
            rows=constraint_rows,
            index_rows=index_rows,
        )
    )
    return issues


async def _column_names(
    connection: Any,
    schema: str,
    table: str,
    columns: dict[tuple[str, str], dict[int, str]],
) -> dict[int, str]:
    """The `attnum -> name` map for one table, read once per validation run.

    A foreign key's target may be a model this registry maps, a table it does
    not, or a table in another schema entirely, so the map is fetched on demand
    rather than assembled from the specs.
    """
    key = (schema, table)
    cached = columns.get(key)
    if cached is not None:
        return cached
    rows = await connection.fetch(_COLUMNS_SQL, schema, table)
    resolved = {int(row[3]): str(row[2]) for row in rows}
    columns[key] = resolved
    return resolved


async def _validate_constraints(
    connection: Any,
    spec: ModelSpec,
    by_position: dict[int, str],
    columns: dict[tuple[str, str], dict[int, str]] | None = None,
    *,
    rows: Any = None,
    index_rows: Any = None,
) -> list[SchemaIssue]:
    if columns is None:
        columns = {}
    columns.setdefault((spec.schema, spec.table), by_position)
    issues: list[SchemaIssue] = []
    if rows is None:
        rows = await connection.fetch(_CONSTRAINTS_SQL, spec.schema, spec.table)
    primary: tuple[str, ...] = ()
    unique: set[tuple[str, ...]] = set()
    foreign: set[tuple[tuple[tuple[str, str], ...], str, str]] = set()
    for row in rows:
        kind = _text(row[0])
        local = _names(row[1], by_position)
        if kind == "p":
            primary = local
        elif kind == "u":
            unique.add(local)
        elif kind == "f":
            remote_schema, remote_table = _text(row[3]), _text(row[4])
            remote_by_position = await _column_names(
                connection, remote_schema, remote_table, columns
            )
            foreign.add(
                (
                    _pairs(row[1], row[2], by_position, remote_by_position),
                    remote_schema,
                    remote_table,
                )
            )

    if index_rows is None:
        index_rows = await connection.fetch(_INDEXES_SQL, spec.schema, spec.table)
    for row in index_rows:
        unique.add(_names(row[0], by_position))

    declared_primary = tuple(item.database_name for item in spec.primary_key)
    if primary != declared_primary:
        issues.append(
            SchemaIssue(
                spec.schema,
                spec.table,
                "",
                "primary_key_mismatch",
                f"declared primary key ({', '.join(declared_primary)}) but the "
                f"database has ({', '.join(primary) or 'none'})",
            )
        )
    for column in spec.columns:
        if column.unique and (column.database_name,) not in unique:
            issues.append(
                SchemaIssue(
                    spec.schema,
                    spec.table,
                    column.database_name,
                    "missing_unique",
                    "declared unique=True but the database has no unique constraint or index on it",
                )
            )
    # Index corresponding local/remote column pairs so each declared reference
    # validates both ends with one lookup. Flattening composite constraints here
    # preserves O(C + F) behavior while checking each paired target column.
    foreign_keys = {
        (local_name, schema, table, remote_name)
        for pairs, schema, table in foreign
        for local_name, remote_name in pairs
    }
    for column in spec.columns:
        reference = column.reference
        if reference is None:
            continue
        if (
            column.database_name,
            str(reference.schema),
            reference.table,
            reference.column,
        ) not in foreign_keys:
            issues.append(
                SchemaIssue(
                    spec.schema,
                    spec.table,
                    column.database_name,
                    "missing_foreign_key",
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

    The SQL casts both spellings to `text`, and they render differently:
    `conkey` is an `int2[]` and arrives as `{1,2}`, while `indkey` is an
    `int2vector` and arrives as `1 2`. `confkey` is NULL on every
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
    return tuple(by_position[item] for item in _positions(value) if item in by_position)


def _pairs(
    local: Any,
    remote: Any,
    by_position: dict[int, str],
    remote_by_position: dict[int, str],
) -> tuple[tuple[str, str], ...]:
    """Pair a foreign key's local columns with their targets, by *name*.

    `conkey` and `confkey` are physical `attnum` vectors, and an `attnum`
    is the order PostgreSQL created the columns in, which is not the order the
    model declares them in -- wreath's own DDL generator sorts operations, so a
    table declared `id, name, slug` is created `name, ..., slug, id` and
    `id` is attnum 6. Comparing either vector against a declaration index is
    therefore wrong on any table whose creation order differs, which is most of
    them; both sides are resolved to names here so the comparison is made in the
    only vocabulary the declaration and the catalog share.

    A position missing from its map is dropped rather than guessed at. On the
    local side that means a column the catalog does not have, already reported
    as `missing_column`; on the remote side, a target table that does not
    exist, which surfaces as the `missing_foreign_key` this pairing feeds.
    """
    return tuple(
        (local_name, remote_name)
        for local_position, remote_position in zip(
            _positions(local), _positions(remote), strict=False
        )
        if (local_name := by_position.get(local_position)) is not None
        and (remote_name := remote_by_position.get(remote_position)) is not None
    )


def _normalize_default(value: str) -> str:
    """Compare server defaults ignoring whitespace and PostgreSQL's casts.

    Peels matched outer `(...)` pairs (stripping whitespace between them) using
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


__all__ = [
    "ExtensionTypeResolution",
    "SchemaDiff",
    "SchemaIssue",
    "declared_extension_columns",
    "declared_index_methods",
    "probe_default_opclasses",
    "probe_extension_types",
    "resolve_default_opclasses",
    "resolve_extension_types",
    "validate_registry",
]
