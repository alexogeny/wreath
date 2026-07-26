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

**Reference:** [`wreath.testing`](../reference/testing.md).
