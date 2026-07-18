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

## Composing routers

As an application grows you will want to keep related routes together and out of
one enormous file. A `Router` is a small, self-contained circle of routes that
you weave into the application when you're ready:

```python
from wreath import Router

items = Router()

@items.get("/items/{id}")
async def show(request, id: int) -> dict:
    return {"id": id}

app.include(items)
```

A router can carry its own middleware and its own authentication requirements.
When you include it, those are flattened into the application alongside the
route, so the arrangement you see in the file is exactly the arrangement that
runs — no hidden precedence to reason about.

**Reference:** [`wreath.router`](../reference/router.md),
[`wreath.app`](../reference/app.md).
