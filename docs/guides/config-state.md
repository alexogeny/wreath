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
from wreath.config import load_env
env = load_env(".env", apply=True)      # strict KEY=value; no shell expansion
```

The dotenv parser is deliberately strict and literal: `KEY=value`, nothing
clever, no variable expansion or command substitution to surprise you. Server
settings bind from `WREATH_*` variables — document them in a committed
`.env.example` — and you can declare the application secrets you can't start
without, so a missing one is a friendly warning at boot rather than a crash later:

```python
from wreath.server import run
run(app, required_env=["DATABASE_URL", "SECRET_KEY"])
```

**Reference:** [`wreath.config`](../reference/config.md),
[`wreath.state`](../reference/state.md).
