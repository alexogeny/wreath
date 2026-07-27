# Native PostgreSQL tenancy and ORM plan — Stage 1: schema ownership and request path

## Status

Deprecated. Replaced by [`wreath-metal-postgres-tenancy-migrations.md`](wreath-metal-postgres-tenancy-migrations.md). Retained only as historical design context; do not implement from this plan.

This plan deliberately supersedes, subject to an ADR, the current ORM statement that Wreath only validates schemas and never creates/alters/drops them. `wreath.migrations` is presently a reserved empty module. Raw SQL remains first class and Wreath remains PostgreSQL-specific.

Stage 2 covers migration detection, generation, and running. Stage 3 defines the red/green TDD sequence, benchmarks, files, risks, and acceptance gates.

## Goal

Extend the existing native PostgreSQL driver and first-class ORM to support:

1. **single-schema mode:** central and application/tenant-template models resolve to one configured physical schema;
2. **isolated-tenant-schema mode:** central models use one fixed shared schema while tenant models resolve to a transaction-bound physical schema and, when security isolation is requested, a tenant-specific database role;
3. one shared compiled ORM/query shape across tenant schemas without leaking connection state, prepared plans, model instances, or schema identifiers between tenants.

The optimized request path should be:

```text
trusted tenant resolution
    -> numeric TenantContext
    -> exclusive ORM Session lease
    -> BEGIN
    -> SET LOCAL ROLE (isolated role mode)
    -> pg_catalog.set_config('search_path', ..., true)
    -> shared ORM SQL / prepared-plan cache
    -> native decode + direct model hydration
    -> COMMIT or ROLLBACK
    -> verified clean connection release
```

Central-table SQL remains physically qualified. Tenant-template SQL is unqualified only inside a bound transaction context.

## Existing repository mechanisms to extend

### PostgreSQL driver

`wreath.postgres` already has:

- native and pure protocol backends;
- bounded pools with exclusive leases;
- read/write/security-read workloads;
- transaction barriers that reject concurrent operations inside explicit transactions;
- per-connection prepared-plan LRU keyed by SQL text;
- pipelining, cancellation recovery, binary codecs, and direct decode destinations;
- explicit `Statement` registration and native `Plan` objects;
- pool release/connection discard behavior.

No tenant or schema-context state exists. Pool release currently knows whether a connection belongs to the pool, not whether it is clean of tenant role/search-path/transaction state.

### ORM

The ORM already has:

- model `schema` and `table` declarations;
- immutable `ModelSpec`, `ColumnSpec`, `RelationshipSpec`, and registry fingerprints;
- fully qualified SQL through `compiler.qualified(spec)` for selects, joins, inserts, updates, deletes, and select-in loads;
- native query-shape keys and bounded plan cache;
- request-scoped `Session` with identity map and exclusive lazy connection;
- native fixed-size model storage and direct PostgreSQL hydration;
- unit-of-work flush ordering, transactions, savepoints, and raw SQL;
- startup `pg_catalog` validation for columns, defaults, PKs, unique constraints, and foreign keys;
- route-compiled `FromORM` session injection.

The missing abstraction is logical schema ownership. `ModelSpec.schema` is currently one concrete string and the registry fingerprint includes `schema.table`.

### Schema validation

`orm/introspection.py` already queries `pg_catalog`, compares deterministic issues, and validates after database startup but before user startup handlers. It currently queries each model separately and assumes one physical schema per model.

### Reserved migration surface

`src/wreath/migrations.py` exports nothing. Existing docs and agent policy explicitly say ORM does not manage schema. An accepted ADR must replace that policy when implementation begins.

## Public schema model

### Schema modes

```python
from wreath.orm import CENTRAL_SCHEMA, TENANT_SCHEMA, SchemaMode

registry = app.orm(
    database="main",
    models=[Account, Tenant, Order, Invoice],
    schema_mode=SchemaMode.single("app"),
)

registry = app.orm(
    database="main",
    models=[Account, Tenant, Order, Invoice],
    schema_mode=SchemaMode.isolated(
        central="wreath_core",
        tenant_context=resolve_tenant_context,
        require_role=True,
    ),
)
```

Model declarations use logical schema references:

```python
class Tenant(Model, table="tenants", schema=CENTRAL_SCHEMA):
    ...

class Order(Model, table="orders", schema=TENANT_SCHEMA):
    ...
```

Existing literal strings remain fixed physical schemas:

```python
class AuditArchive(Model, table="events", schema="archive"):
    ...
```

The names `CENTRAL_SCHEMA` and `TENANT_SCHEMA` are conventional technical terms; do not theme them.

### Internal schema reference

```python
@dataclass(frozen=True, slots=True)
class SchemaRef:
    kind: Literal["fixed", "central", "tenant"]
    name: str | None = None
```

`ModelMeta` stores a `SchemaRef`, while compatibility access may continue exposing a concrete string for fixed models. `ModelSpec` stores logical schema and a precomputed SQL qualification mode.

```python
@dataclass(frozen=True, slots=True, eq=False)
class ModelSpec:
    model_type: type[Model]
    schema: SchemaRef
    table: str
    ...
    sql_namespace: Literal["qualified", "tenant_search_path"]
```

### Single-schema resolution

`SchemaMode.single("app")` resolves both central and tenant logical schemas to fixed `"app"`. SQL remains physically qualified as today. This mode should be behaviorally equivalent to current ORM operation except that logical schema declarations are allowed.

Literal fixed schemas remain literal and are not remapped.

### Isolated resolution

`SchemaMode.isolated(central="wreath_core", ...)` resolves:

- central model → qualified `"wreath_core"."table"`;
- fixed model → qualified declared schema;
- tenant model → unqualified `"table"` under a transaction-local tenant context.

Tenant-template SQL must never execute when no tenant context is bound.

## Tenant context and trust boundary

### Context object

```python
@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str
    schema: str
    role: str | None = None
    generation: int = 0
```

The request path should carry a compiled/native counterpart:

```c
typedef struct {
    uint64_t tenant_key;
    uint32_t schema_id;
    uint32_t role_id;
    uint32_t generation;
    uint32_t flags;
} WreathTenantContext;
```

The numeric IDs reference an application-owned immutable tenant directory snapshot. The native path must not repeatedly hash/quote arbitrary strings.

### Resolver

Tenant identity is established before ORM session creation, normally after trusted-host/proxy handling and authentication. A resolver may read a route parameter, host mapping, or authenticated claim, but its result must be validated against an application-owned directory.

Never derive a PostgreSQL schema or role directly from unchecked request bytes.

The first implementation can use a Python resolver because tenancy is application policy. Once resolved, driver/session/query execution remains on the compiled/native path. A future native directory may remove that callback if measured.

### Security distinction

Schema `search_path` is namespace selection, not by itself a complete security boundary. Define two explicit isolated variants:

- **namespace-isolated:** one request role can access multiple tenant schemas; Wreath prevents accidental cross-context use but PostgreSQL permissions do not prevent malicious raw SQL from naming another schema;
- **role-isolated:** each tenant context includes a database role that has access only to its tenant schema plus approved central objects. The request connection performs transaction-local role selection.

Only role-isolated mode may be documented as database-enforced tenant isolation.

The migration role is separate and never used by request pools.

## Connection schema context

### Why transaction-local search path

PostgreSQL reparses prepared statements when `search_path` changes between uses. `SET LOCAL` is cleared at transaction end on commit or rollback. This allows one shared tenant SQL string and connection plan cache without permanently mutating pooled connection state.

There remains a documented PostgreSQL edge: creating a same-named table earlier in the path does not alone force immediate reparse until another invalidation. Wreath avoids this by provisioning/migrating schemas outside request sessions and by treating tenant context changes as explicit driver state with tests.

### Bound transaction

Every isolated tenant ORM session is transaction-bound, including reads:

```sql
BEGIN;
SET LOCAL ROLE "tenant_role"; -- role-isolated mode only
SELECT pg_catalog.set_config(
    'search_path',
    'pg_catalog,"tenant_schema","wreath_core"',
    true
);
```

`set_config` accepts a bound text value, avoiding dynamic SQL for the path. Role is an identifier and must use the native validated identifier writer.

`pg_catalog` is explicit and first. Central SQL is still qualified, so tenant objects cannot shadow it. Tenant tables are unqualified and resolve only through the active context.

### Native connection state

Add equivalent state to pure/native connections:

```text
schema_mode
schema_context_id
schema_generation
role_id
context_transaction_owned
context_ready
context_failed
```

Operations are rejected when:

- tenant SQL is submitted without a ready context;
- a different tenant context is requested while operations are outstanding;
- explicit transaction control attempts to escape the session-owned tenant transaction;
- a context setup/rollback operation is unresolved;
- connection transaction status is not idle at release.

### Prepared-plan cache

The current driver cache is keyed by SQL text. Preserve shared tenant SQL where PostgreSQL's search-path reparse behavior is proven.

Add context-generation metadata to each native/pure `Plan` for diagnostics and stale-completion checks, but do not automatically multiply cached SQL by tenant unless tests show PostgreSQL invalidation is insufficient.

Focused tests must prove on each supported PostgreSQL version:

1. prepare unqualified tenant SQL in schema A;
2. commit/rollback;
3. bind schema B;
4. execute the same named prepared statement;
5. observe only schema B rows and schema B result metadata;
6. switch repeatedly after DDL/statistics invalidation;
7. ensure no stale decoder/hydration plan survives changed result OIDs/layout.

If this cannot be certified, use one of two explicit fallbacks:

- invalidate/deallocate tenant-sensitive prepared plans on context change; or
- namespace plan cache by schema context.

Never silently rely on stale plans for speed.

### Pool release invariant

A connection can return to an ordinary pool only when all are true:

```text
transaction status = idle
no tenant context setup/teardown pending
no outstanding operations
no retained tenant role/search-path state
no failed cancellation recovery
```

Because `SET LOCAL`/`SET LOCAL ROLE` end with the transaction, a successful commit or rollback is the cleanup mechanism. If cleanup cannot be confirmed, discard the connection.

Expose the invariant through a native boolean/enum rather than Python inspecting protocol internals.

## ORM compiler changes

### Qualification

Replace the single `qualified(spec)` decision with a startup-compiled table reference:

```python
def table_sql(spec: ModelSpec, mode: SchemaMode) -> str:
    if spec.sql_namespace == "tenant_search_path":
        return quote(spec.table)
    return f"{quote(resolved_schema)}.{quote(spec.table)}"
```

This seam already covers:

- SELECT source tables;
- joined/select-in relationships;
- INSERT;
- UPDATE;
- DELETE.

No query node should carry a physical tenant schema string.

### Query/cache fingerprints

Split current registry fingerprinting:

- `template_fingerprint`: model/table/column/relationship semantics plus logical schema kind; same for every tenant;
- `deployment_fingerprint`: template plus central/fixed physical schema configuration;
- `tenant_actual_fingerprint`: live catalog fingerprint for one tenant schema.

Native/pure query shape remains tenant-independent. Existing bounded registry plan cache should therefore compile one tenant query shape, not one per tenant.

### Relationship rules

Allow:

- central → central;
- tenant → tenant within the same active context;
- tenant → central when PostgreSQL FK and privileges permit;
- fixed → fixed/central when explicitly declared.

Reject during registry compilation:

- central → tenant-template FK/relationship;
- tenant A → tenant B concepts;
- fixed/tenant combinations whose physical target cannot be resolved deterministically;
- tenant relationship loading without a bound context.

Tenant → central join SQL qualifies the central side and leaves tenant side unqualified.

## ORM session and binding changes

### Session ownership

Extend `Session` with immutable tenant context and context-transaction ownership:

```python
Session(registry, workload, tenant_context=None)
```

Its existing identity map is already session-local. Include tenant context in diagnostic identity and assert that hydrated native objects are owned by the current session. No cross-request object cache is introduced.

### Lazy acquisition

On first database use:

1. acquire exclusive connection;
2. if isolated tenant models can be used, begin and bind context atomically;
3. only mark session usable after ReadyForQuery confirms setup;
4. execute central or tenant statements;
5. commit/rollback on close according to unit-of-work outcome;
6. verify clean state before release.

Context setup should be a single driver operation/tape where safe, not several interleavable Python submissions.

### Explicit transaction behavior

Tenant session's outer transaction is context ownership. `Session.begin()` inside it creates a savepoint/nested unit of work rather than a second outer BEGIN. A user cannot commit away the context while continuing to use the session.

Raw SQL remains first class and runs under the same role/path. Database privileges—not SQL text scanning—are the security boundary. Wreath may reject direct transaction-control statements that violate session ownership, as it already treats transaction SQL specially.

### Route binding

Extend route-compiled session binding rather than resolving registries dynamically:

```python
FromORM(database="main", workload="read", tenant=True)
```

Prefer a registry-level mode so `tenant=True` is inferred where unambiguous. Endpoint compilation records whether tenant context is required before activation. A missing context fails before connection acquisition.

One request still receives one session per `(registry, workload, tenant_context)` pair, matching current deduplication behavior.

## Application and startup behavior

`Wreath.orm()` gains schema mode and tenant directory/resolver configuration. Registry declarations still compile immediately. Startup order becomes:

1. start databases;
2. validate central/fixed schema;
3. validate tenant template according to configured policy (representative, all, sampled, or migration-status based);
4. start clients;
5. run user startup handlers.

Validating every tenant synchronously may be unacceptable at large counts. Startup policy must be explicit; the migration history/fingerprint table should provide the fast readiness answer once migrations exist.

## Flight Recorder and Inspector seam

Use numeric metadata only:

- schema mode and template fingerprint in static metadata;
- tenant key/schema ID optionally on request/dependency records under privacy policy;
- context bind time, rollback/discard, prepared-plan reparse/invalidation, and pool pressure counters;
- tenant ID is not a metric label by default;
- never record schema/role strings or query values repeatedly.

Tenant context ownership must not depend on Flight Recorder availability.

## Correctness rules

- A request without a validated tenant context cannot execute tenant-template SQL.
- Central SQL is always physically qualified in isolated mode.
- Request credentials do not have migration privileges.
- Namespace isolation and database role isolation are documented as different guarantees.
- Tenant role/path state is transaction-local and verified cleared before pool reuse.
- Failed cleanup discards the connection.
- No connection runs concurrent operations across schema contexts.
- Query shapes and ORM plans are shared across tenants; result objects and sessions are not.
- Raw SQL remains available under the active database role/path.
- Literal fixed schemas continue working unchanged.
- Single-schema mode remains equivalent to current fully qualified behavior.
- Prepared-plan behavior is certified against supported PostgreSQL versions before shared tenant plans are enabled.
