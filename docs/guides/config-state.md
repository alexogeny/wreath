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
