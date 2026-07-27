# 0016. Validate the schema at startup; never `create_all`

Date: 2026-07-27
Status: Accepted

## Context

`create_all` is convenient and dangerous: it makes the application a schema
authority, so a deployment with an out-of-date image can create or fail to
create tables, and a production database's shape depends on which process
started first. It also makes the drift question unanswerable — the application
cannot tell "this column is missing" from "I am about to create it".

The alternative is to make the application read-only about schema and loud about
disagreement.

## Decision

The application never creates schema. At lifespan startup it **validates** the
declared models against the live catalog. `validate_schema` defaults to
`"error"` (`src/wreath/app.py:697`), with `"warn"` and `"off"` available.

Schema creation belongs to `wreath migrations apply` (ADR 0014).

## Consequences

- A deployment against a drifted database fails at startup, before serving one
  request, with the specific mismatches named.
- Startup does catalog reads, so the validation path is on every boot — which
  makes it a path that must actually work, and it did not. Two defects lived
  there: a catalog read whose error could not reach the caller, so startup hung
  forever; and, behind it, a foreign-key comparison that compared a physical
  `attnum` against a declaration index.
- That second one ran in **both** directions. A correct schema reported eight
  phantom `missing_foreign_key`s, and an FK pointing at the **wrong column**
  reported zero issues — the subsystem whose entire job is detecting drift
  silently accepted drift.
- Nothing caught either for a long time because the introspection tests drove a
  fake scripted with Python `str`/`int` rows — rows no PostgreSQL would send.
  See ADR 0020.
- The default being `"error"` means this path cannot be quietly broken again
  without breaking every application's startup, which is the property worth
  having.

## Alternatives rejected

- **`create_all` for development convenience.** Rejected: a development
  convenience that changes production behaviour is a production feature.
- **Default `"warn"`.** Rejected: a warning at startup is read once and then
  filtered, and the failure it predicts arrives as a query error much later,
  detached from its cause.
- **Validate lazily on first query.** Rejected: it moves a deployment-time
  failure into request-time, which is the wrong direction.

## What would reverse this

Nothing for `create_all`. The default could move to `"warn"` if validation ever
proved unreliable enough to block deployments spuriously — but the two defects
above argue the opposite, since it was `"error"` that eventually surfaced them.
