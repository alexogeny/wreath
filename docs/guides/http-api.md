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

## Advertise the shape of related resources

`Link-Template` lets a response describe related resource families without expanding
one link per object. Wreath validates the complete RFC 6570 template when the
`LinkTemplate` is constructed and serializes its RFC 9652 field once:

```python
from wreath.link_template import LinkTemplate
from wreath.response import JSONResponse


async def catalogue() -> JSONResponse:
    response = JSONResponse({"products": 42})
    response.set_link_templates(
        LinkTemplate("/products/{product_id}", rel="item"),
        LinkTemplate("/products{?cursor,limit}", rel="collection"),
    )
    return response
```

The setter replaces any existing `Link-Template` fields, avoiding two declarations
with different meanings. `StreamingResponse`, `SSEResponse` and `FileResponse`
provide the same setter. Attribute values containing Unicode use RFC 9651 Display
Strings; URI templates themselves remain ASCII and must percent-encode non-ASCII
characters.

## Tell OAuth clients how to step up

An API can require a recent or stronger user authentication without inventing an
error body the client has to understand. `oauth_step_up()` reads the standard
`auth_time` and `acr` claims from the verified access-token identity and emits the
RFC 9470 Bearer challenge when either requirement is missing:

```python
from wreath import Request, Wreath
from wreath.auth import BearerTokenBackend, Identity, oauth_step_up


async def verify_access_token(token: str) -> Identity | None:
    claims = await verify_and_decode(token)
    return None if claims is None else Identity(claims["sub"], claims=claims)


app = Wreath()
app.configure_auth(BearerTokenBackend(verify_access_token))


@app.post("/transfers")
@oauth_step_up(max_age=300, acr_values=("urn:example:loa:3",))
async def transfer(request: Request) -> dict[str, bool]:
    return {"transferred": True}
```

A valid but insufficient token receives 401 with
`error="insufficient_user_authentication"`, `max_age="300"` and the requested
`acr_values`. The client can carry those values into a fresh authorization request
and retry with the resulting token. A missing or invalid token still gets the
backend's ordinary Bearer challenge, so the route does not disclose its stronger
requirements to an unauthenticated caller.

Use `second_factor()` for Wreath's browser-session verification flow. It reads
`second_factor_at` and keeps its 403 contract; Wreath refuses to combine the two
decorators because an OAuth reauthorization and an in-app factor prompt are
different recovery paths.

## Carry integrity across every HTTP hop

TLS protects one connection at a time. RFC 9530 `Content-Digest` lets the
application verify the bytes after they have crossed proxies, stores or queues;
`Repr-Digest` describes the complete selected representation even when one response
contains only a range.

Verify an upload before using it:

```python
from wreath import Request


async def upload(request: Request) -> dict[str, str]:
    algorithm = await request.verify_content_digest(required=True)
    body = await request.body()
    return {"verified_with": algorithm, "bytes": str(len(body))}
```

`required=False` accepts a request without the field but still refuses a malformed,
unsupported or mismatching one when present. Wreath checks the strongest active
member it supports, so a field carrying both `sha-256` and `sha-512` cannot make a
bad SHA-512 value pass by falling back to SHA-256.

A response can emit an unsolicited digest or honor the client's weighted preference:

```python
from wreath import Request
from wreath.response import Response


async def download(request: Request) -> Response:
    response = Response(b"stable artifact")
    algorithm = request.preferred_content_digest("sha-512", "sha-256")
    if algorithm is not None:
        response.set_content_digest(algorithm)
    return response
```

Use `set_repr_digest(..., representation=complete_bytes)` for a 206 response; Wreath
refuses to hash the partial body as though it were the complete representation.
`DigestPreferences` and the response `set_want_*_digest()` methods express what the
server wants on future requests. A precomputed integrity field also prevents the
compression policy from changing the bytes after they were hashed.

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
