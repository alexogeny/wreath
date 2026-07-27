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

    Catalog-driven generation, locked single-schema application, and inverse
    downgrade are available for the object kinds documented below: tables,
    columns, primary keys (including composite), per-column *and* composite
    unique constraints, foreign keys with referential actions and deferrability,
    and single- and multi-column btree indexes (including unique indexes). WMA1
    binds operations, literal metadata, and metal-derived SQL as one authority.
    Expression, partial, covering, and non-btree indexes, rename hints, and
    tenant-fleet *execution* are not complete and are emitted as `MANUAL`, so
    keep Alembic for schemas that use them rather than treating partial coverage
    as parity.

## User story: fail CI when the models and the database disagree

> *As an API author, I keep changing my ORM models, and I want CI to fail the
> moment the models and the live schema have drifted apart — before a deploy, not
> after — without hand-writing a migration just to find out.*

```bash
wreath migrations check app:app --database main
# exits 1 when the compiled ORM and the live schema differ; 0 when they agree
```

`check` imports the application, starts only the selected database, and compares
one resolved physical schema against its compiled ORM intent in metal. Use
`detect` for the same comparison as human-readable output that always exits `0`,
and reach for `generate` / `apply` once you're ready to turn a detected diff into
a reviewed artifact. (Within the coverage in the status note above — unsupported
alterations surface as `MANUAL`, so `check` is not yet a complete drift gate on
its own.)

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

## Bind a tenant request context

An isolated registry compiles tenant-template models to *unqualified* SQL that
resolves through PostgreSQL's `search_path`. A request selects which tenant that
is by binding a `TenantContext` on its session:

```python
from wreath.orm import Session, TenantContext

context = TenantContext(schema="tenant_7", role="tenant_7_role")
session = Session(registry, "write", tenant=context)

async with session.begin():
    orders = await session.fetch(Order.select())
```

The context is bound *transaction-locally*: on `begin()` the session issues
`SET LOCAL search_path` (and, under role isolation, `SET LOCAL ROLE`) before any
tenant-template statement runs, and PostgreSQL discards the binding at
`COMMIT`/`ROLLBACK`, so a pooled connection never carries one tenant's namespace
into the next lease. Because the binding is transaction-scoped, a tenant session
requires an explicit transaction: a read outside `async with session.begin()`
is refused rather than run under whatever namespace the connection last held.

Resolve the schema and role from an application-owned tenant directory — never
from a request host, path, header, or token. `TenantContext` validates both as
unquoted identifiers, so the `SET LOCAL` statements can never carry injected
text, but only the directory can say *which* tenant a request is.

## Resolve fleet readiness

To ask, in one metal call, how a whole fleet stands against a target migration,
hand the runner your directory's per-tenant state:

```python
from wreath.migrations import HISTORY_VERIFIED, TenantState, resolve_fleet

states = [
    TenantState(tenant_id, migration, checksum, generation, HISTORY_VERIFIED)
    for tenant_id, migration, checksum, generation in directory_rows
]
result = resolve_fleet(
    states,
    target_migration=head_migration,
    target_checksum=head_checksum,
    directory_generation=current_generation,
)
# result.current / apply / verify / ambiguous / blocked — bounded counts, not
# one Python object per tenant.
```

The runner packs the directory into the native snapshot and classifies every
tenant in a single Wreath-metal invocation: a tenant is `current` only when its
trusted, verified history sits at the target for the current directory
generation; unknown, stale, or wrong-generation tenants fall to `verify`
(catalog audit), and `ambiguous`/`blocked` stay terminal. Classification reads
no database and holds no DDL authority — turning a `verify` count into per-tenant
catalog audits is a separate, explicitly privileged step.

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
5. refuse any column this artifact narrows that a chunked pass is still
   converting (see below);
6. execute one metal-built PostgreSQL DDL block;
7. stream the catalog again, require the target fingerprint, append history, and
   commit—or roll the whole transaction back.

Any `MANUAL` operation makes the artifact ineligible for application. Error
messages name the failing operation or field and include expected and observed
fingerprints/checksums; generic format errors are not used.

Catalog rows stream through the PostgreSQL field tape into a native packed image.
Wreath does not allocate one Python record per table or column. Desired metadata
is compiled once into the same canonical representation; SHA-256 fingerprints
and the linear merge diff are native. The command stops the selected database
before returning and never starts ASGI lifespan, clients, or user startup hooks.

The current detector covers ordinary/permanent tables, columns, primary keys
(single and composite), per-column and composite unique constraints, foreign
keys with their referential actions and deferrability, and single- and
multi-column btree indexes (including unique indexes). Column signatures include
OID, nullability, identity/generated flags, and server-default text. Physical
`attnum` position is deliberately excluded because PostgreSQL leaves gaps after
dropped columns and Wreath does not mistake those gaps for schema drift.
Expression, partial, covering, and non-btree indexes and index-method options
are still being implemented and are emitted as `MANUAL`; changing an existing
foreign key's action is likewise `MANUAL` (drop and recreate it). So `detect` is
not yet a complete Alembic replacement and must not be used as the sole
production drift gate.

### A pass still converting a column blocks the migration that narrows it

Step 5 is the only refusal that reads state outside the catalog. A
[chunked pass](chunked-passes.md) converting `treks.grade` publishes a fact when
its gate verifies every row is done; until then, an artifact that drops that
column or changes its type is rejected before any DDL runs, inside the same
transaction as the other four checks — so a refused narrowing leaves nothing
behind.

The reason this is a refusal rather than a warning: the DDL *succeeds*. Drop the
column while the walk is at row four million and PostgreSQL does exactly what
you asked, the migration records as applied, and the rows behind the cursor are
simply gone. There is no error to notice.

```bash
wreath migrations check app:app --database main
#   pending pass: normalize_grades guards column:app.treks.grade
#     (walking) -- a migration narrowing it is refused
```

A database that has never run a pass has no ledger table, and that is read as
"nothing is converting" rather than as an error — so this costs nothing to
applications that do not use passes.

## Downgrade an artifact

A downgrade is not a second migration you have to write and hope matches. Every
WMP1 operation already carries both its *before* and its *after* signature, so
the exact inverse is derivable in metal: `_migration_reverse_plan` flips each add
to a drop (and back), swaps every before/after, and the reversed plan re-derives
its own WMO1 and WMS1 through the same code the forward plan used. The reverse of
a create-table plan is a drop-table plan; the reverse of a type change restores
the old type. Nothing is guessed.

```bash
wreath migrations down app:app migrations/0007/migration.bin \
  --database main --allow-destructive
```

`down` is the precise inverse of `apply`. It requires the artifact to be the
current history tip, verifies the live catalog matches the artifact *target*,
runs one transactional reverse DDL block, requires the catalog to return to the
artifact *source*, deletes the tip history row, and commits — or rolls the whole
transaction back. Reapplying afterwards works exactly as before, because history
and schema are returned to precisely their pre-`apply` state. A downgrade drops
what an upgrade added, so it is destructive by nature: `--allow-destructive` is
required and the approval is recorded.

### The downgrade will not strand your running code

The danger of any downgrade is dereferencing production: you roll a column back
while the deployed release still reads it. Before it runs, `down` scans the
reverse plan against your application's live compiled ORM image — a bounded
native intersection — and refuses if the code you are running still maps any
table or column the downgrade would remove, or any column whose type it would
change:

```text
refusing to downgrade schema 'tenant_7': the running ORM still maps 1 object(s)
this downgrade removes or retypes, so the deployed code would dereference
columns that no longer exist:
  - would drop column tenant_7.orders.loyalty_points, still mapped by the ORM
Roll the models back with the code, or pass force=True (CLI: --force) …
```

Roll the model change back together with the release and the scan goes quiet and
the downgrade proceeds. When you genuinely intend to rewind ahead of the code —
rebuilding a local stack to re-migrate, say — pass `--force` to apply anyway. The
guard is on by default precisely so that a production downgrade cannot quietly
outrun the code that depends on it.

Only automatic operations reverse automatically: a forward artifact containing a
`MANUAL` operation is as ineligible for `down` as it is for `apply`.

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

## User story: rewrite ten million rows without blocking the deploy

> *As an engineer, I need `Trek.status` to stop holding numeric codes and start
> holding names. There are ten million treks. I do not want to hold a deploy for
> an hour, and I do not want to do it by hand in a psql session at 2am.*

A **deferred migration** converts the values while the application keeps
serving. [`wreath.passes`](chunked-passes.md) already does the walking — durably,
resumably, one transaction per chunk, paced so it does not starve requests. What
a deferred migration adds is the part a backfill library cannot: deciding
whether running it is *safe*.

!!! warning "The precondition, before anything else"

    **A pass converts the past. The application writes the future in the shape
    the pass is converting to.** Everything below rests on that. Deploy the code
    that writes the new shape *first*, then start the conversion.

### Where the declaration lives

Beside the model, not in a migration file:

```python
from wreath.migrations import Recode

class Trek(Model, table="treks"):
    id: Mapped[int] = column(Int64, primary_key=True)
    status: Mapped[str] = column(Text)

restyle_status = Recode(
    Trek.status,
    mapping={"1": "planned", "2": "walking", "3": "done"},
    chunk=10_000,
)
```

Wreath generates migration artifacts from the catalog rather than from authored
migration files, so there is no `@migration` decorator to hang this on — and a
declaration beside the model is the better home anyway, because the scan below
has to read it as a value.

Drive it like any other pass:

```python
jobs.drive(restyle_status.build())
```

### The window is the dangerous part, and the scan is why

While the pass runs, some rows hold `"1"` and some hold `"planned"`. A
comparison against the wrong one **does not raise** — it returns `False`. A
filter quietly drops rows. A join quietly matches nothing. A chart quietly reads
low. Nobody finds out until someone reconciles a number by hand.

So every read of a converting column is classified before the migration starts:

| Written | Verdict |
| --- | --- |
| `status == "planned"` | **rewritable** — widens to `status IN ('planned', '1')` |
| `status != "planned"` | **rewritable** — widens to `NOT IN`, which is the one people get wrong by hand |
| `status.in_([...])` | **rewritable** — every value in both encodings |
| `status >= "planned"` | **refused** — `1 < 2` but `'planned' > 'done'`; no correct transitional form exists |
| `status.like("plan%")` | **refused** — a pattern matches an encoding |
| `ORDER BY status` | **refused** — a half-converted sort is unstable between requests as the pass advances |
| `GROUP BY status` | **refused** — one group splits into two, so counts halve and a category appears to fork |
| a join on `status` | **refused** — rows vanish from an inner join, and no `where`-clause scan would catch it |
| `status == Param("s")` | **undecidable** — the value arrives with the request and could be in either encoding |

The last three are why this is not just a filter problem. Grouping and joining
are not filters, and they are worse.

`wreath migrations check` runs the scan and exits nonzero on anything unproven:

```bash
wreath migrations check app:app --database main
```

### Ordered comparison, and the mapping that happens to preserve order

An order-preserving mapping *looks* like it should license `>=`. It is still
refused, and the reason is totality rather than order: "preserves order over the
values in the mapping" is not "preserves order over the values in the table",
and a row holding an unmapped value is much of why a re-encode gets run. If you
know your mapping is total, waive the read and say so — a written sentence is
worth more than a permission inferred from an unproven premise.

### Waiving a read

Waivers attach to the read, never to the migration, because a blanket waiver is
indistinguishable from not having the feature. There is no `strict=False`.

```python
from wreath.migrations import transitional_read, waive_transitional

@transitional_read(
    Trek.status,
    reason="the admin console filters on status for display only; a row read "
           "under the old code shows a stale label for the length of the "
           "backfill and nothing downstream branches on it",
)
def recent_by_status(session): ...

# A declared query is an immutable value with nowhere to hang an attribute,
# so it is waived by its declared name instead.
waive_transitional(
    Trek.status,
    site="TrekQueries.by_status",
    reason="operational dashboard; both encodings render the same label",
)
```

Waivers are counted in `wreath migrations check` output, so "we waived
everything" is visible in review rather than discovered afterwards.

### A scan that looked at nothing is not a pass

If no declared query, model check or calculated view names the column, the scan
says so and **refuses** rather than reporting clean. An inline predicate inside a
handler is invisible to it until source-level analysis lands, and a tool that
reported success after examining nothing would be worse than no tool.

### Changing a column's *type* is a different shape

`Recode` re-encodes values in one column. Changing the type is `Retype`, and it
cannot happen in place — `ALTER COLUMN … TYPE` rewrites the table under
`ACCESS EXCLUSIVE`, which is the hour-long block being avoided. So it is the
four-step form, with two columns alive throughout:

```python
grade_to_text = Retype(
    Trek.grade,                    # the column a later migration will drop
    into="grade_next",             # added nullable by this migration's DDL
    using="CASE grade WHEN 1 THEN 'gentle' WHEN 2 THEN 'rolling' END",
)
```

There is nothing to scan here, and that is not an omission: both columns are
separately typed and correct throughout, so an unconverted row reads `NULL`
rather than reading *wrong*. Nullable-then-not is a shape the ORM already
models, and it is loud where a bad comparison is silent.

The gate proves the nulls gone using the constraint the swap migration is about
to add:

```sql
ALTER TABLE treks ADD CONSTRAINT grade_next_present
    CHECK (grade_next IS NOT NULL) NOT VALID;      -- brief lock, no scan
ALTER TABLE treks VALIDATE CONSTRAINT grade_next_present;  -- does not block reads or writes
```

Because the thing proven and the thing later enforced are the same predicate, a
walk that was subtly wrong cannot verify its own bug. When it passes, the pass
publishes a fact — and a later migration that drops `grade` is
[refused until that fact exists](#apply-one-authoritative-artifact).

### A downgrade will not silently strand your re-encoded data

A `Recode` changes values and leaves the schema alone. That is what makes it
safe to run against a live application — and what makes reverting the migration
around it dangerous in a way nothing else here catches. The reverse DDL applies
cleanly, the catalog fingerprint returns to the artifact's source exactly as it
should, every check `wreath migrations down` performs passes, and the column is
full of values the restored definition was never written for.

So the ledger records the column a re-encode rewrote, and a downgrade that would
revert that column's definition is refused:

```
refusing to downgrade schema 'app': it reverts the definition of 1 column(s)
whose values a deferred migration has already re-encoded, and reverting DDL
does not restore data:
  - app.treks.status, re-encoded by 'recode_app_treks_status', which has
    finished, so every row is in the new encoding
```

**A finished re-encode is the worse case, not the safe one.** While one is still
running the column holds a mixture, so some originals survive; once it completes
none do. The refusal therefore does not clear when the pass finishes, and unlike
the code-stranding guard above there is **no `--force`** — the schema check that
would normally catch a bad downgrade is precisely the one a re-encode slips past,
so a flag to skip this would be a flag to skip the only thing looking.

To undo one, convert the values back with a `Recode` declaring the inverse
mapping, let it finish, and downgrade after.

### The record the refusal reads, and why it cannot be tidied away

The refusal needs to know that a column's values were changed. That fact lives
in two places, and the second is the one that matters.

The pass ledger row carries it, but a ledger row is working state — a pass
rewrites it on every claim, and a "tidy up finished passes" job is a perfectly
reasonable thing to write. If the refusal read only that row, such a job would
silently re-enable the downgrade. Worse, it would be invisible: a column that
was *never* re-encoded is also the absence of a row, so there is nothing to tell
the two apart.

So the fact is also written to `"wreath".pass_rewrites`, which is append-only.
The database refuses `DELETE`, `UPDATE` and `TRUNCATE` on it:

```
ERROR:  wreath: app.pass_rewrites is append-only; a row here records that a
column's values were overwritten in place, and a downgrade reads it to refuse
restoring the old schema over the new data. Removing it does not make the
downgrade safe, only silent.
```

A downgrade reads both. If the ledger row is gone but the record survives, it
still refuses and says so — it cannot report the pass's phase, because that
lived on the row that was deleted:

```
  - app.treks.status, re-encoded by 'recode_app_treks_status', whose ledger row
    is gone -- only the append-only pass_rewrites record remains, so the pass's
    phase cannot be read. The values were still changed
```

**What this does not protect against.** Anyone who can run DDL on the schema can
`DROP TRIGGER`, `DROP TABLE`, or set `session_replication_role = replica` to
disable triggers wholesale; restoring a backup taken before the re-encode has
the same effect. The guard is aimed at routine tidying — a purge job, a cleanup
script, a well-meaning `DELETE` — not at a determined operator. Deliberate DDL
against the guard is a decision someone made; that is the difference.

The table holds one row per re-encoded column per schema and is never updated,
so it stays small: a deployment that re-encodes a column a month accumulates a
few hundred bytes a year.

!!! note "What is not built yet"

    Stages 1 and 2 have shipped: the two declarations, the pass each derives,
    the operator lattice, and per-read waivers. Rolling a completed re-encode
    back is a forward `Recode` with the inverse mapping, written by hand —
    deriving it automatically is not built. Tenant-fleet deferred migrations are
    not built either.

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
