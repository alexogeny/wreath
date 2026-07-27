# Native C ORM implementation plan

## Goal

Add a dependency-free `neo.orm` layer that compiles deterministic model declarations and immutable query expressions into parameterized PostgreSQL statements, preserves unrestricted raw SQL, integrates with Neo application lifespan and handler binding, and uses the existing native PostgreSQL decoder to hydrate fixed-size per-model C objects without an intermediate `Record`. The pure backend must expose the same behavior. PostgreSQL remains responsible for physical query planning; Neo optimizes projections, loading strategy, batching, allocations, and round trips.

## Repository constraints

- Target CPython 3.14 and use the CPython C API directly where useful.
- Keep `src/neo` free of mandatory third-party runtime dependencies.
- Preserve `neo.postgres.Connection`, `Database`, `Statement`, and raw `execute`/`fetch`/`fetchrow`/`fetchval` APIs unchanged.
- Preserve native/pure observable parity. `NEO_PURE=1` must run the ORM through the reference PostgreSQL backend.
- Compile declarations and query shapes once; do not repeat annotation inspection or relationship resolution per request.
- Keep application, registry, session, identity-map, and receive-slab ownership explicit. Do not introduce a process-global model registry or query cache.
- Never perform hidden asynchronous I/O from model attribute access.
- Do not add migrations, automatic DDL execution, transparent replica failover, cross-request object caching, or a second SQL execution engine.
- Preserve PostgreSQL protocol synchronization, cancellation recovery, pool bounds, pipeline limits, and receive-slab lifetime rules documented in `docs/native/postgres.md`.
- Add SIMD only behind runtime dispatch, only with scalar parity, and only after retained repeated benchmarks show that a specific kernel is material.

## Prescribed public API

### Model declarations

Create `src/neo/orm/` as the public package. The declaration syntax is explicit and ordered:

```python
from neo.orm import Mapped, Model, column, relationship
from neo.orm.types import Int64, Text, Timestamp


class User(Model, table="users"):
    id: Mapped[int] = column(Int64, primary_key=True)
    email: Mapped[str] = column(Text, unique=True)
    name: Mapped[str] = column(Text)
    created_at: Mapped[object] = column(Timestamp)
    posts = relationship("Post", foreign_key="author_id", load="selectin")


class Post(Model, table="posts"):
    id: Mapped[int] = column(Int64, primary_key=True)
    author_id: Mapped[int] = column(Int64, references=User.id)
    title: Mapped[str] = column(Text)
    author = relationship(User, foreign_key=author_id, load="raise")
```

Implement these contracts exactly:

- `Mapped[T]` is a typing-only generic alias/protocol; class access returns a SQL expression and instance access returns `T`.
- `column(pg_type, *, primary_key=False, nullable=False, unique=False, default=MISSING, server_default=None, references=None)` returns a descriptor.
- `relationship(target, *, foreign_key, back_populates=None, load="raise")` accepts `"raise"`, `"selectin"`, or `"joined"`. String targets resolve only inside the application-owned registry.
- Declaration order is class-body order. Inherited columns precede subclass columns. Duplicate Python names, duplicate database column names, multiple implicit primary keys, unresolved targets, ambiguous foreign keys, and invalid defaults fail while the registry compiles.
- Every model must declare at least one primary-key column in the initial implementation. Composite primary keys are supported and preserve declaration order.
- Table and column identifiers are validated as unquoted PostgreSQL identifiers initially. Do not add user-controlled quoting or schema search-path behavior. Add `schema="name"` as an explicit model class option, defaulting to `"public"`.
- Model constructors accept declared column names as keyword arguments only. Unknown keys and positional arguments fail. Missing non-null fields are allowed only when a Python default or server default exists.

### Application registration

Add `Neo.orm()` in `src/neo/app.py`:

```python
registry = app.orm(
    database="main",
    models=[User, Post],
    validate_schema="error",  # "off" | "warn" | "error"
)
```

Rules:

- `database` refers to an existing `app.postgres()` registration and fails immediately if unknown.
- One ORM registry is allowed per database name. Store registries in a new `Neo._orm_registries` dictionary; do not put them in a module global.
- `app.orm()` resolves models and relationships immediately, freezes metadata, computes layouts/fingerprints, and marks the application dirty.
- Schema validation runs after the database has started and before user startup handlers. Update `_lifespan()` rather than registering an order-sensitive synthetic handler.
- Shutdown does not own separate network resources; active request sessions must already be closed by dependency cleanup before database shutdown.

### Session injection

Add `FromORM` and `Session`:

```python
from typing import Annotated
from neo.orm import FromORM, Session

@app.get("/users/{user_id}")
async def get_user(
    request,
    session: Annotated[Session, FromORM("main", workload="read")],
    user_id: int,
):
    return await session.get(User, user_id)
```

Extend `src/neo/binding.py` at the existing `Connection`/`FromDatabase` compilation seam:

- Recognize only `Annotated[Session, FromORM(...)]`; bare `Session` is rejected.
- Validate the registry and workload while compiling the handler.
- Construct one request-scoped session per distinct `(registry, workload)` pair, even when multiple parameters request it.
- Acquire a connection lazily on the first operation.
- Close sessions in reverse acquisition order on success, exception, and cancellation. Closing rolls back an active transaction and returns the connection exactly once.
- A `Session` must reject use after close and concurrent use while an explicit transaction is active. It may use the driver’s normal pipelining only outside explicit transactions.

### Query and raw SQL APIs

Implement immutable query objects:

```python
query = (
    User.select(User.id, User.email, User.name)
    .where(User.email == email)
    .include(User.posts.selectin())
    .order_by(User.id)
    .limit(100)
)
users = await session.fetch(query)
```

Initial public methods:

```text
Model.select(*fields) -> Select[Model]
Select.where(*predicates) -> Select
Select.include(*load_options) -> Select
Select.order_by(*expressions) -> Select
Select.limit(value: int) -> Select
Select.offset(value: int) -> Select
Select.for_update() -> Select
Session.get(model, primary_key, *, load=()) -> Model | None
Session.fetch(query) -> list[Model]
Session.fetch_one(query) -> Model | None
Session.add(model) -> None
Session.delete(model) -> None
Session.flush() -> None
Session.begin() -> async context manager
Session.raw(sql, *args) -> RawQuery
RawQuery.execute() -> str
RawQuery.fetch() -> list[Record]
RawQuery.fetchrow() -> Record | None
RawQuery.fetchval() -> object
RawQuery.models(model) -> list[Model]
```

Preserve direct driver use without deprecation. `Session.raw()` delegates to the leased existing `Connection`; it does not parse, rewrite, or cache arbitrary SQL. `RawQuery.models(Model)` requires every selected model column exactly once by database column name, verifies returned names/OIDs, and then uses the normal model hydrator. Extra columns are rejected initially rather than silently discarded.

Do not implement string-based filter names, implicit joins, magic `save()`, synchronous lazy loading, or SQL interpolation.

## Data model and static layout

### Immutable metadata

Add these frozen, slotted internal objects in `src/neo/orm/schema.py`:

```text
ColumnSpec
- python_name: str
- database_name: str
- position: int
- pg_type: PgType
- oid: int
- nullable: bool
- primary_key: bool
- unique: bool
- default: object
- server_default: str | None
- reference: ColumnRef | None

RelationshipSpec
- name: str
- target: ModelSpec
- local_columns: tuple[ColumnSpec, ...]
- remote_columns: tuple[ColumnSpec, ...]
- cardinality: "one" | "many"
- default_load: "raise" | "selectin" | "joined"

ModelSpec
- model_type: type[Model]
- schema: str
- table: str
- columns: tuple[ColumnSpec, ...]
- primary_key: tuple[ColumnSpec, ...]
- relationships: tuple[RelationshipSpec, ...]
- fingerprint: bytes
- storage: StorageSpec
```

`Registry.compile()` is the only relationship-resolution entry point. Once compiled, descriptors point to immutable specs and reject metadata mutation.

Compute `ModelSpec.fingerprint` as SHA-256 over a versioned canonical byte encoding, not `repr()` or Python’s randomized hash. Include schema/table, ordered database names, OIDs, nullability, key flags, references, defaults represented by stable tagged encodings, and relationship mappings. Fail compilation when a Python default cannot be represented deterministically.

### Native object representation

Add model storage to the existing `neo._native._postgres` extension rather than creating a separate extension. This permits direct use of `NeoPgDecoderPlan`, field tapes, slabs, and codecs without a private cross-extension ABI.

Add:

```text
src/neo/_native/postgres/model.c
src/neo/_native/postgres/model.h
src/neo/_native/postgres/hydrate.c
src/neo/_native/postgres/hydrate.h
```

Register them in `setup.py` and initialize them from `src/neo/_native/_postgresmodule.c` after record/decode initialization and before connection initialization.

Each compiled Python model receives a dedicated heap type created by an internal `_compile_model_layout(spec)` native function. Its `tp_basicsize` is fixed for that model and contains:

```c
typedef struct {
    PyObject_HEAD
    PyObject *identity_owner;   /* owning Session or NULL */
    uint64_t state_flags;
    /* fixed count loaded/null/dirty bitmap words */
    /* aligned fixed-width cells and PyObject* cells */
} NeoPgModel;
```

Do not place `ModelSpec *` in every instance. Store the spec/layout on the generated type. Layout generation must:

1. order fixed-width cells by decreasing alignment;
2. assign nullable, loaded, and dirty bit indexes by declaration position;
3. store bool as one byte initially rather than bit-packing mutable values;
4. store `int2`, `int4`, `int8`, `float4`, `float8`, date, timestamp, and UUID inline;
5. store text, bytea, JSON, arrays, unknown OIDs, and user-codec values in `PyObject *` cells initially;
6. emit a pointer bitmap used by `tp_traverse` and `tp_clear`;
7. check every offset/size operation for overflow before setting `tp_basicsize`.

The type implements descriptor-backed field access, GC traversal/clear, deallocation, repr, and no instance `__dict__`. Instances of one model type therefore have one deterministic size. Variable-length payloads remain separately allocated; do not claim that arbitrary text is inline or that total retained memory is fixed.

The pure model uses `__slots__`, the same loaded/null/dirty semantics, and normal Python values. Tests compare behavior, not `sys.getsizeof()` equality.

### Object state

Use explicit state bits:

```text
TRANSIENT  constructed, not present in identity map
PERSISTENT loaded or inserted and owned by one open Session
DELETED   scheduled for deletion
DETACHED  owner Session closed; loaded scalar reads remain valid
```

Rules:

- Reading an unloaded scalar raises `UnloadedAttributeError`.
- Reading an unloaded relationship raises `UnloadedRelationshipError`; no property access starts I/O.
- Assigning a field validates/coerces through its `PgType`, sets loaded, clears null when appropriate, and marks dirty only when the semantic value changes.
- Assigning `None` to a non-nullable column fails before SQL execution.
- Primary-key mutation on a persistent object is rejected initially.
- An object cannot be attached to two sessions.
- Closing a session detaches its objects but does not invalidate already-loaded scalar values.

## SQL expression compiler

Add `src/neo/orm/expressions.py`, `query.py`, and `compiler.py`.

Represent expressions as frozen, slotted nodes: `ColumnExpr`, `ValueExpr`, `BinaryExpr`, `BooleanExpr`, `UnaryExpr`, `OrderExpr`, and `Select`. Operator overloads may only construct nodes; they must never execute or inspect a database.

The compiler returns:

```text
CompiledQuery
- sql: str
- bind_values: tuple[object, ...]
- bind_oids: tuple[int, ...]
- result_model: ModelSpec | None
- selected_columns: tuple[ColumnSpec, ...]
- load_plan: LoadPlan
- shape_key: bytes
```

Compile with these rules:

- Quote only registry-validated identifiers using a single compiler helper.
- Render values as `$1`, `$2`, ... and keep values out of SQL/cache keys.
- Reject expressions that mix registries or database instances.
- Reject negative limits/offsets and non-integer values before execution.
- Add a primary-key tiebreaker only when Neo itself creates a select-in relationship query; never change ordering on a user query.
- `fetch_one()` adds `LIMIT 2` only when no stricter limit exists and raises `MultipleResultsError` if two rows return.
- `get()` compiles a primary-key equality query with `LIMIT 1`; composite keys require a tuple of exact arity.
- `for_update()` requires a write-workload session and an explicit transaction.

Keep the compiler in Python initially so generated SQL is easy to audit. Native acceleration belongs in hydration and fixed-size storage first. Move a measured compiler hotspot to C only with exact SQL parity fixtures.

### Bounded plan caching

Store an LRU query-shape cache on `Registry`, bounded by an explicit `query_cache_size` argument defaulting to 512. The shape key includes the registry fingerprint, expression node kinds, column identities, operators, selected projection, load graph, and query flags, but excludes runtime values.

Cache SQL templates, bind extraction instructions, and hydration plans. Do not cache results, model instances, exceptions, or connections. Cache insertion and eviction must remain correct under free-threaded CPython; use registry-owned synchronization rather than relying on the GIL.

## Loading and query optimization rules

Implement only semantics-preserving, deterministic rewrites:

- Remove duplicate selected columns while retaining first occurrence.
- Select primary-key columns even when omitted by the user because identity-map construction requires them; keep them internal unless explicitly projected.
- Deduplicate structurally identical joins and relationship load requests.
- Lower `.exists()` to `SELECT EXISTS(...)` when that API is added; do not detect arbitrary user intent.
- Reuse prepared-plan and decoder-plan machinery already owned by the PostgreSQL connection.
- Skip hydration when an identity-map object has all requested scalar fields loaded, but merge newly selected fields into an existing object.
- Batch select-in keys and deduplicate them by identity.
- Bound a select-in batch by both 1,000 identities and PostgreSQL’s 65,535-parameter limit; composite keys reduce the identity count accordingly.

Loading defaults:

- Explicit `.joined()` or `.selectin()` always wins.
- A declared to-one `load="joined"` may use a `LEFT JOIN`.
- A declared to-many relationship always uses select-in, even if `load="joined"` is requested initially; reject the unsupported request rather than multiplying parent rows.
- Multiple collection relationships use separate select-in statements.
- Default `load="raise"` performs no relationship query.
- Explicit lazy loading is `await session.load(instance_or_sequence, relationship)` and uses the same select-in batcher.

Do not use live row counts, timing history, `EXPLAIN`, or mutable heuristics to choose plans in the initial implementation. PostgreSQL chooses scan and join algorithms. Neo chooses only data shape and round-trip strategy.

## Session, transactions, and writes

Implement `Session` in `src/neo/orm/session.py` with slots for registry, workload, leased connection, identity map, new/deleted/dirty sets, transaction depth, closed flag, and relation batch state.

Identity keys are `(ModelSpec, primary_key_values)`. Never admit an object with null or unloaded primary-key components. Hydration must return the existing object for an identity and merge only fields present in the result. If the database returns conflicting values for an already loaded field in the same session, overwrite only when the object is not dirty; preserve dirty values.

`Session.begin()`:

- lazily acquires one connection;
- sends `BEGIN` at outer entry and `COMMIT` on clean outer exit;
- sends `ROLLBACK` on exception/cancellation;
- implements nested contexts with deterministic `SAVEPOINT neo_sp_<depth>` names;
- marks the connection unusable and lets the existing pool discard/recover it when transaction state cannot be proven synchronized.

`flush()` ordering is deterministic:

1. inserts in model registration order, then object attachment order;
2. updates in model registration order, then identity-key order;
3. deletes in reverse model registration order, then identity-key order.

Initial write SQL:

- `INSERT` includes only loaded columns without server defaults and uses `RETURNING` for all server-generated or unloaded columns.
- `UPDATE` includes dirty non-primary-key columns and a primary-key predicate. A model with no dirty writable columns emits no SQL.
- `DELETE` uses the complete primary key.
- Each statement is parameterized and grouped by identical column shape for plan reuse.
- Flush runs only in an explicit transaction. If called outside one, open a transaction for the flush and commit/rollback it atomically.
- On any write error, leave object dirty/new/deleted state intact for inspection, require rollback, and do not mark values persistent prematurely.

Do not add cascades, orphan deletion, optimistic-version columns, bulk COPY, upsert, or many-to-many persistence in the first implementation. Those require separate public contracts.

## Direct native hydration

Extend the existing decoder without weakening `Record` behavior.

Add a destination abstraction to `decode.h`:

```c
typedef enum {
    NEO_PG_DEST_RECORD,
    NEO_PG_DEST_MODEL
} NeoPgDestinationKind;
```

A model hydration plan contains one entry per returned column:

```c
typedef struct {
    uint32_t oid;
    uint16_t format;
    uint16_t field_index;
    Py_ssize_t offset;
    NeoPgRawDecoder decoder;
    NeoPgStoreFunction store;
} NeoPgModelColumn;
```

Implementation requirements:

- Reuse OID/format decoder selection from `decode.c`; do not duplicate codec tables.
- Validate result names and OIDs before decoding the first row. A mismatch raises `MappingError` and consumes/recoveries the operation according to current protocol rules.
- Allocate one model instance per previously unseen identity. If the primary key is not first in the result, decode key fields into a small bounded key scratch area before allocation/lookup.
- Store fixed binary values directly into inline cells only when the existing decoder proves the PostgreSQL representation and length. Text-format and fallback codecs produce Python objects.
- Set loaded/null bits only after successful decode. On failure, decref all pointer cells, remove any provisional identity-map entry, and leave no partially visible object.
- Preserve slab ownership exactly as the existing field tape requires. The initial implementation may materialize variable-width Python objects. Slab-backed lazy strings require a later dedicated ownership design and benchmark; do not add borrowed pointers to the first implementation.
- Keep `fetch()` and `fetchrow()` Record paths unchanged. Add private connection hooks used by `Session`, such as `_fetch_models(compiled_plan, args, identity_map)`; do not expose decoder plans as public API.

Add allocation/debug counters following the existing native PostgreSQL test-hook conventions so benchmarks can prove Record elimination and tests can detect leaks.

## SIMD policy and implementation seam

Create `src/neo/_native/postgres/simd.c` and `simd.h` only after a scalar native hydration benchmark identifies a dominant eligible loop. The first candidate is loaded/null bitmap initialization or fixed-width null scanning across batch rows; UTF-8 validation is eligible only if Neo owns validation for that codec.

Requirements:

- Provide a scalar implementation in the same translation unit.
- Detect CPU features once per interpreter/module initialization; never execute unsupported instructions.
- Compile architecture-specific functions with guarded compiler flags without raising the baseline requirement for the extension.
- Test every alignment, tail length, null pattern, and scalar/SIMD result equivalence.
- Report SIMD as unavailable on unsupported platforms and continue through the scalar path.
- Do not vectorize Python C-API calls, hold borrowed pointers across calls that can execute Python, or call Python while the GIL is released.

## Schema validation without migrations

Add `src/neo/orm/introspection.py`. On startup, query `pg_catalog.pg_class`, `pg_namespace`, `pg_attribute`, `pg_type`, `pg_constraint`, and `pg_index` through the configured read or write pool. Do not use `information_schema` when it loses PostgreSQL-specific identity.

Compare:

- schema and table existence;
- ordered column names;
- base type OIDs and array element OIDs;
- nullability;
- primary-key column order;
- declared unique constraints;
- declared foreign-key local/remote columns.

Do not compare Python defaults to server expressions except exact normalized server-default text when `server_default` was explicitly declared.

Produce a stable `SchemaDiff` sorted by `(schema, table, column, issue_code)`. `validate_schema="error"` raises one `SchemaMismatchError` containing the full bounded diff; `"warn"` emits one warning; `"off"` performs no catalog query. Never create, alter, or drop database objects.

## Concrete implementation tasks

### 1. Add declaration and metadata primitives

Create:

```text
src/neo/orm/__init__.py
src/neo/orm/errors.py
src/neo/orm/types.py
src/neo/orm/fields.py
src/neo/orm/model.py
src/neo/orm/schema.py
src/neo/orm/relations.py
```

Implement explicit PostgreSQL types for bool, int2/int4/int8, float4/float8, text/varchar, bytea, UUID, date, timestamp/timestamptz, and JSON. Reuse conversion semantics from the PostgreSQL codec layer; do not silently add types unsupported by both driver backends.

Add declaration, inheritance, fingerprint, invalid-model, constructor, field-state, and pure/native parity tests under `tests/orm/`.

### 2. Add application registry and schema validation

Modify `src/neo/app.py` to own registries and invoke compile/validation in the prescribed startup order. Add `src/neo/orm/registry.py` and `introspection.py`. Test duplicate registries, unknown databases, startup failure cleanup, warning/error/off behavior, and deterministic diffs using real PostgreSQL integration fixtures where catalog fidelity matters.

### 3. Add immutable expressions and SQL compilation

Create `expressions.py`, `query.py`, and `compiler.py`. Add golden SQL tests that assert SQL text, bind order/OIDs, shape keys, identifier validation, cross-registry rejection, projection behavior, and no runtime values in cache keys. Keep generated SQL snapshots small and reviewed.

### 4. Add request-scoped sessions and binding

Create `session.py`, add `FromORM`, and modify `src/neo/binding.py`. Reuse existing dependency cleanup mechanics rather than adding a second request-finalization system. Test one-session-per-key deduplication, lazy acquire, reverse cleanup, cancellation, use-after-close, unknown registry/workload errors, and connection return exactly once.

### 5. Add reads, identity mapping, raw SQL, and explicit loading

Implement `get`, `fetch`, `fetch_one`, `raw`, `load`, joined to-one assembly, and select-in loading through the pure Record path first. Test composite identities, repeated rows, partial projections, dirty-field merge protection, no hidden I/O, batching bounds, and raw Record compatibility.

### 6. Add transactions and deterministic writes

Implement `begin`, nested savepoints, `add`, `delete`, and `flush`. Test success, server errors, cancellation at waiting/emitted/active driver stages, rollback, pool reuse, insert RETURNING, partial updates, deterministic ordering, and object state restoration. Do not proceed to native hydration until these semantics pass with `NEO_PURE=1` and the native driver.

### 7. Add fixed-size native model storage

Add `model.c/.h`, wire `setup.py` and `_postgresmodule.c`, and connect descriptors to generated native layouts. Test layout offsets/alignment, nullable/loaded/dirty bitmaps, GC cycles, deallocation, constructor failures, free-threaded operation, and native/pure behavior. Add a test-only `__layout__` summary rather than exposing raw addresses.

### 8. Add direct model hydration

Add `hydrate.c/.h` and extend `decode.c/.h`, plan ownership, and private connection dispatch. Retain existing Record tests unchanged. Add direct-path tests proving zero `Record` allocations, exact values/nulls, fallback OIDs, malformed data cleanup, identity reuse, fragmentation, multi-slab results, cancellation recovery, and reconnect behavior.

### 9. Add bounded optimization and benchmark coverage

Create focused benchmarks beside existing PostgreSQL benchmarks:

```text
benchmarks/postgres/bench_orm_hydrate.py
benchmarks/postgres/bench_orm_identity.py
benchmarks/postgres/bench_orm_relations.py
```

Measure at minimum:

- Record-to-pure-model baseline;
- native Record-to-model baseline;
- direct native model hydration;
- full-row versus projected-row hydration;
- identity-map hit/miss;
- joined to-one and select-in to-many at controlled cardinalities;
- allocation count and retained memory;
- median, p95, p99, throughput, and errors over repeated runs.

Retain Python version, platform, compiler flags, PostgreSQL version/configuration, row width/count, pool size, concurrency, warmup, and raw trial output. Only add a SIMD kernel after these results identify one; retain scalar/SIMD trials separately.

### 10. Complete public documentation and agent routing

Update:

```text
docs/guides/postgres.md
docs/native/postgres.md
docs/reference/services.md
docs/internals/performance.md
docs/agents/index.md
docs/agents/manifest.json
docs/agents/contracts.md
mkdocs.yml
```

Add an ORM guide and API reference page, including raw SQL escape hatches, no-hidden-I/O rules, session ownership, schema validation, and explicit non-support for migrations. Update `docs/llms.txt` if the new pages are public entry points.

## Correctness rules

- ORM use must not alter direct `neo.postgres` behavior.
- Generated SQL is always parameterized; only validated metadata supplies identifiers.
- Model and query metadata become immutable after registry compilation.
- A query cannot cross registries, databases, sessions, or workload connections.
- Attribute access never performs I/O.
- Identity maps and query caches are bounded by request/registry ownership respectively; no result cache is added.
- Cancellation either restores protocol/transaction synchronization or discards the connection.
- Native decode failure cannot expose a partially initialized model or leak a slab, field tape, pointer cell, or identity-map entry.
- Pure/native values, null handling, exceptions, object state, SQL, and loading behavior match.
- Fixed-size means the model object’s native struct size is fixed per compiled type; variable-length payload memory is explicitly excluded from that claim.
- Automatic optimization never changes user predicates, ordering, limits, locks, transaction boundaries, or replica routing.

## Files touched

```text
setup.py
src/neo/app.py
src/neo/binding.py
src/neo/postgres.py
src/neo/orm/
src/neo/_native/_postgresmodule.c
src/neo/_native/postgres/decode.c
src/neo/_native/postgres/decode.h
src/neo/_native/postgres/plan.c
src/neo/_native/postgres/plan.h
src/neo/_native/postgres/model.c
src/neo/_native/postgres/model.h
src/neo/_native/postgres/hydrate.c
src/neo/_native/postgres/hydrate.h
tests/orm/
tests/postgres/test_direct_path.py
benchmarks/postgres/bench_orm_hydrate.py
benchmarks/postgres/bench_orm_identity.py
benchmarks/postgres/bench_orm_relations.py
docs/guides/postgres.md
docs/native/postgres.md
docs/reference/services.md
docs/internals/performance.md
docs/agents/index.md
docs/agents/manifest.json
docs/agents/contracts.md
mkdocs.yml
```

`src/neo/_pure/postgres.py` should require no ORM-specific API beyond existing Record execution unless direct pure hydration proves necessary. Keep ORM fallback logic in `src/neo/orm/` rather than coupling the reference protocol driver to model declarations.

## Verification commands

Run focused checks while implementing:

```bash
uv run pytest tests/orm
uv run pytest tests/postgres/test_direct_path.py tests/postgres/test_batch_decode.py
NEO_PURE=1 uv run pytest tests/orm tests/postgres
uv run ruff check src/neo/orm tests/orm
uv run ty check
```

After native C changes:

```bash
uv run python tools/sanitizers/build_postgres.py
uv run pytest tests/postgres tests/orm
```

Run the repository-wide and documentation gates before completion:

```bash
uv run pytest
uv run ruff check .
uv run ty check
uv run --group docs mkdocs build --strict
```

Use the existing sanitizer playbook’s ASan/UBSan environment when executing the built extension. Run free-threaded and optional-JIT test modes separately where the repository supports them; do not infer one mode from another.

## Acceptance checks

- Two applications can register the same model classes against different database instances without sharing sessions, identity objects, or cached plans.
- Invalid models and relationships fail during `app.orm()`; schema mismatches fail or warn deterministically during lifespan startup according to configuration.
- A handler receives one request-scoped `Session`, acquires its connection only on first use, and returns it exactly once on success, error, or cancellation.
- `User.select().where(User.id == value)` emits stable parameterized SQL whose text/cache key does not contain `value`.
- Repeated rows for one primary key produce one model object per session and merge partial projections without overwriting dirty fields.
- Reading an unloaded field or relationship raises without issuing SQL; `await session.load(...)` batches relationship loads.
- To-one joined loading and to-many select-in loading return correct graphs without N+1 queries or parent-row multiplication.
- Insert/update/delete flushes are atomic, deterministic, parameterized, and recover the pooled connection after rollback.
- `Session.raw()` returns the existing native/pure `Record` behavior, and direct `Connection.fetch()` remains unchanged.
- `RawQuery.models(User)` validates result names/OIDs and hydrates typed objects; mismatches fail before partially visible results escape.
- Native direct hydration creates no intermediate `Record`, has a fixed `tp_basicsize` for each model type, handles nulls/fallback codecs, and passes GC/sanitizer tests.
- `NEO_PURE=1` passes the same ORM behavioral suite.
- Query caches and select-in batches remain within configured bounds.
- Repeated benchmark artifacts demonstrate where time and allocations changed; any SIMD or performance claim is supported by retained scalar/native trials rather than a single run.
