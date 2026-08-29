"""Wreath-metal PostgreSQL migration configuration and bounded result views."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ._migrations.deferred import DeferredDeclarationError, Recode, Retype
from ._migrations.scan import (
    ScanReport,
    TransitionalHazard,
    transitional_read,
    waive_transitional,
)
from ._native import _postgres
from ._pgname import validate_unquoted_identifier
from .orm.fields import _IMPLICIT_OPCLASS_METHODS
from .orm.types import BIT_OID, ExtensionType

#: Built-in types whose type *modifier* is part of the type, so the OID alone
#: cannot reconstruct the SQL. These spell themselves in the descriptor exactly as
#: an extension type does, and `_SINGLE_CATALOG_SQL` above hard-codes the same set
#: because a `CASE` cannot read a Python frozenset. Adding a member means editing
#: both, and `tests/migrations/test_vector.py` holds the two sides equal.
#:
#: `bit` is the only member: every other built-in wreath declares is
#: unparameterised (`Numeric` takes no precision, `Varchar` no length), which is
#: why `oid >= 16384` was a sufficient test until `Bit(length)` existed.
_MODIFIER_BEARING_OIDS = frozenset({BIT_OID})

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
    WHERE n.nspname = $1::text
      AND c.relkind IN ('r', 'p')

    UNION ALL

    SELECT
        n.nspname::text,
        c.relname::text,
        a.attname::text,
        2::int4,
        concat_ws(
            E'\\x1f', 'column', a.atttypid::text,
            -- Field 2 is the type's *spelling*, and is empty for almost every
            -- built-in type so their signatures are byte-identical to what they
            -- always were. An extension type has to spell itself: its OID is
            -- assigned by CREATE EXTENSION, so the renderer cannot map it back to
            -- SQL, and `vector(1536)` versus `vector(3)` is a rewrite the OID
            -- alone cannot see. 16384 is PostgreSQL's first user-assignable OID.
            --
            -- `bit` (1560) is the one built-in that spells itself too, because it
            -- is the only one wreath declares whose *modifier* is part of the
            -- type: `bit(8)` and `bit(512)` share an OID, so blanking the
            -- spelling loses the width and the renderer has nothing to emit. Kept
            -- in step with `_MODIFIER_BEARING_OIDS` below, which must produce the
            -- identical string from the desired side.
            CASE WHEN a.atttypid < 16384 AND a.atttypid <> 1560 THEN ''
                 ELSE pg_catalog.format_type(a.atttypid, a.atttypmod) END,
            a.attnotnull::int::text, a.attidentity::text, a.attgenerated::text,
            COALESCE(pg_catalog.pg_get_expr(d.adbin, d.adrelid), '')
        )::text
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
    LEFT JOIN pg_catalog.pg_attrdef d
      ON d.adrelid = c.oid AND d.adnum = a.attnum
    WHERE n.nspname = $1::text
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
        concat(
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
            ),
            CASE WHEN con.contype = 'f'
                THEN concat(
                    ':', con.confdeltype::text, ':', con.confupdtype::text,
                    ':', con.condeferrable::int::text)
                ELSE ''
            END
        )::text AS signature
    FROM pg_catalog.pg_constraint con
    JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_catalog.pg_class fc ON fc.oid = con.confrelid
    LEFT JOIN pg_catalog.pg_namespace fn ON fn.oid = fc.relnamespace
    WHERE n.nspname = $1::text
      AND con.contype IN ('p', 'u', 'f')

    UNION ALL

    SELECT
        n.nspname::text,
        c.relname::text,
        concat(
            CASE WHEN i.indisunique THEN 'ui:' ELSE 'i:' END,
            columns.column_names,
            -- btree stays implicit so its object name is unchanged; a non-btree
            -- access method is appended so it round-trips against the ORM image.
            CASE WHEN am.amname = 'btree' THEN '' ELSE ':' || am.amname END,
            -- A partial index always spells its method and appends a digest of
            -- the predicate, because two indexes over the same columns with
            -- different WHERE clauses are two different indexes. Four parts
            -- therefore means partial. Must match _predicate_digest().
            CASE WHEN i.indpred IS NULL THEN '' ELSE
                CASE WHEN am.amname = 'btree' THEN ':' || am.amname ELSE '' END
                || ':'
                || substr(md5(pg_get_expr(i.indpred, i.indrelid)), 1, 8)
            END
        )::text,
        4::int4,
        concat(
            'index:', CASE WHEN i.indisunique THEN 'ui:' ELSE 'i:' END,
            columns.column_names, ':', am.amname,
            -- The predicate rides in the signature after a 0x1f, the way a
            -- foreign key's referential actions do, because it contains '::'
            -- and the signature above is ':'-delimited.
            CASE WHEN i.indpred IS NULL THEN '' ELSE
                chr(31) || pg_get_expr(i.indpred, i.indrelid)
            END,
            -- An approximate index carries two more fields: the operator class
            -- per column, and the access method's own options. Both are emitted
            -- only for the methods that need them, so every btree and GIN
            -- signature is byte-identical to what it was. A predicate slot is
            -- always written first (possibly empty) so the field positions are
            -- fixed rather than depending on whether the index is partial.
            CASE WHEN am.amname IN ('btree', 'gin') THEN '' ELSE concat(
                CASE WHEN i.indpred IS NULL THEN chr(31) ELSE '' END,
                chr(31), COALESCE(opclasses.names, ''),
                chr(31), COALESCE(array_to_string(ic.reloptions, ','), '')
            ) END
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
    CROSS JOIN LATERAL (
        -- Only a *non-default* operator class is recorded. The default is what
        -- an index with no opclass clause gets, so naming it here would report
        -- every such index as drifted against a declaration that (rightly) says
        -- nothing about it.
        SELECT string_agg(
            CASE WHEN op.opcdefault THEN '' ELSE op.opcname END, ',' ORDER BY key.ord
        ) AS names
        FROM unnest(i.indclass::oid[]) WITH ORDINALITY AS key(opcoid, ord)
        JOIN pg_catalog.pg_opclass op ON op.oid = key.opcoid
    ) opclasses
    WHERE n.nspname = $1::text
      AND am.amname IN ('btree', 'gin', 'gist', 'hnsw', 'ivfflat')
      AND i.indisvalid
      AND i.indisready
      -- indexprs stays excluded: an index *on* an expression is a different
      -- feature from an index with a WHERE, and is still MANUAL.
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
#: The same catalog query with every *tenant-local* schema name blanked, so two
#: structurally identical tenant schemas produce the same image and therefore the
#: same fingerprint. Without this a fleet cannot exist: `nspname` is the first
#: column of every branch, so `t_alice` and `t_bob` fingerprint differently even
#: when their structure is byte-identical, and one artifact could never verify
#: against a second tenant.
#:
#: **A foreign key's target schema is neutralised only when it is the tenant's
#: own.** An FK into a *shared* schema is a genuine structural fact that every
#: tenant has identically, so blanking it would erase a real difference between
#: an artifact that references shared data and one that does not; leaving a
#: tenant-local target named would make every tenant differ. The `CASE` is the
#: whole distinction and it is why this is a derivation rather than a blanket
#: `replace` of `nspname`.
_FLEET_CATALOG_SQL = (
    _SINGLE_CATALOG_SQL.replace("n.nspname::text AS schema_name", "''::text AS schema_name")
    .replace("        n.nspname::text,", "        ''::text,")
    .replace(
        "COALESCE(fn.nspname, '')",
        "CASE WHEN fn.nspname = $1::text THEN '' ELSE COALESCE(fn.nspname, '') END",
    )
)


# History status codes shared with the native fleet resolver
# (wreath/_native/postgres/migration_resolver.c). A tenant-directory row
# carries one of these to say how much the control plane trusts its recorded
# migration state before the metal resolver classifies it.
HISTORY_UNKNOWN = 0
HISTORY_VERIFIED = 1
HISTORY_AMBIGUOUS = 2
HISTORY_BLOCKED = 3

# One packed tenant-directory row, matching WREATH_MIGRATION_ROW_SIZE (32B) and
# the field offsets the native resolver reads: migration@8, checksum@16,
# generation@24, status@28. The leading id is carried for the caller's benefit
# and is not read by the resolver.
_FLEET_ROW = struct.Struct("<QQQIB3x")
# A raise, not an `assert`: `python -O` strips asserts, and this one guards a
# layout the native resolver reads by fixed offset. Stripped, a mislaid format
# string would reach the resolver and be misparsed rather than refused here.
if _FLEET_ROW.size != 32:
    raise RuntimeError(
        f"the packed fleet row is {_FLEET_ROW.size} bytes, but the native "
        "resolver reads WREATH_MIGRATION_ROW_SIZE (32); the format string and "
        "the offsets documented above it have diverged"
    )


@dataclass(frozen=True, slots=True)
class TenantState:
    """One tenant's recorded migration state, as the directory knows it.

    `status` is one of the `HISTORY_*` codes. `VERIFIED` means the
    checksummed history was proven and can be trusted as the fast readiness
    authority; `UNKNOWN` forces catalog verification; `AMBIGUOUS` and
    `BLOCKED` are terminal operational states the resolver never treats as
    current. Applications build these from their own tenant directory; the
    runner never invents tenant identity.
    """

    tenant_id: int
    migration: int
    checksum: int
    generation: int
    status: int = HISTORY_UNKNOWN

    def __post_init__(self) -> None:
        for name in ("tenant_id", "migration", "checksum", "generation"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"TenantState.{name} must be a non-negative integer")
        if self.status not in (
            HISTORY_UNKNOWN,
            HISTORY_VERIFIED,
            HISTORY_AMBIGUOUS,
            HISTORY_BLOCKED,
        ):
            raise ValueError(f"TenantState.status {self.status!r} is not a HISTORY_* code")
        if self.tenant_id > 0xFFFFFFFFFFFFFFFF or self.migration > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("tenant_id and migration must fit in 64 bits")
        if self.checksum > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("checksum must fit in 64 bits")
        if self.generation > 0xFFFFFFFF:
            raise ValueError("generation must fit in 32 bits")


def pack_tenant_directory(tenants: Any) -> bytes:
    """Pack tenant-directory rows into the resolver's contiguous snapshot.

    The layout is the one the native metal resolver consumes; keeping the pack
    here (rather than in each caller) means the wire format has exactly one
    author.
    """
    buffer = bytearray()
    for state in tenants:
        buffer += _FLEET_ROW.pack(
            state.tenant_id,
            state.migration,
            state.checksum,
            state.generation,
            state.status,
        )
    return bytes(buffer)


def resolve_fleet(
    tenants: Any,
    *,
    target_migration: int,
    target_checksum: int,
    directory_generation: int,
) -> FleetResolution:
    """Classify a whole tenant fleet's readiness in one metal invocation.

    This is the managed fleet runner: it packs the caller's tenant directory
    into the native snapshot and resolves it with a single Wreath-metal call,
    returning bounded per-bucket counts rather than one Python object per
    tenant. A tenant is `current` only when its trusted, verified history is
    at the target migration and checksum for the current directory generation;
    unknown, stale, or wrong-generation tenants fall to `verify` (catalog
    audit), and `ambiguous`/`blocked` stay terminal.

    The runner reads no live database and acquires no DDL authority. Turning a
    `verify` count into per-tenant catalog audits is a separate, explicitly
    privileged step; classification is deliberately side-effect free.
    """
    if not isinstance(target_migration, int) or target_migration < 0:
        raise ValueError("target_migration must be a non-negative integer")
    if not isinstance(target_checksum, int) or target_checksum < 0:
        raise ValueError("target_checksum must be a non-negative integer")
    if not isinstance(directory_generation, int) or directory_generation < 0:
        raise ValueError("directory_generation must be a non-negative integer")
    snapshot = pack_tenant_directory(tenants)
    return _resolve_managed_snapshot(
        snapshot,
        target_migration=target_migration,
        target_checksum=target_checksum,
        directory_generation=directory_generation,
    )


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
class MigrationRevertResult:
    """Verified result of one locked, transactional single-schema downgrade."""

    migration_id: bytes
    checksum: bytes
    source_fingerprint: bytes
    target_fingerprint: bytes
    destructive_approved: bool
    forced: bool


_HAZARD_KIND_NAMES = {1: "table", 2: "column", 3: "constraint", 4: "index"}


class FleetRunInProgress(RuntimeError):
    """Another runner holds the fleet lock for this migration."""


@dataclass(frozen=True, slots=True)
class DowngradeHazard:
    """One ORM-mapped object a downgrade would strand or retype under the code."""

    schema: str
    table: str
    name: str
    kind: str
    reason: str

    def explain(self) -> str:
        target = ".".join(part for part in (self.table, self.name) if part)
        verb = "would drop" if self.reason == "removed" else "would change the type of"
        return f"{verb} {self.kind} {self.schema}.{target}, still mapped by the ORM"


class DowngradeWouldStrandCode(RuntimeError):
    """A downgrade was refused because live ORM code still references what it removes."""

    def __init__(self, schema: str, hazards: tuple[DowngradeHazard, ...]) -> None:
        self.schema = schema
        self.hazards = hazards
        listing = "\n".join(f"  - {hazard.explain()}" for hazard in hazards)
        super().__init__(
            f"refusing to downgrade schema {schema!r}: the running ORM still maps "
            f"{len(hazards)} object(s) this downgrade removes or retypes, so the "
            "deployed code would dereference columns that no longer exist:\n"
            f"{listing}\n"
            "Roll the models back with the code, or pass force=True "
            "(CLI: --force) to downgrade anyway (e.g. to re-migrate a local stack)."
        )


@dataclass(frozen=True, slots=True)
class PendingPassHazard:
    """One column this artifact narrows while a pass is still converting it."""

    schema: str
    table: str
    column: str
    action: str
    pass_name: str
    tenant: str
    phase: str
    holes_open: int

    def explain(self) -> str:
        verb = "drops" if self.action == "drop" else "changes the type of"
        where = f"{self.schema}.{self.table}.{self.column}"
        who = self.pass_name if not self.tenant else f"{self.pass_name}[{self.tenant}]"
        state = f"phase {self.phase}"
        if self.holes_open:
            state += f", {self.holes_open} chunk(s) given up on"
        return f"{verb} {where}, which pass {who!r} has not finished ({state})"


class MigrationBlockedByPass(RuntimeError):
    """A migration was refused because a pass has not published for a column it narrows."""

    def __init__(self, schema: str, hazards: tuple[PendingPassHazard, ...]) -> None:
        self.schema = schema
        self.hazards = hazards
        listing = "\n".join(f"  - {hazard.explain()}" for hazard in hazards)
        blocked = tuple(h for h in hazards if h.holes_open)
        remedy = (
            "Let the pass finish -- `wreath passes status` shows where it is -- and "
            "apply this migration afterwards. The pass's gate publishes when it has "
            "verified every row converted, and that publication is what clears this."
        )
        if blocked:
            remedy += (
                "\nSome of these have chunks that were given up on; a pass cannot "
                "publish while a hole bars its gate. Clear them with "
                "`wreath passes retry <name>` first."
            )
        super().__init__(
            f"refusing to apply migration to schema {schema!r}: it narrows "
            f"{len(hazards)} column(s) a chunked pass is still converting, and "
            "narrowing a column before its backfill finishes loses the rows "
            "behind the cursor:\n"
            f"{listing}\n"
            f"{remedy}"
        )


@dataclass(frozen=True, slots=True)
class RecodedColumnHazard:
    """One column this downgrade touches whose values a re-encode has changed."""

    schema: str
    table: str
    column: str
    action: str
    pass_name: str
    tenant: str
    phase: str
    finished: bool
    #: `False` when only the append-only record survives. See
    #: `wreath._passes.ledger.RewrittenColumn`.
    ledger_row_present: bool = True

    def explain(self) -> str:
        where = f"{self.schema}.{self.table}.{self.column}"
        who = self.pass_name if not self.tenant else f"{self.pass_name}[{self.tenant}]"
        if not self.ledger_row_present:
            return (
                f"{where}, re-encoded by {who!r}, whose ledger row is gone -- only the "
                "append-only pass_rewrites record remains, so the pass's phase cannot "
                "be read. The values were still changed"
            )
        state = (
            "which has finished, so every row is in the new encoding"
            if self.finished
            else f"which is still running (phase {self.phase}), so the rows are a mixture"
        )
        return f"{where}, re-encoded by {who!r}, {state}"


class DowngradeWouldStrandRecodedData(RuntimeError):
    """A downgrade was refused because a re-encode has changed the values under it.

    Reverse DDL can verify cleanly while leaving values encoded for the newer
    schema. This refusal requires the values to be converted back before the
    schema definition is reverted.
    """

    def __init__(self, schema: str, hazards: tuple[RecodedColumnHazard, ...]) -> None:
        self.schema = schema
        self.hazards = hazards
        listing = "\n".join(f"  - {hazard.explain()}" for hazard in hazards)
        super().__init__(
            f"refusing to downgrade schema {schema!r}: it reverts the definition of "
            f"{len(hazards)} column(s) whose values a deferred migration has already "
            "re-encoded, and reverting DDL does not restore data:\n"
            f"{listing}\n"
            "The reverse DDL would apply cleanly and the catalog fingerprint would "
            "verify, because a re-encode changes values and not schema -- so nothing "
            "downstream of this refusal would notice.\n"
            "Convert the values back first, with a Recode declaring the inverse "
            "mapping, and downgrade once it has finished. There is deliberately no "
            "flag to skip this: the schema check that would normally catch a bad "
            "downgrade is exactly the one a re-encode slips past."
        )


class TransitionalContractUnproven(RuntimeError):
    """A deferred migration whose reads could not be proven safe for the window.

    The mirror of `MigrationBlockedByPass`, one step earlier: that one
    refuses a migration for narrowing a column a pass has not finished, this one
    refuses to *start* the pass while a read of that column would silently mean
    something else while it runs.

    Refusal is the default and the asymmetry is the argument -- a false refusal
    costs an argument with the tool, a false permission costs a data incident
    that surfaces a week later.
    """

    def __init__(self, report: Any) -> None:
        self.report = report
        blocking = report.blocking
        listing = "\n".join(f"  - {item.explain()}" for item in blocking)
        if report.scanned_nothing:
            super().__init__(
                f"refusing to start the deferred migration for {report.column}: "
                "nothing was scanned. No declared query, model check or calculated "
                "view names this column, so the scan proved nothing rather than "
                "proving it safe -- an inline predicate in a handler is invisible "
                "until the source analyser lands. Waive the reads you know about "
                "with waive_transitional(...) once you have checked them."
            )
            return
        super().__init__(
            f"refusing to start the deferred migration for {report.column}: "
            f"{len(blocking)} read(s) cannot be proven safe while the values are "
            f"half converted, and a wrong comparison does not raise -- it returns "
            f"False and quietly drops rows:\n"
            f"{listing}\n"
            "Rewrite each read to accept both encodings, or waive it with a "
            "written reason: waive_transitional(column, site=..., reason=...). "
            "The waiver count appears in `wreath migrations check`."
        )


def scan_transitional_reads(
    declaration: Any,
    *,
    registry: Any = None,
    queries: Any = (),
    views: Any = (),
    strict: bool = True,
) -> Any:
    """Classify every read of the column *declaration* converts.

    Returns the report. With *strict*, raises
    `TransitionalContractUnproven` when anything is unproven -- including
    when nothing was scanned at all. An empty scan is absence of evidence, not
    proof that reads remain valid during the transition.
    """
    report = declaration.scan(registry=registry, queries=queries, views=views)
    if strict and (report.blocking or report.scanned_nothing):
        raise TransitionalContractUnproven(report)
    return report


@dataclass(frozen=True, slots=True)
class MigrationGeneration:
    """Review metadata retained by the native named operation planner."""

    desired_fingerprint: bytes
    actual_fingerprint: bytes
    diff: NativeMigrationDiff
    plan: NativeMigrationPlan
    sql: NativeMigrationSql


@dataclass(frozen=True, slots=True)
class MigrationBaseline:
    """A reviewed zero-operation root adopting one existing live schema.

    `artifact` is a normal checksummed WMA1 artifact, but its source and
    target fingerprints are identical and all three operation tapes are empty.
    Applying it therefore records history and executes no DDL.  `descriptor`
    is the bounded WMD1 catalog inventory used for human review.
    """

    artifact: NativeMigrationArtifact
    fingerprint: bytes
    descriptor: bytes
    object_count: int


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
    counts = _postgres._migration_resolve_managed(
        snapshot,
        target_migration,
        target_checksum,
        directory_generation,
    )
    return FleetResolution(*counts)


def _predicate_digest(predicate: str) -> str:
    """A short, stable tag distinguishing partial indexes on the same columns.

    `md5` because PostgreSQL's catalog query must compute the identical value
    and `md5()` is the one digest built into every server; it is a name, not a
    security boundary.
    """
    return hashlib.md5(predicate.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


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
    return struct.pack("<HHHHI", *(len(value) for value in parts), kind) + b"".join(parts)


def _registry_descriptor(registry: Any, *, fleet: bool = False) -> bytes:
    """Pack immutable ORM intent once for native migration compilation.

    `fleet=True` writes every schema name as **empty**, which is what makes one
    artifact usable across a fleet of identical tenant schemas: the desired
    image no longer names the tenant it was generated from, and the rendered DDL
    comes out unqualified so it binds to whatever `search_path` the applying
    transaction set -- which `wreath.orm.TenantContext` is already how you set.

    A `tenant_search_path` spec declares no schema of its own and is therefore
    only describable this way; it used to be refused outright, with a message
    naming the compiler that did not exist yet.
    """
    records: list[bytes] = []
    # This database's default operator class per (access method, indexed type),
    # if anything has read it yet. See the operator-class comment below; a
    # registry that was never resolved against a database contributes none and
    # every declared operator class is written out verbatim, which is what the
    # offline renderers want.
    default_opclasses = registry.default_opclasses or {}
    for spec in registry.specs:
        if not fleet and (spec.sql_namespace != "qualified" or not spec.schema):
            raise ValueError(
                "a tenant template has no schema of its own; build it with "
                "fleet=True, which writes the schema empty so one artifact "
                "applies to every tenant under its own search_path"
            )
        # Empty for a fleet, so nothing downstream can name one tenant: the
        # image is hashed over these bytes and the DDL is rendered from them.
        schema = "" if fleet else spec.schema
        records.append(_descriptor_record(schema, spec.table, "", 1, "table\x1fr\x1fp"))
        for column in spec.columns:
            # An extension type spells itself; a built-in leaves the slot empty
            # so its signature is unchanged. See `_SINGLE_CATALOG_SQL`, which
            # has to produce the identical string from `format_type`.
            # An *unresolved* extension type is refused rather than described.
            # Its OID is 0, which fails the `>= 16384` test and so blanks the
            # spelling: the column would be described as a built-in of type 0,
            # every run would rediscover it as drift, and nothing would say why.
            if isinstance(column.pg_type, ExtensionType):
                column.pg_type.require_oid(
                    f"the migration descriptor for {spec.model_type.__name__}.{column.python_name}"
                )
            spelling = (
                column.pg_type.sql
                if column.pg_type.oid >= 16384 or column.pg_type.oid in _MODIFIER_BEARING_OIDS
                else ""
            )
            # Field 5 is `attgenerated` and field 6 is `pg_get_expr(adbin)`. A
            # stored generated column sets both: PostgreSQL keeps its expression
            # in the same catalog slot an ordinary default lives in, so the two
            # are mutually exclusive and share the field.
            generated = column.generated_sql
            signature = "\x1f".join(
                (
                    "column",
                    str(column.oid),
                    spelling,
                    "1" if not column.nullable else "0",
                    "",
                    "s" if generated is not None else "",
                    generated if generated is not None else (column.server_default or ""),
                )
            )
            records.append(
                _descriptor_record(
                    schema,
                    spec.table,
                    column.database_name,
                    2,
                    signature,
                )
            )
        primary_columns = ",".join(column.database_name for column in spec.primary_key)
        primary_name = f"p:{primary_columns}:::"
        records.append(_descriptor_record(schema, spec.table, primary_name, 3, primary_name))
        for column in spec.columns:
            if column.unique and not column.primary_key:
                unique_name = f"u:{column.database_name}:::"
                records.append(_descriptor_record(schema, spec.table, unique_name, 3, unique_name))
            if column.indexed:
                # btree keeps the bare "i:<col>" name so its object id (and the
                # derived index name) is unchanged; a non-btree method is folded
                # into the name so the renderer can emit "using <method>".
                method = column.index_method or "btree"
                base = f"i:{column.database_name}"
                index_name = base if method == "btree" else f"{base}:{method}"
                signature = f"index:{base}:{method}"
                if method not in _IMPLICIT_OPCLASS_METHODS:
                    # Three fixed trailing fields for an approximate index: an
                    # (always empty here) predicate slot, the operator class,
                    # and the method options. Matches `_SINGLE_CATALOG_SQL`.
                    options = ",".join(f"{name}={value}" for name, value in column.index_with)
                    # A declared operator class that *is* this database's default
                    # for the method is written as the empty string, because that
                    # is what `_SINGLE_CATALOG_SQL` records for it -- PostgreSQL
                    # does not remember that a default was named, so the two sides
                    # could otherwise never agree and the index would be
                    # rediscovered as drift on every run. Dropping it from the
                    # descriptor also drops it from the emitted CREATE INDEX,
                    # which builds the identical index: naming the default and
                    # omitting it are the same statement to PostgreSQL.
                    ops = column.index_ops or ""
                    if ops and default_opclasses.get((method, column.oid)) == ops:
                        ops = ""
                    signature += f"\x1f\x1f{ops}\x1f{options}"
                records.append(
                    _descriptor_record(
                        schema,
                        spec.table,
                        index_name,
                        4,
                        signature,
                    )
                )
            reference = column.reference
            if reference is not None:
                target = registry.spec_for(reference.model_type)
                # `reference.column` already holds the target's name. Reading it
                # back out of `target.columns` by position was self-consistent --
                # that list is declaration-ordered, not catalog-ordered -- but it
                # is the same positional assumption that made schema validation
                # compare a `confkey` attnum against a declaration index, and the
                # name needs no assumption at all.
                # The target's schema is neutralised on the same terms as the
                # owner's: a fleet registry describes tenant-local models, so
                # its foreign keys point within the tenant and naming one would
                # make every tenant differ from every other.
                target_schema = "" if fleet else target.schema
                foreign_name = (
                    f"f:{column.database_name}:{target_schema}:{target.table}:{reference.column}"
                )
                # The name is the FK's identity (columns + target); the signature
                # adds the referential actions so a changed ON DELETE/UPDATE or
                # deferrability shows up as drift. Codes match pg_constraint.
                foreign_signature = (
                    f"{foreign_name}:{reference.on_delete}:{reference.on_update}:"
                    f"{1 if reference.deferrable else 0}"
                )
                records.append(
                    _descriptor_record(schema, spec.table, foreign_name, 3, foreign_signature)
                )
        for constraint in spec.table_uniques:
            unique_name = f"u:{','.join(constraint.columns)}:::"
            records.append(_descriptor_record(schema, spec.table, unique_name, 3, unique_name))
        for table_index in spec.table_indexes:
            prefix = "ui" if table_index.unique else "i"
            columns = ",".join(table_index.columns)
            predicate = getattr(table_index, "where_sql", None)
            if predicate is None:
                index_name = f"{prefix}:{columns}"
                signature = f"index:{index_name}:btree"
            else:
                # A partial index's identity includes its predicate: two indexes
                # over the same columns with different WHERE clauses are two
                # indexes, and the derived object name is a hash of this string.
                # The predicate itself cannot go in the name -- names are
                # ':'-delimited and SQL predicates contain '::' -- so the name
                # carries a digest and the signature carries the text, after a
                # 0x1f, the way a foreign key carries its referential actions.
                index_name = f"{prefix}:{columns}:btree:{_predicate_digest(predicate)}"
                signature = f"index:{prefix}:{columns}:btree\x1f{predicate}"
            records.append(_descriptor_record(schema, spec.table, index_name, 4, signature))
    return b"WMD1" + struct.pack("<II", 1, len(records)) + b"".join(records)


def _compile_registry_image(registry: Any) -> bytes:
    """Compile immutable ORM intent into one native desired image."""
    descriptor = _registry_descriptor(registry)
    module = _postgres
    return module._migration_compile_desired(descriptor)


async def _decode_catalog_snapshot(
    connection: Any,
    sql: str,
    args: tuple[object, ...] = (),
) -> NativeCatalogSnapshot:
    """Decode catalog rows directly without allocating Python records."""
    module = _postgres
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
    module = _postgres
    builder = module._migration_catalog_builder()
    await connection._fetch_into(sql, args, builder)
    return builder.finish()


async def _read_single_catalog(connection: Any, schema: str) -> bytes:
    """Read tables and columns for one schema through the direct metal destination."""
    return await _decode_catalog_image(connection, _SINGLE_CATALOG_SQL, (schema,))


def _fingerprint_image(image: bytes) -> bytes:
    module = _postgres
    return module._migration_image_fingerprint(image)


def _plan_descriptors(desired: bytes, actual: bytes) -> NativeMigrationPlan:
    module = _postgres
    tape = module._migration_plan_descriptors(desired, actual)
    return NativeMigrationPlan(int.from_bytes(tape[8:12], "little"), tape)


def _render_sql_plan(plan: NativeMigrationPlan) -> NativeMigrationSql:
    module = _postgres
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
    module = _postgres
    tape = module._migration_diff_images(desired, actual)
    operation_count = int.from_bytes(tape[8:12], "little")
    return NativeMigrationDiff(operation_count, tape)


async def _resolve_default_opclasses(registry: Any, connection: Any) -> None:
    """Teach `registry` this database's default operator classes, once.

    Done here rather than only at application startup because a migration is
    routinely run from a CLI that never started the application, and the desired
    descriptor cannot be written correctly without the answer. It is one query,
    cached on the registry, and skipped entirely by a registry that declares no
    index method with an operator class -- which is every registry that has no
    pgvector index.
    """
    from .orm.introspection import resolve_default_opclasses

    await resolve_default_opclasses(registry, connection)


async def detect_single(registry: Any, connection: Any) -> MigrationDetection:
    """Compare one compiled single-schema registry with live PostgreSQL."""
    await _resolve_default_opclasses(registry, connection)
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


async def generate_single_plan(
    registry: Any, connection: Any, *, fleet: bool = False
) -> MigrationGeneration:
    """Build a deterministic named plan for one live physical schema.

    `fleet=True` fingerprints the catalog with every tenant-local schema name
    blanked, so the artifact verifies against *any* structurally identical
    tenant rather than only the one it was generated from. That is the whole
    reason one artifact can cross a fleet: `nspname` is the first column of
    every catalog branch, so two byte-identical tenants otherwise fingerprint
    differently and an artifact could never match the second one.

    A fleet artifact and a single-schema artifact are therefore **not
    interchangeable**, and cannot be confused for one another: their
    fingerprints differ, so applying the wrong kind fails the source-fingerprint
    refusal rather than migrating anything.
    """
    await _resolve_default_opclasses(registry, connection)
    desired_descriptor = _registry_descriptor(registry, fleet=fleet)
    desired = _postgres._migration_compile_desired(desired_descriptor)
    schemas = {spec.schema for spec in registry.specs}
    if len(schemas) != 1:
        raise ValueError("generate_single_plan requires exactly one resolved physical schema")
    actual = await _decode_catalog_snapshot(
        connection,
        _FLEET_CATALOG_SQL if fleet else _SINGLE_CATALOG_SQL,
        (next(iter(schemas)),),
    )
    diff = _diff_packed_images(desired, actual.image)
    plan = _plan_descriptors(desired_descriptor, actual.descriptor)
    if plan.operation_count != diff.operation_count:
        raise RuntimeError("native named plan and image diff disagree")
    derived_operations = _postgres._migration_operations_from_plan(plan.tape)
    if derived_operations != diff.tape:
        raise RuntimeError("native named plan describes different operations than the image diff")
    sql = _render_sql_plan(plan)
    return MigrationGeneration(
        desired_fingerprint=_fingerprint_image(desired),
        actual_fingerprint=_fingerprint_image(actual.image),
        diff=diff,
        plan=plan,
        sql=sql,
    )


_EMPTY_OPERATION_TAPE = b"WMO1" + struct.pack("<II", 1, 0)
_EMPTY_NAMED_PLAN = b"WMP1" + struct.pack("<II", 1, 0)
_EMPTY_SQL_TAPE = b"WMS1" + struct.pack("<II", 1, 0)


def unpack_catalog_descriptor(descriptor: bytes) -> list[dict[str, Any]]:
    """Decode a bounded WMD1 catalog inventory into reviewable values.

    This is an operator view, not migration execution input.  Execution always
    consumes the native image and verified artifact; editing this list changes
    nothing about what can be adopted.
    """
    if len(descriptor) < 12 or descriptor[:4] != b"WMD1":
        raise ValueError("catalog descriptor is not WMD1")
    version, count = struct.unpack_from("<II", descriptor, 4)
    if version != 1:
        raise ValueError(f"unsupported WMD1 catalog descriptor version {version}")
    offset = 12
    objects: list[dict[str, Any]] = []
    kinds = {1: "table", 2: "column", 3: "constraint", 4: "index"}
    for index in range(count):
        if len(descriptor) - offset < 12:
            raise ValueError(f"catalog descriptor is truncated at object {index}")
        schema_len, table_len, name_len, signature_len, kind = struct.unpack_from(
            "<HHHHI", descriptor, offset
        )
        offset += 12
        payload_len = schema_len + table_len + name_len + signature_len
        if table_len == 0 or payload_len > len(descriptor) - offset:
            raise ValueError(f"catalog descriptor object {index} is invalid")
        ends = (
            offset + schema_len,
            offset + schema_len + table_len,
            offset + schema_len + table_len + name_len,
            offset + payload_len,
        )
        schema = descriptor[offset : ends[0]].decode("utf-8")
        table = descriptor[ends[0] : ends[1]].decode("utf-8")
        name = descriptor[ends[1] : ends[2]].decode("utf-8")
        signature = descriptor[ends[2] : ends[3]].decode("utf-8")
        offset = ends[3]
        objects.append(
            {
                "kind": kinds.get(kind, f"unknown-{kind}"),
                "schema": schema,
                "table": table,
                "name": name,
                "signature": signature,
            }
        )
    if offset != len(descriptor):
        raise ValueError("catalog descriptor has trailing bytes")
    return objects


async def generate_single_baseline(
    registry: Any,
    connection: Any,
    *,
    migration_id: bytes,
) -> MigrationBaseline:
    """Adopt current PostgreSQL state as a root without replaying its history.

    The live catalog and declared ORM image must match exactly.  Drift is never
    folded into a baseline: doing that would bless a model that does not
    describe the database and make the first later migration plan relative to
    the wrong state.  The returned artifact contains no operation and therefore
    cannot execute DDL when applied.
    """
    if len(migration_id) != 16:
        raise ValueError("baseline migration_id must be exactly 16 bytes")
    await _resolve_default_opclasses(registry, connection)
    schemas = {spec.schema for spec in registry.specs}
    if len(schemas) != 1:
        raise ValueError("generate_single_baseline requires exactly one resolved physical schema")
    schema = next(iter(schemas))
    desired = _compile_registry_image(registry)
    actual = await _decode_catalog_snapshot(connection, _SINGLE_CATALOG_SQL, (schema,))
    diff = _diff_packed_images(desired, actual.image)
    desired_fingerprint = _fingerprint_image(desired)
    actual_fingerprint = _fingerprint_image(actual.image)
    if diff.operation_count or desired_fingerprint != actual_fingerprint:
        raise ValueError(
            "cannot baseline schema with drift: "
            f"{diff.operation_count} operation(s), desired "
            f"{desired_fingerprint.hex()}, actual {actual_fingerprint.hex()}"
        )
    artifact = _build_native_artifact(
        migration_id=migration_id,
        parent_checksum=bytes(32),
        source_fingerprint=actual_fingerprint,
        target_fingerprint=actual_fingerprint,
        operation_tape=_EMPTY_OPERATION_TAPE,
        named_plan=_EMPTY_NAMED_PLAN,
        sql_tape=_EMPTY_SQL_TAPE,
    )
    objects = unpack_catalog_descriptor(actual.descriptor)
    return MigrationBaseline(
        artifact=artifact,
        fingerprint=actual_fingerprint,
        descriptor=actual.descriptor,
        object_count=len(objects),
    )


async def connect_migration(dsn: str) -> Any:
    """Open a dedicated Wreath-metal migration connection."""
    return await _postgres.connect(dsn)


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
        raise ValueError("apply_single_artifact requires exactly one resolved physical schema")
    await _bootstrap_migration_history(connection)
    outcome = await _apply_artifact_to_schema(
        connection,
        artifact,
        schema=next(iter(schemas)),
        allow_destructive=allow_destructive,
        skip_if_applied=False,
    )
    if outcome is None:  # pragma: no cover - skip_if_applied is False here
        raise RuntimeError("artifact reported already applied without being asked")
    return outcome


async def adopt_single_baseline(
    registry: Any,
    connection: Any,
    artifact_data: bytes,
) -> MigrationApplyResult:
    """Record one zero-operation root after re-verifying code and catalog.

    The only DDL this may issue is creation of Wreath's own migration-history
    schema/table.  It never builds or executes an application DDL block.  A
    prior history row, non-root artifact, operation, or fingerprint mismatch is
    refused inside the same advisory-locked transaction that records adoption.
    """
    artifact = _load_native_artifact(artifact_data)
    if artifact.parent_checksum != bytes(32):
        raise ValueError("a baseline must be a root artifact")
    if artifact.source_fingerprint != artifact.target_fingerprint:
        raise ValueError("a baseline source and target fingerprint must be identical")
    for label, tape, magic in (
        ("operation", artifact.operation_tape, b"WMO1"),
        ("named plan", artifact.named_plan, b"WMP1"),
        ("SQL", artifact.sql_tape, b"WMS1"),
    ):
        if len(tape) != 12 or tape[:4] != magic or tape[8:12] != bytes(4):
            raise ValueError(f"a baseline {label} tape must be empty")
    schemas = {spec.schema for spec in registry.specs}
    if len(schemas) != 1:
        raise ValueError("adopt_single_baseline requires exactly one resolved physical schema")
    schema = next(iter(schemas))
    await _resolve_default_opclasses(registry, connection)
    desired = _compile_registry_image(registry)
    desired_fingerprint = _fingerprint_image(desired)
    if desired_fingerprint != artifact.target_fingerprint:
        raise RuntimeError(
            "cannot adopt baseline: current ORM fingerprint "
            f"{desired_fingerprint.hex()} does not match reviewed baseline "
            f"{artifact.target_fingerprint.hex()}"
        )
    await _bootstrap_migration_history(connection)
    history = _qualified_history_table()
    await connection.execute("BEGIN")
    committed = False
    try:
        await connection.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
            f"wreath:migrations:{schema}",
        )
        previous = await connection.fetchrow(
            f"""SELECT checksum
                FROM {history}
                WHERE target_schema = $1
                ORDER BY sequence DESC
                LIMIT 1""",
            schema,
        )
        if previous is not None:
            raise RuntimeError(
                f"cannot adopt baseline: schema {schema!r} already has Wreath history"
            )
        actual = await _decode_catalog_snapshot(connection, _SINGLE_CATALOG_SQL, (schema,))
        actual_fingerprint = _fingerprint_image(actual.image)
        if actual_fingerprint != artifact.target_fingerprint:
            raise RuntimeError(
                "cannot adopt baseline: live catalog fingerprint "
                f"{actual_fingerprint.hex()} does not match reviewed baseline "
                f"{artifact.target_fingerprint.hex()}"
            )
        await connection.execute(
            f"""INSERT INTO {history} (
                target_schema, migration_id, checksum, parent_checksum,
                source_fingerprint, target_fingerprint, destructive_approved
            ) VALUES ($1, $2, $3, $4, $5, $6, false)""",
            schema,
            artifact.migration_id,
            artifact.checksum,
            artifact.parent_checksum,
            artifact.source_fingerprint,
            artifact.target_fingerprint,
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
        destructive_approved=False,
    )


async def _apply_artifact_to_schema(
    connection: Any,
    artifact: Any,
    *,
    schema: str,
    allow_destructive: bool,
    skip_if_applied: bool,
    catalog_sql: str = _SINGLE_CATALOG_SQL,
    bind_search_path: bool = False,
) -> MigrationApplyResult | None:
    """One schema's guarded apply. Returns None when already at the target.

    Extracted so the fleet runner reuses this rather than restating it. The five
    refusals below are the whole safety argument of a migration, and a second
    copy of them is a second thing to keep correct -- the fleet path would be
    exactly where a drift went unnoticed, because it is the path nobody runs by
    hand.

    `skip_if_applied` is what makes a fleet run resumable. A run that stopped
    on tenant 400 of 1000 must be re-runnable, and the parent-checksum refusal
    would otherwise reject every tenant that already succeeded -- turning a
    resumed run into a wall of errors that all mean "this one is fine".
    """
    module = _postgres
    ddl_block = module._migration_build_ddl_block(artifact.sql_tape, allow_destructive)
    history = _qualified_history_table()
    zero_checksum = bytes(32)
    await connection.execute("BEGIN")
    committed = False
    try:
        await connection.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
            f"wreath:migrations:{schema}",
        )
        if bind_search_path:
            # A fleet artifact renders its DDL **unqualified** so one artifact
            # describes every tenant; unqualified DDL lands wherever
            # `search_path` points, which is `public` unless something says
            # otherwise. Binding it here is what aims the statements at this
            # tenant -- the same mechanism `wreath.orm.TenantContext` uses for
            # queries, and `SET LOCAL` so it reverts with the transaction
            # rather than leaking onto a pooled connection's next borrower.
            await connection.execute(f'SET LOCAL search_path TO "{schema}"')
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
            if skip_if_applied and previous_checksum == artifact.checksum:
                # Already at the target. Reported as a skip rather than an
                # error, and *inside* the lock so two concurrent runners cannot
                # both decide to apply it.
                await connection.execute("COMMIT")
                committed = True
                return None
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
        actual = await _decode_catalog_snapshot(connection, catalog_sql, (schema,))
        actual_fingerprint = _fingerprint_image(actual.image)
        if actual_fingerprint != artifact.source_fingerprint:
            raise RuntimeError(
                "cannot apply migration: live catalog fingerprint "
                f"{actual_fingerprint.hex()} does not match artifact source "
                f"{artifact.source_fingerprint.hex()} for schema {schema!r}"
            )
        # The fifth refusal, and the only one that reads state outside the
        # catalog: a column may not be narrowed while a pass is still writing
        # it. Inside the same transaction as the other four, so a rejected
        # narrowing leaves nothing behind and cannot race a gate publishing
        # between the check and the DDL.
        pass_hazards = await _pending_pass_hazards(connection, artifact.named_plan)
        if pass_hazards:
            raise MigrationBlockedByPass(schema, pass_hazards)
        await connection.execute(ddl_block)
        resulting = await _decode_catalog_snapshot(connection, catalog_sql, (schema,))
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


@dataclass(frozen=True, slots=True)
class TenantOutcome:
    """What happened to one tenant schema in a fleet run.

    `state` is `"applied"`, `"skipped"` (already at the target) or `"failed"`.
    `error` carries the refusal for a failure and is None otherwise -- the
    message, not the exception, because a result is something a caller logs,
    serialises and compares, and an exception object is none of those.
    """

    schema: str
    state: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FleetApplyResult:
    """One artifact's progress across a tenant fleet.

    **A fleet run has no atomic answer, and this shape refuses to pretend it
    does.** One transaction across a thousand schemas would hold every lock for
    the length of the slowest one and exceed what a server will keep open, so
    each tenant commits on its own -- which means a run that stops leaves the
    fleet genuinely split. The honest report is therefore per tenant, and
    `applied`/`skipped`/`failed` are separate rather than a pass rate.
    """

    outcomes: tuple[TenantOutcome, ...]

    @property
    def applied(self) -> tuple[str, ...]:
        """Schemas this run migrated."""
        return tuple(o.schema for o in self.outcomes if o.state == "applied")

    @property
    def skipped(self) -> tuple[str, ...]:
        """Schemas already at the target before this run."""
        return tuple(o.schema for o in self.outcomes if o.state == "skipped")

    @property
    def failed(self) -> tuple[TenantOutcome, ...]:
        """Schemas that refused, each with the reason."""
        return tuple(o for o in self.outcomes if o.state == "failed")

    @property
    def complete(self) -> bool:
        """Whether every tenant asked for is now at the target."""
        return not self.failed

    def summary(self) -> str:
        """One line for an operator: what moved, what did not, what broke."""
        return (
            f"{len(self.applied)} applied, {len(self.skipped)} already current, "
            f"{len(self.failed)} failed"
        )


async def apply_fleet(
    database: Any,
    artifact_data: bytes,
    schemas: Any,
    *,
    allow_destructive: bool = False,
    stop_on_error: bool = True,
    workload: str = "write",
) -> FleetApplyResult:
    """Apply one artifact across many tenant schemas, under a fleet lock.

    ```python
    result = await apply_fleet(app.postgres("main"), artifact, tenant_schemas)
    if not result.complete:
        raise SystemExit(result.summary())
    ```

    Every tenant goes through the *same* guarded apply as a single-schema run --
    the same five refusals, the same fingerprint verification, the same history
    row -- because a fleet path with its own copy of that logic is exactly where
    a drift would go unnoticed.

    **One transaction per tenant, not one for the fleet.** A single transaction
    spanning a thousand schemas holds every lock for the length of the slowest,
    and no server will keep that open. So a run that stops leaves the fleet
    split, and `FleetApplyResult` reports which side each tenant is on rather
    than reducing it to a boolean.

    **Resumable by construction.** A tenant already at the artifact's checksum
    is skipped, not refused, so re-running after a stop is the ordinary way to
    finish -- rather than a wall of parent-checksum errors that all mean "this
    one already worked".

    **Serial, deliberately.** Concurrent DDL across schemas contends on shared
    catalog rows, and the failure it produces is a deadlock partway through a
    fleet -- the worst possible time to be debugging lock ordering. Ordering is
    the caller's list, so a run is reproducible and a resumed run visits
    tenants in the same order.

    `stop_on_error=True` (the default) halts at the first refusal, which keeps
    the blast radius to one tenant when the artifact is simply wrong for this
    fleet. Pass False to attempt every tenant and collect the failures, which is
    what a caller wants when the failures are expected to be independent.

    The **fleet lock** is a session advisory lock held for the whole run: two
    deploys racing would otherwise interleave per-tenant transactions and leave
    a fleet neither artifact describes. It is `pg_try_advisory_lock`, so a
    second runner is refused immediately rather than queueing behind a run that
    may take an hour.
    """
    targets = [str(schema) for schema in schemas]
    if not targets:
        raise ValueError("apply_fleet needs at least one tenant schema")
    if len(set(targets)) != len(targets):
        seen: set[str] = set()
        repeated = sorted({s for s in targets if s in seen or seen.add(s)})  # type: ignore[func-returns-value]
        raise ValueError(
            f"apply_fleet was given the same schema twice: {', '.join(repeated)}; "
            "a fleet is a set of tenants and a repeat is a directory bug"
        )
    for schema in targets:
        # Interpolated into the catalog query and the lock key, so the same
        # rule the rest of the tree applies to an identifier applies here.
        validate_unquoted_identifier(schema, "tenant schema")

    artifact = _load_native_artifact(artifact_data)
    connection = await database.acquire(workload)
    outcomes: list[TenantOutcome] = []
    try:
        await _bootstrap_migration_history(connection)
        held = await connection.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1::text, 0))",
            f"wreath:migrations:fleet:{artifact.migration_id}",
        )
        if not held:
            raise FleetRunInProgress(
                f"another runner holds the fleet lock for migration "
                f"{artifact.migration_id}; a second concurrent run would interleave "
                "per-tenant transactions and leave the fleet in a state neither "
                "artifact describes"
            )
        try:
            for schema in targets:
                try:
                    result = await _apply_artifact_to_schema(
                        connection,
                        artifact,
                        schema=schema,
                        allow_destructive=allow_destructive,
                        skip_if_applied=True,
                        catalog_sql=_FLEET_CATALOG_SQL,
                        bind_search_path=True,
                    )
                except Exception as error:  # noqa: BLE001 - recorded per tenant
                    # Broad and *reported*: every refusal this can raise names a
                    # tenant, and one tenant's refusal must not hide the state of
                    # the others. Swallowing is what would be wrong; this
                    # surfaces in `failed` with its message.
                    outcomes.append(TenantOutcome(schema, "failed", str(error)))
                    if stop_on_error:
                        break
                    continue
                outcomes.append(TenantOutcome(schema, "skipped" if result is None else "applied"))
        finally:
            await connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended($1::text, 0))",
                f"wreath:migrations:fleet:{artifact.migration_id}",
            )
    finally:
        await database.release(workload, connection)
    return FleetApplyResult(tuple(outcomes))


_PLAN_ACTION_NAMES = {1: "add", 2: "drop", 3: "alter"}
_PLAN_KIND_NAMES = {1: "table", 2: "column", 3: "constraint", 4: "index"}


def unpack_named_plan(tape: bytes) -> list[dict[str, Any]]:
    """Decode a native named plan into one dict per operation.

    Lives here rather than in the CLI because two readers need it now: the CLI
    renders it for review, and `_pending_pass_hazards` asks it which
    columns an artifact narrows.
    """
    if len(tape) < 12 or tape[:4] != b"WMP1":
        raise ValueError("native named migration plan is invalid")
    count = int.from_bytes(tape[8:12], "little")
    offset = 12
    operations: list[dict[str, Any]] = []
    for _ in range(count):
        if len(tape) - offset < 20:
            raise ValueError("native named migration plan is truncated")
        action, kind, *lengths = struct.unpack_from("<IIHHHHHH", tape, offset)
        offset += 20
        if lengths[5] != 0:
            raise ValueError("native named migration plan has unsupported flags")
        values: list[str] = []
        for length in lengths[:5]:
            if length > len(tape) - offset:
                raise ValueError("native named migration plan is truncated")
            values.append(tape[offset : offset + length].decode("utf-8"))
            offset += length
        operations.append(
            {
                "action": _PLAN_ACTION_NAMES.get(action, f"unknown-{action}"),
                "kind": _PLAN_KIND_NAMES.get(kind, f"unknown-{kind}"),
                "schema": values[0],
                "table": values[1],
                "name": values[2],
                "before": values[3],
                "after": values[4],
            }
        )
    if offset != len(tape):
        raise ValueError("native named migration plan has trailing bytes")
    return operations


def _narrowed_columns(named_plan: bytes) -> tuple[tuple[str, str, str, str], ...]:
    """`(schema, table, column, action)` for each column this plan narrows.

    "Narrows" is deliberately both a drop and a retype. Dropping the old column
    is the obvious half; changing its type is the same hazard wearing a
    different verb, because a conversion still running behind the cursor is
    reading rows in the shape the alter is about to remove.

    A dropped *table* is not listed. It takes its columns with it, and a pass
    walking a table someone is dropping in the same deploy is a bigger problem
    than this refusal is shaped to describe.
    """
    out: list[tuple[str, str, str, str]] = []
    for operation in unpack_named_plan(named_plan):
        if operation["kind"] != "column":
            continue
        if operation["action"] not in ("drop", "alter"):
            continue
        out.append(
            (
                operation["schema"],
                operation["table"],
                operation["name"],
                operation["action"],
            )
        )
    return tuple(out)


async def _pass_hazard_facts(
    connection: Any,
    candidates: tuple[tuple[str, str, str, str], ...],
    ledger_schema: str,
) -> tuple[Any, dict[str, tuple[str, str, str, str]]] | None:
    """Resolve column candidates against an existing pass ledger."""
    from ._passes import ledger as _pass_ledger

    if not candidates:
        return None
    table = _pass_ledger.table_name(ledger_schema)
    exists = await connection.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
    if not exists:
        return None
    facts = {
        _column_fact(schema, tbl, column): (schema, tbl, column, action)
        for schema, tbl, column, action in candidates
    }
    return _pass_ledger, facts


def _column_hazards[T](
    entries: Any,
    facts: dict[str, tuple[str, str, str, str]],
    hazard_type: Callable[..., T],
    extras: Callable[[Any], dict[str, object]],
) -> tuple[T, ...]:
    """Project pass-ledger entries onto their typed migration hazards."""
    hazards: list[T] = []
    for entry in entries:
        schema, table, column, action = facts[entry.fact]
        hazards.append(
            hazard_type(
                schema=schema,
                table=table,
                column=column,
                action=action,
                pass_name=entry.name,
                tenant=entry.tenant,
                phase=entry.phase,
                **extras(entry),
            )
        )
    return tuple(hazards)


async def _pending_pass_hazards(
    connection: Any, named_plan: bytes, *, ledger_schema: str = "wreath"
) -> tuple[PendingPassHazard, ...]:
    """Columns this plan narrows that a chunked pass has not finished converting.

    Shaped after `_downgrade_hazards`: read live state, return what is
    wrong, and let the caller decide to refuse. The state read here is the pass
    ledger rather than the ORM registry, because the question is not "does code
    still reference this" but "is something still writing it".

    A database that has never run a pass has no ledger table, and that is not an
    error -- it is the answer "nothing is converting anything". Checked with
    `to_regclass` rather than by catching a failure, so a real error from the
    read is still a real error.
    """
    resolved = await _pass_hazard_facts(connection, _narrowed_columns(named_plan), ledger_schema)
    if resolved is None:
        return ()
    _pass_ledger, facts = resolved
    pending = await _pass_ledger.pending_facts(connection, schema=ledger_schema, facts=tuple(facts))
    return _column_hazards(
        pending,
        facts,
        PendingPassHazard,
        lambda entry: {"holes_open": entry.holes_open},
    )


def _column_fact(schema: str, table: str, column: str) -> str:
    """The spelling `wreath.passes.column_fact` produces, without the import."""
    return f"column:{schema}.{table}.{column}"


def _touched_columns(named_plan: bytes) -> tuple[tuple[str, str, str, str], ...]:
    """`(schema, table, column, action)` for every column operation in a plan.

    Wider than `_narrowed_columns` on purpose. That one asks "would this
    lose rows a pass has not converted yet", which only drops and retypes can
    do. This one asks "does this change the definition a re-encode's values were
    written under", and *adding* a constraint or a default is as capable of
    contradicting them as removing one.
    """
    return tuple(
        (
            operation["schema"],
            operation["table"],
            operation["name"],
            operation["action"],
        )
        for operation in unpack_named_plan(named_plan)
        if operation["kind"] == "column"
    )


async def _recoded_column_hazards(
    connection: Any, reverse_plan: bytes, *, ledger_schema: str = "wreath"
) -> tuple[RecodedColumnHazard, ...]:
    """Columns this reverse plan touches whose values a re-encode has changed.

    Read with **no** filter on whether the pass finished, which is the one thing
    that distinguishes this from `_pending_pass_hazards`. That function
    refuses while a pass is unfinished and relents once it publishes, because a
    published pass has converted every row and the narrowing is then safe. Here
    publication is not a release: a finished re-encode is the case where *no*
    original value survives, so it is the more dangerous state rather than the
    settled one.
    """
    resolved = await _pass_hazard_facts(connection, _touched_columns(reverse_plan), ledger_schema)
    if resolved is None:
        return ()
    _pass_ledger, facts = resolved
    rewritten = await _pass_ledger.rewritten_columns(
        connection, schema=ledger_schema, facts=tuple(facts)
    )
    return _column_hazards(
        rewritten,
        facts,
        RecodedColumnHazard,
        lambda entry: {
            "finished": entry.finished,
            "ledger_row_present": entry.ledger_row_present,
        },
    )


def _downgrade_hazards(registry: Any, reverse_plan: bytes) -> tuple[DowngradeHazard, ...]:
    """Scan the reverse plan against live ORM intent for stranded references."""
    module = _postgres
    desired_image = _compile_registry_image(registry)
    raw = module._migration_downgrade_hazards(reverse_plan, desired_image)
    return tuple(
        DowngradeHazard(
            schema=schema,
            table=table,
            name=name,
            kind=_HAZARD_KIND_NAMES.get(kind, str(kind)),
            reason=reason,
        )
        for schema, table, name, kind, reason in raw
    )


async def revert_single_artifact(
    registry: Any,
    connection: Any,
    artifact_data: bytes,
    *,
    allow_destructive: bool = False,
    force: bool = False,
) -> MigrationRevertResult:
    """Undo one authoritative artifact: the exact inverse of `apply_single_artifact`.

    The named plan is inverted in metal (every add becomes a drop, every
    signature swaps), so the downgrade tape is derived from the same authority
    the upgrade was — never guessed. The artifact must be the current history
    tip; the transaction verifies the live catalog matches the artifact target,
    runs the reverse DDL block, requires the catalog to return to the artifact
    source, deletes the tip history row, and commits — or rolls everything back.

    Unless `force` is set, the downgrade is refused when the running ORM still
    maps a column or table the reverse would drop (or a type it would change):
    downgrading production under code that still references those objects strands
    the deployed code. `force` exists for the legitimate case of rewinding a
    local stack to re-migrate.
    """
    artifact = _load_native_artifact(artifact_data)
    schemas = {spec.schema for spec in registry.specs}
    if len(schemas) != 1:
        raise ValueError("revert_single_artifact requires exactly one resolved physical schema")
    schema = next(iter(schemas))
    module = _postgres
    reverse_plan = module._migration_reverse_plan(artifact.named_plan)
    reverse_sql = module._migration_render_sql(reverse_plan)
    if not force:
        hazards = _downgrade_hazards(registry, reverse_plan)
        if hazards:
            raise DowngradeWouldStrandCode(schema, hazards)
    ddl_block = module._migration_build_ddl_block(reverse_sql, allow_destructive)
    history = _qualified_history_table()
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
            raise RuntimeError(
                f"cannot downgrade migration: database has no Wreath history for schema {schema!r}"
            )
        if bytes(previous[0]) != artifact.checksum:
            raise RuntimeError(
                "cannot downgrade migration: artifact checksum "
                f"{artifact.checksum.hex()} is not the current history tip "
                f"{bytes(previous[0]).hex()} for schema {schema!r}; only the most "
                "recently applied migration can be reverted"
            )
        actual = await _decode_catalog_snapshot(connection, _SINGLE_CATALOG_SQL, (schema,))
        actual_fingerprint = _fingerprint_image(actual.image)
        if actual_fingerprint != artifact.target_fingerprint:
            raise RuntimeError(
                "cannot downgrade migration: live catalog fingerprint "
                f"{actual_fingerprint.hex()} does not match artifact target "
                f"{artifact.target_fingerprint.hex()} for schema {schema!r}"
            )
        # Inside the advisory lock, and deliberately not gated on `force`. The
        # catalog checks either side of this one cannot see the hazard at all --
        # a re-encode leaves the schema untouched, so they verify perfectly
        # while the values contradict the schema being restored.
        recoded = await _recoded_column_hazards(connection, reverse_plan)
        if recoded:
            raise DowngradeWouldStrandRecodedData(schema, recoded)
        await connection.execute(ddl_block)
        resulting = await _decode_catalog_snapshot(connection, _SINGLE_CATALOG_SQL, (schema,))
        resulting_fingerprint = _fingerprint_image(resulting.image)
        if resulting_fingerprint != artifact.source_fingerprint:
            raise RuntimeError(
                "downgrade DDL ran but source verification failed: live catalog "
                f"fingerprint {resulting_fingerprint.hex()} does not match artifact "
                f"source {artifact.source_fingerprint.hex()} for schema {schema!r}; "
                "the transaction will be rolled back"
            )
        await connection.execute(
            f"""DELETE FROM {history}
                WHERE target_schema = $1 AND migration_id = $2 AND checksum = $3""",
            schema,
            artifact.migration_id,
            artifact.checksum,
        )
        await connection.execute("COMMIT")
        committed = True
    finally:
        if not committed:
            await connection.execute("ROLLBACK")
    return MigrationRevertResult(
        migration_id=artifact.migration_id,
        checksum=artifact.checksum,
        source_fingerprint=artifact.source_fingerprint,
        target_fingerprint=artifact.target_fingerprint,
        destructive_approved=allow_destructive,
        forced=force,
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
    module = _postgres
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
    module = _postgres
    checksum, target, count = module._migration_verify_chain(
        bytes(payload), expected_parent, expected_source
    )
    return NativeMigrationChain(checksum, target, count)


def _load_native_artifact(data: bytes) -> NativeMigrationArtifact:
    """Verify checksum, format, lengths, and operation tape before publication."""
    module = _postgres
    migration_id, parent, source, target, tape, plan, sql = module._migration_verify_artifact(data)
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
    "HISTORY_AMBIGUOUS",
    "HISTORY_BLOCKED",
    "HISTORY_UNKNOWN",
    "HISTORY_VERIFIED",
    "DeferredDeclarationError",
    "DowngradeHazard",
    "DowngradeWouldStrandCode",
    "DowngradeWouldStrandRecodedData",
    "MigrationBlockedByPass",
    "PendingPassHazard",
    "RecodedColumnHazard",
    "Recode",
    "Retype",
    "ScanReport",
    "TransitionalHazard",
    "TransitionalContractUnproven",
    "FleetResolution",
    "MigrationConfig",
    "MigrationBaseline",
    "MigrationDetection",
    "MigrationGeneration",
    "MigrationApplyResult",
    "MigrationRevertResult",
    "NativeCatalogSnapshot",
    "NativeMigrationArtifact",
    "NativeMigrationChain",
    "NativeMigrationDiff",
    "NativeMigrationPlan",
    "NativeMigrationSql",
    "ResolutionPolicy",
    "TenantState",
    "apply_single_artifact",
    "adopt_single_baseline",
    "connect_migration",
    "detect_single",
    "generate_single_plan",
    "generate_single_baseline",
    "pack_tenant_directory",
    "resolve_fleet",
    "revert_single_artifact",
    "scan_transitional_reads",
    "transitional_read",
    "unpack_catalog_descriptor",
    "waive_transitional",
]
