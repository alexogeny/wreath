# From FastAPI and Alembic to Wreath-metal migrations

A SaaS migration is not only a change of web framework. Your database history,
tenant boundaries, deployment locks, and recovery procedure already carry years
of decisions. Wreath does not ask you to throw those decisions away. It gives
them a new, explicit home—and moves fleet-wide readiness resolution into one
packed native operation instead of one Python orchestration loop per tenant.

This page is the deep end: schema architecture, readiness policy, and fleet
resolution. For the day-one translation of the rest of your stack — routes,
dependencies, Pydantic models, SQLModel sessions, and the Alembic command
mapping — start with [Coming from FastAPI](../from-fastapi/index.md).

!!! warning "Implementation status"

    Logical schema modes, the first Wreath-metal managed-fleet resolver, direct
    catalog-row decoding into packed schema images, native image diffing, and
    checksummed artifact verification are available.
    Catalog-driven generation and DDL application are not released yet. Keep
    Alembic as the DDL authority until the Wreath runner ships; do not replace a
    working production migration path with an unfinished one.

## What changes

A typical FastAPI SaaS application combines FastAPI request handling, SQLAlchemy
models, Alembic revisions, and an application-specific tenant loop. Wreath's
target architecture assigns those responsibilities differently:

| Existing responsibility | Wreath destination |
|---|---|
| FastAPI routes and dependencies | `Wreath`, route binding, and `Depends` |
| SQLAlchemy models and sessions | `wreath.orm` models and request-scoped sessions |
| Alembic revision files | immutable reviewed Wreath migration artifacts |
| Per-tenant Python upgrade loop | one Wreath-metal fleet resolution and execution plan |
| Tenant schema string from a request | trusted numeric tenant-directory entry |
| Startup `upgrade head` | readiness check only; DDL remains an operator action |

The last distinction matters. A server process should be able to say “this schema
is behind” without acquiring DDL authority or quietly changing production while
new workers are starting.

## Model central and tenant data explicitly

Declare the role a model plays. In a non-tenant deployment both roles can resolve
to one qualified physical schema:

```python
from wreath.orm import CENTRAL_SCHEMA, TENANT_SCHEMA, Mapped, Model, SchemaMode, column
from wreath.orm.types import Int64

class Account(Model, table="accounts", schema=CENTRAL_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)

class Order(Model, table="orders", schema=TENANT_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)

registry = app.orm(
    database="main",
    models=[Account, Order],
    schema_mode=SchemaMode.single("app"),
)
```

For a schema-per-tenant SaaS deployment, central objects stay qualified while the
tenant template compiles once for transaction-local namespace selection:

```python
registry = app.orm(
    database="main",
    models=[Account, Order],
    schema_mode=SchemaMode.isolated(
        central="wreath_core",
        isolation="role",
    ),
)
```

Choose `isolation="role"` when PostgreSQL must enforce hostile-tenant isolation.
Namespace selection alone prevents accidental crossover in Wreath; it does not
stop privileged raw SQL from naming another schema.

## Choose a readiness policy

```python
from wreath.migrations import MigrationConfig, ResolutionPolicy

migration_config = MigrationConfig(
    database="migration",
    policy=ResolutionPolicy.managed(sample_size=32),
    catalog_chunk_size=256,
    concurrency=8,
    max_failures=100,
)
```

`managed` treats checksummed, successfully verified migration history as the fast
readiness authority and sends unknown, stale, ambiguous, and sampled tenants to
catalog verification. It is intended for fleets where out-of-band DDL is
forbidden. `strict` requests catalog inspection for every selected schema and
therefore pays the corresponding PostgreSQL cost.

Migration credentials must be separate from request credentials. Request pools
should never gain `CREATE`, `ALTER`, role-management, or cross-tenant privileges
merely because a deployment also runs migrations.

## Detect live schema drift

The first catalog-backed command imports the application, starts only the
selected PostgreSQL database, and compares one resolved physical schema with its
compiled ORM intent:

```bash
wreath migrations detect app:app --database main
wreath migrations detect app:app --database main --json
wreath migrations check app:app --database main
```

`detect` reports drift and exits successfully, which is convenient for interactive
inspection. `check` performs the same metal work but exits with status 1 when drift
exists, making it the command to use in CI and deployment readiness gates.

To retain names and full before/after signatures for review, generate a named
plan:

```bash
wreath migrations generate app:app --database main
wreath migrations generate app:app --database main --json
wreath migrations generate app:app --database main \
  --output migrations/0001 --migration-id 00112233445566778899aabbccddeeff --initial
```

The native planner emits deterministic `WMP1`, matching the operation count from
the independent fixed-record image diff. With `--output`, generation writes a
metal-checksummed `migration.bin`, deterministic `migration.json`, and quoted
`migration.sql` review view. Use `--initial` only for the root migration; later
migrations require `--parent CHECKSUM`. The JSON and SQL files are presentation
only and never become execution input. Unsupported alterations are visibly marked
`MANUAL` rather than guessed. The signed `WMA1` operation tape remains the
authority. A strict status check can verify the complete parent/source chain in
metal and compare its target with both code and the live catalog:

```bash
wreath migrations status app:app migrations/0001/migration.bin \
  migrations/0002/migration.bin --database main
```

`status` exits 1 if the chain, ORM target, and catalog do not all agree. Application
is still unavailable until locking, history, transactional DDL, and post-apply
target verification land.

Catalog rows stream through the PostgreSQL field tape into a native packed image.
Wreath does not allocate one Python record per table or column. Desired metadata
is compiled once into the same canonical representation; SHA-256 fingerprints
and the linear merge diff are native. The command stops the selected database
before returning and never starts ASGI lifespan, clients, or user startup hooks.

The current detector covers ordinary/permanent tables, columns, primary keys,
per-column uniqueness, and foreign keys. Column signatures include OID, position,
nullability, identity/generated flags, and server-default text. Composite unique
constraints, full foreign-key actions/deferrability, and index coverage are still
being implemented, so `detect` is not yet a complete Alembic replacement and must
not be used as the sole production drift gate.

## Inspect an artifact

Wreath artifacts are bounded binary inputs to the metal engine. The CLI verifies
the format, cryptographic checksum, operation-tape length, source and target
fingerprints, and parent checksum before it prints anything:

```bash
wreath migrations show migrations/0001/migration.bin
wreath migrations show migrations/0001/migration.bin --json
```

`show` does not import an application, start ASGI lifespan, connect to PostgreSQL,
or execute migration code. A checksum or structural failure exits with status 2.
The JSON form is stable machine-readable metadata for review automation; it does
not expand every operation into Python objects.

## Migrate without a flag day

Use four deployment phases:

1. **Inventory.** Export the Alembic revision graph, PostgreSQL objects Alembic
   does not own, tenant directory, role grants, and every use of raw SQL.
2. **Declare.** Introduce Wreath logical schema roles while Alembic remains the
   only DDL writer. In single mode, verify generated ORM SQL is unchanged.
3. **Observe.** Feed existing revision/checksum state into readiness reporting and
   compare its answer with Alembic and direct catalog audits. Do not apply DDL.
4. **Cut over later.** Only after Wreath artifact generation and the runner are
   released, shadow-tested, and supported by a recovery drill should an accepted
   deployment decision transfer DDL authority.

The practical checklist is in
[Move a schema-per-tenant SaaS application from Alembic](../cookbook/recipes/fastapi-alembic-saas.md).

## Read the benchmark honestly

`wreath-bench` includes a migration-resolution section in its generated
`latest.html`. The ranked microbenchmark resolves an already-current linear
history after current state is known. It compares the actual Wreath-metal packed
resolver with Alembic's revision resolver and Django's migration graph. It does
**not** include catalog I/O or DDL, and the Wreath-only fleet row is not ranked
against tools without an equivalent batch API.

That narrowness is intentional: the report may show control-plane overhead, but
it must never imply that PostgreSQL locks, WAL, or table rewrites disappeared.

**Reference:** [`wreath.migrations`](../reference/migrations.md).
