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

## User story: a query on a request-scoped connection

> *As an API author, I have one endpoint that just needs a value out of Postgres.
> I don't want an ORM in the path — I want a pooled read connection handed to my
> handler and returned automatically when it returns.*

```python
from typing import Annotated
from wreath.postgres import Connection, FromDatabase

app.postgres("main", dsn=DATABASE_URL)

@app.get("/widgets/{id}/price")
async def price(
    request,
    conn: Annotated[Connection, FromDatabase("main", workload="read")],
):
    value = await conn.fetchval(
        "SELECT price FROM widgets WHERE id = $1", request.path_params["id"]
    )
    return {"price": value}
```

The connection is leased from the `read` pool for the life of the request and
released when the handler returns — you never acquire or release by hand.
Parameters are bound as numbered `$N` placeholders over the extended-query
protocol, never string-interpolated. For a query on the hot path, register it
once with `db.statement(name, sql, workload=...)` so it is prepared a single
time and routed to its pool.

Each connection's automatic prepared-plan LRU is bounded twice: by
`PoolConfig.statement_cache_size` and by the approximate retained-byte limit
`PoolConfig.statement_cache_bytes`. Keep both finite when SQL text or result
metadata can vary by tenant.

## Sizing the pools for the machine you deploy on

The defaults are `read` and `write` pools of `max_size=10` each, so **one worker
can open 20 connections** and a four-worker host can open 80. On a managed
PostgreSQL with `max_connections=100` that is most of the server, and the cost
lands on the *database* instance: each backend is a process holding several
megabytes, whether or not it is running a query.

Size it from the app instance, not from the database:

```python
Database("main", dsn, pools={
    "read": PoolConfig(min_size=1, max_size=4),
    "write": PoolConfig(min_size=0, max_size=2),
})
```

The reasoning is that a pooled connection is only useful while a worker has a
core to process its results on. A 2-vCPU instance cannot have twenty queries
genuinely in flight; it has twenty connections idling, each costing memory at
both ends and each counting against `max_connections`. Two to four per worker per
pool is the range worth starting from, and `min_size=0` on `write` means a
read-heavy service opens nothing until it first writes.

Two things to check before raising a limit:

- **`workers × max_size × pools` against the server's `max_connections`**, with
  headroom for migrations, `psql`, and your monitoring. Exhaustion surfaces as
  `acquire_timeout` errors under load, which read like a slow database and are
  not.
- **Session-scoped advisory locks hold a connection for their duration**, so a
  fleet using them needs its own budget on top — see
  [distributed locks](distributed-locks.md), which raises a `ResourceWarning`
  when an acquisition would leave the pool no headroom.

Raising `max_size` is the right fix only when the pool is genuinely saturated by
concurrent in-flight queries. When it is saturated by *slow* queries, more
connections move the queue rather than shorten it.

## A client that goes away stops the query

When the caller disconnects mid-request, Wreath's own HTTP/1.1 server cancels
the handler's task, the driver sends PostgreSQL a wire-level `CancelRequest` on
a second connection, and the backend stops scanning. The connection comes back
to the pool clean — `idle`, not `idle in transaction` — and serves the next
request.

**This happens for safe methods only.** `GET`, `HEAD` and `OPTIONS` are defined
by RFC 9110 as having no intended effect on the server, so abandoning one can
lose nothing but the work. A `POST` is left running, and that default is not
timidity: cancelling it rolls its transaction back cleanly, but it does not roll
back the job it enqueued, the card it charged or the mail it sent — and the
client is gone and cannot be told which of those happened. Declare
`cancel_on_disconnect=` on the route to override it in either direction; see
[the server guide](server.md#cancelling-a-handler-when-the-client-goes-away).

The `CancelRequest` is best effort by design: a race can let a statement finish
first, and PostgreSQL is free to ignore one. What is guaranteed is that the
connection is not left poisoned.

Because the exact signatures for pooling, transactions, workloads, and result
types come straight from the driver, the reference is the authoritative place for
them.

**Reference:** [`wreath.postgres`](../reference/postgres.md).
