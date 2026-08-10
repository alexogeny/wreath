---
description: One command that asks every check wreath can answer about a built application, and names the ones it cannot.
keywords: preflight, deploy check, startup, wiring, missing configuration, before deploy, readiness
---

# Before you deploy

Wreath refuses a lot of things, and it refuses them *loudly* — a settings key
nothing supplies, a `DestinationPolicy` that resolved with `allow_private`, a
session middleware mounted after authentication. What it does not do, on its
own, is refuse them all in one place. The answers were spread across
`wreath infra infer`, the startup hardening ruleset, and the route table, and
each of those is a command you have to know to type.

`wreath doctor preflight` types all of them:

```bash
wreath doctor preflight myapp:app --settings myapp.config:Settings --environ
```

```
preflight: myapp:app

blocking (1)
  infra      DATABASE_URL
             myapp.config:Settings requires database_url (str) and nothing supplies DATABASE_URL

advisory (1)
  routes     routes with no declared requirement
             1 route(s) declare no authentication or authorization: GET /health -- this is the
             declaration and not the enforcement: a handler may still refuse from inside, as
             crud's Access.deny() does

not checked here -- each needs something preflight does not open:
  - wreath's own tables exist in the target database -- `wreath schema check <target>` (needs a database)
  - your models match the live schema -- `wreath migrations detect <target>` (needs a database)
  ...
```

It exits `1` when anything blocks, so it is a CI step rather than something you
remember to run.

For the exact route contract rather than a diagnostic summary:

```bash
wreath doctor routes myapp:app --write route-manifest.json
wreath doctor routes myapp:app --check route-manifest.json
```

The first writes canonical, sorted JSON; the second exits `1` if the committed
file differs. It includes wire request and response shapes, stable operation
ids, dependencies, global/application/route middleware, the effective merged
access declaration, policy resources, and typed authorization-vocabulary
coverage. It contains stable qualified names rather than object reprs, so an
unchanged application compares byte for byte across processes.

## It aggregates; it does not invent

Every finding here is one another part of wreath already knows how to produce.
That is deliberate: a fourth opinion that disagreed with the other three would
be worse than no fourth opinion, and an inference that is subtly wrong is worse
than none at all, because it looks authoritative. Three sources:

| Source | What it is | Owned by |
| --- | --- | --- |
| `infra` | the gaps in the deployment plan — a settings key nothing supplies, a pool whose long-lived holders leave nothing for requests | [`wreath.infra`](infra.md) |
| `hardening` | the tier of the startup ruleset read off the live object graph, not off your files | [Hardening](hardening.md) |
| `routes` | how many endpoints declare nothing of the caller | the route table |

**Blocking versus advisory is two values on purpose.** A scale with a middle
invites a middle, and the only question this report answers is whether to
deploy. A settings key nothing supplies blocks — that is a process that starts
and then dies. A route with no declared requirement never blocks, however many
there are: a login endpoint and a health check are supposed to be public, and a
gate that fails on those is a gate that gets turned off in week one.

## What it says it cannot see

The list at the bottom is not a footer. It is the part that makes the rest safe
to read, and it follows the shape [`wreath doctor trace`](../reference/doctor.md)
already set: a report that lists three findings and stops is read as *there are
three*, so every check that needs something preflight will not open is named,
with the command that does reach it.

Preflight opens **nothing** — no socket, no database, no DNS resolver. It
imports your application and reads it. So the whole of the following is outside
it, by construction:

- **Your schema.** `wreath schema check` for wreath's own tables,
  `wreath migrations detect` for yours. Both need a database.
- **Source-level security defects.** `wreath audit code` owns that ruleset and
  reads files rather than objects. Preflight runs the *configuration* tier of
  hardening and names the other one rather than running a second copy of it —
  two spellings of one gate is how they drift.
- **N+1 queries under real traffic.** `wreath doctor n-plus-one <socket>`, which
  needs a running server. See [Finding the N+1 query](n-plus-one.md).
- **Whether mail will be delivered.** `wreath.doctor.check_email_deliverability`
  asks DNS.
- **Whether a per-worker default is right for your fleet.** This is the one that
  bites hardest and preflight genuinely cannot answer it. Sessions, idempotency,
  quotas, and second-factor challenges all default to an in-process store. That
  is correct for one worker and wrong for four — four workers admit four times
  the quota, and a WebAuthn ceremony begun on one is unfinishable on another.
  Each guide names the PostgreSQL-backed alternative; preflight can only tell
  you to go and check.

## Reading the route finding correctly

`routes` reports the *declaration*, and says so in the finding itself. A route
can ask nothing of the caller and still admit nobody: `crud`'s
[`Access.deny()`](crud.md) attaches nothing to the requirement and answers 403
from inside the handler, so `POST /admin/stations` appears in this list even
though it is the most locked-down route in the application. Calling that "open"
would be a report confidently naming the safest thing you own as the risk.

Use it as a list to read, not a list to fix. What you are looking for is the
route you did not expect to be on it.

## From Python

```python
from wreath.doctor import preflight, render_preflight

report = preflight(app, application="myapp:app")
if report.blocking:
    raise SystemExit(render_preflight(report))
```

`Preflight.findings` is every `PreflightFinding` in one tuple; `.blocking` and
`.advisory` partition it; `.unchecked` is the list above. See
[the reference](../reference/doctor.md).
