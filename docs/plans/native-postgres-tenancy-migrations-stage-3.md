# Native PostgreSQL tenancy and ORM plan — Stage 3: TDD delivery and evidence

## Status

Deprecated. Replaced by [`wreath-metal-postgres-tenancy-migrations.md`](wreath-metal-postgres-tenancy-migrations.md). Retained only as historical design context; do not implement from this plan.

Every implementation slice follows the same discipline:

1. add focused executable tests for the missing contract;
2. run them and retain the expected **red** failure output;
3. implement only enough behavior to satisfy that contract;
4. run focused tests to **green**;
5. run adjacent regression/parity suites;
6. refactor only while green;
7. benchmark only after correctness and equivalent-work integrity are established.

Do not merge placeholder tests, broad skips, implemented-behavior `xfail`, or tests that pass because the feature is absent. Native tests must assert missing symbols/behavior during the red run, then exercise the implementation after it lands.

## TDD stage 0 — freeze current behavior and introduce red schema contracts

### Exact scope

Lock current fixed-schema ORM/driver behavior and add failing tests for logical schema references, schema modes, tenant context, and migration module contracts. Record PostgreSQL/ORM baselines before changing SQL or plan ownership.

### Surfaces

```text
src/wreath/orm/model.py
src/wreath/orm/schema.py
src/wreath/orm/registry.py
src/wreath/app.py
src/wreath/migrations.py
tests/orm/
tests/postgres/
benchmarks/
```

### Red tests

Add, run, and retain failures for:

- `CENTRAL_SCHEMA`, `TENANT_SCHEMA`, `SchemaMode`, and `TenantContext` imports;
- model declarations accepting logical schema refs;
- registry exposing template/deployment fingerprints;
- single-schema resolution;
- central→tenant relationship rejection;
- tenant→central and tenant→tenant acceptance;
- `wreath.migrations` typed surface imports while still unimplemented;
- existing literal `schema="public"` behavior remaining expected.

Suggested files:

```text
tests/orm/test_schema_modes.py
tests/orm/test_tenant_declarations.py
tests/migrations/test_scaffold.py
```

The red failures should be missing imports/types or explicit unsupported configuration—not unrelated setup errors.

### Green implementation

Add frozen public/private schema records and validation only. Do not alter SQL or connections yet. Keep existing literal schemas and default `public` behavior unchanged.

### Regression and benchmark evidence

- Existing ORM declaration/compiler/session/native hydration/storage tests.
- Existing PostgreSQL suites unchanged.
- Baseline fixed-schema SQL snapshots.
- Baseline query compile/cache, one ORM read/write, valid/invalid connection lifecycle, allocations, and request crossings.

### Completion criteria

Schema contracts exist, compile deterministically, and old applications generate byte-identical SQL. All tests green. Retained baseline artifact includes Python/PostgreSQL/native build/platform metadata and repeated raw samples.

### Deferred

Tenant connection binding, unqualified SQL, migrations, native acceleration.

## TDD stage 1 — logical schema compilation and single-schema mode

### Exact scope

Teach registry fingerprints and SQL qualification about logical schema roles. Implement single-schema mode first, where central and tenant roles resolve to one physical schema and remain fully qualified.

### Dependencies

TDD stage 0.

### Red tests

Add failing cases before implementation:

- central and tenant models both compile to configured single schema;
- fixed schema remains fixed;
- select/join/select-in/insert/update/delete SQL is fully qualified;
- template fingerprint ignores physical single-schema name while deployment fingerprint changes;
- duplicate tables after role resolution fail at registry compile;
- relationships resolve after role mapping;
- native/pure ORM shape keys remain identical;
- app startup introspection receives resolved physical schemas.

Extend:

```text
tests/orm/test_compiler.py
tests/orm/test_declaration.py
tests/orm/test_native_shape_parity.py
tests/orm/test_introspection.py
```

### Green implementation

Add startup-resolved table references and split fingerprints. Route all ORM SQL through one table-render seam. Keep generated query values and hydration plans unchanged.

### Required verification

- All compiler SQL snapshots.
- Unit-of-work write ordering.
- Native hydration/storage parity.
- Query cache hit/eviction behavior.
- Fixed-schema startup validation.
- Full current ORM suite.

### Performance proof

Compare current fixed `public` mode with `SchemaMode.single("public")`. SQL, query shape, cache hit rate, allocations, and whole ORM request performance should be identical or below measured noise.

### Completion criteria

Single-schema mode is a strict generalization of current behavior. No tenant request semantics exist yet.

### Deferred

Search path, role binding, isolated sessions, migrations.

## TDD stage 2 — native/pure connection schema context

### Exact scope

Implement transaction-local schema/role binding, connection context state, prepared-plan context certification, and clean pool release. Do this below ORM first.

### Dependencies

TDD stage 1 and real supported PostgreSQL test versions.

### Red tests

Write scripted-peer unit tests and real-PostgreSQL integration tests that fail before implementation:

- `bind_schema_context()` emits BEGIN then role/path setup in exact order;
- path setup uses `pg_catalog.set_config(..., true)` with a bound value;
- role identifiers are strictly validated/quoted;
- tenant SQL before context readiness is rejected;
- context switch with outstanding/pipelined operations is rejected;
- explicit COMMIT/ROLLBACK cannot escape driver-owned context;
- commit and rollback clear context;
- failed/cancelled setup rolls back or discards;
- pool rejects/discards a non-idle or context-dirty release;
- namespace mode omits SET ROLE;
- role-isolated mode requires role;
- prepared statement created under tenant A executes against tenant B after context switch;
- result metadata/hydration OIDs correspond to B;
- repeated A/B switching, DDL change, statistics invalidation, and same-named-table provisioning remain safe;
- pure/native protocol traces match.

Suggested files:

```text
tests/postgres/test_schema_context.py
tests/postgres/test_schema_context_integration.py
tests/postgres/test_schema_plan_cache.py
```

The prepared-plan integration test must use a real PostgreSQL server. A fake peer cannot prove server reparse semantics.

### Green implementation

Add context state to pure `Connection`, native connection/protocol structures, `Plan`, and pool release checks. Add one bounded setup operation/tape. Preserve ordinary connection behavior when no context is configured.

Start with shared SQL cache because PostgreSQL documents search-path reparsing. Keep a feature/policy seam to deallocate or namespace plans if supported-version tests fail.

### Required verification

- All connection/protocol/pipeline/pool/cancellation tests.
- Transaction/savepoint behavior.
- Prepared-plan LRU and close ordering.
- Read-only workload behavior.
- Free-threaded ownership and ASan/UBSan.
- Native error/memory/complexity lints.

### Performance proof

Measure:

- context setup and teardown;
- first tenant query and warmed prepared query;
- repeated A/B tenant switches;
- shared cache versus forced invalidation/namespace fallback;
- connection discard/cancellation failure paths;
- query throughput, p50/p99/p999, plans per connection, allocations, and wire round trips.

The benchmark must verify returned tenant markers, not only timing.

### Completion criteria

A connection cannot leak role/path/transaction/prepared-result state across tenant switches. Ordinary non-tenant operations remain below noise. The supported PostgreSQL matrix records certified prepared-plan behavior.

### Deferred

ORM tenant sessions, route resolution, migration DDL.

## TDD stage 3 — isolated tenant ORM sessions and request binding

### Exact scope

Make tenant-template ORM SQL unqualified only under a bound context; keep central/fixed SQL qualified. Bind trusted request tenant context into one request-scoped session and make the context transaction the outer session transaction.

### Dependencies

TDD stages 1–2.

### Red tests

Add failing tests for:

- tenant SELECT/JOIN/INSERT/UPDATE/DELETE uses unqualified tenant tables;
- central tables remain qualified in mixed joins;
- tenant query without context fails before connection acquisition;
- context resolver result must exist in trusted directory;
- invalid schema/role IDs fail without SQL;
- one request/session cannot switch tenant;
- `Session.begin()` becomes a savepoint inside context transaction;
- session success commits and failure/cancellation rolls back;
- connection is released only after verified cleanup;
- identity maps never share objects across tenant sessions;
- raw SQL runs under current role/path;
- tenant→tenant and tenant→central relationships work;
- central→tenant declaration fails;
- route binder deduplicates one session per `(registry, workload, tenant context)`;
- missing context fails before handler activation;
- generic ASGI and native server behavior match.

Suggested files:

```text
tests/orm/test_tenant_compiler.py
tests/orm/test_tenant_session.py
tests/orm/test_tenant_binding.py
tests/orm/test_tenant_isolation_integration.py
```

Use two physical schemas containing identical tables with distinguishable rows. Role-isolation integration tests must prove PostgreSQL rejects explicit access to the other schema.

### Green implementation

Extend `Session`, `FromORM`/binder metadata, application tenant resolution, registry table rendering, and cleanup. Do not duplicate registry/query plans per tenant.

### Required verification

- Entire ORM compiler/session/binding/native hydration/storage suite.
- App auth pipeline ordering.
- Request-boundary trace.
- PostgreSQL pool/pipeline/cancellation.
- Two applications using same model classes remain isolated.

### Performance proof

Benchmark:

- central-only request;
- tenant read warmed/cold;
- tenant write/commit;
- tenant→central join;
- relationship load;
- rapid tenant switching;
- namespace and role-isolated mode;
- high concurrency across tenants.

Record context wire round trips, plan cache size/hits, allocations, native/Python crossings, throughput, CPU/request, p99/p999, and returned-tenant integrity.

### Completion criteria

First-class ORM operations work in single and isolated modes with database-enforced isolation where role mode is configured. Query/shape plans remain shared; sessions/objects/connections never cross context.

### Deferred

Migration schema IR and DDL.

## TDD stage 4 — desired/actual schema IR and pure detector

### Exact scope

Extend ORM declarations into a migration-safe desired image, batch actual `pg_catalog` data into an actual image, and implement deterministic pure diff. Continue using current startup `SchemaDiff` as the diagnostic family.

### Dependencies

TDD stage 1 schema roles; stage 3 only for tenant integration tests.

### Red tests

Build table-driven fixtures and fail first for:

- create/drop schema/table;
- add/drop/rename column with explicit stable hint;
- type, nullability, default changes;
- PK, unique, FK changes;
- composite constraints/indexes;
- FK actions/deferrability;
- index kinds supported in v1;
- generated/identity columns if declared supported;
- deterministic issue/operation sort;
- dependency ordering;
- rename never inferred without hint;
- arbitrary Python checks classified application-only/manual;
- single-schema merge of logical templates;
- separate central/tenant images in isolated mode;
- batch introspection of multiple schemas;
- pure fingerprint equality and mismatch.

Suggested files:

```text
tests/migrations/test_schema_ir.py
tests/migrations/test_introspection.py
tests/migrations/test_detector_pure.py
tests/migrations/fixtures/*.json
```

### Green implementation

Add frozen schema/operation records, declaration fields for explicit DB constraints/indexes and migration IDs/hints, batch catalog SQL, pure detector, and canonical fingerprints. Do not generate or execute DDL yet.

### Required verification

- Existing startup introspection messages/default behavior.
- ORM declaration fingerprints and native shape parity.
- Real PostgreSQL catalog fixtures across supported versions.
- No request-path change.

### Performance proof

Profile one schema, hundreds of tables, and many tenant schemas. This baseline prices Python object creation, catalog round trips, sorting, hashing, and diff. It justifies or rejects native detector work.

### Completion criteria

Pure desired/actual diff is complete for declared v1 objects, deterministic, and reviewable. Unsupported objects become explicit manual issues rather than being ignored.

### Deferred

Native detector, SQL generation, runner.

## TDD stage 5 — native detector and deterministic generator

### Exact scope

Implement native direct decode destination for catalog rows, compact schema images, fingerprinting, sorted-array diff, dependency ordering, validated identifier writer, SQL tape, and immutable artifact generation. Preserve pure parity.

### Dependencies

TDD stage 4 and retained detector baseline.

### Red tests

Add native tests that fail on missing APIs, then exercise:

- compact row decode for columns/constraints/indexes;
- pure/native image and fingerprint parity;
- every operation fixture pure/native parity;
- deterministic topological order;
- cycle diagnostics;
- identifier NUL/length/quote handling;
- tenant schema placeholder cannot accept arbitrary SQL;
- byte-identical JSON/SQL artifact regeneration;
- checksum/parent/source/target fields;
- destructive/manual safety classification;
- PostgreSQL version-specific syntax gates;
- source image too large/corrupt input bounds.

Suggested files:

```text
tests/migrations/test_native_image.py
tests/migrations/test_native_detector.py
tests/migrations/test_generator.py
tests/migrations/test_artifact_format.py
```

### Green implementation

Add native migration C units under `_postgres` or a narrowly shared extension boundary. Prefer `_postgres` for catalog decode/SQL identifier/OID integration. Python remains artifact/orchestration owner.

Do not move ordinary ORM query compilation into the migration engine.

### Required verification

- Pure/native differential property tests.
- Fuzz malformed compact images/artifacts.
- ASan/UBSan, native lints, free-threaded read-only use.
- Existing native PostgreSQL decode/hydration tests.

### Performance proof

Compare pure/native on:

- one ordinary schema;
- large central schema;
- 100/1,000/10,000 tenant metadata images;
- all-equal fingerprints;
- several distinct drift groups;
- SQL generation and artifact size.

Measure catalog round trips, rows/second, allocations, RSS, cycles, and time. Native selection requires an end-to-end detect/generate gain, not only a faster comparator.

### Completion criteria

The same inputs produce byte-identical artifacts through pure/native backends. Native has bounded memory and a demonstrated multi-schema benefit or remains unselected.

### Deferred

DDL execution.

## TDD stage 6 — single-schema migration runner

### Exact scope

Implement dedicated migration authority, central history bootstrap, advisory locking, source/checksum verification, transactional segments, explicit non-transactional checkpoints, cancellation states, target re-introspection, and CLI for one schema.

### Dependencies

TDD stages 4–5.

### Red tests

Use real PostgreSQL and scripted protocol failures:

- dedicated migration DSN required;
- request pool credentials cannot apply DDL;
- history bootstrap is idempotent;
- advisory lock excludes a second runner;
- checksum/parent/source mismatch stops before DDL;
- transactional success records verified target;
- mid-transaction failure rolls back schema and history;
- cancellation reports rolled_back or ambiguous accurately;
- non-transactional operation checkpoint/resume;
- invalid concurrent index cleanup/reporting;
- target fingerprint mismatch after apparent success fails;
- destructive/manual operation requires approval;
- CLI JSON and human output deterministic/bounded;
- app import does not start ordinary lifespan services.

Suggested files:

```text
tests/migrations/test_runner_single.py
tests/migrations/test_runner_failures.py
tests/migrations/test_locking.py
tests/migrations/test_cli.py
```

### Green implementation

Implement `wreath.migrations` public records/API, dedicated connection lifecycle, history, lock keys, operation runner, verification, and CLI. Keep arbitrary Python out of artifacts.

### Required verification

- Driver transaction/cancellation/recovery tests.
- DDL permission separation.
- PostgreSQL version matrix.
- Abrupt connection loss and process restart.
- Existing application startup validation recognizes migrated target.

### Effect proof

Correctness dominates. Measure detect→apply→verify time, lock wait, round trips, memory, and history overhead for small/large schemas. Do not claim request-path performance.

### Completion criteria

A reviewed migration applies exactly once or returns a precise unchanged/rolled-back/ambiguous state, and verified history/live fingerprint agree.

### Deferred

Tenant fan-out.

## TDD stage 7 — isolated tenant migration runner

### Exact scope

Apply one tenant-template artifact to trusted tenant schemas with central-first ordering, fingerprint grouping, bounded concurrency, per-tenant locks/history, resumability, and partial failure reporting.

### Dependencies

TDD stages 3 and 6.

### Red tests

Create many real schemas and fail first for:

- central migration always precedes tenant operations;
- target-fingerprint tenants skip;
- identical drift fingerprints diff once but checkpoint independently;
- configured concurrency is never exceeded;
- two runners cannot migrate the same tenant;
- failure in tenant A does not roll back B;
- fail-fast/continue/max-failures policies;
- cancellation stops new submissions and classifies in-flight tenants;
- restart skips successes and resumes known/inspects ambiguous states;
- dropped/renamed tenant during run is bounded failure;
- untrusted directory schema name never reaches SQL;
- history status/checksum/fingerprint per tenant;
- connection pool/role cleanup after each target;
- central→tenant invalid dependency rejected before apply.

Suggested files:

```text
tests/migrations/test_runner_tenants.py
tests/migrations/test_runner_resume.py
tests/migrations/test_runner_tenant_locks.py
tests/migrations/test_runner_tenant_security.py
```

### Green implementation

Add bounded scheduler, grouping, per-tenant lock/history, status aggregation, and CLI selectors. Reuse one operation/SQL tape with validated schema slots.

### Required verification

- 0, 1, and many tenants.
- Mixed current/drifted/failed/ambiguous populations.
- Real connection loss and PostgreSQL restart.
- Migration role versus request tenant roles.
- Memory remains bounded by concurrency and image/artifact budgets, not tenant count.

### Effect proof

Benchmark 10/100/1,000+ schemas with all-current, one drift group, and many drift groups. Record catalog rows/round trips, grouping ratio, apply concurrency, lock wait, RSS, throughput, failures, and verification accuracy.

### Completion criteria

Successful tenant state is independently durable, failures are bounded/actionable, and rerun safely converges without repeating completed work.

### Deferred

Distributed coordinator beyond PostgreSQL locks/history, cross-database tenancy.

## TDD stage 8 — integration, hardening, and policy replacement

### Exact scope

Integrate startup readiness with migration history/fingerprints, Flight Recorder/Inspector metadata, documentation, security review, soak/fault evidence, and replace old no-DDL policy with the accepted migration contract.

### Dependencies

All earlier stages selected for release.

### Red tests

Before wiring behavior, add failing tests for:

- startup refuses central/tenant template behind required migration version;
- configured warn/off modes remain explicit;
- large tenant fleets use history/fingerprint readiness policy rather than unbounded startup scan;
- Flight Recorder records numeric phase/outcome IDs without SQL/DSN/tenant strings;
- Inspector access control for migration status;
- strict docs/nav/reference imports;
- agent manifest no longer claims migrations are forbidden after implementation.

### Green implementation

Add startup gate, operational metadata, docs/reference/guide/recipes, CLI reference, and accepted ADR. Keep migration running explicit; ordinary server startup does not silently apply DDL.

### Required verification

- Long soak with tenant churn and repeated no-op checks.
- Fault injection at lock, catalog read, DDL write, commit, history, and verify points.
- Security review of roles, search path, artifact loading, identifiers, and error redaction.
- Full pytest including network/fuzz/performance marks, native lints, sanitizers, Ruff, ty, request trace, and strict docs.

### Completion criteria

Operational behavior, support matrix, recovery runbook, and evidence are published. Migrations are explicit/reviewable; server startup only checks readiness unless an operator invokes apply.

## Benchmark acceptance rules

- Record PostgreSQL version/configuration, Python/native build, platform, CPU/governor, event loop, pool sizes, tenant count, schema size, concurrency, duration, and raw trial values.
- Warm up prepared statements separately from cold measurements.
- Verify returned tenant markers and post-migration catalog fingerprints; no benchmark may look fast by querying/migrating the wrong schema or skipping work.
- Report throughput with p50/p95/p99/p999, CPU, allocations, RSS, round trips, plan count/hits/reparses, connection discards, lock wait, failures, and ambiguity.
- Establish interleaved A/A noise before performance claims.
- Use ablation, not cProfile, for native hot paths.
- Native detector/generator is accepted only when whole detect/generate improves beyond noise at representative tenant counts.
- Tenant request support is accepted only when warmed ORM workloads retain Wreath's native PostgreSQL/hydration advantage and isolation tests remain green.

## Expected files

### Add

```text
src/wreath/orm/tenancy.py
src/wreath/orm/migration_schema.py
src/wreath/migrations.py
src/wreath/_migrations_artifact.py
src/wreath/_migrations_cli.py
src/wreath/_pure/migrations.py
src/wreath/_native/postgres/migration.c
src/wreath/_native/postgres/migration.h

tests/migrations/
benchmarks/bench_tenant_orm.py
benchmarks/bench_migrations.py
```

### Change

```text
setup.py
pyproject.toml
src/wreath/app.py
src/wreath/_cli.py
src/wreath/postgres.py
src/wreath/_pure/postgres.py
src/wreath/_native/postgres/plan.h
src/wreath/_native/postgres/plan.c
src/wreath/_native/postgres/protocol.c
src/wreath/_native/postgres/connection.c
src/wreath/_native/postgres/pool.c
src/wreath/orm/model.py
src/wreath/orm/schema.py
src/wreath/orm/registry.py
src/wreath/orm/compiler.py
src/wreath/orm/session.py
src/wreath/orm/introspection.py
src/wreath/orm/fields.py
src/wreath/orm/constraints.py
src/wreath/orm/__init__.py
src/wreath/migrations.py
tests/postgres/
tests/orm/
repo-map.md
docs/agents/manifest.json
docs/reference/roadmap.md
mkdocs.yml
docs/llms.txt
```

When `wreath.migrations` becomes functional, follow `docs/cookbook/agents/documenting-a-module.md`: remove its reserved roadmap row and add reference, guide, recipes, nav, and LLM map entries.

## Major risks and stop conditions

- Shared prepared tenant SQL must be disabled if supported PostgreSQL versions cannot pass repeated cross-schema result/hydration tests.
- Search path without role isolation is not sufficient for hostile tenant isolation; documentation must not overclaim.
- Always-transactional tenant sessions add round trips. If performance is unacceptable, optimize setup batching/native loop integration after correctness; do not weaken cleanup.
- Rich schema generation can outgrow ORM declarations. Unsupported database objects must remain manual rather than guessed.
- Automatic rename inference risks data loss and is forbidden.
- Non-transactional DDL can leave partial effects; runner state must remain inspectable/resumable.
- Migrating thousands of schemas can overload PostgreSQL locks/catalogs. Concurrency defaults remain conservative and measured.
- If native detector gains are below noise outside synthetic scale, retain the pure implementation.
- Existing docs explicitly forbid migration DDL. No implementation is complete until an ADR and policy/docs replacement land.

## Explicit non-goals

- Cross-database portability or SQLAlchemy/Alembic compatibility.
- Automatic tenant creation from request traffic.
- Treating namespace isolation as database-enforced security.
- Cross-tenant ORM relationships or object caches.
- Inferring destructive operations/renames from similarity.
- Arbitrary Python execution from migration artifacts.
- Automatic rollback generation for destructive migrations.
- Silent DDL on ordinary application startup.
- Distributed coordination outside PostgreSQL advisory locks/history in v1.
- Replacing raw SQL or hiding PostgreSQL-specific semantics.

## PostgreSQL references

- [PREPARE and search-path reparse behavior](https://www.postgresql.org/docs/current/sql-prepare.html)
- [SET LOCAL transaction lifetime](https://www.postgresql.org/docs/current/sql-set.html)
- [Schema search path and trust](https://www.postgresql.org/docs/current/ddl-schemas.html)
- [CREATE INDEX and concurrent index behavior](https://www.postgresql.org/docs/current/sql-createindex.html)
- [Advisory lock functions](https://www.postgresql.org/docs/current/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS)
