# Wreath-metal PostgreSQL tenancy and migrations

## Status

Active replacement implementation plan. This document supersedes the deprecated
`native-postgres-tenancy-migrations*.md` plans.

No runtime implementation is included. Releasing migration DDL still requires an
accepted ADR replacing Wreath's current validate-only ORM policy.

## Mandate

Build one PostgreSQL-specific migration and tenancy system whose production engine
is Wreath-metal. Optimize the complete resolution path, not an isolated diff loop.
There is no pure-Python production backend and no slower native compatibility mode.
If the metal extension is unavailable, tenancy and migration APIs fail immediately
with an actionable unsupported-feature error.

The engine must make these cases exceptionally cheap:

1. one non-tenant schema that is already current;
2. one non-tenant schema requiring a migration;
3. a managed fleet where all tenant schemas are current;
4. a fleet containing a small number of distinct drift groups;
5. interruption and restart after some tenants have completed.

Physical DDL remains bounded by PostgreSQL locks, catalog work, storage, and WAL.
The performance target applies to readiness resolution, catalog decoding, drift
classification, planning, dispatch, checkpointing, and resumability. The design
must not claim that expensive DDL itself becomes cheap.

## Fixed decisions

- Wreath owns a PostgreSQL-only migration format and runner; there is no
  SQLAlchemy or Alembic compatibility layer.
- The implementation lives in the native PostgreSQL/metal stack. Python defines
  application intent and exposes bounded control-plane results only.
- Non-tenant and tenant-fleet resolution are separately compiled paths. The
  non-tenant path contains no tenant branches or tenant data structures.
- Existing literal model schemas remain fixed and qualified.
- Logical `CENTRAL_SCHEMA` and `TENANT_SCHEMA` references are compiled at startup.
- Central SQL remains qualified. Tenant ORM SQL is shared and unqualified only in
  a validated transaction-local tenant context.
- Tenant directory entries are trusted, immutable, numeric records. Request bytes
  never become PostgreSQL identifiers.
- Namespace selection and database-enforced role isolation are distinct modes.
- Migration credentials and request credentials are always separate.
- Migration artifacts are immutable reviewable data, never executable Python.
- Ordinary application startup may check readiness but never silently applies DDL.
- Artifact checksums are cryptographic. Hot grouping fingerprints are versioned,
  native, and verified before they authorize a state transition.
- Python test code may provide a small oracle for fixtures and differential tests,
  but it is not a supported backend and is never selected in production.

## Terminology and modes

### Non-tenant mode

`SchemaMode.single("app")` maps logical central and tenant declarations into one
qualified physical schema. Startup compiles a dedicated `SingleSchemaImage` and
selects the single-schema resolver function once.

### Tenant-fleet mode

`SchemaMode.isolated(...)` keeps central/fixed objects qualified and compiles one
shared tenant template. Each trusted directory entry maps numeric tenant, schema,
and optional role IDs to prevalidated identifiers.

Two security variants are explicit:

- `namespace`: Wreath prevents accidental context crossover, but request
  credentials may be able to name another schema;
- `role`: transaction-local role selection limits the request role to the active
  tenant schema and approved central objects.

Only `role` is database-enforced tenant isolation.

### Resolution policy

Fleet readiness has two explicit policies:

- `managed`: out-of-band tenant DDL is forbidden. Verified migration history is
  the fast authority; catalogs are read for unknown, mismatched, ambiguous, or
  sampled tenants.
- `strict`: the selected schemas are introspected before readiness is reported.
  Catalog work is chunked and its cost is reported honestly.

The default for production fleets is `managed`. Operators choose `strict` for
full audits and drift investigation.

## Performance contract

The implementation is unacceptable if work scales through Python objects,
callbacks, or interpreter crossings per tenant, schema object, operation, lock,
or history row.

A resolution invocation has at most:

1. one Python-to-metal call carrying compiled handles and scalar policy;
2. optional bounded progress callbacks at configured coarse intervals;
3. one metal-to-Python return containing aggregate counts and bounded failures.

Required complexity:

- already-current non-tenant resolution: one bounded history/readiness query and
  constant native work after decode;
- managed all-current fleet: `O(tenant_count)` compact row decode/classification,
  without catalog object materialization;
- strict fleet audit: `O(catalog_rows)` streamed decode with memory bounded by
  catalog chunk size;
- diff: linear merge over canonical packed images after one canonicalization;
- execution memory: bounded by compiled images, configured chunk size,
  concurrency, and bounded diagnostics—not total Python object count.

No acceptance claim may compare only against Python. End-to-end resolution must be
compared with the raw PostgreSQL history/catalog wire-and-decode floor.

## Public configuration surface

The initial literal API is:

```python
from wreath.orm import CENTRAL_SCHEMA, TENANT_SCHEMA, SchemaMode
from wreath.migrations import MigrationConfig, ResolutionPolicy

registry = app.orm(
    database="main",
    models=[Account, Tenant, Order],
    schema_mode=SchemaMode.single("app"),
)

registry = app.orm(
    database="main",
    models=[Account, Tenant, Order],
    schema_mode=SchemaMode.isolated(
        central="wreath_core",
        tenant_directory=directory,
        tenant_resolver=resolve_tenant,
        isolation="role",
    ),
)

app.migrations(
    registry=registry,
    config=MigrationConfig(
        database="migration",
        policy=ResolutionPolicy.managed(),
        catalog_chunk_size=256,
        concurrency=8,
        max_failures=100,
    ),
)
```

Model declarations use logical roles:

```python
class Tenant(Model, table="tenants", schema=CENTRAL_SCHEMA): ...
class Order(Model, table="orders", schema=TENANT_SCHEMA): ...
class Archive(Model, table="events", schema="archive"): ...
```

Public Python records are configuration or bounded result views. They must not be
the engine's internal representation.

## Native architecture

### Ownership boundary

Python owns:

- model declarations and explicit migration hints;
- operator policy and approval input;
- CLI argument parsing and bounded presentation;
- application-specific tenant authentication/resolution before native lookup.

Wreath-metal owns:

- immutable desired-image compilation;
- tenant-directory numeric lookup after application resolution;
- direct PostgreSQL history/catalog decoding;
- compact actual images and string arenas;
- canonicalization, fingerprints, grouping, diff, and dependency ordering;
- artifact parsing, verification, and SQL tapes;
- lock/history/setup protocol tapes;
- adaptive bounded scheduling, cancellation, and checkpointing;
- result aggregation and bounded diagnostics.

There is no callback into Python for each tenant or operation.

### Native files

Add narrowly separated units under `src/wreath/_native/postgres/`:

```text
migration_image.c/.h       packed desired/actual images and arenas
migration_compile.c/.h     startup image compiler
migration_catalog.c/.h     direct history/catalog destinations
migration_diff.c/.h        fingerprints, grouping, merge diff, DAG
migration_artifact.c/.h    artifact parser/checksum/SQL tape
migration_resolver.c/.h    single and fleet classifiers
migration_runner.c/.h      locks, scheduling, checkpoints, cancellation
migration_api.c/.h         bounded Python-facing handles/results
schema_context.c/.h        request connection tenant context
```

Register the surface through `_postgresmodule.c`. Reuse existing PostgreSQL
buffer, decode, operation, plan, protocol, slab, and tape primitives rather than
building a second protocol stack.

### Packed images

Production images use immutable packed arrays and interned IDs, not Python object
graphs. Prefer structure-of-arrays where scans touch only a subset of fields.
Every image has:

```text
format_version
mode
object counts and checked byte lengths
string arena
schema/table/column/constraint/index arrays
stable object IDs
canonical-order index
fast fingerprint + algorithm version
```

All offsets and count multiplications are overflow checked. Arenas have explicit
configuration limits. Malformed artifacts or server results fail before partial
image publication.

The desired image is compiled once from registry metadata and retained by a native
handle. Python inspection constructs records lazily and only on request.

### Tenant directory

Compile the application-owned directory into an immutable snapshot:

```c
typedef struct {
    uint64_t tenant_key;
    uint32_t schema_id;
    uint32_t role_id;
    uint32_t generation;
    uint32_t quoted_schema_offset;
    uint32_t quoted_role_offset;
    uint32_t flags;
} WreathTenantEntry;
```

Schema and role strings are validated and quoted once when the snapshot is built.
The request and migration paths carry IDs/offsets, never repeatedly hash or quote
names. Snapshot replacement is atomic; in-flight work retains its original
snapshot generation.

## Request tenant context

Tenant identity is authenticated before ORM session creation and resolved against
the immutable directory. The native connection operation performs, in order:

```text
BEGIN
SET LOCAL ROLE <prevalidated role>       # role mode only
SELECT pg_catalog.set_config(
    'search_path',
    'pg_catalog,<tenant>,<central>',
    true
)
ReadyForQuery verification
```

Setup is one non-interleavable native operation/tape. Tenant SQL cannot be queued
until context readiness is confirmed. Session success commits; failure or
cancellation rolls back. A connection returns to the pool only when PostgreSQL is
idle and native state confirms no context/setup/cancellation residue. Otherwise it
is discarded.

Tenant ORM table SQL and query shapes are compiled once and shared. Sessions,
identity maps, hydrated objects, and connection context are never shared. Prepared
plan reuse across schema switches is enabled only after real PostgreSQL matrix
tests prove result-layout safety; otherwise the compiled policy deallocates or
namespaces tenant-sensitive plans.

## Artifact format

Each migration directory contains:

```text
0002_add_order_status/
  migration.bin
  migration.json
  up.sql
  metadata.json
```

`migration.bin` is the bounded canonical metal input. `migration.json` and
`up.sql` are deterministic review views generated from the same operation tape.
Regeneration must be byte-identical. `metadata.json` contains environment,
version gates, explicit rename hints, and approvals.

An artifact records:

- format and fingerprint algorithm versions;
- migration ID, parent checksum, and cryptographic checksum;
- source and target template fingerprints;
- central/single/tenant target kind;
- operation dependencies and safety classes;
- transactional, non-transactional-resumable, and manual segments;
- supported PostgreSQL version range;
- bounded validated schema identifier slots.

Loading an artifact never imports or executes code. Renames require explicit stable
IDs/hints. Destructive/manual operations require recorded approval.

## Resolution algorithms

### Non-tenant resolver

`wreath_migration_resolve_single()` performs:

1. verify artifact chain once and retain its native handle;
2. fetch the single history head and last verified fingerprint;
3. if managed state exactly matches the requested target, return current;
4. otherwise batch-decode the selected schema catalog into one actual image;
5. compare the versioned fingerprint;
6. on mismatch, run the native sorted merge diff and dependency order;
7. return a packed plan handle or a bounded blocked/manual result.

The function has no tenant mode branch and allocates no tenant vector.

### Managed fleet resolver

`wreath_migration_resolve_fleet()` performs:

1. bulk-read trusted directory IDs plus compact migration history in bounded pages;
2. decode directly into a packed tenant-state vector;
3. classify each row as `CURRENT`, `APPLY`, `VERIFY`, `AMBIGUOUS`, or `BLOCKED`;
4. emit current counts without catalog reads;
5. introspect only `VERIFY`, unknown, ambiguous, policy-sampled, and source-mismatch
   entries in bounded schema chunks;
6. partition verified actual images by versioned fingerprint;
7. compare canonical images before treating a fingerprint group as equivalent;
8. diff each distinct verified image once;
9. attach each tenant index to an immutable shared operation tape;
10. return one packed execution plan.

History is a speed authority only in managed mode and only when migration ID,
artifact checksum, target fingerprint, directory generation, and successful
verification state all match. Any uncertainty moves a tenant to verification; it
never silently becomes current.

### Strict fleet resolver

Strict mode streams all selected schema catalogs in configured chunks. One giant
`pg_catalog` result for an unbounded fleet is forbidden. Native decode builds
per-schema images, fingerprints them, partitions them, and releases chunk storage
as soon as durable plan/group state permits.

Chunk size is benchmarked and configurable. Results report catalog rows, chunks,
wire time, decode time, grouping ratio, and peak arena bytes.

## Runner

The runner uses only a dedicated migration connector/pool. It never borrows request
pools implicitly.

Central migrations execute and verify before tenant migrations. Each active tenant
owns one migration connection and one lock. A compiled transactional protocol tape
contains, where PostgreSQL semantics permit:

```text
BEGIN
versioned advisory transaction lock
history/source recheck
transaction-local schema context
DDL tape
selected target verification
history success checkpoint
COMMIT
```

Dependent messages may be pipelined only when protocol and failure semantics are
proven. Non-transactional operations use explicit pre/post probes and resumable
checkpoints; they never masquerade as transactional.

The native coordinator:

- consumes packed tenant indexes without Python iteration;
- reuses one immutable operation tape per drift group;
- maintains bounded ready/in-flight/completed queues without front deletion;
- stops new submissions promptly on cancellation or failure policy;
- adapts concurrency downward for lock wait, latency, pool pressure, or failures;
- records each tenant independently;
- returns aggregate counts and at most `max_failures` bounded details.

Possible outcomes are:

```text
CURRENT
APPLIED_AND_VERIFIED
ROLLED_BACK
FAILED_KNOWN_STATE
AMBIGUOUS_REQUIRES_INSPECTION
BLOCKED_POLICY
```

An ambiguous or protocol-dirty connection is discarded. Resume always rechecks
history and ambiguous live state before issuing DDL.

## History model

Central history stores immutable artifact definitions and compact tenant state.
The hot fleet query must return fixed/bounded columns suitable for direct native
decode:

```text
migration definitions:
  migration_id, parent_checksum, checksum, target_kind,
  source_fingerprint, target_fingerprint, status, applied_at

tenant state:
  tenant_key, schema_id, directory_generation, migration_id, checksum,
  observed_fingerprint, status, attempt, applied_at, bounded_error_code
```

Do not store DSNs, secrets, unbounded SQL, or raw unbounded server errors. Lock keys
come from versioned stable native hashes, never `hash()`.

## Implementation sequence

Every slice follows red → minimal green → focused regression → refactor → measured
evidence. Missing native symbols must produce real failures, not skips or `xfail`.
Use `update_feature_tdd` at each transition.

### Slice 0 — policy, baseline, and unsupported surface

- Accept an ADR replacing validate-only ORM policy and recording metal-only support.
- Add imports/configuration with an explicit unavailable-metal error.
- Record current fixed-schema SQL, ORM request, PostgreSQL decode, and raw catalog
  query baselines.
- Add benchmark fixtures for one schema and generated 1K/10K tenant histories.

Exit: old applications remain byte-identical; metal-only support is explicit.

### Slice 1 — logical schemas and specialized registry compilation

- Add `CENTRAL_SCHEMA`, `TENANT_SCHEMA`, `SchemaMode`, and relationship validation.
- Compile fixed/single/isolated table references at startup.
- Split template and deployment fingerprints.
- Prove the single-schema compiler emits byte-identical qualified SQL.

Exit: one selected table-render function is embedded in compiled query metadata;
no per-query schema-mode branching is introduced.

### Slice 2 — immutable directory and connection context

- Build atomic native tenant-directory snapshots with prequoted identifiers.
- Add native connection context state and one setup/cleanup tape.
- Add real PostgreSQL A/B switching, role denial, cancellation, pool cleanup, and
  prepared-result-layout tests.
- Bind one request session to one immutable context generation.

Exit: no role/path/transaction/result state can cross tenants.

### Slice 3 — packed image compiler and artifact parser

- Define versioned bounded binary layouts.
- Compile registry intent directly into a native desired-image handle.
- Parse/checksum immutable artifacts without Python operation objects.
- Add malformed-size/offset fuzz tests and deterministic JSON/SQL views.

Exit: repeated image/artifact use performs no Python materialization.

### Slice 4 — non-tenant resolver

- Add direct history and catalog decode destinations.
- Implement fingerprint, canonicalization, merge diff, DAG ordering, and the
  branch-free single resolver.
- Cover columns, PK/unique/FK/check constraints, indexes, identity/generated
  columns selected for v1, explicit renames, and manual unsupported objects.

Exit: current, drifted, destructive, corrupt, and version-mismatch cases are fully
resolved by one metal invocation.

### Slice 5 — managed fleet resolver

- Decode bulk history into packed tenant vectors.
- Implement classification, bounded diagnostics, fingerprint partitioning, and
  one shared plan per verified drift group.
- Prove an all-current managed fleet performs no tenant catalog query.
- Test 0/1/1K/10K tenants without Python-per-tenant activity.

Exit: memory is bounded and all-current resolution approaches the history-query
wire/decode floor.

### Slice 6 — strict catalog streaming

- Add bounded multi-schema catalog queries and chunk lifecycle.
- Canonicalize/group/diff actual images without retaining the whole fleet.
- Test external drift, dropped/renamed schemas, duplicate fingerprints with
  canonical verification, cancellation, and arena limits.

Exit: strict mode is exact for supported objects and bounded by chunk settings.

### Slice 7 — generator and single runner

- Generate deterministic binary/JSON/SQL artifacts from native operation tapes.
- Add dedicated authority, history bootstrap, lock/source/checksum checks,
  transactional and resumable segments, target verification, and cancellation.
- Add process-loss and PostgreSQL-restart tests.

Exit: one reviewed migration applies once or returns a precise recoverable state.

### Slice 8 — native fleet coordinator

- Implement grouped immutable tapes, adaptive scheduler, per-tenant locks/history,
  progress aggregation, bounded failures, and resume.
- Test mixed current/drifted/failed/ambiguous fleets, two competing runners,
  max-failure policies, connection loss, tenant churn, and PostgreSQL restart.

Exit: successful tenants are independently durable and never repeated on resume.

### Slice 9 — startup, CLI, docs, and hardening

- Add literal `detect`, `generate`, `check`, `apply`, and `status` commands.
- App inspection starts only owned migration/database control resources, not ASGI
  lifespan services.
- Add startup readiness gates, numeric Flight Recorder events, authorized Inspector
  summaries, reference/guide/recipes, support matrix, and recovery runbook.
- Run sanitizers, free-threaded tests, native lints, full checks, strict docs, soak,
  and fault injection.

Exit: operational policy and evidence are published; startup still never applies
DDL implicitly.

## Focused test and benchmark files

```text
tests/orm/test_schema_modes.py
tests/orm/test_tenant_compiler.py
tests/orm/test_tenant_session.py
tests/postgres/test_schema_context.py
tests/postgres/test_schema_context_integration.py
tests/migrations/test_metal_image.py
tests/migrations/test_metal_artifact.py
tests/migrations/test_metal_resolver_single.py
tests/migrations/test_metal_resolver_fleet.py
tests/migrations/test_metal_catalog_stream.py
tests/migrations/test_runner_single.py
tests/migrations/test_runner_fleet.py
tests/migrations/test_runner_failures.py
benchmarks/bench_migration_resolution.py
benchmarks/bench_tenant_orm.py
```

Native differential tests may use a deliberately small Python oracle over JSON
fixtures. The oracle is test-only and need not support networking, scheduling,
artifacts, or the public API.

## Benchmark gates

Record PostgreSQL version/configuration, Python/native build, CPU/governor, memory,
pool size, tenant count, schema size, chunk size, concurrency, and raw repeated
samples. Establish interleaved A/A noise and use ablation rather than cProfile.

Required cases:

```text
single: current, one drift, large schema
fleet: 1K / 10K / 100K all-current managed
fleet: managed verification sample
fleet: strict all-current
fleet: one drift group / several groups / all distinct
runner: all apply / partial failure / cancel / resume
request: central / tenant read / tenant write / rapid A-B switching
```

Report:

- raw query/wire time and complete resolver time;
- rows and tenants per second;
- p50/p95/p99/p999;
- CPU, cycles, allocations, and peak RSS;
- native/Python crossings;
- catalog chunks/rows and grouping ratio;
- lock wait, pool pressure, pipeline round trips, and connection discards;
- returned tenant markers and verified catalog fingerprints.

Acceptance requires:

- zero Python crossings proportional to tenant or operation count;
- single current and managed all-current overhead above the raw PostgreSQL floor is
  below measured noise or a separately accepted, quantified budget;
- native compute improves complete representative resolution beyond noise—not just
  a microbenchmark comparator;
- peak memory follows configured chunks/concurrency;
- no throughput result is accepted unless tenant markers and final fingerprints
  prove equivalent correct work;
- concurrency is not raised when it reduces throughput or harms PostgreSQL tails.

## Correctness and security invariants

- Tenant SQL cannot run without a ready trusted context.
- Central SQL is qualified in isolated mode.
- Request credentials cannot perform migration DDL.
- Managed history never overrides checksum, generation, ambiguous-state, or sampled
  verification failures.
- Strict mode never substitutes history for requested catalog inspection.
- Fingerprint equality authorizes grouping only with matching format/algorithm and
  canonical-image verification where live images are available.
- Source mismatch stops apply; successful apply ends with target verification.
- Cleanup failure discards the connection.
- No operation crosses tenant contexts or directory generations.
- Artifact input, catalog input, diagnostics, queues, and arenas are bounded.
- Concurrent/non-transactional DDL has explicit pre/post state and resume rules.
- Raw SQL remains available under active database privileges; SQL scanning is not
  treated as a security boundary.

## Stop conditions

Stop and redesign the affected slice if:

- the resolver needs a Python call or object per tenant/operation;
- all-current managed fleets require catalog scans without an explicit audit policy;
- a full fleet must reside in memory to resolve or execute bounded chunks;
- shared prepared tenant SQL fails supported-version result-layout tests;
- pipeline batching makes cancellation or result state ambiguous;
- a fast fingerprint can authorize an apply without checksum/canonical safeguards;
- measured scheduler concurrency overloads PostgreSQL catalogs or locks;
- native complexity requires weakening `wreath-native-lint` rather than a bounded,
  reasoned in-place waiver;
- request tenancy breaks ASGI framework/server separability.

## Non-goals

- Pure-Python or wreath-native production compatibility for this feature.
- Cross-database migrations or distributed coordination beyond PostgreSQL.
- Automatic tenant creation from request traffic.
- Cross-tenant ORM relationships or shared model objects.
- Similarity-based rename inference or automatic destructive rollback.
- Arbitrary Python migration modules.
- Silent startup DDL.
- Pretending namespace selection is hostile-tenant security.
- Claiming PostgreSQL DDL duration is eliminated by a fast resolver.

## Completion definition

The feature is complete only when both specialized resolvers, the runner, request
context, artifacts, CLI, docs, recovery guidance, and benchmark evidence ship
together; all correctness gates pass; and the measured all-current fleet path is
close to the PostgreSQL wire/decode floor with no tenant-proportional interpreter
work.
