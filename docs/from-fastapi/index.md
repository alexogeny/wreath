# Wreath for FastAPI developers

If you have built services with FastAPI, you already know most of Wreath. The
decorators are the ones you expect, routers compose with `include_router`,
`Depends` means what it has always meant — caching, `yield` cleanup and all —
and your app runs under uvicorn today or under Wreath's native server when you
want the speed. This page is the translation you'd otherwise assemble from a
dozen guide pages: the same application in both dialects, a table of
equivalences, and an honest list of the places where Wreath deliberately does
something different.

The two pages after this one go deeper on the rest of the familiar stack:
[Pydantic and validation](pydantic.md), [SQLModel, SQLAlchemy, and the
ORM](sqlmodel.md), and [Alembic and migrations](alembic.md).

## The same application, twice

=== "FastAPI"

    ```python
    from fastapi import APIRouter, FastAPI, HTTPException, Query
    from pydantic import BaseModel

    class NewItem(BaseModel):
        name: str
        price: int
        tags: list[str] = []

    router = APIRouter(tags=["items"])

    @router.get("/items/{item_id}")
    async def show(item_id: int):
        item = STORE.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="no such item")
        return item

    @router.post("/items", status_code=201)
    async def create(item: NewItem, limit: int = Query(20, ge=1, le=100)):
        return save(item)

    app = FastAPI()
    app.include_router(router)
    ```

=== "Wreath"

    ```python
    from dataclasses import dataclass, field
    from typing import Annotated

    from wreath import Request, Router, Wreath
    from wreath.binding import Query
    from wreath.exceptions import NotFound
    from wreath.response import JSONResponse

    @dataclass
    class NewItem:
        name: str
        price: int
        tags: list[str] = field(default_factory=list)

    router = Router(tags=("items",))

    @router.get("/items/{item_id}")
    async def show(request: Request, item_id: int) -> dict:
        item = STORE.get(item_id)
        if item is None:
            raise NotFound("no such item")
        return item

    @router.post("/items")
    async def create(
        request: Request,
        item: NewItem,
        limit: Annotated[int, Query(minimum=1, maximum=100)] = 20,
    ) -> JSONResponse:
        return JSONResponse(save(item), status=201)

    app = Wreath()
    app.include_router(router)
    ```

Run either one the same way:

```bash
uvicorn app:app             # the ASGI server you already use
wreath run app:app          # or Wreath's native HTTP/1.1, /2, and /3 server
```

Read the Wreath column closely and you'll notice every difference that matters
day to day: the handler takes `request` first, the body schema is a dataclass
rather than a Pydantic model, the binding marker lives inside `Annotated` while
the default stays a plain Python default, and the status code is set on the
response you return rather than on the decorator. Each of those is explained
under [What is genuinely different](#what-is-genuinely-different).

## The equivalence table

| FastAPI | Wreath | Notes |
|---|---|---|
| `FastAPI()` | `Wreath()` | |
| `APIRouter(prefix=, tags=, dependencies=)` | `Router(prefix=, tags=, dependencies=, middleware=, permissions=)` | Routers nest and flatten on include; see [Routing](../guides/routing.md) |
| `app.include_router(r, prefix="/v1")` | `app.include_router(r, prefix="/v1")` | Same name, same shape |
| `@app.get / post / put / patch / delete` | Same five decorators | `HEAD` is answered automatically; other methods via `app.route(path, methods=...)` |
| Pydantic model as body | Dataclass or [`wreath.orm` model](sqlmodel.md) as body | Auto-detected from the annotation, no `Body()` marker needed |
| `Query(20, ge=1, le=100)` | `Annotated[int, Query(minimum=1, maximum=100)] = 20` | Plus `overflow="clamp"` to pin instead of reject |
| `Path(alias=...)`, `Header()`, `Cookie()`, `Form()`, `File()` | Same marker names in `wreath.binding` | Inside `Annotated`; `alias=` only |
| `Depends`, `use_cache`, `yield` cleanup | `Depends`, `use_cache`, `yield` cleanup | Same semantics; the callable takes `request` as its first argument |
| Session dependency you wrote yourself | `Annotated[Session, FromORM("main")]` | Request-scoped, lazy, closed for you; see [the ORM page](sqlmodel.md) |
| `background_tasks: BackgroundTasks` parameter | `response.background = BackgroundTask(fn, *args)` | Bound to the response, runs after the body is flushed |
| `FastAPI(lifespan=...)` context manager | `@app.on_startup` / `@app.on_shutdown` | Handlers take the app; run in registration order |
| `app.add_middleware(CORSMiddleware, allow_origins=[...])` | `app.add_middleware(CORSMiddleware(allow_origins=[...]))` | An instance, not class + kwargs; global middleware via `add_global_middleware` |
| `@app.exception_handler(SomeError)` | `@app.exception_handler(SomeError)` | Plus `add_status_handler(404, ...)` keyed by status |
| `raise HTTPException(status_code=404, detail=...)` | `raise NotFound("...")` | One class per status in `wreath.exceptions`; rendered as RFC 9457 problem+json |
| `app.mount("/static", StaticFiles(directory="static"))` | `app.static("/static", "static")` | Consulted only when no route matches |
| `/docs` and `/openapi.json` by default | `app.enable_docs()` | Off until you ask; same paths once enabled |
| `response_model=` | Nothing — return what you mean | The return annotation feeds OpenAPI; see below |
| `status_code=201` on the decorator | `JSONResponse(data, status=201)` | Status lives on the response |
| `TestClient(app)`, sync calls | `async with TestClient(app)`, `await client.get(...)` | Runs lifespan; responses expose `.status`, not `.status_code` |
| `Security`, `OAuth2PasswordBearer` | `app.configure_auth(BearerTokenBackend(...))`, `@authenticated`, `roles`, `permissions` | See [Authentication and authorization](../guides/auth.md) |
| Hand-rolled permission logic | `@authorize(...)` + the built-in Cedar policy engine (`CedarPolicies`) | Policies parse at startup; forbid overrides permit; default deny |
| `@app.websocket(path)` | `@app.websocket(path)` | Handler receives one `WebSocket` |
| Pydantic `BaseSettings` | `wreath.config.load_env` + `WREATH_*` variables | See [Pydantic and validation](pydantic.md#settings) |
| SQLModel / SQLAlchemy | `wreath.orm` over the native PostgreSQL driver | See [SQLModel, SQLAlchemy, and the ORM](sqlmodel.md) |
| Alembic | Keep Alembic for DDL; `wreath migrations detect / check / show` | See [Alembic and migrations](alembic.md) |

## Your requirements.txt, line by line

The table above translates your code. This one translates your dependency file,
because that is the other half of the move and it is usually the half nobody
shows you. Here is a requirements file from an ordinary FastAPI service —
nothing exotic, every line the sort of thing you added on a Tuesday because you
needed rate limiting:

```text title="requirements.txt (before)"
fastapi
uvicorn[standard]
pydantic
pydantic-settings
python-multipart
sse-starlette
slowapi
sqlalchemy
alembic
asyncpg
celery
redis
httpx
tenacity
authlib
python-jose[cryptography]
passlib[bcrypt]
fastapi-users
casbin
boto3
jinja2
whitenoise
fastapi-pagination
structlog
prometheus-fastapi-instrumentator
opentelemetry-sdk
python-dotenv
respx
pillow
stripe
```

Twenty-seven of those thirty lines have a home in Wreath:

| Line | Where it goes |
| --- | --- |
| `fastapi` | `wreath` — the framework itself |
| `uvicorn[standard]` | `wreath run app:app`, or keep uvicorn: Wreath is a conforming ASGI app — [Native server](../guides/server.md) |
| `pydantic`, `python-multipart` | `wreath.binding` — [Binding](../guides/binding.md), [Forms](../guides/forms.md) |
| `pydantic-settings`, `python-dotenv` | `wreath.config` — [Configuration and state](../guides/config-state.md) |
| `sse-starlette` | `SSEResponse` — [Server-Sent Events](../guides/sse.md) |
| `slowapi` | `wreath.middleware` rate limiting — [Middleware](../guides/middleware.md) |
| `sqlalchemy`, `asyncpg` | `wreath.orm` over `wreath.postgres` — [ORM](../guides/orm.md) |
| `alembic` | `wreath migrations detect / check / apply / down` — [Alembic and migrations](alembic.md) |
| `celery` | `wreath.jobs` — durable, Postgres-backed — [Jobs](../guides/jobs.md) |
| `redis` | `wreath.messaging` for pub/sub, `wreath.cache` for caching, the driver's own advisory locks for locking — [Jobs](../guides/jobs.md), [Caching](../guides/caching.md), [Distributed locks](../guides/distributed-locks.md) |
| `httpx`, `tenacity` | `wreath.http_client`, with rate limiting and retries built in — [Outbound HTTP](../guides/http-client.md) |
| `authlib`, `python-jose[cryptography]` | `wreath.auth` — JWT, OAuth2, and OIDC login — [Auth](../guides/auth.md) |
| `fastapi-users`, `passlib[bcrypt]` | `wreath.users` — registration, login, reset, and scrypt password hashing — [User management](../guides/users.md) |
| `casbin` | `wreath.authorization`, a built-in Cedar engine — [Permissions](../guides/permissions.md) |
| `boto3` | `wreath.objects`, over S3 or local disk — [Object storage](../guides/objects.md) |
| `jinja2` | `wreath.templates` — [Templates](../guides/templates.md) |
| `whitenoise` | `app.static(...)` — [Static files](../guides/static-files.md) |
| `fastapi-pagination` | `wreath.pagination` — [Pagination](../guides/pagination.md) |
| `structlog`, `prometheus-fastapi-instrumentator`, `opentelemetry-sdk` | `wreath.logging` and `wreath.telemetry` — [Observability](../guides/observability.md) |

And three lines survive, which is the point of showing you the whole file.
`pillow` and `stripe` are outside what a web framework should have opinions
about. `respx` stays as well: Wreath's `TestClient` drives your app in-process
and `wreath.replay` re-drives it under fault injection, but neither stubs an
*outbound* call, so if you mock upstreams today keep doing it — see
[Testing](../guides/testing.md). Whatever is domain-specific in your own file —
a provider SDK, a parser, a science library — stays too. And `uvicorn` is
listed above as replaced, but only if you want it to be: Wreath is a conforming
ASGI application and your current deployment keeps working unchanged.

If you would rather read this the other way round, ordered by capability instead
of by your file, the [capability map](../capabilities.md) is the same claim
generated from Wreath's own subsystem list.

## What is genuinely different

Wreath is not a re-badged FastAPI, and pretending otherwise would cost you a
confusing afternoon. These are the deliberate differences, with the reasoning.

**`request` is always the first parameter.** Every handler and every dependency
callable receives the `Request` explicitly. FastAPI lets you omit it; Wreath
never hides it, because the request is where identity, state, and the raw body
live, and an explicit parameter is cheaper to understand than an injection rule.

**Markers annotate, defaults default.** In FastAPI, `Query(20, ge=1)` is both
the marker and the default. In Wreath the marker goes in `Annotated` metadata
and the default stays an ordinary Python default — `limit: Annotated[int,
Query(minimum=1)] = 20`. Your function signature remains callable, and readable,
as plain Python. Writing the FastAPI form is a `TypeError` when routes compile,
naming the parameter and the form to write instead: it is the mistake this page
exists to catch, and Wreath would otherwise bind nothing and hand the handler
the marker object itself. Query, header, and cookie values are scalars (`str`,
`int`, `float`, `bool`, and optional unions of them); structured data belongs in
the body.

**There is no `response_model`.** A handler returns a `dict` (sent as JSON), a
`str`, `bytes`, or a [response object](../guides/requests-responses.md) when it
needs a status, headers, or a stream. Nothing re-validates or re-serializes your
output behind your back. The return annotation is still read — it becomes the
response schema in [OpenAPI](../guides/openapi-typegen.md) — but at runtime what
you return is what is sent.

**Errors are RFC 9457 problem+json.** Where FastAPI answers
`{"detail": [...]}`, Wreath answers `application/problem+json` everywhere —
404s, validation failures, and unhandled errors alike. A validation failure
looks like this, with the familiar `loc` / `msg` / `type` entries intact:

```json
{
  "type": "about:blank",
  "title": "Unprocessable Entity",
  "status": 422,
  "detail": "Request validation failed",
  "errors": [
    {"loc": ["body", "price"], "msg": "value is not an integer", "type": "int"}
  ]
}
```

If your clients or tests assert on FastAPI's error shape, this is the first
thing to update.

**Validation is compiled at startup, and Pydantic is not part of it.** Bodies
are dataclasses or ORM models, validated by code generated when the application
starts. A Pydantic model annotation is not accepted. The
[Pydantic page](pydantic.md) maps everything you currently do with `BaseModel`
onto this surface.

**Lifespan is two hooks, not a context manager.** Register
`@app.on_startup` and `@app.on_shutdown` functions that take the app. Wreath
sequences its own resources around yours: databases and ORM registries start
before your startup handlers, and shut down after your shutdown handlers.

**Middleware are instances, and there are two tiers.** You construct the
middleware yourself — `CORSMiddleware(allow_origins=[...])` — and route
middleware wraps matched handlers while global middleware runs on every request,
including 404s. [The middleware guide](../guides/middleware.md) explains why the
distinction earns its keep.

**The test client is async.** `async with TestClient(app)` runs the lifespan;
every call is awaited; responses expose `.status`, `.json()`, `.text`, and
`.header(name)`. If your test suite is already `pytest-asyncio`-shaped, this is
a mechanical change.

**Routers are declarative, and there is no `mount()` for sub-applications.**
Including a router snapshots its routes and folds prefixes, tags, middleware,
dependencies, and permissions into each one — what you see in the file is what
runs. The only mounting is `app.static()` for files on disk.

**The database story is PostgreSQL, natively.** `wreath.orm` sits on Wreath's
own PostgreSQL driver — there is no dialect layer and no other backend. If that
is your database, the [ORM page](sqlmodel.md) will feel familiar quickly; if it
is not, keep your current data layer and use Wreath for the web tier.

## Where to go next

- [Pydantic and validation](pydantic.md) — bodies, constraints, error shapes,
  and settings without `BaseModel`.
- [SQLModel, SQLAlchemy, and the ORM](sqlmodel.md) — models, sessions, and
  queries in `wreath.orm`.
- [Alembic and migrations](alembic.md) — what replaces which command today, and
  what deliberately doesn't yet.
- [Getting started](../getting-started/index.md) — the ground-up introduction,
  if you'd rather build than translate.

## More equivalences (recent additions)

| FastAPI / ecosystem | Wreath |
| --- | --- |
| `as_form` model decorator | `Annotated[Model, Form()]` — [Form-model binding](../guides/forms.md) |
| `EventSourceResponse` / SSE add-ons | `SSEResponse` — [Server-Sent Events](../guides/sse.md) |
| Celery / arq tasks | `app.jobs()` + `@jobs.task` / `jobs.schedule()` — [Jobs](../guides/jobs.md) |
| Cognito / OIDC via `python-jose`/`authlib` | `app.oidc_provider(...)` + `BearerTokenBackend` — [Auth](../guides/auth.md) |
| `fastapi-users` | `app.users(...)` — [User management](../guides/users.md) |
| `sqlalchemy-dlock` | `db.lock(...)` / `session.lock(...)` — [Distributed locks](../guides/distributed-locks.md) |
| `s3path` / hand-rolled S3 helpers | `wreath.objects` — [Object storage](../guides/objects.md) |
| `fastapi-pagination` | `wreath.pagination` — [Pagination](../guides/pagination.md) |
| `aiometer` / `tenacity` on the client | `app.http_client(..., rate=, retries=)` |
| `prometheus-fastapi-instrumentator` | `app.metrics(...)` / `telemetry.activate_*` — [Observability](../guides/observability.md) |

Most of these are rewritten for you automatically — point [`wreath port`](../guides/porting.md) at your app and read the report.
