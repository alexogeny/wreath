# Health checks, feature flags, and API versioning

Three small conveniences that every production service reaches for. None needs a dependency.

## User story: ship a feature to 25% of users, deterministically

> *As an API author, I want to roll `new_checkout` out to a quarter of users and
> have each user stay on the same side of the line every request — not flicker
> between the old and new path as they click around.*

```python
app.flags(new_checkout="25%")

flags = app.state.flags     # the registered FeatureFlags provider

if flags.enabled("new_checkout", {"id": user.id}):
    return new_checkout(request)
return old_checkout(request)
```

The bucket is computed from the flag name and the subject in the context
(`id`/`key`/`user`) with blake2s, not a coin flip, so the same user lands the same
way across requests and processes. The rule language and the other two
conveniences are below.

## Health and readiness

Liveness answers "is the process up?"; readiness answers "should the load balancer send it traffic?". `app.health()` mounts both:

```python
from wreath.health import postgres_check

app.health(checks=[postgres_check(db)])
# GET /health -> 200 (or 503 while draining)
# GET /ready  -> runs every check concurrently -> 200, or 503 with per-check JSON
```

`postgres_check(db)` probes the reserved `security_read` pool, so a merely busy
instance is not reported unready. For anything else, `callable_check(name, fn)`
wraps any async probe and `database_check(name, ping)` takes a bound coroutine
function — `db.statement("health_ping", "SELECT 1").fetchval`, registered once at
startup. A failing check flips `/ready` to `503` and reports which one failed.
Pass `is_live=` to make `/health` report draining during shutdown. See
[the recipe](../cookbook/recipes/health-checks.md) for the whole shape.

## Feature flags

Register a provider and gate code on it. Flags come from the environment (`WREATH_FLAG_*`), explicit values, or your own `FlagProvider`:

```python
app.flags(new_checkout="25%", beta_ui=True)     # or app.flags() to read the env

from wreath.flags import flags_dependency
```

`FeatureFlags.enabled(name, context)` understands booleans and a small rule language, including percentage rollouts (`"25%"`) that are **deterministic** — the same subject always lands the same side of the line, computed with blake2s rather than a coin flip, so a user's experience is stable across requests and processes.

`FeatureFlags.names()` lists the flags a provider holds. It is deliberately not part of the `FlagProvider` protocol: an external provider may not be able to enumerate without a network call, and a protocol method some implementations cannot answer is worse than an optional one they can be asked for. Callers probe for it and degrade when it is absent.

### A flag a policy can read

Pass a provider to `CedarAuthorizer(flags=...)` and the enabled flags arrive in Cedar's `context` as a set of names, so a rollout and an authorization rule become one decision instead of two that drift:

```cedar
permit(principal in Role::"editor", action == Action::"Invoice::void", resource)
when { context.flags.contains("new_billing") };
```

A flag can never permit on its own — it is an input, and Cedar still decides. The set is resolved once per request, so two policies on one route cannot disagree about whether a caller is inside the same percentage. A name no provider holds fails at startup rather than denying forever in silence. See [Auth](auth.md#feature-flags-in-a-policy) for the whole argument, including why it is a set rather than a map.

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
