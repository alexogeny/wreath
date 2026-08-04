```plate
caption: One package · no runtime dependencies · Python 3.14
title: Everything here is something you no longer install.
lede: Wreath gathers the parts a web application is always assembled from — routing, validation, authentication, data access, background work, and the server that carries it all — into one coherent whole, and gives each of them a module named after the thing it does.
action: What you don't have to install -> capabilities.md
action: Install and build something -> getting-started/index.md
```

A wreath is a circle of separate things — leaves, branches, small flowers —
gathered and woven until they hold a single shape. That is the idea behind this
framework, and the list above is the literal version of it: every one of those
names is a capability that now lives in a module of Wreath, tested and released
with everything it sits beside.

The brand is allowed to be poetic. The API is not.

A middleware is called a middleware. A dependency is a dependency. Routes are
routes, startup is startup, a connection pool is a connection pool. You will
never have to translate a metaphor before you can get work done — no winding a
spool to feed a widget. The imagery lives in the story we tell about Wreath; the
code speaks plain, standard language, so you can guess where a feature lives
without first learning our vocabulary.

Wreath is Python 3.14-first. It runs behind any ASGI server you already use, and
it also brings its own: a native HTTP/1.1, HTTP/2, and HTTP/3 server that moves
the hot path into C when you want the speed — without asking you to change a
line of your application.

```python
from wreath import Wreath, Request

app = Wreath()

@app.get("/hello/{name}")
async def hello(request: Request, name: str) -> dict:
    return {"hello": name}
```

```bash
wreath run app:app          # the native Wreath server
uvicorn app:app             # or any ASGI server you prefer
```

## What Wreath gives you

Everything you need to build a real service is here, and each piece keeps to its
own named module so the whole stays easy to hold in your head.

- **A fast path where it matters.** The request pipeline, router, JSON, and
  PostgreSQL driver are accelerated in C, engaged automatically when built. A
  pure-Python reference is always present and always in agreement — reach for it
  anywhere with `WREATH_PURE=1`. See how it measures up on the
  [Performance](perf/index.md) page — with the methodology, not just the bars —
  and what makes it quick under [the hood](internals/index.md).
- **One obvious home per idea.** A small top level — `Wreath`, `Router`,
  `Request`, `Response`, `JSONResponse`, `Depends` — and a cohesive module for
  everything else, from `wreath.middleware` to `wreath.orm`.
- **The whole circle, not a starter kit.** An ORM and migrations over a native
  Postgres driver, durable jobs and messaging, authentication with a built-in
  Cedar policy engine, OpenAPI with typed clients — and about thirty more things
  you would otherwise assemble a `requirements.txt` from. See
  [what you don't have to install](capabilities.md).
- **The batteries you'd otherwise install.** Native-flight-recorder telemetry
  that bridges to OpenTelemetry, Prometheus, StatsD, CloudWatch EMF and
  OpenMetrics; `wreath audit`, an accessibility (WCAG) and performance auditor
  for the HTML and responses Wreath generates; `wreath mutant`, which deletes
  one control you declared at a time and reports the ones your tests would not
  have noticed; and `wreath port`, which translates an existing FastAPI
  application into Wreath source and tells you, precisely, what it could not.
- **A short walk from FastAPI.** If you build with FastAPI, Pydantic, SQLModel,
  or Alembic today, [Wreath for FastAPI developers](from-fastapi/index.md) shows
  your application in both dialects and maps every habit to its Wreath home —
  and the [migration guide](guides/migrations.md) gives SaaS teams a measured
  path to logical schemas and Wreath-metal readiness without a dangerous
  flag-day change of DDL authority.
- **Correctness before cleverness.** Validation compiled at startup, a firm line
  between configuration and runtime state, and a test suite that treats ASGI
  semantics as the contract Wreath must keep.

## Find your way in

Start here, in order:

- **[Getting started](getting-started/index.md)** — install Wreath and build
  your first application, start to finish.
- **[Coming from FastAPI](from-fastapi/index.md)** — for developers (and coding
  agents) fluent in FastAPI, Pydantic, SQLModel, or Alembic: side-by-side
  translations and every equivalence in one table.

Then follow the **[Guides](guides/routing.md)** as a path, not a pile — each part
of the framework with the reasoning behind it, grouped the way you meet it:

1. **[Handling a request](guides/routing.md)** — routing, requests and responses,
   binding and validation, middleware, forms, and templates: the core loop.
2. **[Working with data](guides/postgres.md)** — the Postgres driver and ORM,
   JSONB and arrays, pagination, generated CRUD, distributed locks, and migration.
3. **[Users, auth, and security](guides/auth.md)** — authentication and the Cedar
   authorization engine, ready-made user flows, and idempotent writes.
4. **[Realtime and background work](guides/websockets.md)** — WebSockets, SSE,
   task progress, durable jobs and messaging, and [streams that survive a
   reconnect](guides/streams.md), where the producer runs detached from the
   connection and a client resumes from the last chunk it saw.
5. **[Talking to other services](guides/http-client.md)** — the outbound client,
   service-to-service calls, serving MCP tools to a model, and object storage.
6. **[Speed and delivery](guides/caching.md)** — caching, compression, static
   files, and content negotiation.
7. **[Your API's surface](guides/openapi-typegen.md)** — OpenAPI and typed
   clients, the built-in docs UI, and Wreath's own static-site generator.
8. **[Configuration and operations](guides/config-state.md)** — config and state,
   health/flags/versioning, observability bridges, and the native server.
9. **[Testing and quality](guides/testing.md)** — the in-process test client,
   the mutation tester that asks whether your tests would notice a control being
   deleted, and the accessibility/performance auditor.

And to get a specific thing done fast:

- **[Cookbook](cookbook/index.md)** — practical, copy-adaptable recipes for
  developers, and a set written for the coding agents who work in this codebase.
- **[API reference](reference/app.md)** — every public module, generated from
  the source so it never drifts.

Wreath is pre-1.0. Expect it to grow — and expect what already works to keep
working as it does.
