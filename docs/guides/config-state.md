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
command substitution. **It has no comment syntax at all**, and that is the part
that surprises people: a `#` line is not skipped, it is a `ValueError` naming
the line number, the same as an `export KEY=value` line or a line with no `=`.
The reasoning is that a config file which quietly means something other than
what it reads is worse than one that refuses to parse.

The practical consequence is worth stating plainly, because wreath's own
example got it wrong: **an `.env.example` cannot be an annotated file.** If it
carries explanatory comments then the instruction to copy it produces a `.env`
that fails to load on the first line, which is the least helpful moment to
discover the dialect. Keep the template to the keys and their defaults, one per
line, and put the explanation of what each key does next to the settings
dataclass that reads it -- where it is in view of the code, and where it cannot
be copied into a file that then refuses to parse.

Root fields use `APP_FIELD`; nested fields use
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

### The `WREATH_*` variables

The single source of truth for these names is `_SERVER_ENV_REGISTRY` in
`src/wreath/server.py`; `ServerConfig.from_env()` iterates it, and the repository's
own `.env.example` lists exactly those keys — a test asserts the two agree, so
the file cannot quietly drift the way it had (`WREATH_PREARM` was missing from
it). Precedence is dataclass default < environment < explicit code argument, and
**an unset or empty value keeps the default** — which is why every key in the
template carries an empty value. Copying it therefore changes nothing until you
fill something in, and it cannot pin today's defaults into a deployment that
should have inherited tomorrow's.

| Variable | Field | Default |
| --- | --- | --- |
| `WREATH_HOST` | `host` | `127.0.0.1` — reaching the network is a decision |
| `WREATH_PORT` | `port` | `8000`; `0` asks the OS |
| `WREATH_BACKLOG` | `backlog` | `2048` |
| `WREATH_KEEP_ALIVE_TIMEOUT` | `keep_alive_timeout` | `5.0` seconds |
| `WREATH_REQUEST_TIMEOUT` | `request_timeout` | `30.0` seconds |
| `WREATH_SHUTDOWN_TIMEOUT` | `shutdown_timeout` | `10.0` seconds |
| `WREATH_SSL_SHUTDOWN_TIMEOUT` | `ssl_shutdown_timeout` | `1.0` seconds; asyncio's own default is `30.0` |
| `WREATH_SERVER_HEADER` | `server_header` | `wreath` |
| `WREATH_DATE_HEADER` | `date_header` | `true`; `true/false/1/0/yes/no/on/off` |
| `WREATH_MAX_REQUEST_LINE` | `max_request_line` | `8192` bytes, then 414 |
| `WREATH_MAX_HEADER_COUNT` | `max_header_count` | `100` fields |
| `WREATH_MAX_HEADER_BYTES` | `max_header_bytes` | `32768` bytes, then 431 |
| `WREATH_MAX_BODY_BYTES` | `max_body_bytes` | `1048576` bytes, then 413 |
| `WREATH_MAX_BODY_CHUNKS` | `max_body_chunks` | `4096` frames |
| `WREATH_LIFESPAN` | `lifespan` | `auto`; `auto`/`on`/`off` |
| `WREATH_PREARM` | `prearm` | connections pre-armed before accept |
| `WREATH_PROTOCOLS` | `protocols` | `http/1.1`; comma-separated from `http/1.1,h2,h3` |

The HTTP/2 and HTTP/3 window sizes are deliberately code-only: they are tuning
an operator has no way to choose well from outside.

Three more are read outside `ServerConfig`. `WREATH_PURE` forces the
pure-Python implementation over the compiled extensions and any non-empty value
enables it. `WREATH_BUILD_HTTP3` and `WREATH_NATIVE_PROFILE` are read by
`setup.py` during `pip install`/`uv sync` and must be exactly `1` — the first
builds the optional HTTP/3 extension (it needs the from-source nghttp3/ngtcp2
toolchain), the second produces a profiling build.

**Reference:** [`wreath.config`](../reference/config.md),
[`wreath.state`](../reference/state.md).
