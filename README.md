<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/wreath-dark.png">
  <img src="docs/assets/wreath-light.png" alt="Wreath" width="320">
</picture>

# Wreath

**Many separate parts, gathered and woven until they hold a single shape.**

[![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-2f855a?style=flat-square&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![ASGI](https://img.shields.io/badge/ASGI-any_server-7c3aed?style=flat-square)](https://asgi.readthedocs.io/)
![HTTP 1.1, 2, 3](https://img.shields.io/badge/HTTP-1.1%20%7C%202%20%7C%203-0891b2?style=flat-square)
![Runtime dependencies: zero](https://img.shields.io/badge/runtime_dependencies-zero-16a34a?style=flat-square)
[![License: MPL-2.0](https://img.shields.io/badge/license-MPL--2.0-64748b?style=flat-square)](https://github.com/alexogeny/wreath/blob/main/LICENSE)

A Python 3.14-first ASGI framework with an ORM over its own PostgreSQL driver,
durable jobs, authentication and policy, OpenAPI with typed clients — and a
native HTTP/1.1, HTTP/2 and HTTP/3 server underneath, when you want it.

**[Documentation](https://alexogeny.github.io/wreath/)** ·
[Getting started](https://alexogeny.github.io/wreath/getting-started/index.html) ·
[From FastAPI](https://alexogeny.github.io/wreath/from-fastapi/index.html) ·
[Issues](https://github.com/alexogeny/wreath/issues)

</div>

---

A wreath is a circle of separate things — leaves, branches, small flowers —
gathered and woven until they hold a single shape. That is the idea behind this
framework. A web application is made of many parts: routing, validation,
authentication, data access, and the server that carries it all. Wreath gathers
those parts into one coherent whole and gives each of them a clear, honest place
to live.

**The brand is allowed to be poetic. The API is not.** A middleware is called a
middleware. A dependency is a dependency. Routes are routes, startup is startup,
a connection pool is a connection pool. You should be able to guess where a
feature lives without first learning our vocabulary — `wreath.pagination` is
pagination, `wreath.jobs` is jobs.

```python
from wreath import Wreath, Request

app = Wreath()

@app.get("/hello/{name}")
async def hello(request: Request, name: str) -> dict:
    return {"hello": name}
```

```bash
wreath run app:app          # the native Wreath server
wreath dev app:app          # ... with autoreload while you develop
uvicorn app:app             # ... or any ASGI server you already run
```

> **Status:** pre-1.0. Everything described below is implemented and tested;
> expect additions, and expect what already works to keep working as it grows.
> What is deliberately *not* shipped yet lives in one file:
> [`docs/reference/roadmap.md`](https://github.com/alexogeny/wreath/blob/main/docs/reference/roadmap.md).

## Why it feels different

| | Wreath |
|---|---|
| **Weaves the whole circle** | Routing, binding, auth, ORM, migrations, jobs, OpenAPI, and a server — one project, one release, one set of docs that agree with each other. |
| **Asks for nothing underneath** | The framework core has no mandatory runtime dependency. The Postgres driver, the JWT implementation, the template engine, the docs generator: all Wreath's own code. |
| **Fast where it matters, honest about it** | The hot path is C — routing, HTTP parsing, the codecs, validation — and the wheel ships it prebuilt. No performance claim comes from a single run. |
| **Speaks plain** | The imagery lives in the story we tell; the code uses conventional names. Nothing is themed — no threads, roots, kindling, or leaves in the API. |
| **Runs where you already run** | Any conforming ASGI server serves a Wreath app unchanged. The native server is an upgrade, never a requirement. |
| **Tests what you declared** | `wreath mutant` deletes one *declared control* at a time — a policy, a refusal, a rate limit — and reports the ones your tests would not have noticed. |

## What's in the circle

Each part keeps to its own clearly-named module, so the whole stays easy to hold
in your head.

**Serving a request**

| Module | What it gives you |
|---|---|
| `wreath.app`, `wreath.router` | The application, composable routers, app state, lifespan, and errors rendered as RFC 9457 problem details |
| `wreath.request`, `wreath.response` | JSON, text, HTML, streaming and server-sent events, file uploads, background tasks, content negotiation over JSON, MessagePack and Protocol Buffers |
| `wreath.binding` | Typed request and response contracts, reusable `Field` constraints, validation compiled at startup, and dependency injection with `Depends` |
| `wreath.middleware` | Rate limiting, CORS, CSRF, sessions, security headers, compression, request IDs, idempotency keys, cache-control policy |
| `wreath.openapi`, `wreath.typegen` | A strict OpenAPI 3.1 contract derived from the runtime validators, a self-contained docs UI, and generated typed clients |
| `wreath.templates`, `wreath.staticfiles` | A template engine that compiles at startup and escapes by default; static files with ETags, conditional and range requests |
| `wreath.graphql`, `wreath.mcp` | A GraphQL surface derived from the same model specs the REST routes use, and a Model Context Protocol server for your callables |

**Knowing who is asking**

| Module | What it gives you |
|---|---|
| `wreath.auth` | Authentication: JWT, API keys, OAuth2 and OIDC login |
| `wreath.authorization` | Authorization on a built-in Cedar policy engine — kept firmly apart from identity |
| `wreath.users` | Registration, login, email verification, password reset, scrypt hashing, TOTP with hashed recovery codes, and WebAuthn passkeys |

**Data**

| Module | What it gives you |
|---|---|
| `wreath.postgres` | A native PostgreSQL driver: pooling, prepared statements, parameter type inference, `COPY`, and cluster-wide advisory locks |
| `wreath.orm` | An async ORM with explicit loading, first-class JSONB and array columns, vector similarity and full-text search without a second datastore, transactions, and tenant-scoped sessions |
| `wreath.migrations` | Schema diffing, migration artifacts, apply, and a derived inverse that refuses to strand live code |
| `wreath.queries`, `wreath.series` | Named compiled reads and hybrid vector/full-text search; chart data as a declaration, bucketed in the database in the reader's own timezone |
| `wreath.pagination`, `wreath.crud` | Page, size, sort and filter turned into safe queries over an allow-list; generated REST CRUD routes, off until you opt in twice |
| `wreath.cache`, `wreath.response_cache` | An in-process snapshot cache for read-mostly data, and HTTP response caching invalidated by the ORM writes behind it |
| `wreath.objects`, `wreath.temporal`, `wreath.geospatial` | S3-compatible or on-disk object storage with presigned URLs; timezone-correct dates and durations; great-circle distance and index-answerable proximity |

**Work that outlives the request**

| Module | What it gives you |
|---|---|
| `wreath.jobs`, `wreath.messaging` | Durable Postgres-backed jobs and schedules, pub/sub, WebSocket rooms, task progress, and supervised background services |
| `wreath.workflows` | Durable multi-step sagas: each step's result recorded before the next starts, resume from the first unrecorded step, compensation newest-first |
| `wreath.passes` | Backfills, rollups, and reindexes as a durable, resumable, paced walk over a big table |

**Running it**

| Module or command | What it gives you |
|---|---|
| `wreath.server` | HTTP/1.1, HTTP/2 and optional HTTP/3, with WebSockets, TLS, and a development runner |
| `wreath.telemetry`, `wreath.logging` | Structured logging on the flight recorder's ring, metrics and traces bridged to OpenTelemetry, Prometheus, StatsD and CloudWatch, and replay |
| `wreath.health`, `wreath.flags`, `wreath.versioning` | Liveness and readiness, feature flags with deterministic percentage rollouts, and API versioning |
| `wreath.testing` | An in-process test client that runs the real lifespan, WebSocket sessions included, plus a pytest plugin |
| `wreath doctor` | Diagnostics for your own handlers, including the N+1 query you did not know you had |
| `wreath audit` | An offline accessibility (WCAG 2.1 A/AA) and performance auditor for the HTML and responses your app returns |
| `wreath port` | A codemod that reads an existing FastAPI, Pydantic, or SQLModel project and reports exactly what maps onto Wreath and what does not |

A fuller table — including what each part means you don't have to install — is
on [the capability map](https://alexogeny.github.io/wreath/capabilities.html).

## Install

Wreath targets Python 3.14 and newer.

```bash
pip install wreath
# or
uv add wreath

# Linux io_uring event loop and native TLS transport
uv add 'wreath[linux]'

# HTTP/3 (the h3 and http3 names are aliases)
uv add 'wreath[h3]'
```

The base wheel always ships Wreath's portable C implementation and needs no
compiler or runtime package. The `linux` extra adds Wreath's io_uring reactor;
`h3`/`http3` adds the HTTP/3 extension with its pinned QUIC/TLS libraries
bundled into the wheel. Neither extra changes the framework API.

## Your first few minutes

Declare where each value comes from, and Wreath compiles a validator for it at
startup. Bad input becomes a clear `422` before your handler runs:

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

A model earns its keep twice — it describes a table, and its columns *are* the
validator for a request body, so the two cannot drift apart:

```python
from wreath.orm import Mapped, Model, column
from wreath.orm.types import Int64, Text

class Widget(Model, table="widgets"):
    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
```

And you never need a socket to test any of it:

```python
from wreath.testing import TestClient

async def test_index():
    async with TestClient(app) as client:
        response = await client.get("/")
        assert response.status == 200
```

Here is the shape of the path that carries it, and where the language boundary
sits when the native build is in use:

```text
   request
      │
      ▼
   ingress ─▶ middleware ─▶ routing ─▶ authentication ─▶ authorization
                                                               │
   ═══ into Python ════════════════════════════════════════════╪══════
                                                               ▼
                                                         your handler
                                                               │
   ═══ back to native ═════════════════════════════════════════╪══════
                                                               │
   response ◀───────────────── egress ◀────────────────────────╯
```

The intended shape is that everything before the handler stays native, and
Python is entered when a route is *activated*. That is a measured property, not
an aspiration: `uv run wreath-request-trace` counts every boundary crossing for
a whole request against a realistic app, attributes each to a lifecycle phase,
and compares the total to a baseline checked into the repository.

## The command line

| Command | Result |
|---|---|
| `wreath new shop` | Write a project that already runs and whose own tests are already green. |
| `wreath capabilities celery` | What already ships that answers a word you know — before you install anything. |
| `wreath run app:app` | Serve an application in one foreground process. |
| `wreath dev app:app` | Serve it and reload after source changes. |
| `wreath docs` | Build a documentation site from markdown — no third-party toolchain. |
| `wreath migrations` | Inspect and run PostgreSQL migrations. |
| `wreath typegen` | Generate consumer type contracts from typed routes. |
| `wreath port` | Port an existing FastAPI app: report, or emit Wreath source. |
| `wreath mutant` | Remove one declared control at a time and see whether the tests notice. |
| `wreath test` | Run pytest behind an animated file heat map, duration profiling, and optional mutation confidence. |
| `wreath audit` | Audit generated HTML and responses for accessibility and performance. |
| `wreath doctor` | Diagnose defects a green test suite cannot see — including `preflight`, one report of everything checkable before a deploy. |
| `wreath inspect` | Query a running server's read-only telemetry inspector. |
| `wreath flight` | Read a flight recorder ring file left behind by a crash. |

`wreath --help` lists the rest — `mcp`, `capture`, `replay`, `passes`, `schema`.

## Documentation

The published site is <https://alexogeny.github.io/wreath/>.

- [**Getting started**](https://alexogeny.github.io/wreath/getting-started/index.html)
  — install and build your first app, start to finish.
- [**Wreath for FastAPI developers**](https://alexogeny.github.io/wreath/from-fastapi/index.html)
  — the same application in both dialects, with every habit mapped to its home.
- [**Guides**](https://alexogeny.github.io/wreath/guides/routing.html) — a page
  for each part of the framework, with the reasoning behind it.
- [**Cookbook**](https://alexogeny.github.io/wreath/cookbook/index.html) —
  recipes for developers, and a set written for coding agents.
- [**API reference**](https://alexogeny.github.io/wreath/reference/app.html) —
  every public module, generated from the source.
- [**Performance**](https://alexogeny.github.io/wreath/perf/index.html) and
  [**internals**](https://alexogeny.github.io/wreath/internals/index.html) — how
  it measures up, with the methodology, and what makes it quick.
- [**Release notes**](https://alexogeny.github.io/wreath/release_notes/index.html)
  — what changed in each version.

Build them locally — there is no mkdocs and no Sphinx here, Wreath renders its
own site:

```bash
uv run wreath-docs            # strict build; --serve to watch
```

## Development

```bash
uv sync                       # dev group; builds the native extensions
uv run wreath-check           # ruff, ty, pytest, native lints, map lint, trace baseline
uv run wreath-check --docs    # ... and a strict docs build
uv run wreath test            # live grid, timing outliers, 192-control confidence sample
uv run wreath test --mutant full  # complete mutation sweep, overlapping green tests
uv run pytest                 # the default suite, serially
uv run pytest -m '' -n 6      # everything, including network, fuzz, and performance
```

The test grid keeps one tile per file: green means passed, purple `▣` means its
tests are currently probing a mutant, and solid gold `▰` means one of those tests
caught the removed control. A surviving mutant is reported as a finding and
never earns the gold state.

> [!IMPORTANT]
> Prefer the task entry points over a bare `uv sync --group X`. `uv sync`
> reconciles the venv to exactly the groups you name and **removes everything
> else**, so syncing one group uninstalls the last one's tools. The task
> runners use `uv sync --inexact`, which adds without evicting.

Contributing — the invariants a change must preserve, how to verify one, and why
each rule exists — is documented in
[`AGENTS.md`](https://github.com/alexogeny/wreath/blob/main/AGENTS.md) and the
cookbook's
[section for coding agents](https://alexogeny.github.io/wreath/cookbook/agents/index.html).
If you are a coding agent, start there:
[`docs/agents/manifest.json`](https://github.com/alexogeny/wreath/blob/main/docs/agents/manifest.json)
maps every subsystem to its sources, tests, and invariants so you can find one
without reading the tree.

## Benchmarks

Wreath ships equivalent applications across several frameworks and a renderer
that reports medians with their run-to-run range, so a real win can be told from
noise. Results are regenerated locally:

```bash
uv run wreath-bench-report    # one report across every benchmark family
```

See [`benchmarks/README.md`](https://github.com/alexogeny/wreath/blob/main/benchmarks/README.md)
for methodology. A single run is not a result, and no number here comes from one.

## License

Wreath is licensed under the
[Mozilla Public License 2.0](https://github.com/alexogeny/wreath/blob/main/LICENSE)
(MPL-2.0).

In practice: use it freely, commercially, as a dependency of anything —
proprietary applications and closed-source SaaS included. Your own code is
never affected. The only obligation is that if you ship a product containing
*modified copies of wreath's own files*, those file changes must remain
source-available to your recipients.

**The spirit of this release** (a request, not a license term): if you make
substantial improvements to wreath — bug fixes, features, performance work —
we'd love to see them upstream rather than living in a fork. Massive divergent
forks are legal; they're just not the point. The MPL grants no rights to the
"wreath" name (§2.3), so a fork should pick its own.

<div align="center">

**Many parts. One shape.**

</div>
