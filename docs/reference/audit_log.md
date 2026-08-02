# `wreath.audit_log`

Who changed what, recorded by the thing that does the changing.

Every hand-rolled audit trail develops holes, and always the same way: it depends
on somebody remembering to call `record()`. The call goes into the handler that
changes a row and not into the background job that changes the same row at 03:00,
and the gap stays invisible until an auditor asks a question the trail cannot
answer.

Wreath does not have to work that way. The ORM knows what it wrote and which
fields changed, and it knows it *inside the transaction that wrote them* — so a
record here is not a thing the application remembers to do, it is a thing the
write does. Declare which models are audited on the models themselves, bind an
actor to whoever is writing, and the trail fills itself in.

Named `audit_log` rather than `audit` because [`wreath.audit`](audit.md) is the
accessibility auditor. Two different things called audit is exactly the collision
the literal-API rule is meant to surface early.

Built on [`wreath.log`](log.md), which is where the cursor, the retention and the
ordering live.

::: wreath.audit_log
