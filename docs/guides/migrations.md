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

    Catalog-driven generation and locked single-schema application are available
    for the object kinds documented below. WMA1 binds operations, literal metadata,
    and metal-derived SQL as one authority. Indexes, composite constraints, rename
    hints, and tenant-fleet execution are not complete, so keep Alembic for schemas
    that use unsupported objects rather than treating partial coverage as parity.

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
`migration.sql` view. Use `--initial` only for the root migration; later
migrations require `--parent CHECKSUM`. The sole `WMA1` artifact binds all three
authoritative representations: fixed operations (`WMO1`), literal names and
signatures (`WMP1`), and dependency-ordered SQL statements (`WMS1`). Wreath-metal
re-derives both WMO1 and WMS1 from WMP1 while building and loading the artifact;
a matching operation count is not considered sufficient. Unsupported alterations
are encoded as `MANUAL` with no executable statement, which makes application
ineligible rather than inviting a guess. The exported JSON and SQL files are
review conveniences for the same bound artifact, not a second source of truth. A strict status check can verify the complete parent/source chain in
metal and compare its target with both code and the live catalog:

```bash
wreath migrations status app:app migrations/0001/migration.bin \
  migrations/0002/migration.bin --database main
```

`status` exits 1 if the chain, ORM target, and catalog do not all agree.

## Apply one authoritative artifact

Application requires a dedicated migration DSN. Wreath never falls back to the
application's request pool:

```bash
export WREATH_MIGRATION_DSN='postgresql://migration-role@db/service'
wreath migrations apply app:app migrations/0001/migration.bin --database main
```

For a destructive tape, the operator must additionally pass
`--allow-destructive`; the approval is written to history. Application performs a
fixed number of Python orchestration steps, never one call per migration operation:

1. verify the WMA1 checksum and re-derive WMO1 and WMS1 from WMP1 in metal;
2. begin a transaction and acquire a schema-specific advisory transaction lock;
3. bootstrap central `wreath_migrations.history` and verify its parent/source tip;
4. stream the locked live catalog into WMI1 and verify the artifact source;
5. execute one metal-built PostgreSQL DDL block;
6. stream the catalog again, require the target fingerprint, append history, and
   commit—or roll the whole transaction back.

Any `MANUAL` operation makes the artifact ineligible for application. Error
messages name the failing operation or field and include expected and observed
fingerprints/checksums; generic format errors are not used.

Catalog rows stream through the PostgreSQL field tape into a native packed image.
Wreath does not allocate one Python record per table or column. Desired metadata
is compiled once into the same canonical representation; SHA-256 fingerprints
and the linear merge diff are native. The command stops the selected database
before returning and never starts ASGI lifespan, clients, or user startup hooks.

The current detector covers ordinary/permanent tables, columns, primary keys,
per-column uniqueness, foreign keys, and declared single-column btree indexes.
Column signatures include OID,
nullability, identity/generated flags, and server-default text. Physical `attnum`
position is deliberately excluded because PostgreSQL leaves gaps after dropped
columns and Wreath does not mistake those gaps for schema drift. Composite unique
constraints, full foreign-key actions/deferrability, expression/partial/covering
indexes, and index-method options are still being implemented, so `detect` is not
yet a complete Alembic replacement and must not be used as the sole production
drift gate.

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

`wreath-bench` includes a migration section in its generated `latest.html`,
covering exactly what has shipped:

- **Resolution (ranked).** Resolving an already-current linear history after
  current state is known: the Wreath-metal packed resolver against Alembic's
  revision resolver and Django's migration graph.
- **Plan generation (side by side, never ranked).** Planning and rendering the
  same two-object drift: Wreath's native image diff → named plan → SQL tape
  against Alembic autogenerate. The arms do not do identical work — Alembic's
  number includes reflecting an in-memory SQLite database per call, while
  Wreath diffs images compiled once at startup — so the table states that
  asymmetry instead of pretending a ranking.
- **Artifact verification (Wreath-only, unranked).** Verifying one
  checksummed `WMA1` artifact from bytes; Alembic revision files have no
  equivalent verifiable envelope.

None of it includes catalog I/O against PostgreSQL or DDL execution — the
`apply` path needs a live database and operator credentials and is deliberately
not benchmarked. That narrowness is intentional: the report may show
control-plane overhead, but it must never imply that PostgreSQL locks, WAL, or
table rewrites disappeared.

**Reference:** [`wreath.migrations`](../reference/migrations.md).
