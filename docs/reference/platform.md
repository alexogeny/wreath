# `wreath.platform`

The cross-tenant operator console. [`wreath.admin`](admin.md) is the
customer-facing back office over registered models and has no notion of a tenant
at all; this is the other one — every tenant's state on one page, and the
actions to suspend, retry or deprovision.

It composes rather than re-implements: `migrations.resolve_fleet`,
`wreath jobs list`, `wreath passes status`, [`metrics.collect`](metrics.md), the
quota stores, `doctor trace` and [`audit_log`](audit_log.md) are the inputs.

See [the guide](../guides/platform.md) for the three defects it exists not to
have.

::: wreath.platform
