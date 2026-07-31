---
keywords: path parameters, url parameters, route matching, http methods, sub-application
---
# Routing

Routing is how a request finds its handler. In Wreath you describe that mapping
with decorators — on the application directly, or on a `Router` you compose and
then include. There is a single public routing module, `wreath.router`; the
compiled matchers that make it fast are kept private, because you should never
have to think about them to add a route.

```python
from wreath import Wreath, Request

app = Wreath()

@app.get("/items/{id}")
async def show(request: Request, id: int) -> dict:
    return {"id": id}

@app.post("/items")
async def create(request: Request) -> dict:
    return await request.json()
```

`get`, `post`, `put`, `patch`, and `delete` are methods on both `Wreath` and
`Router`, named exactly for the HTTP methods they register. A path segment in
braces — `{id}` — becomes a parameter and is converted to the type you annotate;
if the conversion fails, the caller gets a `422` and your handler is never
entered. The [Binding, validation, and dependencies](binding.md) guide picks up
that thread.

Name a route to reverse it, and use the trailing `path` converter when one value
must contain slashes:

```python
@app.get("/assets/{asset_path:path}", name="asset")
async def asset(request, asset_path: str):
    return {"canonical": request.url_for("asset", asset_path=asset_path)}

app.url_path_for("asset", asset_path="css/site.css")
# /assets/css/site.css
```

`host="{tenant}.example.com"` adds host matching and binds the placeholder by
name. Host-specific routes are checked ahead of a host-agnostic route at the
same path. Ordinary static and single-segment routes stay on the native compiled
table; host and trailing-path routes use an ordered startup-compiled fallback.

## Synchronous handlers

A handler may be `def` instead of `async def` when it never awaits anything:

```python
@app.get("/healthz")
def healthz(request) -> dict[str, str]:
    return {"status": "ok"}
```

Binding, validation, and the response contract work exactly as they do for an
`async def` handler — the only difference is the call convention. A `def`
handler is called directly, so it costs no coroutine object and no suspension
machinery, which is the same trade `before_sync` and `after_sync` make for
middleware.

The word to weigh is *never*. A synchronous handler runs **on the event loop**,
not in a thread pool, so anything that blocks inside one blocks every other
connection this worker is serving — a `requests.get`, a `time.sleep`, a
synchronous database driver. Wreath does not move a `def` handler to a thread
behind your back, because doing so would silently cost more than the coroutine
it saved and would make the fast spelling the slow one. Reach for `def` when the
handler is pure computation over what it was handed, and for `async def`
everywhere else.

A `def` handler's return value is not awaited. Returning a coroutine from one is
a mistake rather than a shorthand: write `async def` and await it.

## User story: a resource with a typed id

> *As an API author, I'm exposing an `orders` resource: fetch one by id, and
> create one. I want the id typed as an integer, so a request to `/orders/abc`
> is turned away before my handler runs — not parsed and re-checked inside it.*

```python
@app.get("/orders/{id}")
async def get_order(request: Request, id: int) -> dict:
    return {"id": id}

@app.post("/orders")
async def create_order(request: Request) -> dict:
    return await request.json()
```

Because `id` is annotated `int`, `/orders/42` reaches the handler as the integer
`42` while `/orders/abc` never does — the caller gets a `422` and the conversion
happens once, on the way in, instead of scattered through your code.

Wreath refuses ambiguous path spellings before routing: percent-encoded `/` or
`\\`, and a decoded backslash receive `400`. Reverse proxies disagree about
whether to decode those forms before ACLs;
refusing them prevents the proxy authorizing one path while the application
activates another.

## Composing routers

As an application grows you will want to keep related routes together and out of
one enormous file. A `Router` is a small, self-contained circle of routes that
you weave into the application when you're ready:

```python
from wreath import Router

items = Router(prefix="/items", tags=("items",))

@items.get("/{id}")
async def show(request, id: int) -> dict:
    return {"id": id}

app.include_router(items)
```

A router can carry a prefix, tags, middleware, dependencies, and permission
requirements — and routers include other routers, so a versioned API composes
naturally: `app.include_router(v1, prefix="/v1")`. Including a router takes a
snapshot of its routes and folds all of that context into each one, so the
arrangement you see in the file is exactly the arrangement that runs — no
hidden precedence to reason about. A `Router` is still declarative and flattened;
when you actually need request-time ASGI composition, mount the application:

```python
app.mount("/service", child_asgi_app, name="service")
```

The child receives the prefix removed from `path` and appended to `root_path`.
Parent global middleware still runs, and response-header edits from its egress
hooks are merged into the child's response. The child retains its own status,
body streaming, routing, and middleware.

**Reference:** [`wreath.router`](../reference/router.md),
[`wreath.app`](../reference/app.md).
