# Native PostgreSQL tenancy, ORM, and migrations

## Status

Deprecated. Replaced by [`wreath-metal-postgres-tenancy-migrations.md`](wreath-metal-postgres-tenancy-migrations.md). Retained only as historical design context; do not implement from this plan.

This work intentionally changes Wreath's current ORM boundary: today the ORM validates live schemas and explicitly does not create, alter, or drop database objects. An accepted ADR must supersede that policy before migration implementation is released.

## Documents

1. [`native-postgres-tenancy-migrations-stage-1.md`](native-postgres-tenancy-migrations-stage-1.md) — logical central/tenant schemas, trusted tenant context, transaction-local PostgreSQL role/search-path binding, prepared-plan safety, ORM compiler/session/binder integration, and isolation guarantees.
2. [`native-postgres-tenancy-migrations-stage-2.md`](native-postgres-tenancy-migrations-stage-2.md) — desired/actual schema images, native detector and generator, immutable migration artifacts, single-schema runner, isolated-tenant runner, locking, history, cancellation, and resumability.
3. [`native-postgres-tenancy-migrations-stage-3.md`](native-postgres-tenancy-migrations-stage-3.md) — the executable red/green TDD sequence, focused test files, benchmark/evidence gates, expected repository changes, risks, and non-goals.

## Fixed architectural decisions

- Existing literal model schemas remain fixed and fully qualified.
- Logical central and tenant schema roles are added without duplicating model classes per tenant.
- Single-schema mode resolves central and tenant roles to one qualified physical schema.
- Isolated mode keeps central SQL qualified and executes shared unqualified tenant SQL only inside a validated transaction-local tenant context.
- PostgreSQL `SET LOCAL`/role cleanup is verified before a connection returns to a pool; failed cleanup discards the connection.
- Namespace isolation and tenant-role database isolation are separate documented guarantees.
- ORM query/shape plans are shared across tenants; sessions, identity maps, model objects, and connection context are not.
- Migration detector/generator has a pure oracle and optional native C acceleration.
- Migration artifacts are immutable reviewable data, not arbitrary Python modules.
- Central migrations run before tenant-template migrations.
- Tenant application uses bounded concurrency, per-tenant locking/history, target fingerprint verification, and resumable outcomes.
- Ordinary application startup checks migration readiness but never silently applies DDL.

## TDD rule

Every implementation slice first adds executable tests that fail for the missing contract, records the expected red output, implements the narrow behavior, runs focused tests to green, runs adjacent regression/parity suites, and only then benchmarks or refactors. Missing native symbols, unsupported modes, and absent public APIs must produce genuine red tests rather than skips or `xfail`.
