---
description: Operate a complex multi-tenant AI SaaS with SAML, SCIM, isolation, entitlements and audit.
keywords: multi tenant SaaS SAML SSO SCIM provisioning Cedar quotas entitlements isolation audit support
boost: 1.4
---

```hero
eyebrow: Story 05 · the difficult second eighty percent of B2B SaaS
title: Land the enterprise.
lede: One model-evaluation platform serves many companies. Each brings an identity provider, user lifecycle, limits, support expectations and private data.
signal: SAML and OIDC
signal: SCIM lifecycle
signal: database isolation
signal: platform operations
action: Follow one tenant -> #one-tenant-end-to-end
action: Browse data and identity -> ../reference/index.md#identity-policy-and-tenancy
```

## The scene

A new customer wants a private evaluation workspace for sensitive datasets. They use
SAML for sign-in, SCIM for people and groups, regional data placement, custom quotas
and an internal security role that may approve exports. Support occasionally needs to
inspect the tenant, but every such visit must have a reason.

This is not an authentication tutorial with “add multitenancy later.” The tenant is
the unit of identity, storage, policy, work, telemetry and eventual deletion.

## The moment

Remove an employee in the customer's identity provider. SCIM deactivates their Wreath
identity and their next access is refused. A background evaluation they started remains
attributed to them and bounded to the tenant. Support can inspect the problem only
through a recorded impersonation. No neighbouring tenant appears in queries, logs,
queues or counts.

> The invariant: every operation resolves exactly one tenant before touching tenant
> state, and that tenant remains explicit across requests, jobs and support actions.

## One tenant, end to end

```text
sales handoff
    └─> provision schema + role
          └─> configure SAML/OIDC
                └─> sync users + groups over SCIM
                      └─> apply policy, quota and entitlements
                            └─> operate, audit, suspend, deprovision
```

| Lifecycle concern | Wreath surface | Responsibility |
|---|---|---|
| tenant resolution | `wreath.tenancy` | host, session or explicit service binding |
| database boundary | `wreath.tenancy`, `wreath.postgres` | schemas, roles and isolation verification |
| enterprise login | `wreath.saml`, `wreath.sso` | assertion verification and provider directory |
| identity lifecycle | `wreath.organizations.scim_router`, `wreath.users` | users, groups and memberships |
| authorization | `wreath.authorization` | Cedar policy over tenant and organisation facts |
| commercial limits | `wreath.quota`, principal entitlements | plan and consumption boundaries |
| support control plane | `wreath.platform`, `wreath.audit_log` | inspection, suspension and reasoned impersonation |
| deletion | `wreath.privacy`, tenant deprovisioning | retention and erasure work |

## Build it in four acts

### 1. Provision before serving

Create the tenant's database boundary and verify it. Bind the tenant to its host and
identity-provider configuration. A request for a suspended or half-provisioned tenant
should fail before application work begins.

### 2. Let the directory own membership

Accept SAML login, map trusted attributes and provision the initial person. Add SCIM
users and groups, including patch and deactivation. Convert groups into organisation
roles deliberately rather than treating every IdP string as an application role.

### 3. Carry the tenant through work

Enqueue an evaluation, emit telemetry and publish progress. Inspect each boundary to
prove the tenant is present. Apply connection budgets and quota so one customer cannot
turn shared infrastructure into their private queue.

### 4. Operate the lifecycle

Impersonate with an operator, reason and audit entry. Suspend the tenant without
destroying evidence. Deprovision through explicit retention decisions and verify that
the database boundary is gone.

## Implement tenant resolution first

The host name is allowed to choose a directory key. It is never allowed to become a
schema name directly. The directory is the allow-list and also owns lifecycle state.

```python title="app.py"
from wreath import Request, Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, CedarPolicies
from wreath.organizations import InMemoryOrganizationStore, Memberships, scim_router
from wreath.response import JSONResponse
from wreath.tenancy import (
    InMemoryTenantDirectory,
    Tenancy,
    TenancyMiddleware,
    Tenant,
    TenantHostLabel,
    UnknownTenant,
    current_tenant,
)
from wreath.users import InMemoryUserStore

POLICY = """
permit(principal == User::"directory", action == Action::"scim_read", resource);
permit(principal == User::"directory", action == Action::"scim_write", resource);
permit(principal, action == Action::"workspace_read", resource)
when { context.organizations.contains(context.tenant) };
"""


def verify(token: str) -> Identity | None:
    if token == "directory-token":
        return Identity("directory")
    if token.startswith("user:"):
        return Identity(token.removeprefix("user:"))
    return None


directory = InMemoryTenantDirectory(
    [
        Tenant(key="acme", schema="tenant_acme", role="tenant_acme"),
        Tenant(key="globex", schema="tenant_globex", role="tenant_globex"),
    ]
)
tenancy = Tenancy(directory=directory, source=TenantHostLabel("example.test"))
users = InMemoryUserStore()
organizations = InMemoryOrganizationStore(roles={"admin", "member", "exporter"})

app = Wreath()
app.add_global_middleware(TenancyMiddleware(tenancy))
app.configure_auth(
    BearerTokenBackend(verify),
    CedarAuthorizer(
        engine=CedarPolicies(POLICY),
        organizations=Memberships(organizations),
    ),
)


@app.exception_handler(UnknownTenant)
async def unknown_tenant(request: Request, error: UnknownTenant):
    return JSONResponse({"error": str(error)}, status=404)


app.include_router(
    scim_router(
        app,
        users=users,
        organizations=organizations,
        organization=lambda request: request.state.tenant.key,
    )
)


@app.get("/tenant")
async def tenant_identity(request: Request) -> dict:
    tenant = current_tenant()
    return {"tenant": tenant.key, "schema": tenant.schema, "role": tenant.role}
```

The middleware is global because authorization and route hooks must never run before
the tenant is bound. A request for `ghost.example.test` is rejected; it cannot turn
`ghost` into a `search_path` by accident.

### Prove the boundary and the directory lifecycle

```python title="test_enterprise.py"
from wreath.testing import TestClient

from app import app, organizations

DIRECTORY = {
    "authorization": "Bearer directory-token",
    "host": "acme.example.test",
}


async def test_the_host_resolves_through_the_tenant_directory() -> None:
    async with TestClient(app) as client:
        response = await client.get("/tenant", headers=DIRECTORY)

    assert response.status == 200
    assert response.json() == {
        "tenant": "acme",
        "schema": "tenant_acme",
        "role": "tenant_acme",
    }


async def test_scim_writes_the_membership_authorization_reads() -> None:
    async with TestClient(app) as client:
        created = await client.post(
            "/scim/v2/Users",
            headers=DIRECTORY,
            json={"userName": "ada@acme.example"},
        )

    assert created.status == 201
    user_id = created.json()["id"]
    memberships = await organizations.memberships(user_id)
    assert memberships[0].organization == "acme"


async def test_an_unknown_customer_never_falls_back() -> None:
    async with TestClient(app) as client:
        response = await client.get(
            "/tenant",
            headers={**DIRECTORY, "host": "ghost.example.test"},
        )

    assert response.status == 404
```

```bash
uv run wreath test -k enterprise
uv run wreath dev app:app
```

## Add one SAML trust boundary per customer

The organisation comes from the login Wreath issued, not from the assertion that
answers it. That prevents Acme's trusted signer from minting a Globex identity.

```python title="saml_config.py"
from dataclasses import dataclass

from wreath.config import Environment, Secret, read_osenv
from wreath.sso import (
    IdentityProviderConfig,
    IdentityProviderDirectory,
    SamlServiceProvider,
)


@dataclass(frozen=True)
class SamlSettings:
    acme_saml_certificate: Secret[str]
    globex_saml_certificate: Secret[str]


settings = Environment(read_osenv()).bind(SamlSettings)

identity_providers = IdentityProviderDirectory(
    [
        IdentityProviderConfig(
            organization="acme",
            entity_id="https://acme.okta.com/saml",
            sso_url="https://acme.okta.com/app/wreath/sso/saml",
            certificates=(settings.acme_saml_certificate.reveal(),),
            roles=("member",),
            require_second_factor=True,
        ),
        IdentityProviderConfig(
            organization="globex",
            entity_id="https://login.microsoftonline.com/globex/saml2",
            sso_url="https://login.microsoftonline.com/globex/saml2",
            certificates=(settings.globex_saml_certificate.reveal(),),
            roles=("member",),
        ),
    ]
)

saml = SamlServiceProvider(
    entity_id="https://app.example.test/saml/metadata",
    acs_url="https://app.example.test/saml/acs",
    directory=identity_providers,
)


def begin_acme_login(browser_session_id: str):
    pending = saml.begin_login(
        organization="acme",
        session_id=browser_session_id,
    )
    return {
        "destination": pending.redirect_url,
        "request": saml.authn_request_xml(pending),
        "relay_state": pending.relay_state,
    }
```

Serve `saml.metadata_xml()` to the customer administrator. At the assertion consumer,
pass the raw response, `InResponseTo`, relay state, browser session id and a shared
single-use ledger to `await saml.consume(...)`. The returned assertion is verified
identity evidence; session creation and just-in-time provisioning remain explicit
application decisions.

## Replace the tutorial stores before production

The in-memory stores make the protocol easy to exercise. The production composition
keeps the same routes and policies, while moving tenant metadata, organisations,
users, replay keys and jobs into PostgreSQL:

```python title="production_stores.py"
from dataclasses import dataclass

from wreath import Wreath
from wreath.config import Environment, read_osenv
from wreath.organizations import PostgresOrganizationStore


@dataclass(frozen=True)
class Settings:
    database_url: str


settings = Environment(read_osenv()).bind(Settings)

app = Wreath()
database = app.postgres("main", dsn=settings.database_url)
organizations = PostgresOrganizationStore(
    database,
    roles={"admin", "member", "exporter"},
)
jobs = app.jobs("evaluations", database="main", concurrency=24)
```

Keep one rule visible in review: tenant-scoped work enters the queue with `tenant=`,
and workers bind that tenant before opening an ORM session. The queue stores the key
in its system schema; it does not rely on whichever `search_path` happened to be
active when the work was enqueued.

## The larger idea

“Multi-tenant” is not a column sprinkled onto models. It is a fact that must survive
every boundary crossing. Wreath makes the lifecycle impressive because the identity
protocols, policy engine, data boundary, jobs and platform console can agree on the
same tenant.

Next: [analyse the evaluations over honest time](time-series-lab.md), or
[browse the complete surface map](../reference/index.md).
