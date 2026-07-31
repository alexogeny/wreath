# Configuration and state

Two ideas that look alike and behave nothing alike, so Wreath keeps them in
separate modules and keeps their APIs separate too.

**Configuration** is how Wreath *starts*: the settings read from the environment
before a single request is served. It lives in `wreath.config` (environment and
dotenv parsing into typed settings) and in `ServerConfig`. It is fixed for the
life of the process.

**State** is what your application *holds while it runs*: values scoped to the
application or to a single request. It lives in `wreath.state`. It changes.

Treating one as the other — reading request state as if it were startup config,
or mutating config at runtime — is a category error, so the two never share an
interface.

## User story: compute a value once per request, read it downstream

> *As an API author, my auth middleware resolves the current tenant from the
> request. Every handler needs it. I want to compute it once, early, and read it
> later without re-parsing anything — and I want it gone when the request ends.*

```python
from wreath.middleware import MiddlewareHooks

async def resolve_tenant(request):
    request.state.tenant = tenant_from_host(request.headers)
    return None                         # None → continue to the handler

app.add_middleware(MiddlewareHooks(before=resolve_tenant))

@app.get("/dashboard")
async def dashboard(request):
    return {"tenant": request.state.tenant}
```

`request.state` is a fresh, per-request bag: set an attribute on it in a
`before` hook and any later stage sees it; it's discarded when the request ends,
and two concurrent requests never share it. Values that must outlive a single
request live on `app.state` instead — that's where `app.http_client(...)` and the
other application resources register themselves.

## Reading the environment

```python
from dataclasses import dataclass
from typing import Annotated, Literal

from wreath.config import Env, Environment, Secret

@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int = 5432
    password: Secret[str] = Secret("")

@dataclass(frozen=True)
class Settings:
    debug: bool
    mode: Literal["development", "production"]
    database: DatabaseSettings
    token: Annotated[Secret[str], Env("SERVICE_TOKEN")] = Secret("")

env = Environment.load(".env")
settings = env.bind(Settings, prefix="APP")
```

The dotenv parser remains strict and literal: `KEY=value`, no expansion or
command substitution. Root fields use `APP_FIELD`; nested fields use
`APP_DATABASE__HOST`. `Env` supplies an absolute alias. Binding converts the
stdlib scalar/container types, enums, literals, optionals, UUID, Decimal, Path,
and ISO dates, and reports every missing or malformed value in one
`SettingsError`. `Secret` redacts both `repr` and `str`; call `reveal()` only at
the integration boundary. `Environment.source(key)` reports whether the winning
value came from the process or the resolved dotenv path.

Server settings still bind from `WREATH_*` variables, and a server process can
also require raw variables at its boundary:

```python
from wreath.server import run
run(app, required_env=["DATABASE_URL", "SECRET_KEY"])
```

**Reference:** [`wreath.config`](../reference/config.md),
[`wreath.state`](../reference/state.md).
