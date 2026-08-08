---
description: A cross-tenant operator console that binds each tenant, never grants more than the user held, and confirms what it destroys.
keywords: operator console, platform admin, support tool, impersonation, view as user, suspend tenant, back office, sre
---

# The operator console

[`wreath.admin`](admin.md) is the customer-facing back office over registered
models. `grep -c tenant src/wreath/admin.py` returns zero, and it is not the
thing anyone needs at three in the morning.

What they need is the other console: every tenant's migration state, dead
letters, stalled passes and quota burn on one page, with the actions to suspend,
retry or deprovision. Every input already existed — `resolve_fleet`,
`wreath jobs list`, `wreath passes status`, `metrics.collect`, the quota stores,
`doctor trace`, `audit_log` — and nothing composed them, so every deployment
writes this console by hand.

**The hand-written version always has the same three defects.** That is what
this module is shaped against.

## It reads across tenants with no binding

The obvious implementation queries with the application's own role and a schema
name interpolated from the URL segment. That is precisely the cross-tenant read
[multi-tenancy](tenancy.md) exists to prevent, reintroduced by the tool built to
supervise it.

```python
with admin.inspect("acme") as scope:
    ...   # the tenant's own context is bound for the block
```

Binding a second tenant inside one inspection is **refused**, so a join across
two customers is unexpressible rather than merely discouraged. And the binding
is released when the block ends — a console that leaked one would silently scope
every later query in that task to whichever customer was looked at last.

## Its impersonation grants more than the user held

"View as this user" is a delegation, so it is one:

```python
delegated = impersonate(
    operator="ops-1", user="u-9", scope=("read",), ttl=900,
    user_permissions=user_permissions,
)
assert delegated.permitted <= delegated.of_user     # always
```

The permitted set is an **intersection**, which makes composition-never-grants
arithmetic rather than something a policy set could get wrong. `scope` and `ttl`
have no defaults — an unscoped delegation is the user's whole authority handed
over, and a session that does not end is an account. It cannot nest, because an
operator impersonating a user who impersonates another is a chain whose
effective permissions nobody can compute.

Every impersonation reaches Cedar as `impersonated_by`, so a policy can forbid a
destructive action *under* impersonation: support reading an account is
ordinary, support deleting one on the customer's behalf is not. And the audit
row is written by the grant rather than beside it — an audit that is a second
statement is one a crash between them makes invisible.

## Its destructive actions have no confirmation

```python
deprovision_tenant("acme", confirm="acme", operator="ops-1", reason="churn")
```

Typing the name back is the only confirmation that survives becoming a habit;
[`privacy.erase`](privacy.md) recomputes its plan and refuses on a moved digest
for the same reason. Every action needs an `operator` and a `reason` — there are
no anonymous or unexplained ones.

Suspension stops **both** halves. A tenant whose requests are refused while its
jobs keep draining is one still sending email, still calling webhooks, still
spending quota — after somebody was told it was stopped.

## What it will not omit

A source that raises costs its column, not the page — the rule
[`metrics.collect`](../reference/metrics.md) already holds, because this console
is most needed when something is broken. But the source is **named**:

```
not read for this tenant: jobs -- these numbers are incomplete rather than low
```

A missing dead-letter count must not read like a healthy one. It is the same
reason `wreath doctor trace` prints what it did not search.

## Reaching it

Three explicit opt-ins, as the admin has: construct, register, include. There is
no default authorizer and `Access.public()` is refused.

The action vocabulary is `platform:`-prefixed and **disjoint** from the CRUD
one, and an organisation-scoped principal is refused whatever roles it holds.
Organisation roles are namespaced `<org>:<role>` exactly so one tenant's admin is
never another's, and a console evaluated against that vocabulary would put
`acme:admin` one policy mistake away from every customer's data.

It ships no JavaScript, so its CSP is `default-src 'none'` with no nonce, and a
write operation requires `csrf=` — `CsrfPolicy` is header-only and an HTML
form cannot carry a header.
