---
description: Build a typed HTTP API with explicit inputs, refusals and tests.
keywords: guide HTTP routing binding validation errors responses TestClient
---

# Build an HTTP API

Start with the wire contract. Wreath compiles the handler signature during lifespan
startup, so unsupported annotations and contradictory routes fail before traffic.

```python title="app.py"
from dataclasses import dataclass
from typing import Annotated

from wreath import Request, Wreath
from wreath.binding import Body, Field
from wreath.exceptions import NotFound
from wreath.response import JSONResponse


@dataclass
class ProductCreate:
    name: Annotated[str, Field(min_length=2, max_length=80)]
    quantity: Annotated[int, Field(ge=0, le=100_000)]


products: dict[int, dict] = {}
app = Wreath()


@app.get("/products/{product_id}")
async def product(request: Request, product_id: int) -> dict:
    try:
        return products[product_id]
    except KeyError:
        raise NotFound(f"product {product_id} does not exist") from None


@app.post("/products")
async def create_product(
    request: Request,
    command: Annotated[ProductCreate, Body()],
) -> JSONResponse:
    product_id = len(products) + 1
    created = {
        "id": product_id,
        "name": command.name,
        "quantity": command.quantity,
    }
    products[product_id] = created
    return JSONResponse(created, status=201)
```

The path capture becomes an `int`; the body becomes a dataclass; constraints run
before the handler. HTTP exceptions become problem responses rather than ad hoc error
dictionaries.

```python title="test_app.py"
from wreath.testing import TestClient

from app import app, products


async def test_create_then_read_a_product() -> None:
    products.clear()
    async with TestClient(app) as client:
        created = await client.post(
            "/products",
            json={"name": "pink cable", "quantity": 12},
        )
        fetched = await client.get("/products/1")

    assert created.status == 201
    assert fetched.json() == created.json()


async def test_invalid_input_never_reaches_the_store() -> None:
    products.clear()
    async with TestClient(app) as client:
        malformed = await client.post(
            "/products",
            json={"name": "x", "quantity": -1},
        )
        missing = await client.get("/products/404")

    assert malformed.status == 422
    assert missing.status == 404
    assert products == {}
```

## Publish the schema deliberately

Call `enable_api_docs()` after registering the routes it should describe. The docs
page and OpenAPI document come from the same compiled signatures as request binding
and generated clients:

```python title="api_docs.py"
from app import app


app.enable_api_docs(
    path="/docs",
    spec_path="/openapi.json",
    environments=("development", "staging"),
    authenticated=True,
    permissions=("docs:read",),
    try_it_out=False,
    title="Catalogue API",
    version="1.0.0",
)
```

The current environment is the explicit `env=` argument, then `WREATH_ENV`, then
`"production"`. If it is not listed, neither route is registered and both paths
return 404. Authentication and permission checks use the application's configured
identity backend; `auth=` can instead give these two routes their own backend.

`enable_docs()` is the compatibility shorthand. It publishes `/docs` and
`/openapi.json` only in Wreath's non-production environments by default, but it cannot
express the authorization options above. Passing `environments=None` registers the
routes everywhere; without an auth requirement that makes both surfaces public. In
production, expose them only as an explicit, reviewed decision—normally behind
authentication, with `try_it_out` disabled unless operators genuinely need it.

```bash
uv run wreath test -k product
uv run wreath dev app:app
uv run wreath typegen app:app --target typescript --output client.ts
```

Use [application and HTTP reference](../reference/application.md) for every binding
source, response class and router method.
