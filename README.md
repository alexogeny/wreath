# Wreath

A wreath is a circle of separate things — leaves, branches, small flowers —
gathered and woven until they hold a single shape. That is the idea behind this
framework. A web application is made of many parts: routing, validation,
authentication, data access, and the server that carries it all. Wreath gathers
those parts into one coherent whole and gives each of them a clear, honest place
to live.

**The brand is allowed to be poetic. The API is not.** A middleware is called a
middleware. A dependency is a dependency. Routes are routes, startup is startup,
a connection pool is a connection pool. You should be able to guess where a
feature lives without first learning our vocabulary.

Wreath is a Python 3.14-first ASGI framework. It runs behind any ASGI server you
already use, and it also brings its own: a native HTTP/1.1, HTTP/2, and HTTP/3
server that moves the hot path into C when you want the speed — without asking
you to change a line of your application.

> **Status:** pre-1.0. The feature set below is implemented and tested; expect
> additions, and expect what already works to keep working as it grows.

**Documentation: <https://alexogeny.github.io/wreath/>** ·
[Source](https://github.com/alexogeny/wreath) ·
[Issues](https://github.com/alexogeny/wreath/issues)

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

## What's in the circle

Everything you need to build a real service, each part kept in its own
clearly-named module:

- **Routing & binding** — decorator routes on the app or composable `Router`
  modules; typed parameters validated at startup-compiled speed (`wreath.router`,
  `wreath.binding`).
- **Requests & responses** — JSON, text, HTML, streaming, file, redirect, and
  RFC 9457 problem responses (`wreath.request`, `wreath.response`).
- **Middleware** — CORS, security headers, compression, rate limiting, request
  IDs, timing, proxy headers, CSRF, sessions, trusted hosts (`wreath.middleware`).
- **Auth** — authentication (identity) and authorization (roles, permissions, a
  Cedar policy engine), kept firmly apart (`wreath.auth`, `wreath.authorization`).
- **Data** — a native PostgreSQL driver and an ORM built on it, in a strict
  one-way relationship (`wreath.postgres`, `wreath.orm`).
- **More of the whole** — WebSockets, an outbound HTTP client, signed webhooks,
  templates, an application cache, OpenAPI with typed client generation, and an
  in-process test client.
- **A native server** — HTTP/1.1, HTTP/2, and optional HTTP/3, with a pure-Python
  reference always in agreement (`wreath.server`; force pure with `WREATH_PURE=1`).

## Install

Wreath targets Python 3.14 and newer.

```bash
pip install wreath
# or
uv add wreath
```

Installing from source builds the native C extensions automatically, and falls
back to the pure-Python implementation when no compiler is available.

## Documentation

The published site is <https://alexogeny.github.io/wreath/>.

- [**Getting started**](https://alexogeny.github.io/wreath/getting-started/index.html)
  — install and build your first app.
- [**Guides**](https://alexogeny.github.io/wreath/guides/routing.html) — a page
  for each part of the framework, with the reasoning behind it.
- [**Cookbook**](https://alexogeny.github.io/wreath/cookbook/index.html) —
  recipes for developers, and a set written for coding agents.
- [**API reference**](https://alexogeny.github.io/wreath/reference/app.html) —
  every public module, generated from the source.
- [**Release notes**](https://alexogeny.github.io/wreath/release_notes/index.html)
  — what changed in each version.

Build the docs locally:

```bash
uv run wreath-docs            # strict build; --serve to watch
```

## Development

```bash
uv sync                       # dev group; builds the native extensions
uv run wreath-check           # ruff, ty, pytest, native lints, trace baseline
uv run pytest                 # the default suite (~3.5s, run serially)
uv run pytest -m '' -n 4      # everything, including network/fuzz/performance
```

Contributing — including the invariants a change must preserve and how to verify
one — is documented in
[`AGENTS.md`](https://github.com/alexogeny/wreath/blob/main/AGENTS.md) and the
cookbook's
[section for coding agents](https://alexogeny.github.io/wreath/cookbook/agents/index.html).

## Benchmarks

Wreath ships equivalent applications across several frameworks and a renderer
that reports medians with their run-to-run range, so a real win can be told from
noise. Results are regenerated locally; render the holistic report with:

```bash
uv run wreath-bench-report    # one report across every benchmark family
```

See [`benchmarks/README.md`](https://github.com/alexogeny/wreath/blob/main/benchmarks/README.md)
for methodology.

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
