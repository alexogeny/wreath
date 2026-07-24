# PostgreSQL

`wreath.postgres` is a native PostgreSQL driver — connections, pooling,
transactions, prepared operations, wire-format codecs, and low-level query
results. It is the foundation the [ORM](orm.md) is built on, and the relationship
between them is strict and one-directional: `wreath.orm` depends on
`wreath.postgres`, and `wreath.postgres` knows nothing about models. That
boundary is deliberate — it keeps the driver a clean, general PostgreSQL client
that you can use directly whenever the ORM would only get in your way.

You declare a database over a DSN, acquire connections from its workload pools,
and run your queries inside transactions. Prepared operations and native codecs
let a query decode its rows straight into records — or into [ORM models](orm.md)
— without building an intermediate list first, which is much of where the
driver's speed comes from.

```python
app.postgres("main", dsn="postgres://user:pass@localhost/app")
```

Declared this way, the database's pools are started during lifespan startup and
stopped gracefully at shutdown — the
[database pool recipe](../cookbook/recipes/database-lifespan.md) shows the
pattern. (Outside an application, `wreath.postgres.Database` gives you the same
object with explicit `start()` and `stop()`.) Declare `DATABASE_URL` as a
boot-critical variable so a missing or malformed DSN is caught at startup rather
than on the first query.

Each connection's automatic prepared-plan LRU is bounded twice: by
`PoolConfig.statement_cache_size` and by the approximate retained-byte limit
`PoolConfig.statement_cache_bytes`. Keep both finite when SQL text or result
metadata can vary by tenant.

Because the exact signatures for pooling, transactions, workloads, and result
types come straight from the driver, the reference is the authoritative place for
them.

**Reference:** [`wreath.postgres`](../reference/postgres.md).
