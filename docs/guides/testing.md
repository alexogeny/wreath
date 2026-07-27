# Testing

A framework you can't test comfortably is a framework you'll come to distrust.
Wreath's test client runs your application entirely in process — no sockets, no
ports, no fixtures to tear down — while still going through the *whole* pipeline:
middleware, authentication, binding, and your handler, exactly as production
would.

## User story: prove the auth gate actually rejects

> *As an API author, I need a test that a protected endpoint returns `401` with no
> token and `200` with one — going through the real middleware and auth, not a
> mock — so the test fails the day I misconfigure the guard.*

```python
from wreath.testing import TestClient

async def test_requires_auth():
    async with TestClient(app) as client:
        assert (await client.get("/account")).status == 401

        ok = await client.get("/account", headers={"Authorization": f"Bearer {token}"})
        assert ok.status == 200
        assert ok.json()["id"] == "u_123"
```

Nothing is stubbed: the request travels the same middleware, authentication, and
binding path production uses, so a passing test means a real caller sees the same
result. `headers=` sets request headers, `json=` sends a body, and `.json()` /
`.status` read the response.

```python
from wreath.testing import TestClient

async def test_create():
    async with TestClient(app) as client:
        response = await client.post("/items", json={"name": "widget"})
        assert response.status == 201
```

Because the client exercises the real request path, a passing test means the
behaviour a user would see is correct — not merely that a function returns the
right value in isolation. The lifespan runs too, so startup and shutdown logic is
covered. WebSocket handlers get their own `WebSocketTestSession` for driving a
conversation and asserting on what comes back.

## User story: the same request, as three different people

> *Authorization tests are the same call repeated per role. Doing that with
> tokens means every test carries a `Bearer` literal that has nothing to do with
> what it is checking, and minting a real token per role is a fixture nobody
> wants to maintain.*

`acting_as` gives you a client that *is* someone:

```python
async def test_only_editors_may_edit_a_llama():
    async with TestClient(app) as client:
        admin = client.acting_as("root", roles=["admin"])
        editor = client.acting_as("ada", roles=["editor"])
        rider = client.acting_as("bo", roles=["rider"])

        assert (await rider.patch("/llamas/7", json={"name": "Bea"})).status == 403
        assert (await editor.patch("/llamas/7", json={"name": "Bea"})).status == 200
        assert (await admin.delete("/llamas/7")).status == 200
```

Each derived client shares the application and its lifespan, so make as many as
you have roles. The identity travels on the request rather than on the backend,
so `admin` and `rider` can have calls in flight simultaneously without
interfering — which matters as soon as you write a concurrency test.

Pass a whole `Identity` when you need permissions or a non-default principal
type; pass an id with `roles=`/`permissions=` for the common case. Passing both
is an error, because two sources for the same fact is how a test ends up lying
about what it covers.

!!! warning "It bypasses authentication"

    While an acting-as client exists, the application's authentication backend
    is replaced with one that trusts the request scope, and it is restored when
    the client exits. That is the right trade for testing *authorization* and
    the wrong one for testing *authentication* — use a real token there, as in
    the first example.

For headers you want on every request without touching identity, there is
`client.with_headers(x_tenant="acme")`.

**Reference:** [`wreath.testing`](../reference/testing.md).
