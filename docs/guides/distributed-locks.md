# Distributed locks

When two workers must not run the same critical section at once — a rebalance, a nightly rollup, a singleton reconciler — you need a lock that spans processes. Wreath builds it directly into the PostgreSQL driver, so `async with db.lock(...)` is correct by construction: it holds one connection for the lock's whole life and releases on the same backend, never leasing a still-locked connection back to the pool.

## User story: never settle one account twice at once

> *As an API author, my `POST /accounts/{id}/settle` must not run twice for the
> same account concurrently — two in-flight requests would double-count. I want a
> lock keyed on the account that releases itself when the transaction ends, with
> no unlock to remember.*

```python
@app.post("/accounts/{id}/settle")
async def settle(
    request,
    session: Annotated[Session, FromORM("main", workload="write")],
):
    account_id = request.path_params["id"]
    async with session.begin():
        await session.lock(f"account:{account_id}", scope="xact")
        await settle_account(session, account_id)
    return {"settled": account_id}
```

The `xact`-scoped lock rides the connection the session already holds and is
released by PostgreSQL at `COMMIT`/`ROLLBACK` — a second concurrent request for
the same account blocks on `session.lock` until the first commits. This is the
preferred form; the sections below show it and the connection-held variants in
full.

## A fleet-wide mutex

```python
db = app.postgres("main", dsn=DSN)

async with db.lock("job:rebalance"):
    await do_the_thing()
```

Non-blocking, with a real timeout (a server-side `lock_timeout`, not a busy loop):

```python
async with db.try_lock("job:rebalance", timeout=2.0) as held:
    if held is None:
        return Response(status_code=409)
    await do_the_thing()
```

## The preferred form: transaction-scoped

Inside a session transaction, a lock rides the connection the session already holds and is released automatically at `COMMIT`/`ROLLBACK` — no unlock, no bookkeeping, no leak on error:

```python
async with session.begin():
    await session.lock(f"account:{account_id}", scope="xact")
    ...
```

For an isolated-tenant session, the tenant schema is folded into the key automatically, so two tenants never collide on `"invoice:1"`.

## Run something once across the fleet

`db.run_singleton` turns the lock into a leadership token: the winner runs the loop while it holds the lock; if its process dies, the connection drops, PostgreSQL releases the lock, and a follower is promoted within a poll interval — no lease renewal, no TTLs, no clocks.

## Know the guarantees

Advisory locks are coordination, not a safety barrier: a connection blip releases them silently, and they don't survive a failover to a replica. Keep guarded sections idempotent, route locks to the primary, and prefer the transaction-scoped form. Held session-scoped locks each hold a pooled connection — give a busy fleet of them their own budget so they don't starve request serving. Wreath helps you catch that early: acquiring a session-scoped lock raises a `ResourceWarning` when it would leave the pool no headroom for ordinary queries, so raise the pool `max_size` or move to the transaction-scoped form before it bites in production.
