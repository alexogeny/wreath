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

### Which arguments are SQL

`upsert`'s `values` and `update` take a bind placeholder (`"$1"`) or an
explicitly marked `Sql(...)` fragment. A plain string is refused: column names
are checked as identifiers, but the expressions beside them are arbitrary SQL by
definition, so the *type* is the check — text that arrived from a request cannot
reach the statement without somebody having written `Sql(...)` around it.

`purge_count()` runs the purge and reports how many rows went (`None` when the
driver's status cannot be read), so a scheduled purge can record what it did.

