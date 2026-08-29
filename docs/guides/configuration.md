---
description: Bind typed configuration, own runtime state and acquire resources through ASGI lifespan.
keywords: guide configuration environment secrets state lifespan startup shutdown dependencies flags
---

# Configuration and lifecycle

Configuration is typed input read once. Runtime state belongs to an application or a
request. External resources open and close through lifespan. Keeping those three
separate prevents a process-global client, an unvalidated environment string or a
request value from quietly acquiring the wrong lifetime.

```python title="app.py"
from dataclasses import dataclass

from wreath import Request, Wreath
from wreath.config import Environment


@dataclass(frozen=True)
class Settings:
    service_name: str = "catalogue"
    page_size: int = 40


settings = Environment({"APP_PAGE_SIZE": "60"}).bind(Settings, prefix="APP")
app = Wreath()


@app.on_startup
async def open_catalogue(application: Wreath) -> None:
    application.state.catalogue = {"pink-cable": 12}


@app.on_shutdown
async def close_catalogue(application: Wreath) -> None:
    del application.state.catalogue


@app.get("/about")
async def about(request: Request) -> dict:
    catalogue = app.state.require("catalogue")
    return {
        "service": settings.service_name,
        "page_size": settings.page_size,
        "products": len(catalogue),
    }
```

```python title="test_app.py"
from wreath.testing import TestClient

from app import app


async def test_lifespan_owns_the_catalogue() -> None:
    async with TestClient(app) as client:
        response = await client.get("/about")
        assert app.state.catalogue == {"pink-cable": 12}

    assert response.json() == {
        "service": "catalogue",
        "page_size": 60,
        "products": 1,
    }
    assert app.state.get("catalogue") is None
```

Use `Environment(read_osenv())` in a deployed process. Wrap credentials in
`Secret[T]`; their string and representation stay redacted until `reveal()` is called
at the client boundary. Defaults are useful for harmless development values. Required
production settings should have no default, so startup names every missing key.

`app.state` lasts for one application instance. `request.state` lasts for one request
and is lazy. Neither is configuration or cross-worker storage. Databases, job runners,
message buses, HTTP clients, object stores, flags and services registered through
`Wreath` expose their application-owned instances during lifespan and contribute any
schema or infrastructure they own.

Dependencies remain the handler-level way to derive a value from a request. Use state
for an owned resource, a dependency for a per-operation view of it, and PostgreSQL or
another shared owner for facts several workers must agree on.

See [application reference](../reference/application.md),
[operations reference](../reference/operations.md), and
[PostgreSQL and models](data.md).
