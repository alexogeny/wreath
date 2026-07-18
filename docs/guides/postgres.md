# PostgreSQL

`wreath.postgres` is a native PostgreSQL driver — connections, pooling,
transactions, prepared operations, wire-format codecs, and low-level query
results. It is the foundation the [ORM](orm.md) is built on, and the relationship
between them is strict and one-directional: `wreath.orm` depends on
`wreath.postgres`, and `wreath.postgres` knows nothing about models. That
boundary is deliberate — it keeps the driver a clean, general PostgreSQL client
that you can use directly whenever the ORM would only get in your way.

You open a pool over a DSN, acquire connections from it, and run your queries
inside transactions. Prepared operations and native codecs let a query decode its
rows straight into records — or into [ORM models](orm.md) — without building an
intermediate list first, which is much of where the driver's speed comes from.

```python
from wreath.postgres import Pool

pool = Pool("postgres://user:pass@localhost/app")
await pool.start()
```

Tie the pool's lifetime to your application lifespan (the
[database pool recipe](../cookbook/recipes/database-lifespan.md) shows the
pattern), and declare `DATABASE_URL` as a boot-critical variable so a missing or
malformed DSN is caught at startup rather than on the first query.

Because the exact signatures for pooling, transactions, workloads, and result
types come straight from the driver, the reference is the authoritative place for
them.

**Reference:** [`wreath.postgres`](../reference/postgres.md).
