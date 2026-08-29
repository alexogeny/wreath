---
description: Build an agentic chat control plane that assigns work across every device on an account.
keywords: agent chat device fleet computer provisioning durable jobs resumable streams ownership
---

```hero
eyebrow: Story 02 · agentic work across personal compute
title: Turn your computers into an agent fleet.
lede: A person assigns work from one conversation. Their desktop, laptop, server and GPU box become places the control plane can safely run it.
signal: device presence
signal: durable attempts
signal: resumable output
signal: human intervention
action: See the handoff -> #the-handoff
action: Meet the MCP boundary -> mcp-control-room.md
```

## The scene

A user provisions four computers. Each advertises capabilities rather than a vague
online flag: local files, a GPU, a repository checkout, a trusted network or simply
spare CPU. From a phone, the user asks one conversation to run tests on the desktop,
render on the GPU box and search notes that remain on the laptop.

The central service owns identity, assignment and the durable record. Devices own the
work they have successfully claimed. Chat is the control surface, not the scheduler.

## The handoff

Start a long task on the laptop and close its lid. The conversation distinguishes
four states that a spinner cannot: the device is disconnected, the lease is still
valid, the attempt may now be retried, and another capable device has taken ownership.
Output resumes in the same stream when the replacement begins.

> The invariant: at most one device owns a live attempt, while the user's request and
> its output survive every individual device connection.

## The system shape

```text
phone / browser ──> account conversation ──> durable task
                            │                    │
                      resumable stream       capability match
                                                 │
                             ┌──────────┬────────┴────────┐
                           laptop    desktop          GPU host
```

| Concern | Wreath surface | Role in the story |
|---|---|---|
| account and device identity | `wreath.users`, `wreath.tokens`, `wreath.signatures` | enrolment and bounded credentials |
| one device per attempt | `wreath.entity` | lease-backed ownership and request/reply |
| durable task lifecycle | `wreath.jobs`, `wreath.workflows` | claims, retries, cancellation and deadlines |
| chat and presence | `wreath.rooms`, `wreath.messaging` | conversation membership and cross-worker events |
| output after reconnect | `wreath.streams`, `wreath.progress` | cursors, continuation and status |
| intervention | `wreath.notifications` | reach the person on another channel |

## Build it in four acts

### 1. Provision a device

Create a one-use enrolment action, bind the resulting device to the account and issue
credentials that name its allowed operations. A device should never become equivalent
to the human who owns it.

### 2. Assign one piece of work

Match declared capabilities, create a durable task and let a device claim the attempt.
Show ownership in the conversation. A second device asking for the same attempt gets a
clear refusal.

### 3. Stream the useful part

Separate transient presence from durable output. Reconnect the browser and continue
from its cursor. Cancel the task and prove the underlying device operation receives
the cancellation rather than merely hiding the UI.

### 4. Lose a computer

Expire the ownership lease, select another suitable device and start a new attempt
with the original task context. Keep both attempts in the record so “eventually
finished” never erases what happened.

## Build the control plane

Begin with a central service that knows which account owns each device and routes a
work envelope to the socket for that device. This first cut is live-only on purpose:
an offline device is refused instead of pretending the task was queued.

```python title="app.py"
import asyncio
from dataclasses import dataclass
from typing import Annotated

from wreath import Request, Wreath
from wreath.binding import Body
from wreath.exceptions import Conflict
from wreath.protobuf import encode, field, message
from wreath.rooms import RoomRegistry
from wreath.websocket import WebSocket


@dataclass
class WorkRequest:
    work_id: str
    device_id: str
    instruction: str


@message
class WorkAssignment:
    work_id: str = field(1)
    device_id: str = field(2)
    instruction: str = field(3)
    state: str = field(4)


app = Wreath()
rooms = RoomRegistry()
devices: dict[str, str] = {}
assignments: dict[str, dict] = {}
assignment_lock = asyncio.Lock()


@app.post("/accounts/{account_id}/devices/{device_id}")
async def provision_device(request: Request, account_id: str, device_id: str) -> dict:
    owner = devices.setdefault(device_id, account_id)
    if owner != account_id:
        raise Conflict(f"device {device_id!r} belongs to another account")
    return {"device_id": device_id, "account_id": account_id}


@app.post("/accounts/{account_id}/work")
async def assign_work(
    request: Request,
    account_id: str,
    command: Annotated[WorkRequest, Body()],
) -> dict:
    if devices.get(command.device_id) != account_id:
        raise Conflict(f"device {command.device_id!r} is not in this account")
    room = f"device:{command.device_id}"
    if rooms.members(room) == 0:
        raise Conflict(f"device {command.device_id!r} is offline")

    async with assignment_lock:
        existing = assignments.get(command.work_id)
        if existing is not None:
            return existing
        assignment = {
            "work_id": command.work_id,
            "device_id": command.device_id,
            "instruction": command.instruction,
            "state": "assigned",
        }
        assignments[command.work_id] = assignment

    await rooms.broadcast(room, encode(WorkAssignment(**assignment)))
    return assignment


@app.websocket("/accounts/{account_id}/devices/{device_id}/connect")
async def connect_device(socket: WebSocket) -> None:
    account_id = socket.path_params["account_id"]
    device_id = socket.path_params["device_id"]
    if devices.get(device_id) != account_id:
        await socket.close(code=1008, reason="device is not provisioned to this account")
        return
    room = f"device:{device_id}"
    await socket.accept()
    await rooms.join(room, socket)
    try:
        async for _ in socket:
            pass
    finally:
        await rooms.leave(room, socket)
```

### Drive it from both sides

```python title="test_app.py"
from wreath.protobuf import decode
from wreath.testing import TestClient

from app import WorkAssignment, app, assignments, devices


async def test_work_reaches_only_the_provisioned_device() -> None:
    devices.clear()
    assignments.clear()
    async with TestClient(app) as client:
        await client.post("/accounts/a-1/devices/laptop")
        async with client.websocket("/accounts/a-1/devices/laptop/connect") as device:
            response = await client.post(
                "/accounts/a-1/work",
                json={
                    "work_id": "work-42",
                    "device_id": "laptop",
                    "instruction": "run the test suite",
                },
            )
            delivered = decode(WorkAssignment, await device.receive_bytes())

    assert response.status == 200
    assert delivered.work_id == "work-42"
    assert delivered.instruction == "run the test suite"


async def test_an_offline_device_is_not_fake_queued() -> None:
    devices.clear()
    assignments.clear()
    async with TestClient(app) as client:
        await client.post("/accounts/a-1/devices/gpu-box")
        response = await client.post(
            "/accounts/a-1/work",
            json={
                "work_id": "render-9",
                "device_id": "gpu-box",
                "instruction": "render scene 9",
            },
        )

    assert response.status == 409
    assert "render-9" not in assignments
```

```bash
uv run wreath test -k device
uv run wreath dev app:app
```

## Make assignment durable

Once the interaction is right, replace process-local presence and work with Wreath's
shared owners. A device holds `device:<id>` while its authenticated socket is alive.
Any API worker can ask that holder a question, and durable jobs retain the user's
intent when no suitable computer is connected.

```python title="durable.py"
from dataclasses import dataclass

from wreath import Wreath
from wreath.config import Environment, read_osenv


@dataclass(frozen=True)
class Settings:
    database_url: str


settings = Environment(read_osenv()).bind(Settings)

app = Wreath()
app.postgres("main", dsn=settings.database_url)
app.messaging("fleet", database="main")
devices = app.entities("devices", database="main", bus="fleet", lease=20)
jobs = app.jobs("agent_work", database="main", concurrency=32, lease=60)


@devices.answers("device")
async def run_on_connected_device(device_id: str, envelope: dict) -> dict:
    return await connected_devices[device_id].request(envelope)


@jobs.task("dispatch", retries=20, timeout=45)
async def dispatch(ctx, device_id: str, work_id: str, instruction: str) -> None:
    ctx.report(0.1, f"waiting for {device_id}")
    result = await devices.ask(
        "device",
        device_id,
        {"work_id": work_id, "instruction": instruction, "fence": ctx.fence},
        timeout=30,
    )
    await save_attempt(work_id, ctx.attempt, ctx.fence, result)
    ctx.report(1.0, "result stored")


async def enqueue_work(device_id: str, work_id: str, instruction: str) -> int | None:
    return await jobs.enqueue(
        "dispatch",
        device_id,
        work_id,
        instruction,
        key=f"work:{work_id}",
    )
```

`key=` deduplicates enqueue, not side effects. The device still rejects a stale
`fence`, and the result store still keys attempts by `(work_id, attempt)`. That is what
lets the chat say “the laptop disappeared; attempt 2 continued on the desktop” instead
of flattening both histories into one green tick.

## The larger idea

Agentic products are distributed systems wearing a conversational interface. The
light-bulb is not that a model can call a computer. It is that ownership, cancellation,
progress and recovery can remain explicit while the conversation stays pleasantly
simple.

Next: [give the fleet governed MCP operations](mcp-control-room.md), or
[choose a conventional learning path](../start/paths.md).
