# Alembic and migrations

This is the page where an Alembic habit meets the biggest change in mindset, so
here is the honest status first and plainly:

!!! info "What ships today"

    Wreath ships the whole single-schema loop end to end: drift **detection**,
    CI **checking**, named **artifact generation**, checksummed **verification**,
    transactional **apply**, and — new — a first-class **downgrade** that inverts
    an artifact in metal and refuses to strand your running code. Keep Alembic
    only for schemas that use objects Wreath does not model yet (composite
    constraints, full foreign-key actions, expression/partial indexes) and for
    *fleet* execution: Wreath resolves a whole tenant fleet's readiness in one
    native call, but applies one schema at a time.

Everything below runs against a real database with a real migration credential.
Nothing here reaches for your application's request pool.

## The command mapping

| Alembic habit | Wreath today |
|---|---|
| `alembic check` | `wreath migrations check app:app --database main` — exits `1` on drift; built for CI and deploy gates |
| `alembic revision --autogenerate` (the diff) | `wreath migrations detect app:app --database main` — reports the drift, exits `0`; add `--json` for machines |
| `alembic revision --autogenerate` (the file) | `wreath migrations generate app:app --database main --output migrations/0001 --migration-id … --initial` — writes a checksummed `migration.bin` plus review-only `migration.json`/`migration.sql`; later migrations take `--parent CHECKSUM` |
| `alembic upgrade head` | `wreath migrations apply app:app migrations/0001/migration.bin --database main` — locks the schema, verifies source and target fingerprints against the live catalog, applies one metal-built DDL block in a transaction, records history |
| `alembic downgrade -1` | `wreath migrations down app:app migrations/0001/migration.bin --database main` — inverts the artifact in metal and applies it under the same guarantees, then deletes the history tip |
| `alembic downgrade base` | `down` each artifact newest-first, one at a time |
| Reviewing a revision file | `wreath migrations show migrations/0001/migration.bin` — verifies checksum, fingerprints, and operation tape before printing; exits `2` on failure |
| `alembic current` per tenant, in a loop | one native `resolve_fleet` batch — the [migration guide](../guides/migrations.md#resolve-fleet-readiness) covers it |
| `upgrade head` in a startup hook | nothing, on purpose — see below |

Two mechanical notes. `detect`, `check`, `generate`, `apply`, and `down` import
your application, start only the named database, do their work, and stop — no
ASGI lifespan, no user startup hooks, no clients. And `show` never imports an
application or touches a database at all; it verifies one immutable artifact and
prints what it proved.

## A day in the life: ship a column, then take it back

Say you are adding a loyalty programme and your `Order` model grows a column:

```python
class Order(Model, table="orders", schema=TENANT_SCHEMA):
    id: Mapped[int] = column(Int64, primary_key=True)
    loyalty_points: Mapped[int] = column(Int64, server_default="0")  # new
```

Generate the reviewed artifact, look at exactly what it will do, and apply it:

```bash
wreath migrations generate app:app --database main \
  --output migrations/0007 --migration-id 7a5c… --parent 9f2b…
wreath migrations show migrations/0007/migration.bin      # read the SQL you're about to trust

export WREATH_MIGRATION_DSN='postgresql://migration-role@db/service'
wreath migrations apply app:app migrations/0007/migration.bin --database main
# applied migration 7a5c…
```

Then the loyalty numbers come out wrong and you want the column gone. In Alembic
you would reach for `downgrade -1` and hope nothing in the running release still
reads `Order.loyalty_points`. Wreath will not let you make that mistake:

```bash
wreath migrations down app:app migrations/0007/migration.bin \
  --database main --allow-destructive
# refusing to downgrade schema 'tenant_7': the running ORM still maps 1 object(s)
# this downgrade removes or retypes, so the deployed code would dereference
# columns that no longer exist:
#   - would drop column tenant_7.orders.loyalty_points, still mapped by the ORM
```

The downgrade is *inverted from the same artifact you applied* — every add
becomes a drop, every type change flips — so it is derived, never guessed. And
before it runs, Wreath scans the reverse plan against your live ORM image in
metal and refuses if the code you are still running references anything the
downgrade would remove or retype. Roll the model change back with the release
and the downgrade goes through clean; or, when you genuinely mean it (rewinding a
local stack to re-migrate), pass `--force`.

Because a downgrade drops what the upgrade added, it is inherently destructive —
`--allow-destructive` is required, exactly as it is for a destructive upgrade,
and the approval is written to history.

## What `detect` sees — and what it doesn't yet

Detection covers ordinary tables, columns, primary keys (single and composite),
per-column and composite unique constraints, foreign keys with their referential
actions and deferrability, and single- and multi-column btree indexes (including
unique indexes); column signatures include type OID, nullability, identity and
generated flags, and server-default text. Expression, partial, covering, and
non-btree indexes are still being implemented and are emitted as `MANUAL`
operations that cannot be applied — which means the same artifact cannot be
downgraded automatically either. `wreath migrations check` is a strong extra
gate but not yet a complete `alembic check` replacement — an expression index
dropped by hand can still get past it today. Run both; they disagree in
instructive ways.

## The philosophical difference

Alembic's model is a chain of Python scripts, each free to run arbitrary code,
applied by whoever calls `upgrade`. Wreath separates three authorities Alembic's
model tends to blur:

- **The application can *ask*** whether a schema is current — cheaply, at scale,
  without DDL privileges. That is the readiness surface.
- **An artifact *proves* what it would do** — `show` verifies the checksum, the
  source and target fingerprints, and the bounded operation tape before anything
  trusts it. A migration is data to be verified, not a script to be trusted; and
  because that data carries both a before and an after for every operation, the
  *inverse* is exact — which is what makes a first-class downgrade possible.
- **Only an operator applies DDL** — with migration credentials that request
  pools never hold. `upgrade head` in a startup hook is the pattern Wreath is
  designed to make unnecessary: a booting worker should be able to say "this
  schema is behind" without acquiring the power to change it.

If you run one database, this is tidy hygiene. If you run a schema-per-tenant
fleet, it is the difference between a deploy that checks thousands of tenants in
one native batch and a Python loop holding DDL credentials while it iterates.

## Where to go from here

- [From FastAPI and Alembic to Wreath-metal migrations](../guides/migrations.md)
  — the full architecture: schema modes, readiness policies, apply, downgrade,
  and the four-phase adoption path that never needs a flag day.
- [Move a schema-per-tenant SaaS application from Alembic](../cookbook/recipes/fastapi-alembic-saas.md)
  — the step-by-step checklist, deliberately reversible.
- [`wreath.migrations` reference](../reference/migrations.md) — the exact types
  and their guarantees.
