# Organisations and delegation

A request rarely arrives as a bare identity any more. It arrives as *a person,
in one of the organisations they belong to, on a plan, sometimes through an
agent acting on their behalf*. Every one of those is a fact an authorization
decision wants to read, and every one of them is a fact an application would
otherwise resolve separately, in its own way, in a different place.

Wreath resolves them together and hands them to the policy engine it already
uses for everything else. One decision point, one vocabulary — and one law that
holds across all of it.

## The law: composition never grants

This is the sentence worth reading twice, because everything else follows from
it:

> Every operation on a principal can only **reduce** what it may do.

`member_of("acme")` does not make somebody a member of Acme. It says *"and only
in Acme"* — the organisation store decides whether they are a member at all, and
composition narrows that answer. `on_plan("pro")` does not put anyone on the pro
plan. `narrow(...)` does not hand an agent authority; it takes some away.

That is a stronger guarantee than it looks. It means no code path that builds a
principal can escalate one, so reviewing delegation is a matter of reading one
combining rule rather than auditing every caller.

## Organisations, members, roles

```python
from wreath.organizations import Memberships, PostgresOrganizationStore

store = PostgresOrganizationStore(
    app.postgres("main"), roles={"admin", "member", "billing"}
)
await store.add_member("acme", "alice", roles={"admin"})
await store.add_member("globex", "alice", roles={"member"})
```

Roles are declared up front. That is what lets a policy naming a role that does
not exist fail at startup instead of denying quietly forever, and it is the same
bargain `wreath.flags` makes.

`PostgresOrganizationStore` owns three indexed tables: organisations,
memberships, and invitations. Once it reaches `Memberships(store)` on the
configured authorizer, Wreath discovers and applies that schema during lifespan
startup. Invitation acceptance marks the invitation consumed and writes the
membership in one statement, so another worker — or a new API process after a
restart — sees both. Use `InMemoryOrganizationStore` only for tests and
single-process development where losing all three maps is intentional.

Wire the memberships into the authorizer and they become Cedar context:

```python
from wreath.authorization import CedarAuthorizer, CedarPolicies

app.configure_auth(backend, CedarAuthorizer(
    engine=CedarPolicies(POLICY),
    organizations=Memberships(store),
))
```

```cedar
permit (principal, action == Action::"read", resource)
when { context.organizations.contains(resource.owner) };

permit (principal, action == Action::"invite", resource)
when { context.org_roles.contains("acme:admin") };
```

### Why roles carry their organisation

`context.org_roles` holds `"acme:admin"`, never a bare `"admin"` — because a
bare role name cannot say *where* it applies, and an admin of one tenant is not
an admin of another. Spelling it this way makes the cross-tenant leak
unrepresentable rather than merely discouraged.

There is one convenience on top: the organisation a request is *acting in*
also contributes its roles unqualified. So `context.org_roles.contains("admin")`
— the reading a policy author reaches for first — means "admin of the
organisation this request is acting in", which is the safe meaning. The active
organisation comes from the session, and a request that has not chosen one has
no unqualified roles at all.

## Invitations

The common case is inviting an email address that belongs to nobody yet, so an
invitation is a record rather than a call:

```python
invitation = await store.invite("acme", "new@example.com", roles={"member"}, ttl=86400)
# ... send invitation.token in a link ...
membership = await store.accept(invitation.token, user_id)
```

Single-use and expiring. The in-memory implementation compares the token in
constant time; the PostgreSQL implementation looks up a fixed-width SHA-256
digest and consumes it in the same statement that writes membership.

The organisation store is only one owner in the identity lifecycle. Persist all
three production owners: accounts through `OrmUserStore` over your user model,
organisations through `PostgresOrganizationStore`, and server-side sessions
through `PostgresSessionStore` passed to `SessionPolicy`. Persist the session
signing secret outside the process too. Mixing a durable organisation store
with `InMemoryUserStore`, a memory session double, or a freshly generated secret
still logs everybody out or loses accepted accounts at the next restart.

## Delegation: an agent acting for a person

An agent should never hold a copy of its principal's authority. It holds a
*narrowing* of it:

```python
from wreath.authorization import ANY_SCOPE, human, member_of

principal = human(identity) | member_of("acme", role="admin")
delegated = principal.narrow(actor=agent_id, scope={"photos:read"}, ttl=300)

request_identity = delegated.bind()
```

`scope` is the set of route actions the delegation permits and it has no
default — a security parameter with a default gets defaulted, so "this agent may
do anything I may do" must be written as `scope=ANY_SCOPE` and survive review.

Narrowing composes. A sub-agent's delegation intersects its parent's scope and
inherits the earlier of the two expiries, so authority strictly decreases with
every hop:

```python
subagent = delegated.narrow(actor=sub_id, scope={"photos:read", "photos:write"}, ttl=600)
# scope is still {"photos:read"}; the expiry is still the parent's
```

### Why a delegate can never exceed its delegator

Scope and expiry are checked before the policy engine is consulted, so no policy
can widen them. Then, when a delegated request is authorized, wreath evaluates
the decision **twice**: once as though the person had made the request
themselves, and once with the delegation visible. The results are combined with
*and*.

That conjunction is what makes the guarantee hold for policies nobody has
written yet. Even a policy that permits only agents —

```cedar
permit (principal, action == Action::"read", resource)
when { context.delegated };
```

— cannot grant an agent something its human was denied, because the human's own
decision is still a required conjunct. The property is tested over generated
policy sets rather than hand-picked ones, and the test is verified to fail when
the conjunction is removed.

An application whose policies never mention `context.delegated` or
`context.actor` pays for only one evaluation: the second would be the same query
with the same answer, so it is skipped.

## Entitlements

A plan's entitlements are a principal fact like any other:

```cedar
permit (principal, action == Action::"export", resource)
when { context.entitlements.contains("export") };
```

Supply an entitlement provider — anything with `entitlements(identity)`, plus an
optional `names()` so a misspelled entitlement is refused at startup — and
`@requires_entitlement` stops being an `if` in every handler. Quota *enforcement*
and metering are separate concerns and live elsewhere; this is only the fact.

## What all these facts have in common

Membership, roles, entitlements, feature flags and geofence regions are one
mechanism with five declarations. Each of them:

- is **resolved once per request**, so two policies on one route cannot disagree
  about the same caller;
- is **resolved only when a policy names it**, so an application that never
  mentions organisations never pays for a membership lookup;
- **fails closed** — no provider means an empty set, and an empty set denies in
  both the `when` and the `unless` shape;
- is **refused at startup** when a policy names something the provider does not
  hold, so a typo fails where it is written.

The one deliberate exception is organisation ids. A role is configuration and can
be enumerated; an organisation id is a *row*, and refusing to boot because a
policy names a tenant nobody has created yet would refuse a correct application.

## One honest limit

The permission manifest tags its answer with these facts, so a client
revalidating an `ETag` sees a membership or entitlement change. It still does not
model *freshness* — the same optimistic-chrome property recorded for
`@second_factor` and for feature flags. A manifest can say a button is available
and the route can then answer 403. Draw chrome from it; never make a decision
behind it.

## When the directory writes instead of a person

Past a certain size a customer stops creating accounts by hand and points their
identity provider at you. [SCIM provisioning](scim.md) is that adapter, and it
writes into exactly the stores on this page — a SCIM group *is* one of the roles
you declared above — so a de-provisioning removes the row `context.org_roles`
reads rather than a row in a table of its own.

Reference: [`wreath.organizations`](../reference/organizations.md),
[`wreath.authorization`](../reference/authorization.md)
