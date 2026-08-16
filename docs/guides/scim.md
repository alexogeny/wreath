# SCIM provisioning

Somewhere past a thousand seats, a customer stops creating accounts in your
application. Their identity provider becomes the source of truth: a new hire
appears in Okta or Entra ID on their first morning, lands in the right team, and
is gone from every system on their last afternoon — without anybody logging into
an admin screen. The protocol that carries all of that is **SCIM 2.0**, RFC 7643
for the schema and RFC 7644 for the wire, and it is the thing enterprise
procurement asks for by name.

It is also the feature most likely to quietly grow a second copy of your
application's identity model. A SCIM endpoint has users, and groups, and
membership; so does everything else. Build it as its own thing and you now have
two answers to *who is in this organisation* — and the one your authorization
policies read is not the one the directory writes.

So wreath's SCIM support is an adapter, in the strict sense. It has no tables.

```python
from wreath.organizations import InMemoryOrganizationStore, Memberships, scim_router

store = InMemoryOrganizationStore(roles={"admin", "member", "billing"})

app.configure_auth(backend, CedarAuthorizer(
    engine=CedarPolicies(POLICY),
    organizations=Memberships(store),
))
app.include_router(scim_router(app, users=users, organizations=store,
                               organization="acme"))
```

Point a directory at `https://your.app/scim/v2` with a bearer token and it can
list, create, update and de-provision. What it writes is what everything else
reads: a SCIM `User` is a `wreath.users` record, a SCIM `Group` is a role in the
vocabulary you declared above, and membership is
[`wreath.organizations`](organizations.md)' `Membership`. That is what makes a
de-provisioning actually take effect — the row the directory removes is the row
`context.organizations` resolves from on the next request.

## A group is a role

The one mapping worth sitting with. SCIM says a group is a named set of users;
wreath already models "which users hold which named grants inside one
organisation", and that is the same sentence. So `/Groups` serves your declared
role vocabulary, and adding a member to `admin` gives that user the `admin` role
in this organisation — which a policy reads as `context.org_roles` in the
namespaced `"acme:admin"` spelling that keeps one tenant's admin from being
another's.

Two consequences a directory administrator will notice:

- **Groups cannot be created or deleted over SCIM**, and both verbs answer 501
  with an explanation. The role vocabulary is configuration — every Cedar policy
  in your deployment names it by string — and a directory that could mint one
  would be editing your policy surface over HTTP. Add the role where the store is
  constructed and it appears in `/Groups` immediately.
- **`displayName` is immutable**, for the same reason. Renaming a group would
  rename a role your policies name.

## Authorization is your policy set, not SCIM's

Every SCIM route carries `@authorize`, and the application's own
`CedarAuthorizer` answers it. There is no allow-list inside the endpoint, no
"SCIM token" concept, and nothing to keep in step with the rest of your rules:

```
permit (principal == User::"directory-bot",
        action == Action::"scim_write",
        resource == Organization::"acme");
```

The actions are `scim_read` and `scim_write` by default — rename them with
`read_action=` and `write_action=` to match your vocabulary — and they show up in
`permissions_router`'s answer like every other declared action, because they are
read off the routes.

Building the router **refuses** if the application has no authorizer configured.
A provisioning API is the last place to discover a half-wiring on the first
request, and the alternative would have been serving your directory to anyone
holding a session.

## The two ways to de-provision, which are not the same

This is the decision to make before you turn SCIM on, because reversing it later
loses data.

| The directory does | Wreath does |
| --- | --- |
| `DELETE /Users/{id}` | removes the **membership**; the account and everything it owns survive |
| `PATCH` … `active: false` | disables the **account** — `is_active` on the user record |

The first is scoped to this organisation and is what you want in almost every
product: a person who leaves Acme should lose access to Acme's data, not have
their documents deleted, and not be signed out of a personal account that
happens to share an email address. The resource then answers 404, which is what
a directory expects of a user it removed.

The second is not scoped, because `is_active` is a property of the account.
Wreath therefore **refuses** it when the user is a member of another
organisation as well, with `scimType: "mutability"` and a detail naming how many:
one tenant's directory does not get to sign a user out of somebody else's
tenant. Use `DELETE` there — the refusal says so.

Provisioning is idempotent in the direction that matters. `POST /Users` with a
`userName` that already has an account **adopts** it into the organisation
rather than minting a second account for one email address; the same call for
somebody already provisioned here answers 409 `uniqueness`. Removing a group
member who is not in the group is a no-op rather than an error, so a directory
retrying a de-provisioning converges instead of alarming.

## Filtering, and the refusal that saves you

`GET /Users?filter=userName eq "alice@example.com"` works, along with the rest of
RFC 7644 §3.4.2.2 — `eq ne co sw ew gt ge lt le pr`, `and`/`or`/`not`,
parentheses, and value paths like `emails[type eq "work"]`.

A filter naming an attribute this provider does not hold — `externalId`,
`name.givenName`, most of what §4.1 makes optional — is answered with **400
`invalidFilter`**, not an empty page. That is deliberate and it is the most
important line in this guide: `wreath.users.UserRecord` has nowhere to put an
`externalId`, and giving SCIM its own table to hold one would be exactly the
second user store this design exists to avoid. An empty page is how a directory
concludes the user is missing and creates them a second time; a 400 is something
an operator can read. The published `/Schemas` document advertises only what is
actually stored, for the same reason.

Filters are bounded — length, nesting depth, and how many members one filtered
request will examine (`max_filter_scan`, refused with `tooMany` over the
ceiling). A filter is the one place a directory hands your process a program to
run.

## Sorting and what is not implemented

`sortBy` and `sortOrder` work for published top-level attributes and are applied
before paging. Unsupported attributes and orders are refused rather than
silently ignored. Bulk operations and `ETag` versions remain absent;
`ServiceProviderConfig` reports both as unsupported.

Reference: [`wreath.organizations`](../reference/organizations.md).
Recipe: [Connect an identity provider over SCIM](../cookbook/recipes/scim-provisioning.md).
