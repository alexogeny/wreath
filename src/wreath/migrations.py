"""Wreath-metal PostgreSQL migration configuration and bounded result views."""

from __future__ import annotations

import importlib
import struct
from dataclasses import dataclass
from typing import Any, Literal

_SINGLE_CATALOG_SQL = """
WITH migration_objects AS (
    SELECT
        n.nspname::text AS schema_name,
        c.relname::text AS table_name,
        ''::text AS object_name,
        1::int4 AS object_kind,
        concat_ws(E'\\x1f', 'table', c.relkind::text, c.relpersistence::text)::text
            AS signature
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = $1
      AND c.relkind IN ('r', 'p')

    UNION ALL

    SELECT
        n.nspname::text,
        c.relname::text,
        a.attname::text,
        2::int4,
        concat_ws(
            E'\\x1f', 'column', a.atttypid::text, ''::text,
            a.attnotnull::int::text, a.attidentity::text, a.attgenerated::text,
            COALESCE(pg_catalog.pg_get_expr(d.adbin, d.adrelid), '')
        )::text
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
    LEFT JOIN pg_catalog.pg_attrdef d
      ON d.adrelid = c.oid AND d.adnum = a.attnum
    WHERE n.nspname = $1
      AND c.relkind IN ('r', 'p')
      AND a.attnum > 0
      AND NOT a.attisdropped

    UNION ALL

    SELECT
        n.nspname::text,
        c.relname::text,
        concat_ws(
            ':', con.contype::text,
            COALESCE((
                SELECT string_agg(a.attname, ',' ORDER BY key.ord)
                FROM unnest(con.conkey) WITH ORDINALITY AS key(attnum, ord)
                JOIN pg_catalog.pg_attribute a
                  ON a.attrelid = con.conrelid AND a.attnum = key.attnum
            ), ''),
            COALESCE(fn.nspname, ''), COALESCE(fc.relname, ''),
            COALESCE((
                SELECT string_agg(a.attname, ',' ORDER BY key.ord)
                FROM unnest(con.confkey) WITH ORDINALITY AS key(attnum, ord)
                JOIN pg_catalog.pg_attribute a
                  ON a.attrelid = con.confrelid AND a.attnum = key.attnum
            ), '')
        )::text AS object_name,
        3::int4,
        concat_ws(
            ':', con.contype::text,
            COALESCE((
                SELECT string_agg(a.attname, ',' ORDER BY key.ord)
                FROM unnest(con.conkey) WITH ORDINALITY AS key(attnum, ord)
                JOIN pg_catalog.pg_attribute a
                  ON a.attrelid = con.conrelid AND a.attnum = key.attnum
            ), ''),
            COALESCE(fn.nspname, ''), COALESCE(fc.relname, ''),
            COALESCE((
                SELECT string_agg(a.attname, ',' ORDER BY key.ord)
                FROM unnest(con.confkey) WITH ORDINALITY AS key(attnum, ord)
                JOIN pg_catalog.pg_attribute a
                  ON a.attrelid = con.confrelid AND a.attnum = key.attnum
            ), '')
        )::text AS signature
    FROM pg_catalog.pg_constraint con
    JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_catalog.pg_class fc ON fc.oid = con.confrelid
    LEFT JOIN pg_catalog.pg_namespace fn ON fn.oid = fc.relnamespace
    WHERE n.nspname = $1
      AND con.contype IN ('p', 'u', 'f')

    UNION ALL

    SELECT
        n.nspname::text,
        c.relname::text,
        concat(
            CASE WHEN i.indisunique THEN 'ui:' ELSE 'i:' END,
            columns.column_names
        )::text,
        4::int4,
        concat(
            'index:', CASE WHEN i.indisunique THEN 'ui:' ELSE 'i:' END,
            columns.column_names, ':btree'
        )::text
    FROM pg_catalog.pg_index i
    JOIN pg_catalog.pg_class c ON c.oid = i.indrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
    JOIN pg_catalog.pg_am am ON am.oid = ic.relam
    CROSS JOIN LATERAL (
        SELECT string_agg(a.attname, ',' ORDER BY key.ord) AS column_names
        FROM unnest(i.indkey::smallint[]) WITH ORDINALITY AS key(attnum, ord)
        JOIN pg_catalog.pg_attribute a
          ON a.attrelid = i.indrelid AND a.attnum = key.attnum
    ) columns
    WHERE n.nspname = $1
      AND am.amname = 'btree'
      AND i.indisvalid
      AND i.indisready
      AND i.indpred IS NULL
      AND i.indexprs IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_constraint con
          WHERE con.conindid = i.indexrelid
      )
)
SELECT schema_name, table_name, object_name, object_kind, signature
FROM migration_objects
ORDER BY object_kind, schema_name, table_name, object_name
"""


@dataclass(frozen=True, slots=True)
class ResolutionPolicy:
    """Whether fleet readiness trusts verified history or audits every catalog."""

    kind: Literal["managed", "strict"]
    sample_size: int = 0

    @classmethod
    def managed(cls, *, sample_size: int = 0) -> ResolutionPolicy:
        if not isinstance(sample_size, int) or sample_size < 0:
            raise ValueError("sample_size must be a non-negative integer")
        return cls("managed", sample_size)

    @classmethod
    def strict(cls) -> ResolutionPolicy:
        return cls("strict")


@dataclass(frozen=True, slots=True)
class MigrationConfig:
    database: str
    policy: ResolutionPolicy
    catalog_chunk_size: int = 256
    concurrency: int = 8
    max_failures: int = 100

    def __post_init__(self) -> None:
        for name in ("catalog_chunk_size", "concurrency", "max_failures"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class FleetResolution:
    current: int
    apply: int
    verify: int
    ambiguous: int
    blocked: int

    @property
    def total(self) -> int:
        return self.current + self.apply + self.verify + self.ambiguous + self.blocked


@dataclass(frozen=True, slots=True)
class NativeMigrationDiff:
    """A deterministic packed operation tape produced entirely by Wreath-metal."""

    operation_count: int
    tape: bytes


@dataclass(frozen=True, slots=True)
class NativeMigrationChain:
    """Metal-verified artifact-chain tip and migration count."""

    checksum: bytes
    target_fingerprint: bytes
    migration_count: int


@dataclass(frozen=True, slots=True)
class NativeMigrationArtifact:
    """Verified immutable artifact metadata with its packed operation tape."""

    data: bytes
    checksum: bytes
    migration_id: bytes
    parent_checksum: bytes
    source_fingerprint: bytes
    target_fingerprint: bytes
    operation_tape: bytes
    named_plan: bytes
    sql_tape: bytes


@dataclass(frozen=True, slots=True)
class NativeMigrationSql:
    """Deterministic WMS1 statement tape derived from a named native plan."""

    operation_count: int
    manual_count: int
    destructive_count: int
    tape: bytes


@dataclass(frozen=True, slots=True)
class NativeMigrationPlan:
    """Deterministic named WMP1 operation plan produced by Wreath-metal."""

    operation_count: int
    tape: bytes


@dataclass(frozen=True, slots=True)
class NativeCatalogSnapshot:
    """Canonical image plus its bounded native names/signatures descriptor."""

    image: bytes
    descriptor: bytes


@dataclass(frozen=True, slots=True)
class MigrationDetection:
    """Bounded result of comparing ORM intent with one live schema."""

    desired_fingerprint: bytes
    actual_fingerprint: bytes
    diff: NativeMigrationDiff

    @property
    def current(self) -> bool:
        return self.diff.operation_count == 0


@dataclass(frozen=True, slots=True)
class MigrationApplyResult:
    """Verified result of one locked, transactional single-schema application."""

    migration_id: bytes
    checksum: bytes
    source_fingerprint: bytes
    target_fingerprint: bytes
    destructive_approved: bool


@dataclass(frozen=True, slots=True)
class MigrationGeneration:
    """Review metadata retained by the native named operation planner."""

    desired_fingerprint: bytes
    actual_fingerprint: bytes
    diff: NativeMigrationDiff
    plan: NativeMigrationPlan
    sql: NativeMigrationSql


def _metal() -> Any:
    try:
        module = importlib.import_module("wreath._native._postgres")
    except ImportError as error:
        raise RuntimeError(
            "wreath.migrations requires the Wreath-metal PostgreSQL extension"
        ) from error
    if not hasattr(module, "_migration_resolve_managed"):
        raise RuntimeError(
            "the installed Wreath-metal PostgreSQL extension does not provide migrations; "
            "rebuild Wreath's native extensions"
        )
    return module


def _resolve_managed_snapshot(
    snapshot: object,
    *,
    target_migration: int,
    target_checksum: int,
    directory_generation: int,
) -> FleetResolution:
    """Classify a trusted packed history snapshot in one metal invocation.

    This is an internal seam for the direct PostgreSQL history destination and
    benchmark fixtures. Applications use the migration runner rather than build
    snapshots themselves.
    """
    counts = _metal()._migration_resolve_managed(
        snapshot,
        target_migration,
        target_checksum,
        directory_generation,
    )
    return FleetResolution(*counts)


def _descriptor_record(
    schema: str,
    table: str,
    name: str,
    kind: int,
    signature: str,
) -> bytes:
    parts = tuple(value.encode("utf-8") for value in (schema, table, name, signature))
    if any(len(value) > 0xFFFF for value in parts):
        raise ValueError("migration descriptor value exceeds 65535 bytes")
    return struct.pack(
        "<HHHHI", *(len(value) for value in parts), kind
    ) + b"".join(parts)


def _registry_descriptor(registry: Any) -> bytes:
    """Pack immutable ORM intent once for native migration compilation."""
    records: list[bytes] = []
    for spec in registry.specs:
        if spec.sql_namespace != "qualified" or not spec.schema:
            raise ValueError(
                "isolated tenant templates require the fleet desired-image compiler"
            )
        records.append(
            _descriptor_record(spec.schema, spec.table, "", 1, "table\x1fr\x1fp")
        )
        for column in spec.columns:
            signature = "\x1f".join(
                (
                    "column",
                    str(column.oid),
                    "",
                    "1" if not column.nullable else "0",
                    "",
                    "",
                    column.server_default or "",
                )
            )
            records.append(
                _descriptor_record(
                    spec.schema,
                    spec.table,
                    column.database_name,
                    2,
                    signature,
                )
            )
        primary_columns = ",".join(column.database_name for column in spec.primary_key)
        primary_name = f"p:{primary_columns}:::"
        records.append(
            _descriptor_record(spec.schema, spec.table, primary_name, 3, primary_name)
        )
        for column in spec.columns:
            if column.unique and not column.primary_key:
                unique_name = f"u:{column.database_name}:::"
                records.append(
                    _descriptor_record(
                        spec.schema, spec.table, unique_name, 3, unique_name
                    )
                )
            if column.indexed:
                index_name = f"i:{column.database_name}"
                records.append(
                    _descriptor_record(
                        spec.schema,
                        spec.table,
                        index_name,
                        4,
                        f"index:{index_name}:btree",
                    )
                )
            reference = column.reference
            if reference is not None:
                target = registry.spec_for(reference.model_type)
                target_column = target.columns[reference.position - 1]
                foreign_name = (
                    f"f:{column.database_name}:{target.schema}:{target.table}:"
                    f"{target_column.database_name}"
                )
                records.append(
                    _descriptor_record(
                        spec.schema, spec.table, foreign_name, 3, foreign_name
                    )
                )
    return b"WMD1" + struct.pack("<II", 1, len(records)) + b"".join(records)


def _compile_registry_image(registry: Any) -> bytes:
    """Compile immutable ORM intent into one native desired image."""
    descriptor = _registry_descriptor(registry)
    module = _metal()
    if not hasattr(module, "_migration_compile_desired"):
        raise RuntimeError(
            "the installed Wreath-metal PostgreSQL extension lacks desired images; "
            "rebuild Wreath's native extensions"
        )
    return module._migration_compile_desired(descriptor)


async def _decode_catalog_snapshot(
    connection: Any,
    sql: str,
    args: tuple[object, ...] = (),
) -> NativeCatalogSnapshot:
    """Decode catalog rows directly without allocating Python records."""
    module = _metal()
    if not hasattr(module, "_migration_catalog_builder"):
        raise RuntimeError(
            "the installed Wreath-metal PostgreSQL extension lacks catalog images; "
            "rebuild Wreath's native extensions"
        )
    builder = module._migration_catalog_builder()
    await connection._fetch_into(sql, args, builder)
    descriptor = builder.descriptor()
    return NativeCatalogSnapshot(builder.finish(), descriptor)


async def _decode_catalog_image(
    connection: Any,
    sql: str,
    args: tuple[object, ...] = (),
) -> bytes:
    """Run one catalog query directly into a native image destination."""
    module = _metal()
    builder = module._migration_catalog_builder()
    await connection._fetch_into(sql, args, builder)
    return builder.finish()


async def _read_single_catalog(connection: Any, schema: str) -> bytes:
    """Read tables and columns for one schema through the direct metal destination."""
    return await _decode_catalog_image(connection, _SINGLE_CATALOG_SQL, (schema,))


def _fingerprint_image(image: bytes) -> bytes:
    module = _metal()
    if not hasattr(module, "_migration_image_fingerprint"):
        raise RuntimeError(
            "the installed Wreath-metal PostgreSQL extension lacks image fingerprints; "
            "rebuild Wreath's native extensions"
        )
    return module._migration_image_fingerprint(image)


def _plan_descriptors(desired: bytes, actual: bytes) -> NativeMigrationPlan:
    module = _metal()
    if not hasattr(module, "_migration_plan_descriptors"):
        raise RuntimeError(
            "the installed Wreath-metal PostgreSQL extension lacks named plans; "
            "rebuild Wreath's native extensions"
        )
    tape = module._migration_plan_descriptors(desired, actual)
    return NativeMigrationPlan(int.from_bytes(tape[8:12], "little"), tape)


def _render_sql_plan(plan: NativeMigrationPlan) -> NativeMigrationSql:
    module = _metal()
    if not hasattr(module, "_migration_render_sql"):
        raise RuntimeError(
            "the installed Wreath-metal PostgreSQL extension lacks SQL tapes; "
            "rebuild Wreath's native extensions"
        )
    tape = module._migration_render_sql(plan.tape)
    if len(tape) < 12 or tape[:4] != b"WMS1" or int.from_bytes(tape[4:8], "little") != 1:
        raise RuntimeError("Wreath-metal returned an invalid SQL tape")
    count = int.from_bytes(tape[8:12], "little")
    offset = 12
    manual = destructive = 0
    for _ in range(count):
        if len(tape) - offset < 8:
            raise RuntimeError("Wreath-metal returned a truncated SQL tape")
        flags = int.from_bytes(tape[offset : offset + 4], "little")
        length = int.from_bytes(tape[offset + 4 : offset + 8], "little")
        offset += 8
        if flags & ~3 or length > len(tape) - offset:
            raise RuntimeError("Wreath-metal returned an invalid SQL statement")
        manual += bool(flags & 2)
        destructive += bool(flags & 1)
        offset += length
    if offset != len(tape) or count != plan.operation_count:
        raise RuntimeError("native SQL tape and named plan disagree")
    return NativeMigrationSql(count, manual, destructive, tape)


def _diff_packed_images(desired: object, actual: object) -> NativeMigrationDiff:
    """Diff two canonical native schema images without materializing operations."""
    module = _metal()
    if not hasattr(module, "_migration_diff_images"):
        raise RuntimeError(
            "the installed Wreath-metal PostgreSQL extension lacks migration images; "
            "rebuild Wreath's native extensions"
        )
    tape = module._migration_diff_images(desired, actual)
    operation_count = int.from_bytes(tape[8:12], "little")
    return NativeMigrationDiff(operation_count, tape)


async def detect_single(registry: Any, connection: Any) -> MigrationDetection:
    """Compare one compiled single-schema registry with live PostgreSQL."""
    desired = _compile_registry_image(registry)
    schemas = {spec.schema for spec in registry.specs}
    if len(schemas) != 1:
        raise ValueError("detect_single requires exactly one resolved physical schema")
    actual = await _read_single_catalog(connection, next(iter(schemas)))
    return MigrationDetection(
        desired_fingerprint=_fingerprint_image(desired),
        actual_fingerprint=_fingerprint_image(actual),
        diff=_diff_packed_images(desired, actual),
    )


async def generate_single_plan(registry: Any, connection: Any) -> MigrationGeneration:
    """Build a deterministic named plan for one live physical schema."""
    desired_descriptor = _registry_descriptor(registry)
    desired = _metal()._migration_compile_desired(desired_descriptor)
    schemas = {spec.schema for spec in registry.specs}
    if len(schemas) != 1:
        raise ValueError(
            "generate_single_plan requires exactly one resolved physical schema"
        )
    actual = await _decode_catalog_snapshot(
        connection, _SINGLE_CATALOG_SQL, (next(iter(schemas)),)
    )
    diff = _diff_packed_images(desired, actual.image)
    plan = _plan_descriptors(desired_descriptor, actual.descriptor)
    if plan.operation_count != diff.operation_count:
        raise RuntimeError("native named plan and image diff disagree")
    derived_operations = _metal()._migration_operations_from_plan(plan.tape)
    if derived_operations != diff.tape:
        raise RuntimeError(
            "native named plan describes different operations than the image diff"
        )
    sql = _render_sql_plan(plan)
    return MigrationGeneration(
        desired_fingerprint=_fingerprint_image(desired),
        actual_fingerprint=_fingerprint_image(actual.image),
        diff=diff,
        plan=plan,
        sql=sql,
    )


async def connect_migration(dsn: str) -> Any:
    """Open a dedicated Wreath-metal migration connection."""
    module = _metal()
    if not hasattr(module, "connect"):
        raise RuntimeError(
            "Wreath-metal PostgreSQL connection support is unavailable; rebuild native extensions"
        )
    return await module.connect(dsn)


def _qualified_history_table() -> str:
    return '"wreath_migrations"."history"'


async def _bootstrap_migration_history(connection: Any) -> None:
    history = _qualified_history_table()
    await connection.execute("BEGIN")
    committed = False
    try:
        await connection.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
            "wreath:migrations:bootstrap",
        )
        await connection.execute('CREATE SCHEMA IF NOT EXISTS "wreath_migrations"')
        await connection.execute(
            f"""CREATE TABLE IF NOT EXISTS {history} (
                sequence bigint GENERATED ALWAYS AS IDENTITY,
                target_schema text NOT NULL,
                migration_id bytea NOT NULL,
                checksum bytea NOT NULL,
                parent_checksum bytea NOT NULL,
                source_fingerprint bytea NOT NULL,
                target_fingerprint bytea NOT NULL,
                destructive_approved boolean NOT NULL,
                applied_at timestamp with time zone NOT NULL DEFAULT clock_timestamp(),
                PRIMARY KEY (target_schema, migration_id),
                UNIQUE (target_schema, checksum)
            )"""
        )
        await connection.execute("COMMIT")
        committed = True
    finally:
        if not committed:
            await connection.execute("ROLLBACK")


async def apply_single_artifact(
    registry: Any,
    connection: Any,
    artifact_data: bytes,
    *,
    allow_destructive: bool = False,
) -> MigrationApplyResult:
    """Apply one authoritative artifact under a transaction-scoped native plan."""
    artifact = _load_native_artifact(artifact_data)
    schemas = {spec.schema for spec in registry.specs}
    if len(schemas) != 1:
        raise ValueError(
            "apply_single_artifact requires exactly one resolved physical schema"
        )
    schema = next(iter(schemas))
    module = _metal()
    ddl_block = module._migration_build_ddl_block(
        artifact.sql_tape, allow_destructive
    )
    history = _qualified_history_table()
    zero_checksum = bytes(32)
    await _bootstrap_migration_history(connection)
    await connection.execute("BEGIN")
    committed = False
    try:
        await connection.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
            f"wreath:migrations:{schema}",
        )
        previous = await connection.fetchrow(
            f"""SELECT checksum, target_fingerprint
                FROM {history}
                WHERE target_schema = $1
                ORDER BY sequence DESC
                LIMIT 1""",
            schema,
        )
        if previous is None:
            if artifact.parent_checksum != zero_checksum:
                raise RuntimeError(
                    "cannot apply migration: database has no Wreath history for schema "
                    f"{schema!r}, but artifact parent is {artifact.parent_checksum.hex()} "
                    "instead of the all-zero root checksum"
                )
        else:
            previous_checksum = bytes(previous[0])
            previous_target = bytes(previous[1])
            if artifact.parent_checksum != previous_checksum:
                raise RuntimeError(
                    "cannot apply migration: artifact parent "
                    f"{artifact.parent_checksum.hex()} does not match database history tip "
                    f"{previous_checksum.hex()} for schema {schema!r}"
                )
            if artifact.source_fingerprint != previous_target:
                raise RuntimeError(
                    "cannot apply migration: artifact source fingerprint "
                    f"{artifact.source_fingerprint.hex()} does not match history target "
                    f"{previous_target.hex()} for schema {schema!r}"
                )
        actual = await _decode_catalog_snapshot(
            connection, _SINGLE_CATALOG_SQL, (schema,)
        )
        actual_fingerprint = _fingerprint_image(actual.image)
        if actual_fingerprint != artifact.source_fingerprint:
            raise RuntimeError(
                "cannot apply migration: live catalog fingerprint "
                f"{actual_fingerprint.hex()} does not match artifact source "
                f"{artifact.source_fingerprint.hex()} for schema {schema!r}"
            )
        await connection.execute(ddl_block)
        resulting = await _decode_catalog_snapshot(
            connection, _SINGLE_CATALOG_SQL, (schema,)
        )
        resulting_fingerprint = _fingerprint_image(resulting.image)
        if resulting_fingerprint != artifact.target_fingerprint:
            raise RuntimeError(
                "migration DDL ran but target verification failed: live catalog fingerprint "
                f"{resulting_fingerprint.hex()} does not match artifact target "
                f"{artifact.target_fingerprint.hex()} for schema {schema!r}; "
                "the transaction will be rolled back"
            )
        await connection.execute(
            f"""INSERT INTO {history} (
                target_schema, migration_id, checksum, parent_checksum,
                source_fingerprint, target_fingerprint, destructive_approved
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            schema,
            artifact.migration_id,
            artifact.checksum,
            artifact.parent_checksum,
            artifact.source_fingerprint,
            artifact.target_fingerprint,
            allow_destructive,
        )
        await connection.execute("COMMIT")
        committed = True
    finally:
        if not committed:
            await connection.execute("ROLLBACK")
    return MigrationApplyResult(
        migration_id=artifact.migration_id,
        checksum=artifact.checksum,
        source_fingerprint=artifact.source_fingerprint,
        target_fingerprint=artifact.target_fingerprint,
        destructive_approved=allow_destructive,
    )


def _build_native_artifact(
    *,
    migration_id: bytes,
    parent_checksum: bytes,
    source_fingerprint: bytes,
    target_fingerprint: bytes,
    operation_tape: bytes,
    named_plan: bytes,
    sql_tape: bytes,
) -> NativeMigrationArtifact:
    """Build and immediately verify one deterministic artifact in metal."""
    module = _metal()
    if not hasattr(module, "_migration_build_artifact"):
        raise RuntimeError(
            "the installed Wreath-metal PostgreSQL extension lacks migration artifacts; "
            "rebuild Wreath's native extensions"
        )
    data = module._migration_build_artifact(
        migration_id,
        parent_checksum,
        source_fingerprint,
        target_fingerprint,
        operation_tape,
        named_plan,
        sql_tape,
    )
    return _load_native_artifact(data)


def _verify_native_chain(
    artifacts: tuple[bytes, ...],
    *,
    expected_parent: bytes,
    expected_source: bytes,
) -> NativeMigrationChain:
    payload = bytearray(b"WMC1" + struct.pack("<II", 1, len(artifacts)))
    for artifact in artifacts:
        if len(artifact) > 0xFFFFFFFF:
            raise ValueError("migration artifact exceeds WMC1 length limit")
        payload += struct.pack("<I", len(artifact)) + artifact
    module = _metal()
    if not hasattr(module, "_migration_verify_chain"):
        raise RuntimeError(
            "the installed Wreath-metal PostgreSQL extension lacks chain verification; "
            "rebuild Wreath's native extensions"
        )
    checksum, target, count = module._migration_verify_chain(
        bytes(payload), expected_parent, expected_source
    )
    return NativeMigrationChain(checksum, target, count)


def _load_native_artifact(data: bytes) -> NativeMigrationArtifact:
    """Verify checksum, format, lengths, and operation tape before publication."""
    module = _metal()
    if not hasattr(module, "_migration_verify_artifact"):
        raise RuntimeError(
            "the installed Wreath-metal PostgreSQL extension lacks migration artifacts; "
            "rebuild Wreath's native extensions"
        )
    migration_id, parent, source, target, tape, plan, sql = (
        module._migration_verify_artifact(data)
    )
    return NativeMigrationArtifact(
        data=data,
        checksum=data[136:168],
        migration_id=migration_id,
        parent_checksum=parent,
        source_fingerprint=source,
        target_fingerprint=target,
        operation_tape=tape,
        named_plan=plan,
        sql_tape=sql,
    )


__all__ = [
    "FleetResolution",
    "MigrationConfig",
    "MigrationDetection",
    "MigrationGeneration",
    "MigrationApplyResult",
    "NativeCatalogSnapshot",
    "NativeMigrationArtifact",
    "NativeMigrationChain",
    "NativeMigrationDiff",
    "NativeMigrationPlan",
    "NativeMigrationSql",
    "ResolutionPolicy",
    "apply_single_artifact",
    "connect_migration",
    "detect_single",
    "generate_single_plan",
]
