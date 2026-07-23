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

## 5. Keep Alembic applying DDL

The Wreath artifact generator and DDL runner are not released yet. Continue to
run your reviewed Alembic deployment job. Application startup may reject or warn
about an outdated schema, but it should not call `upgrade head`.

## 6. Prepare the eventual cutover

Before transferring authority, require all of the following:

- byte-stable Wreath artifacts generated from the intended schema;
- source and target fingerprint agreement with live PostgreSQL;
- destructive/manual operation review;
- lock, cancellation, process-loss, and resume tests;
- a role-isolation test proving tenant A cannot name tenant B's objects;
- a restore and recovery drill;
- repeated benchmark samples that verify returned tenant markers.

Until those gates exist and pass, the safe migration is coexistence—not a flag
day.

See [From FastAPI and Alembic to Wreath-metal migrations](../../guides/migrations.md)
for the architecture and configuration choices.
