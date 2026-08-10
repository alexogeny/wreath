# Installation and your first app

Welcome. This page takes you from an empty directory to a running application,
explaining each step rather than rushing past it. By the end you will have a
Wreath service that handles requests, validates input, and can be tested without
opening a socket.

!!! tip "Coming from FastAPI?"

    If you already build with FastAPI, Pydantic, SQLModel, or Alembic, start
    with [Wreath for FastAPI developers](../from-fastapi/index.md) — the same
    application in both dialects, and a table of every equivalence.

## Install

Wreath targets Python 3.14 and newer.

```bash
pip install wreath
# or, with uv
uv add wreath
```

A wheel ships the C extensions prebuilt, so this needs no compiler. Installing
from source builds them, which needs a C compiler and the CPython headers.

Routing, HTTP parsing, the JSON and msgpack codecs, header handling, validation
and policy evaluation are C. There is no slower mode to fall back to, so a build
without them says so at import rather than degrading quietly.

## Your first application

Create `app.py`. An application begins as a single `Wreath()`, and routes are
added to it with decorators:

```python
from wreath import Wreath, Request

app = Wreath()

@app.get("/")
async def index(request: Request) -> dict:
    return {"status": "ok"}

@app.get("/hello/{name}")
async def hello(request: Request, name: str) -> dict:
    return {"hello": name}
```

A handler is an ordinary async function. Returning a `dict` sends JSON; when you
want control over the status, headers, or body, return one of the
[response types](../reference/response.md) instead. The `{name}` in the path
becomes a parameter, converted to the type you annotate — here, a `str`.

## Run it

```bash
wreath run app:app            # the native Wreath server
wreath dev app:app            # ... with autoreload while you develop
uvicorn app:app               # ... or any ASGI server you already run
```

`wreath run` reads `WREATH_*` environment variables so the same code can be
configured per environment (see [Configuration and state](../guides/config-state.md)),
and `wreath run --help` lists every option.

## Let Wreath validate your input

You rarely need to read the request by hand. Declare where each value comes from,
and Wreath compiles a validator for it when the app starts. Bad input becomes a
clear `422` response before your handler ever runs:

```python
from typing import Annotated

from wreath import Request
from wreath.binding import Query
from wreath.response import JSONResponse

@app.get("/search")
async def search(
    request: Request,
    q: str,
    limit: Annotated[int, Query(minimum=1, maximum=100)] = 20,
) -> JSONResponse:
    return JSONResponse({"q": q, "limit": limit})
```

A marker like `Query` rides inside `Annotated` and says where the value comes
from and what bounds it; the default stays an ordinary Python default, so the
signature remains plain, callable Python.

This is the heart of everyday Wreath work; the [Binding, validation, and
dependencies](../guides/binding.md) guide covers it in full.

## Test it without a socket

You don't need a running server to test a Wreath application. The test client
drives the whole pipeline — middleware, authentication, binding — in process:

```python
from wreath.testing import TestClient

async def test_index():
    async with TestClient(app) as client:
        response = await client.get("/")
        assert response.status == 200
```

## Where to go next

- [Project structure and deployment](deployment.md) — how a real Wreath project
  is laid out, and how to ship it.
- [Guides](../guides/routing.md) — one page for each part of the framework.
- [Cookbook](../cookbook/index.md) — recipes for common tasks.
