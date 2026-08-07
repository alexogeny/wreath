# `wreath.tenancy`

`wreath.tenancy` makes tenant isolation something PostgreSQL enforces rather
than something the application remembers. It supplies the tenant directory
[`wreath.orm.TenantContext`](orm.md) always required and never had, the
resolution in front of it, and the role-and-grant provisioning behind it.

See [the guide](../guides/tenancy.md) for the argument and the deployment shape;
this page is the API.

::: wreath.tenancy
