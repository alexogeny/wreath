---
description: Test ASGI behavior, WebSockets, declaration refusals and production evidence.
keywords: guide testing TestClient WebSocket test runner mutation fuzz replay
---

# Testing and evidence

Test the boundary callers observe. Wreath's client runs lifespan, HTTP and WebSocket
ASGI messages in process without replacing the application with a different adapter.

```python title="app.py"
from wreath import Request, Wreath
from wreath.protobuf import field, message
from wreath.websocket import WebSocket


@message
class Ping:
    kind: str = field(1)


app = Wreath()


@app.get("/sum/{left}/{right}")
async def add(request: Request, left: int, right: int) -> dict:
    return {"total": left + right}


@app.websocket("/echo")
async def echo(socket: WebSocket) -> None:
    await socket.accept()
    async for message in socket:
        await socket.send(message)
```

```python title="test_app.py"
from wreath.protobuf import decode, encode
from wreath.testing import TestClient

from app import Ping, app


async def test_http_binding_and_refusal() -> None:
    async with TestClient(app) as client:
        good = await client.get("/sum/20/22")
        bad = await client.get("/sum/twenty/22")

    assert good.json() == {"total": 42}
    assert bad.status == 422


async def test_websocket_frames() -> None:
    async with TestClient(app) as client:
        async with client.websocket("/echo") as socket:
            await socket.send_bytes(encode(Ping(kind="ping")))
            response = decode(Ping, await socket.receive_bytes())

    assert response.kind == "ping"
```

Run focused work and the ordinary suite through Wreath's runner:

```bash
uv run wreath test -k echo
uv run wreath test
uv run wreath test -m ''
uv run wreath-check
```

The runner adds parallel collection, timing history, state tracking, bounded mutation
confidence and schedule fuzzing. Keep a focused test green in the same change; do not
park unfinished behavior behind skips or expected failures. Use request recording and
deterministic replay when the defect depends on a production-shaped sequence.

See [testing API](../reference/application.md) and
[recording and replay API](../reference/operations.md).
