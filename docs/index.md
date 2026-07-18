# Wreath

A wreath is a circle of separate things — leaves, branches, small flowers —
gathered and woven until they hold a single shape. That is the idea behind this
framework. A web application is made of many parts: routing, validation,
authentication, data access, and the server that carries it all. Wreath gathers
those parts into one coherent whole and gives each of them a clear, honest place
to live.

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
  anywhere with `WREATH_PURE=1`.
- **One obvious home per idea.** A small top level — `Wreath`, `Router`,
  `Request`, `Response`, `JSONResponse`, `Depends` — and a cohesive module for
  everything else, from `wreath.middleware` to `wreath.orm`.
- **The whole circle, not a starter kit.** Middleware, authentication and
  authorization (with a Cedar policy engine), WebSockets, an outbound HTTP
  client, signed webhooks, templates, an ORM over a native Postgres driver,
  OpenAPI with typed client generation, and an in-process test client.
- **Correctness before cleverness.** Validation compiled at startup, a firm line
  between configuration and runtime state, and a test suite that treats ASGI
  semantics as the contract Wreath must keep.

## Find your way in

- **[Getting started](getting-started/index.md)** — install Wreath and build
  your first application, start to finish.
- **[Guides](guides/routing.md)** — a page for each part of the framework, with
  the reasoning behind it, not just the syntax.
- **[Cookbook](cookbook/index.md)** — practical recipes for developers, and a
  set written for the coding agents who work in this codebase.
- **[API reference](reference/app.md)** — every public module, generated from
  the source so it never drifts.

Wreath is pre-1.0. Expect it to grow — and expect what already works to keep
working as it does.
