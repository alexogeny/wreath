---
description: Tenant isolation the database enforces — a role and a grant set per tenant, a central schema every tenant reads and none may write.
keywords: multi tenant, multi-tenancy, tenant isolation, schema per tenant, row level security, saas, tenant, shared schema, cross-tenant
---

# Multi-tenancy

The failure this guide is about has one shape: a query that reads the wrong
customer's rows. It is rarely dramatic — a debug query somebody qualified with a
schema name, a background job that lost its tenant, an admin console that
interpolated a URL segment — and it is always the same outcome.

Wreath's answer is that **the boundary is a PostgreSQL role**, and everything
else is ergonomics.

## Why `search_path` is not the boundary

Binding `SET LOCAL search_path = tenant_acme` is isolation by *naming*. It
decides where an **unqualified** name resolves, and that is all it does:

```sql
SELECT * FROM item                  -- resolves in tenant_acme. Fine.
SELECT * FROM tenant_globex.item    -- resolves in tenant_globex. Also fine!
```

Nothing about the second line is an error. A search path has no opinion on a
name that is already qualified, so one `RawQuery`, one migration helper, one
copy-pasted diagnostic is a cross-tenant read — and you cannot audit your way
out of it, because the thing you would have to audit is every query anyone ever
writes.

A role is different. A tenant's role holds privileges on its own schema and
`SELECT` on the central schema, and holds **nothing at all** on any other
tenant's. The second line above is then:

```
ERROR:  permission denied for schema tenant_globex
```

whatever the query said, whoever wrote it. And the audit is a catalog query
rather than a code review — [`isolation_report`](../reference/tenancy.md) is
that query.

Both halves ship. The search path is what keeps tenant-local SQL unqualified and
readable; the grants are what make it safe.

## Getting there

```python
from wreath.tenancy import (
    InMemoryTenantDirectory, Tenancy, TenancyMiddleware, TenantHostLabel,
)

tenancy = Tenancy(
    directory=directory,
    source=TenantHostLabel("example.com"),   # acme.example.com -> "acme"
)
app.add_global_middleware(TenancyMiddleware(tenancy))
```

**`source=` has no default**, deliberately. Guessing at a subdomain is how a
service that was never multi-tenant on its apex starts resolving `www` as a
customer. Three are supplied — [`TenantHeader`](../reference/tenancy.md),
`TenantHostLabel`, `TenantSessionClaim` — and the last is the strongest, because
the name comes from state the server wrote and a caller cannot name a tenant at
all.

**A source *names* a tenant; the directory *finds* it.** Those are separate on
purpose: a header used directly as a schema is a header that can name any schema
in the database, while a header used as a key into a directory can only ever
name a tenant somebody provisioned. A miss is `UnknownTenant`, never a fallback.

Then bind sessions declaratively:

```python
from typing import Annotated
from wreath.orm import FromORM, Session
from wreath.tenancy import FromTenant

TenantSession = Annotated[Session, FromORM("main", tenant=FromTenant())]

@router.get("/items")
async def list_items(request: Request, session: TenantSession) -> ItemPage: ...
```

`FromORM(tenant=…)` is the piece that matters most. Before it the only route to
a tenant-bound session was constructing one by hand in the handler body — so the
spelling the guides teach produced an *unbound* session and the safe path was
the one you had to remember. A tenant-isolated registry bound without a tenant
is now refused at **route-compile time**, naming the spelling that fixes it.

## Provisioning

```python
tenant = await provision_tenant(
    connection, key="acme", central="central", login_role="app",
)
# ... apply your migration artifact to tenant.schema, then mark it ACTIVE
```

One call issues the schema, the role, the grants, and the `ALTER DEFAULT
PRIVILEGES` that keeps them true. That last one is what stops the grant set
drifting: without it every migration that adds a table adds one the tenant
cannot read, found by a 500 rather than by the deploy.

It is idempotent throughout, so a run stopped by a lock or a deploy is finished
by running it again — the property
[`apply_fleet`](../reference/migrations.md) already holds for the fleet.

A new tenant is returned `PROVISIONING`, not `ACTIVE`. The schema existing and
the artifact having been applied are different facts, and `require_bindable()`
refuses the gap rather than letting a half-migrated tenant answer a request with
a missing-relation error deep inside a handler.

## The central schema

The case that makes isolation worth building rather than avoiding: tables every
tenant reads and none may write — an immutable role vocabulary, a country list,
a plan catalogue.

```sql
SELECT i.name, p.name FROM item i JOIN central.plan p ON p.id = i.plan_id
```

One statement. `SELECT` for every tenant role, no write privilege for any of
them, and it stays in the search path behind the tenant's own schema. If a
central reference cost a second round trip and an application-side join, nobody
would use it and the vocabulary would be copied per tenant — which is the
outcome a shared schema exists to prevent.

## What this stops, and what it does not

Stated precisely, because a security claim that is nearly true is worse than
none.

**Stopped.** Ambient cross-tenant access. The application's login role is
`NOINHERIT` and holds membership of each tenant role *without inheriting it*, so
outside a `SET LOCAL ROLE` it has no privilege on any tenant schema. That is what
makes `RESET ROLE` a dead end, and what makes a lost binding fail closed rather
than fail open.

**Refused at startup.** `verify_isolation` will not let the application connect
as a superuser, as a role that inherits, or as one that owns tenant schemas.
None of those can be compensated for by any grant set — an owner's privileges
are implicit and cannot be revoked from itself — so they are refused where
somebody can act on them rather than discovered as a breach.

**Not stopped: a deliberate `SET ROLE other_tenant`.** Membership is what lets
the pool switch roles at all, and PostgreSQL has no switch-once mode. Nothing
wreath emits does this, and `hardening` finds the source-level shape of it, but
it is a residual. Removing it means a connection whose login role *is* the
tenant's — see [what that costs](#what-a-connection-per-tenant-costs) below,
which is less than this guide first claimed.

**Not stopped: table names.** `pg_catalog` is readable by every role. Revoking
`USAGE` stops a peer *reaching* an object, not *seeing* that it exists, and no
grant hides a row of `pg_class`. Data does not leak; names do. If the names
themselves are confidential — a schema named after the customer is how this
bites — the answer is a database per tenant.

## What a connection per tenant costs

The residual above disappears if a connection cannot switch roles at all —
because its login role is the tenant's own and it is a member of nothing. That
was described as expensive before it was measured. Measured, against PostgreSQL
17:

| | per connection |
| --- | --- |
| application memory | 162 KiB |
| **database server memory** | **~15 MiB** |
| setup | 4.19 ms |

**The cost is not in your application**, which is where the guess put it. It is
server RAM and the `max_connections` ceiling — and both multiply by worker
count, which is the half people forget:

```python
budget = connection_budget(tenants=200, workers=4, max_connections=200)
require_connection_budget(budget)
```

```
TenancyError: connection-per-tenant isolation does not fit this deployment:
200 tenants x 4 workers x 1 connections = 800 backends against
max_connections=200; roughly 11.7 GiB of database server memory at ~15 MiB per
backend (an estimate that moves with work_mem and shared_buffers). Raise
max_connections, reduce workers, or use isolation='role' ...
```

So: **affordable for tens of tenants, not for thousands.** Twenty tenants across
four workers is eighty backends and about 1.2 GiB — a reasonable price for
removing the residual entirely. Two hundred tenants is not.

The refusal names the arithmetic rather than the conclusion, because raising
`max_connections`, cutting workers, and staying on role isolation are all valid
answers and none of them is wreath's to choose. The memory figure is reported as
an estimate because it genuinely is one — it moves with `work_mem` and
`shared_buffers`.

## Background work

A job enqueued inside a tenant scope inherits that tenant:

```python
with tenant_scope(tenant):
    await runner.enqueue("rebuild_index")     # carries tenant.key
```

`JobRunner.enqueue(tenant="")` defaults to empty, and an empty tenant on a
worker three hours later reads the wrong schema with no request left to
attribute it to. Inside a scope the scope wins; an explicit `tenant=` that
*contradicts* the scope is refused, because one of the two is a bug and nothing
here can tell which.

The same scope feeds `cedar_context()` — so a policy can say "this tenant's plan
permits it" — and `telemetry_attributes()`, so "which tenant is slow" is
answerable. The tenant key is a separate Cedar fact from
`context.organizations`: an organisation is who you are acting as, a tenant is
where the rows live, and a deployment can have either without the other.
