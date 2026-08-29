---
description: Choose rooms, streams, jobs or workflows by the lifetime of the work.
keywords: guide WebSocket rooms messaging streams jobs workflows durable realtime
---

# Realtime and durable work

Live fan-out and durable work are different promises. A room lasts while sockets are
connected. A workflow records completed steps and resumes after a process disappears.

## Broadcast to connected clients

```python title="app.py"
from wreath import Wreath
from wreath.rooms import RoomRegistry
from wreath.websocket import WebSocket

app = Wreath()
rooms = RoomRegistry()


@app.websocket("/rooms/{room}")
async def room_socket(websocket: WebSocket) -> None:
    room = websocket.path_params["room"]
    await websocket.accept()
    await rooms.join(room, websocket)
    try:
        async for message in websocket:
            await rooms.broadcast(room, message)
    finally:
        await rooms.leave(room, websocket)
```

```python title="test_app.py"
from wreath.testing import TestClient

from app import app, rooms


async def test_a_room_broadcasts_only_while_connected() -> None:
    async with TestClient(app) as client:
        async with client.websocket("/rooms/dispatch") as socket:
            await socket.send_text("vehicle-7 ready")
            assert await socket.receive_text() == "vehicle-7 ready"
            assert rooms.members("dispatch") == 1
    assert rooms.members("dispatch") == 0
```

Pass `app.message_bus("main")` to `RoomRegistry` for cross-worker fan-out. Delivery is
still at-most-once and ephemeral; persist anything that must survive disconnection.

## Resume a multi-step effect

```python title="workflow.py"
from wreath.workflows import InMemoryWorkflowStore, Workflow

store = InMemoryWorkflowStore()
checkout = Workflow("checkout")


@checkout.step
async def reserve_stock(context) -> str:
    return "reservation-42"


@checkout.step
async def charge(context) -> str:
    return f"charged:{context.results['reserve_stock']}"
```

```python title="test_workflow.py"
from workflow import checkout, store


async def test_one_instance_key_does_not_repeat_completed_steps() -> None:
    first = await checkout.run(store=store, key="checkout-42")
    second = await checkout.run(store=store, key="checkout-42")

    assert first.results["charge"] == "charged:reservation-42"
    assert second.results == first.results
```

Use `PostgresWorkflowStore` when restart survival is the requirement. Use jobs for one
durable attempt, streams for resumable output and entity leases for one live owner.
Their exact contracts are in [realtime and durable work](../reference/realtime.md).
