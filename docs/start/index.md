---
description: Install Wreath, create a typed route and run it on the native development server.
keywords: install quickstart first app route request response dev server uv
boost: 1.5
---

```hero
eyebrow: Start · one route, no ceremony
title: Your first Wreath application.
lede: Create a typed ASGI application, run it on Wreath's development server, and keep the boundary small enough to understand completely.
signal: Python 3.14
signal: any ASGI server
signal: typed binding
action: Choose what to build next -> paths.md
action: Browse the surface -> ../reference/index.md
```

## Install

Wreath targets CPython 3.14. With an existing uv project:

```bash
uv add wreath
```

Or create a clean project first:

```bash
uv init --python 3.14
uv add wreath
```

## Create the application

```python title="app.py"
from dataclasses import dataclass
from typing import Annotated

from wreath import Request, Wreath
from wreath.binding import Body

app = Wreath()


@dataclass
class Greeting:
    name: str


@app.get("/hello/{name}")
async def hello(request: Request, name: str) -> dict:
    return {"hello": name}


@app.post("/hello")
async def create_greeting(
    request: Request,
    greeting: Annotated[Greeting, Body()],
) -> dict:
    return {"hello": greeting.name}
```

Path parameters and the dataclass body are bound from the handler signature. An
invalid request is rejected before the handler runs.

## Run it

```bash
uv run wreath dev app:app
```

Then call both routes:

```bash
curl http://127.0.0.1:8000/hello/Mara
curl -X POST http://127.0.0.1:8000/hello \
  -H 'content-type: application/json' \
  -d '{"name":"Mara"}'
```

The application is ordinary ASGI. If another conforming server is already part of
your deployment, keep it:

```bash
uv run uvicorn app:app
```

## Test the boundary

Wreath's test client drives the ASGI application without opening a socket.

```python title="test_app.py"
from wreath.testing import TestClient

from app import app


async def test_hello() -> None:
    async with TestClient(app) as client:
        response = await client.get("/hello/Mara")

    assert response.status_code == 200
    assert response.json() == {"hello": "Mara"}
```

Run it through the project test runner:

```bash
uv run wreath test
```

For a complete application rather than another isolated snippet, explore the
[canonical camera-trap service](https://github.com/alexogeny/wreath/blob/main/example/README.md).
It combines models, migrations, typed queries, authorization, deterministic seed data
and a runnable PostgreSQL-backed API.

## Add the difficult part next

Do not add architecture because a framework tour says it exists. Choose the pressure
your product actually has:

- [live shared state and contention](../stories/energy-depot.md)
- [durable agentic work across devices](../stories/agent-fleet.md)
- [a governed MCP surface](../stories/mcp-control-room.md)
- [enterprise tenant lifecycle](../stories/enterprise.md)
- [calendar-aware analysis](../stories/time-series-lab.md)
- [retries around scarce inventory](../stories/noon-drop.md)
- [bounded work through disconnection](../stories/field-operations.md)
