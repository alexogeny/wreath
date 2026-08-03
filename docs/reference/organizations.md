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

`scim_router` is the directory-provisioning surface, and it is the clearest
illustration of that promise: SCIM 2.0 (RFC 7643 and RFC 7644) served straight
over these stores, with no user, group or membership table of its own. A SCIM
group *is* a role in the vocabulary you declared, so a directory adding somebody
to `admin` is writing the row `context.org_roles` reads. Every route is
authorized by the application's own Cedar policies. The
[SCIM provisioning](../guides/scim.md) guide covers the mapping, the two
de-provisioning verbs, and the places where SCIM's model and this one disagree.

The companion piece is [`wreath.authorization`](authorization.md), where a
membership becomes a Cedar fact — `context.organizations` and
`context.org_roles` — and where a principal can be narrowed for a delegated
agent. See the [Organisations and delegation](../guides/organizations.md) guide
for the whole picture.

::: wreath.organizations
