# `wreath.organizations`

Tenancy at the identity layer: who belongs to which organisation, with which
role, and how somebody who does not yet have an account is invited into one.
Reach for this when more than one customer shares a deployment, when a role only
means something *inside* a tenant, or when an enterprise buyer asks for
directory provisioning — SCIM and SAML both land on this model rather than
inventing a second one.

It is deliberately not an ORM model you have to adopt. `OrganizationStore` is
the seam and `InMemoryOrganizationStore` is the reference implementation, in the
same spirit as `wreath.users`' pluggable `UserStore`, because organisations are
the table an existing application is most likely to already have under a
different name.

The companion piece is [`wreath.authorization`](authorization.md), where a
membership becomes a Cedar fact — `context.organizations` and
`context.org_roles` — and where a principal can be narrowed for a delegated
agent. See the [Organisations and delegation](../guides/organizations.md) guide
for the whole picture.

::: wreath.organizations
