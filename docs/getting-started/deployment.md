# Project structure and deployment

## A layout that grows with you

Wreath doesn't impose a project structure, but a small amount of convention pays
off as an application grows. A shape that works well:

```
myapp/
  app.py            # constructs Wreath(), includes routers
  routes/           # Router modules, gathered into the app
  models.py         # wreath.orm models
  .env              # local configuration (gitignored)
  .env.example      # the same keys and nothing else, committed
```

**`.env.example` cannot be an annotated file**, and that is worth knowing before
you write one. Wreath's dotenv dialect is `KEY=value` and has no comment syntax
at all: a `#` line is a `ValueError` naming the line number, not a line that is
skipped. So a template carrying explanatory comments produces a `.env` that
fails to load on its first line, which is the least helpful moment to discover
the dialect. Keep the template to the keys — an empty value keeps the default,
so a copied file is inert until somebody fills it in — and put the explanation
of what each key does beside the settings dataclass that reads it. See
[Configuration and state](../guides/config-state.md).

The idea is to keep related routes together as [`Router`](../reference/router.md)
modules and weave them into the application in one place, rather than growing a
single file until no one wants to open it:

```python
# routes/items.py
from wreath import Router, Request
router = Router()

@router.get("/items/{id}")
async def show(request: Request, id: int) -> dict:
    return {"id": id}
```

```python
# app.py
from wreath import Wreath
from routes.items import router as items

app = Wreath()
app.include_router(items)
```

## Configuration

Keep two things straight, because they answer different questions. Server
settings come from `ServerConfig` (or `WREATH_*` variables); your application's
secrets are your own keys, loaded from a `.env` file through `wreath.config`.
More broadly, configuration is *how Wreath starts* and [state](../guides/config-state.md)
is *what it holds while running* — they never share an API. Name the variables
you truly cannot start without, and a missing one greets you at boot instead of
failing deep in a request:

```python
from wreath.server import run
run(app, required_env=["DATABASE_URL"])
```

## Shipping it

Because Wreath is an ordinary ASGI application, any ASGI server will serve it:

```bash
uvicorn app:app --workers 4
```

Or run the native server, which adds HTTP/2 and — when built for it — HTTP/3:

```bash
wreath run app:app --host 0.0.0.0 --port 8000 \
    --protocol http/1.1 --protocol h2 \
    --tls-cert cert.pem --tls-key key.pem
```

The [Native server](../guides/server.md) guide covers protocol negotiation, TLS,
and tuning, and the [Deploy behind a proxy](../cookbook/recipes/behind-a-proxy.md)
recipe walks through running behind a load balancer.
