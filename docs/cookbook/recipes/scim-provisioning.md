# Connect an identity provider over SCIM

An enterprise customer wants their directory — Okta, Entra ID, OneLogin — to
create and remove accounts in your application without anybody touching an admin
screen. That is SCIM 2.0, and in wreath it is a router over the organisation
model you already have, not a second copy of it:

```python
from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, CedarPolicies
from wreath.organizations import Memberships, PostgresOrganizationStore, scim_router
from wreath.users import OrmUserStore, default_user_model

app = Wreath()
database = app.postgres("main", dsn=SETTINGS.database_url)
User = default_user_model()
users = OrmUserStore(session, User)
organizations = PostgresOrganizationStore(
    database, roles={"admin", "member", "billing"}
)

POLICY = """
permit(principal == User::"acme-directory",
       action == Action::"scim_read",
       resource == Organization::"acme");
permit(principal == User::"acme-directory",
       action == Action::"scim_write",
       resource == Organization::"acme");
"""

app.configure_auth(
    BearerTokenBackend(verify_directory_token),
    CedarAuthorizer(engine=CedarPolicies(POLICY),
                    organizations=Memberships(organizations)),
)
app.include_router(scim_router(app, users=users, organizations=organizations,
                               organization="acme"))
```

Here `session` is the application transaction serving the user model; include
`User` in the application's migrations. The organisation tables are discovered
through the configured authorizer and applied at lifespan startup. Neither
accepted accounts nor memberships then depend on the lifetime of the API
process.

Give the customer the base URL `https://your.app/scim/v2` and a bearer token
whose identity is the one the policy names. The directory discovers the rest
itself from `/ServiceProviderConfig`, `/ResourceTypes` and `/Schemas`.

What the directory does, and what it changes:

| Directory action | Wreath |
| --- | --- |
| provision a user | a `wreath.users` record plus a membership in `acme` |
| assign a group | that role on the membership — `context.org_roles` reads it |
| set `active: false` | disables the **account** (refused if the user is also in another organisation) |
| remove a user | removes the **membership**; the account and its content survive |

Serving several customers from one mount is a callable and a path parameter, and
the same value becomes the Cedar resource, so the tenant you read and the tenant
the policy is asked about cannot drift apart:

```python
app.include_router(scim_router(
    app, users=users, organizations=organizations,
    prefix="/scim/v2/{tenant}",
    organization=lambda request: request.path_params["tenant"],
))
```

Two things to tell the customer's administrator up front: groups are your
declared role vocabulary, so creating one over SCIM answers 501 (add the role in
your own configuration and it appears), and a filter on `externalId` answers 400
rather than an empty page, because wreath stores no `externalId` and an empty
page is how a directory decides to create everybody twice. The
[SCIM guide](../../guides/scim.md) has the reasoning behind both.
