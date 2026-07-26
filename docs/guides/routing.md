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
hidden precedence to reason about, and no request-time sub-application
dispatch.

**Reference:** [`wreath.router`](../reference/router.md),
[`wreath.app`](../reference/app.md).
