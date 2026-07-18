# Testing

A framework you can't test comfortably is a framework you'll come to distrust.
Wreath's test client runs your application entirely in process — no sockets, no
ports, no fixtures to tear down — while still going through the *whole* pipeline:
middleware, authentication, binding, and your handler, exactly as production
would.

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
