# Add liveness and readiness endpoints

A load balancer needs two different answers: *is this process alive?* and
*should I send it traffic right now?* Wreath mounts both with one call —
`/health` for liveness and `/ready` for readiness:

```python
from wreath.health import database_check

app.health(checks=[database_check("db", ping=lambda: db.pool("read").fetchval("SELECT 1"))])
# GET /health -> 200 (200 always, unless draining)
# GET /ready  -> runs every check concurrently -> 200, or 503 with per-check JSON
```

Liveness is deliberately dumb: it returns `200` as long as the process is up, so
the orchestrator only restarts you when the process is genuinely wedged — not
because a downstream database blinked. Readiness is where the dependencies get
probed. `database_check(name, ping)` wraps a probe that runs e.g. `SELECT 1`;
supply the ping from your own pool so the check stays decoupled from the driver.
For any other dependency, `callable_check(name, fn)` wraps any async callable —
a probe that raises is reported as unhealthy, never a `500`:

```python
from wreath.health import callable_check, database_check

app.health(checks=[
    database_check("db", ping=lambda: db.pool("read").fetchval("SELECT 1")),
    callable_check("cache", lambda: app.state.cache.ping()),
])
```

`/ready` runs every check concurrently and is `200` only when all pass; a single
failure flips it to `503` and the JSON body names which check failed and why.

To drain cleanly on shutdown, pass `is_live=` — while it returns `False`,
`/health` reports `shutting_down` with a `503`, so the load balancer pulls the
instance out before it stops accepting connections:

```python
app.health(checks=[...], is_live=lambda: not app.state.draining)
```

The paths are configurable (`liveness_path=`, `readiness_path=`) if your platform
expects different names.
