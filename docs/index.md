---
description: Start with Wreath by goal: build, migrate, look up an API, or verify the framework's claims.
keywords: start here, documentation home, docs map, learn wreath, navigation
boost: 2
---

```plate
caption: Python 3.14 · ASGI · no mandatory runtime dependencies
title: One system. One obvious home.
lede: Wreath brings the parts of a production web service into one release. The API names each part plainly, and these docs follow the same rule: choose what you are trying to do, then take one path.
action: Build your first service -> getting-started/index.md
action: Browse every path -> map.md
```

Wreath is an ASGI framework, PostgreSQL stack, background-work system, policy
engine, and native HTTP server. You can use the framework under any conforming
ASGI server or run the native stack without changing the application.

## What are you here to do?

| Your goal | The shortest route |
|---|---|
| start from an empty directory | [Install Wreath and build one route](getting-started/index.md) |
| understand the shape before choosing | [See the whole documentation map](map.md) |
| solve a concrete task | [Open the cookbook](cookbook/index.md) |
| learn one subsystem | [Follow the guides](guides/routing.md) |
| look up a public symbol | [Open the API reference](reference/index.md) |
| migrate an existing FastAPI stack | [Read the side-by-side migration path](from-fastapi/index.md) |
| evaluate what Wreath replaces | [Inspect the generated capability map](capabilities.md) |
| check the speed claim | [Read the measurements and method](perf/index.md) |

The **Browse** link in the header returns to this map from every page. `Ctrl K`
or `/` searches headings, prose, module names, and the package names in the
capability map.

## The shape of the system

Every feature has one owner. Higher-level features reuse those owners instead
of building parallel stacks.

| Part | Begin with | It leads to |
|---|---|---|
| requests | [Routing](guides/routing.md) | binding, policy, responses, OpenAPI |
| data | [PostgreSQL](guides/postgres.md) | ORM, migrations, queries, pagination |
| identity | [Authentication and authorization](guides/auth.md) | users, organisations, SSO, SCIM |
| long-running work | [Jobs and messaging](guides/jobs.md) | progress, streams, workflows, notifications |
| service boundaries | [Outbound HTTP](guides/http-client.md) | webhooks, objects, MCP, signatures, provenance |
| delivery | [Native server](guides/server.md) | caching, compression, static files, edge proxy |
| operations | [Configuration and state](guides/config-state.md) | health, telemetry, logging, testing, hardening |

## Sixty seconds of Wreath

```python
from wreath import Request, Wreath

app = Wreath()

@app.get("/hello/{name}")
async def hello(request: Request, name: str) -> dict:
    return {"hello": name}
```

```bash
wreath run app:app
```

The native server is optional. `uvicorn app:app` serves the same application.

## The line Wreath keeps

The brand may be poetic. The API is not. A route is a route, a job is a job,
and a connection pool is a connection pool.

The performance story follows the same discipline. Wreath moves repeated work
to startup and byte-heavy work to native kernels, but only after a retained
measurement identifies the cost. The [request-path account](internals/index.md)
shows where Python begins. The [performance page](perf/index.md) carries the
numbers and their limits.

Wreath is pre-1.0. Implemented surfaces live in the guides and reference. Named
work that has not shipped lives only in the [roadmap](reference/roadmap.md).
