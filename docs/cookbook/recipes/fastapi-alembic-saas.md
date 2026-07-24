# Move a schema-per-tenant SaaS application from Alembic

This recipe keeps Alembic in authority while you introduce Wreath's schema model
and verify the metal resolver. It is deliberately reversible.

## 1. Freeze the existing contract

Record the Alembic head and checksum used by every tenant, export the revision
DAG, and take a strict catalog snapshot. List extensions, triggers, functions,
views, grants, and custom SQL separately; an ORM declaration may not describe all
of them.

## 2. Separate credentials

Keep three roles at minimum:

- a request role with no DDL privileges;
- tenant roles restricted to one tenant schema plus approved central objects;
- a migration role used only by the operator migration job.

Never accept schema or role names directly from a host, path, header, or token.
Resolve authenticated tenant identity through an application-owned directory.

## 3. Declare schema roles

Mark shared models with `CENTRAL_SCHEMA`, tenant-template models with
`TENANT_SCHEMA`, and archival or externally-owned schemas with their literal
physical names. Start with `SchemaMode.single()` in a staging copy when you need
to prove compatibility with an existing single-schema deployment; use
`SchemaMode.isolated()` for the schema-per-tenant target.

Reject central-to-tenant relationships. Tenant-to-central relationships are
allowed when their foreign keys and role grants make them valid.

## 4. Shadow readiness

Run Wreath readiness as an observer and compare each classification with:

- the Alembic current revision;
- artifact/revision checksums;
- tenant-directory generation;
- a sampled strict catalog audit.

Treat `VERIFY`, `AMBIGUOUS`, and `BLOCKED` as operational states, not as aliases
for “apply whatever seems necessary.” Resolve every disagreement before moving
on.

## 5. Generate and apply on one staging schema

The Wreath artifact generator and single-schema runner have shipped, so prove
them on one staging tenant before you touch the fleet:

- `wreath migrations generate app:app --database main --output migrations/NNNN …`
  writes a checksummed artifact; `wreath migrations show` verifies it from bytes.
- `wreath migrations apply app:app migrations/NNNN/migration.bin --database main`
  locks the schema, checks the live source fingerprint, runs one transactional
  DDL block, requires the target fingerprint, and records history — using a
  dedicated `WREATH_MIGRATION_DSN`, never the request pool.
- `wreath migrations down app:app migrations/NNNN/migration.bin --database main`
  reverses that same artifact in metal and refuses if the running ORM still maps
  a column the downgrade removes (override with `--force` for local rewinds).

Application startup still only *checks* readiness — it never calls the runner.
Keep Alembic authoritative for any schema that uses objects Wreath still marks
`MANUAL` (composite constraints, full foreign-key actions, expression/partial
indexes) and for **fleet execution**: `resolve_fleet` classifies every tenant in
one native call, but there is not yet a per-tenant apply loop.

## 6. Prepare the eventual fleet cutover

Before transferring authority for the whole fleet, require all of the following:

- byte-stable Wreath artifacts generated from the intended schema;
- source and target fingerprint agreement with live PostgreSQL;
- destructive/manual operation review, and a rehearsed `down` for each artifact;
- lock, cancellation, process-loss, and resume tests;
- a role-isolation test proving tenant A cannot name tenant B's objects;
- a restore and recovery drill;
- repeated benchmark samples that verify returned tenant markers.

Until those gates pass for the fleet, the safe path is coexistence—apply per
schema, keep Alembic where objects are unsupported—not a flag day.

See [From FastAPI and Alembic to Wreath-metal migrations](../../guides/migrations.md)
for the architecture and configuration choices.
