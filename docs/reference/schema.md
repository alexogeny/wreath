# `wreath.schema`

The tables wreath needs for itself, created by wreath, in a schema wreath owns.

Fourteen subsystems ship database tables of their own — the job queue, messaging,
sessions, rate limiting, idempotency, the webhook inbox and outbox, the pass
ledger, settled series buckets. None of them belongs in your migration artifact:
`wreath migrations generate` derives that from your ORM models, and none of these
is one. So the two mechanisms stay apart. **Your artifact describes your models;
wreath owns its own furniture and applies it at startup.**

A component is collected by *asking* rather than from a list. Anything the
application holds that offers `component()` contributes one, so registering a
subsystem is the whole of registering its schema — there is no second place to
remember.

Each component carries its own version marker rather than a single global
counter, because a global number forces unrelated subsystems into one upgrade
order for no reason. Steps are additive, and the rule that makes a rolling deploy
safe is stated as a rule: **an upgrade step must be safe for the previous version
to keep running against.** An older build meeting a newer schema runs, warns, and
does not downgrade.

Steps also cannot rely on a transaction — a fresh pooled connection has an
outstanding operation, so `BEGIN` is rejected — which is why **every step must be
individually idempotent**, a crash mid-step re-applies harmlessly, and the version
marker is a fast path rather than the source of truth. `verify()` reads the
catalog.

## When the application may not create relations

A role without `CREATE SCHEMA` is an ordinary deployment, not an edge case, so
the opt-out is first class. Turn management off, and startup **refuses by name**
— naming the component and the relation it needs — instead of failing later at
the first enqueue. `wreath schema sql` prints exactly what a DBA has to apply and
`wreath schema check` verifies it landed.

```
wreath schema sql   > ddl.sql     # hand this to whoever owns the database
psql -f ddl.sql
wreath schema check               # exit 0 once every component is present
```

Five components predate the shared schema and still create an unqualified,
`wreath_`-prefixed table resolved through `search_path` — session, rate limit,
idempotency, and the two webhook tables. They are registered where their rows
actually are, because moving them is not additive: a worker on the previous
version would look for the old name.

::: wreath.schema
