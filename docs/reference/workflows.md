# `wreath.workflows`

Durable multi-step workflows — sagas. Reach for this when a single unit of work
is not one unit: when "check out" means reserving stock, charging a card and
booking a courier, and a failure at the third step has to undo the first two.

[`wreath.jobs`](jobs.md) is the right tool for one durable step. This is the
right tool when there are several, they must happen in order, and a worker can
die between any two of them.

**Reference:** the guide is [Workflows](../guides/workflows.md).

::: wreath.workflows
