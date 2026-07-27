# `wreath.store`

One keyed table, declared once. The storage discipline behind rate limiting,
idempotency, and sessions: a plain-identifier check on the table name, a
`schema_sql()` that is offered and never applied, statements prepared lazily
(a store is built before the database is up), an atomic
`INSERT … ON CONFLICT … RETURNING` claim where a returned row **is** the claim,
`clock_timestamp()` rather than `now()`, and one expiry predicate so a purge can
never drop a row a read would still honour.

Distinct from [`wreath.objects`](objects.md), which stores blobs.

::: wreath.store
