# Add liveness and readiness endpoints

A load balancer needs two different answers: *is this process alive?* and
*should I send it traffic right now?* Wreath mounts both with one call —
`/health` for liveness and `/ready` for readiness:

```python
from wreath.health import postgres_check

app.health(checks=[postgres_check(db)])
# GET /health -> 200 (200 always, unless draining)
# GET /ready  -> runs every check concurrently -> 200, or 503 with per-check JSON
```

Liveness is deliberately dumb: it returns `200` as long as the process is up, so
the orchestrator only restarts you when the process is genuinely wedged — not
because a downstream database blinked. Readiness is where the dependencies get
probed.

`postgres_check(db)` is the one to reach for when `db` is a Wreath `Database`. It
acquires from the `security_read` pool, runs a round trip, releases in a
`finally`, and reports `round_trip_ms` in the body. It probes the reserved pool
on purpose: the app pools are what saturate under load, so probing one would
report *unready* for an instance that is merely busy, and the load balancer would
then pull the instance that is working hardest.

For any other dependency, `database_check(name, ping)` takes a probe you write
and `callable_check(name, fn)` wraps any async callable. A probe that raises is
reported as unhealthy, never a `500`:

```python
from wreath.health import callable_check, database_check

# Register the ping once, at startup — `statement()` refuses a duplicate name,
# so it cannot be built inside the probe.
ping = db.statement("health_ping", "SELECT 1")

checks = [
    database_check("db", ping.fetchval),
    callable_check("cache", app.state.cache.ping, critical=False, timeout=0.25),
]
app.health(checks=checks)
```

`database_check` wants the bound coroutine function itself, not a lambda that
digs a query out of a pool. `db.pool("read")` hands back the `Pool` — which
leases connections and has no query methods on it at all — so a probe written
that way fails on every request, and readiness answers `503` forever with the
`AttributeError` buried in the JSON body. Register a `Statement` and pass its
`fetchval`, or use `postgres_check`.

`critical=False` is the other half. A failing critical check answers `503` and
takes the instance out of rotation; a failing non-critical one reports
`degraded`, still serves, and tells an operator to look. A lagging cache is not
a reason to drop traffic.

`/ready` runs every check concurrently under a per-check `timeout`, so one slow
dependency cannot hang the endpoint — an overrun is recorded as `timeout` and
treated as a failure. Every check reports its own `duration_ms`, so a readiness
endpoint that got slow names the dependency that did it.

To drain cleanly on shutdown, pass `is_live=` — while it returns `False`,
`/health` reports `shutting_down` with a `503`, so the load balancer pulls the
instance out before it stops accepting connections:

```python
app.health(checks=checks, is_live=lambda: not app.state.draining)
```

The paths are configurable (`liveness_path=`, `readiness_path=`) if your platform
expects different names. For conditions that need a person rather than a load
balancer — a stalled backfill, say — build `wreath.health.health_router` directly
and pass them as `alerts=`; that endpoint always answers `200` and never touches
`/ready`.
