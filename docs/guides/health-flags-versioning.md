# Health checks, feature flags, and API versioning

Three small conveniences that every production service reaches for. None needs a dependency.

## Health and readiness

Liveness answers "is the process up?"; readiness answers "should the load balancer send it traffic?". `app.health()` mounts both:

```python
from wreath.health import database_check

app.health(checks=[database_check("db", ping=lambda: db.ping())])
# GET /health -> 200 (or 503 while draining)
# GET /ready  -> runs every check concurrently -> 200, or 503 with per-check JSON
```

`callable_check(name, fn)` wraps any async probe; a failing check flips `/ready` to `503` and reports which one failed. Pass `is_live=` to make `/health` report draining during shutdown.

## Feature flags

Register a provider and gate code on it. Flags come from the environment (`WREATH_FLAG_*`), explicit values, or your own `FlagProvider`:

```python
app.flags(new_checkout="25%", beta_ui=True)     # or app.flags() to read the env

from wreath.flags import flags_dependency
```

`FeatureFlags.enabled(name, context)` understands booleans and a small rule language, including percentage rollouts (`"25%"`) that are **deterministic** — the same subject always lands the same side of the line, computed with blake2s rather than a coin flip, so a user's experience is stable across requests and processes.

## API versioning

`VersionedRouter` collects a `Router` per version and mounts each under a URL prefix; `negotiate_version` reads an opt-in `Accept-Version` header when you'd rather branch inside a handler:

```python
from wreath.versioning import VersionedRouter

api = VersionedRouter()          # prefixes default to /v{version}

v2 = api.version("2")            # a Router mounted under /v2

@v2.get("/llamas")
async def list_v2(request): ...

app.include_router(api.router())  # mounts every version router at its prefix
```

Versioning is a router-construction helper, not a lifecycle resource — so it composes with ordinary `include_router` rather than an `app.*` factory.
