# `wreath.admin`

A generated administration surface: list, detail, create, edit and delete
screens drawn from your models, gated per action *and per field* by Cedar, paged
by `wreath.pagination`, rendered by `wreath.templates`, and attributed into the
audit trail by `wreath.audit_log`.

Reach for it when the people who run your application need to look at and correct
data, and you would otherwise be writing the same five screens for the fifteenth
time. Do not reach for it as a customer-facing UI — see
[the guide](../guides/admin.md) for why that boundary matters more here than
almost anywhere else.

Everything on this page is composed from parts that already shipped. The admin
adds no storage, no second authorization path, and no JavaScript.

::: wreath.admin
