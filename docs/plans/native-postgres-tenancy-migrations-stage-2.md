# Native PostgreSQL tenancy and ORM plan — Stage 2: migration detector, generator, and runner

## Status

Deprecated. Replaced by [`wreath-metal-postgres-tenancy-migrations.md`](wreath-metal-postgres-tenancy-migrations.md). Retained only as historical design context; do not implement from this plan.

The implementation must intentionally replace the current documented policy that Wreath validates schema but never performs DDL. Migration artifacts remain deterministic, reviewable, and PostgreSQL-specific. The runner never runs on request pool credentials.

## Goal

Provide a C-accelerated migration subsystem that:

- detects differences between compiled ORM intent and live `pg_catalog` state;
- generates deterministic reviewable migration operations and SQL;
- runs central/single-schema migrations transactionally where PostgreSQL permits;
- generates a tenant-template migration once and safely applies it across isolated tenant schemas with bounded concurrency, locking, checkpointing, and resumability;
- uses native PostgreSQL decoding, compact schema images, diffing, ordering, identifier rendering, and operation tapes while Python owns orchestration, safety policy, operator review, and CLI presentation.

## Existing schema metadata and gaps

### Already represented

Current `ModelSpec`/`ColumnSpec`/introspection cover much of a desired schema:

- logical/fixed schema and table names;
- ordered columns and PostgreSQL OIDs;
- nullability;
- primary keys;
- per-column uniqueness;
- Python and server defaults;
- foreign-key targets;
- model dependency order;
- deterministic model/registry fingerprints;
- live columns, defaults, PK, unique constraints, and FKs from `pg_catalog`.

### Required additions

Migration-safe desired/actual IR must also represent, where supported:

- stable object IDs/names for tables, columns, constraints, and indexes;
- multi-column unique constraints and indexes;
- ordinary, unique, partial, and expression indexes;
- check constraints that have canonical SQL representations;
- FK update/delete actions, match mode, and deferrability;
- identity/generated columns and sequences;
- explicit constraint/index names;
- PostgreSQL extensions/enums/domains only if Wreath chooses to own them;
- comments/metadata used for rename hints;
- table/column/index ownership by central, tenant-template, or fixed schema role.

Current ORM `Check` objects are application validation. Arbitrary Python `Predicate`, regex implementation, or generated Python source cannot automatically become PostgreSQL CHECK DDL. Add a separate SQL-representable database constraint declaration or an explicit `database=True` form with a canonical SQL expression compiler. Never infer SQL from arbitrary Python.

## Canonical schema image

### Desired image

Add frozen records, likely under `wreath.orm.schema` or a private shared module:

```python
@dataclass(frozen=True, slots=True)
class DesiredSchema:
    version: int
    mode: SchemaMode
    central: SchemaTemplate
    tenant: SchemaTemplate | None
    fixed: tuple[SchemaTemplate, ...]
    fingerprint: bytes

@dataclass(frozen=True, slots=True)
class SchemaTemplate:
    role: Literal["central", "tenant", "fixed"]
    name: str | None
    tables: tuple[TableSpec, ...]
    fingerprint: bytes

@dataclass(frozen=True, slots=True)
class TableSpec:
    stable_id: str
    name: str
    columns: tuple[ColumnDDLSpec, ...]
    constraints: tuple[ConstraintSpec, ...]
    indexes: tuple[IndexSpec, ...]
```

`stable_id` is generated from model/field declaration identity or explicitly supplied for renames. It is not a database OID and must remain deterministic across processes.

### Actual image

Batch-decode `pg_catalog` into compact native records:

```c
typedef struct {
    uint32_t schema_id;
    uint32_t table_id;
    uint32_t column_id;
    uint32_t type_oid;
    uint32_t flags;
    int32_t position;
    uint32_t default_offset;
} WreathActualColumn;
```

Analogous arrays represent constraints, indexes, and dependency edges. Strings live in one bounded arena. Sort by canonical `(schema, object-kind, table, name)` order once.

For many tenants, one catalog query should fetch all selected tenant schemas rather than one model/query round trip at a time. Reuse the native PostgreSQL decoder/direct destination hooks to populate schema-image arrays without Python records.

### Fingerprints

Maintain separate hashes:

- desired central template;
- desired tenant template;
- actual central schema;
- actual tenant schema per tenant;
- migration operation stream;
- immutable migration artifact checksum.

Fingerprint equality is a fast skip, not proof when image versions differ. Detailed diff is required on mismatch.

For large tenant populations, group schemas by actual fingerprint and diff each distinct group once. The runner still records/checkpoints each tenant independently.

## Native detector

### Pure oracle first

Implement a pure sorted-image diff that produces stable `SchemaIssue`/`MigrationOperation` records. Extend current `SchemaDiff` rather than create unrelated diagnostics.

The native detector receives normalized arrays only; it never introspects Python model classes or issues SQL.

### Diff result

```python
@dataclass(frozen=True, slots=True)
class MigrationOperation:
    kind: OperationKind
    target: ObjectRef
    safety: Literal["safe", "locking", "destructive", "manual"]
    transactional: bool
    dependencies: tuple[OperationRef, ...]
    before: SchemaObject | None
    after: SchemaObject | None
```

Operation kinds may include:

```text
CREATE_SCHEMA
CREATE_TABLE
RENAME_TABLE
ADD_COLUMN
RENAME_COLUMN
ALTER_COLUMN_TYPE
ALTER_NULLABILITY
ALTER_DEFAULT
DROP_COLUMN
ADD/DROP/RENAME_CONSTRAINT
CREATE/DROP/RENAME_INDEX
DROP_TABLE
CUSTOM_SQL (explicit only)
```

### Rename policy

Never infer rename from similarity alone. A drop/add pair may be data-destructive. Renames require an explicit stable ID/history hint, for example:

```python
column(Text, migration_id="user.email", renamed_from="email_address")
```

Without a hint, generate add/drop and classify the drop as destructive/manual.

### Safety classification

Classify using PostgreSQL semantics and operator policy, not generic labels:

- adding nullable/no-rewrite column may be safe;
- adding NOT NULL without a safe staged default is locking/manual;
- type changes require an explicit `USING` expression unless binary-coercible and certified;
- drops are destructive;
- unique/foreign-key validation may scan/lock and can use staged NOT VALID/VALIDATE where supported;
- concurrent indexes are non-transactional and resumable;
- arbitrary default/check SQL is manual unless compiled from supported canonical forms.

The detector never silently changes an operation to make it executable.

## Generator

### Artifact format

Migration artifacts should be immutable, reviewable data rather than arbitrary Python execution. A directory may contain:

```text
migrations/
  0001_initial/
    migration.json
    up.sql
    metadata.json
  0002_add_order_status/
    migration.json
    up.sql
    metadata.json
```

`migration.json` is the canonical operation stream with schema/image versions, parent checksum, source/target fingerprints, central/tenant target kind, safety classifications, and operation dependencies. `up.sql` is deterministic review output and must match regenerated SQL. `metadata.json` carries generation environment and explicit operator approvals/hints.

A Python API may expose typed records, but loading an artifact never executes arbitrary module code.

### SQL tape

Compile operations into a native SQL tape:

```text
TEXT
SCHEMA_IDENTIFIER role
TABLE_IDENTIFIER id
COLUMN_IDENTIFIER id
TYPE_NAME oid
EXPRESSION approved_offset
STATEMENT_END transactional_flag
```

Physical tenant schema is supplied at execution through a validated identifier slot. It is never ordinary `.format()` interpolation.

Central/fixed schema names are compiled constants. All identifiers pass the same strict quoting/length/NUL rules as ORM SQL.

### Dependency order

Build a deterministic DAG and topological order:

1. schemas/extensions/types owned by the migration;
2. tables/sequences;
3. columns/defaults;
4. PK/unique constraints needed as FK targets;
5. foreign keys;
6. ordinary indexes/check validation;
7. drops in reverse dependency order.

Cycles produce an actionable generation error or an explicit staged operation; never rely on incidental declaration order.

### Central and tenant artifacts

Generate central and tenant-template operation streams separately:

```text
0007:
  central operations: once per database
  tenant operations: same template for every tenant schema
```

Central runs first. Tenant → central FKs can then resolve. Central → tenant-template references are rejected by registry compilation.

Single-schema mode may combine logical central and tenant templates into one physical desired image and one operation graph, deduplicating shared objects before diff.

### Determinism

Same desired image, actual image, hints, PostgreSQL target version, and policy must produce byte-identical operation JSON and SQL. Sort diagnostics and operations by stable object identity plus dependency order. Record PostgreSQL minimum/maximum feature version when syntax differs.

## Runner

### Dedicated authority

The runner uses a dedicated migration DSN/connector or explicitly configured migration pool. Request `read`/`write`/`security_read` pools cannot be reused implicitly. Migration credentials may create/alter schemas and roles; request credentials may not.

### History tables

Store authoritative history in the central schema, provisioned by a small idempotent bootstrap:

```text
wreath_migration_definitions
  migration_id
  parent_checksum
  checksum
  target_kind
  source_fingerprint
  target_fingerprint
  generated_at (UTC instant)
  applied_at (UTC instant)
  status

wreath_tenant_migrations
  tenant_key
  schema_name or schema_id
  migration_id
  checksum
  attempt
  started_at (UTC instant)
  applied_at (UTC instant)
  status
  error_code
  error_summary
  observed_fingerprint
```

Timestamps are immutable UTC instants. Human displays may add local formatting but retain sortable permanent UTC/offset form.

Do not store secrets, DSNs, raw server errors with credentials, or unbounded SQL in history rows.

### Locking

Use PostgreSQL advisory locks plus history-row state:

- database/application lock for central migration ordering;
- tenant-template generation/version lock;
- per-tenant lock for isolated application;
- transaction-level locks for transactional segments;
- carefully released session-level lock only where non-transactional operations require it.

Lock keys derive from versioned stable hashes, not Python hash(). Lock timeout/cancellation produces explicit no-change or ambiguous-state results.

### Single-schema run

1. acquire migration connection;
2. acquire application advisory lock;
3. verify history chain/checksum and live source fingerprint;
4. reject unreviewed drift;
5. execute one transactional segment;
6. run non-transactional segments with explicit checkpoints where present;
7. re-introspect target objects;
8. verify target fingerprint;
9. atomically record success where transaction boundaries permit;
10. release lock/connection.

If live source differs from artifact source, do not “best effort” apply. Require regeneration or an explicit reviewed drift override recorded in metadata/history.

### Isolated tenant run

1. migrate central schema and history first;
2. read trusted tenant/schema directory;
3. verify tenant artifact once;
4. group tenants by current fingerprint;
5. skip verified target fingerprints;
6. apply with configured bounded concurrency;
7. hold one tenant lock and one migration connection per active tenant;
8. checkpoint status independently;
9. re-introspect and verify each tenant;
10. report complete/failed/skipped/ambiguous counts and bounded failure detail;
11. allow safe resume without rerunning successful tenants.

A failure in tenant A does not roll back completed tenant B. Fail-fast/continue/max-failures is explicit policy.

### Transactional and non-transactional segments

Most PostgreSQL DDL is transactional, but operations such as `CREATE INDEX CONCURRENTLY` cannot be treated as ordinary transaction contents and can leave invalid objects on failure.

Artifacts split segments and encode:

```text
transactional
non_transactional_resumable
manual
```

The runner validates expected pre/post state around every non-transactional segment. It does not assume an exception means no effect.

### Cancellation and ambiguity

Cancellation handling reports one of:

```text
not_started
rolled_back
applied_and_verified
failed_with_known_state
ambiguous_requires_inspection
```

An ambiguous connection is discarded. The next run introspects before deciding whether to resume, repair, or stop.

## CLI/control surface

The eventual literal CLI could be:

```text
wreath migrations detect app:app
wreath migrations generate app:app --name add-order-status
wreath migrations show 0002
wreath migrations check app:app
wreath migrations apply app:app
wreath migrations apply app:app --tenants all --concurrency 4
wreath migrations status app:app
```

Commands importing an app for inspection must not start ordinary ASGI lifespan or request services. The migration command explicitly starts only the configured migration/database control resources it owns.

Machine-readable JSON output includes schema/image versions, checksums, operation safety, target counts, statuses, and loss/truncation markers. Human output is deterministic and bounded.

## Flight Recorder and Inspector integration

Migration operations are control-plane events, not request spans.

Expose bounded numeric events/counters:

- migration ID/checksum reference;
- central/tenant target kind;
- tenant schema ID under policy;
- detect/generate/apply/verify phase duration;
- lock wait;
- operation count by safety/transaction class;
- completion/failure/ambiguous outcome;
- active migration concurrency and queue depth.

Never record DDL values, tenant names, DSNs, or raw database errors by default. Inspector should show migration pressure/status only to authorized local control clients.

## Pure/native ownership

### Python owns

- model declaration and schema intent;
- migration policy and explicit hints;
- artifact loading/review/approval;
- CLI and orchestration;
- bounded tenant scheduling;
- operator callbacks and presentation.

### Native C owns when measured

- direct batch decode of `pg_catalog` rows;
- compact actual schema images;
- fingerprinting and sorted-array diff;
- dependency graph/topological ordering;
- validated identifier rendering;
- deterministic SQL tape execution/rendering;
- native driver protocol, transaction, cancellation, and result-state tracking.

A pure detector/generator remains the oracle. Native is selected only after parity and multi-schema evidence.

## Security and audit rules

- Migration credentials are separate from request credentials.
- Tenant schema/role identifiers come only from the trusted directory and strict validation.
- Artifact checksums and parent chain are verified before execution.
- Generated destructive/manual operations require explicit approval metadata.
- No arbitrary Python from migration artifacts.
- No automatic rename inference.
- No runtime schema creation from an untrusted request.
- Central migration completes before tenant template application.
- History timestamps are UTC and immutable.
- History/status records are bounded and redact server error detail.
- Advisory locks and database privileges remain the authoritative concurrency/security mechanisms.
- Schema isolation without tenant-specific role privileges is not documented as a database security boundary.

## Correctness rules

- Detector output is deterministic and pure/native identical.
- Generator output is byte-identical for the same inputs.
- Source fingerprint mismatch stops apply.
- Successful apply ends with target fingerprint verification.
- Per-tenant success is independently durable and resumable.
- Connection cancellation leaves synchronized state or discards the connection.
- Non-transactional operations have explicit pre/post state checks.
- Runner cannot borrow ordinary request pools implicitly.
- Raw SQL remains available; migration-generated SQL is reviewable and PostgreSQL-specific.
- Single-schema and isolated-tenant modes are tested separately; neither is inferred from the other.
