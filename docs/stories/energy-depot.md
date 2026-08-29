---
description: Design a realtime EV depot that balances chargers, vehicles and microgrids under contention.
keywords: EV charging microgrid websocket room concurrency lease telemetry dispatch
---

```hero
eyebrow: Story 01 · physical systems under contention
title: Balance a live energy depot.
lede: Thousands of vehicles want power. Several microgrids have different limits. Operators need one live picture while workers compete to make the next decision.
signal: realtime rooms
signal: exclusive ownership
signal: scheduled work
signal: live energy series
action: See the system shape -> #the-system-shape
action: Choose a build path -> ../start/paths.md
```

## The scene

It is 5:42 pm. Buses are returning, delivery vans are arriving early and a battery
bank has stopped reporting. Every vehicle has a departure time and a minimum useful
charge. Every charger has a state. Every microgrid has a limit that can change while
the allocator is running.

The operator sees one room per depot. A vehicle reconnects and its tile returns.
A transformer loses headroom and the allocation changes in front of everyone. The
screen feels immediate, but the power budget underneath it is never optimistic.

## The moment

Open two control rooms. Introduce a sudden 20% shortfall while hundreds of charger
updates arrive. Both rooms converge on the same allocation, no charger receives two
commands, and the combined plan never exceeds the remaining capacity.

> The invariant: one active owner may command a charger, and all accepted allocations
> fit inside the current microgrid budget.

That sentence is the architecture. The rest is arranging where it is enforced and
how the result reaches people.

## The system shape

```text
meters + chargers ──> ingest ──> depot state ──> allocation workflow
       │                              │                  │
       └── telemetry series           └── room events    └── device commands
                                             │
                                      operator screens
```

The request path accepts typed device reports. `wreath.entity` gives a charger one
live owner. `wreath.jobs` and `wreath.workflows` carry dispatch across process
boundaries. `wreath.rooms` and `wreath.messaging` move the accepted state to whichever
worker holds an operator's WebSocket. `wreath.series` turns the same reports into
honest historical views.

| Pressure | Wreath surface | What it owns |
|---|---|---|
| many operators watching | `wreath.rooms`, `wreath.websocket` | membership and live delivery |
| workers competing for a charger | `wreath.entity` | ownership, leases and fencing |
| dispatch that must survive | `wreath.jobs`, `wreath.workflows` | attempts, retries and progress |
| changing capacity over time | `wreath.series`, `wreath.temporal` | buckets, comparison and late data |
| many processes | `wreath.messaging` | cross-worker fan-out |
| a failure worth reproducing | `wreath.recording`, `wreath.replay` | bounded evidence and deterministic replay |

## Build it in four acts

### 1. Make the depot visible

Model vehicles, chargers and meter readings. Accept reports and publish only accepted
state into the depot room. The first screen should already feel alive.

### 2. Add the hard boundary

Introduce charger ownership and the shared capacity claim. Race two workers on
purpose. The loser should receive a named refusal rather than discovering the
collision after sending a command.

### 3. Make allocation durable

Move rebalancing into a workflow. Give each step a deadline and report progress.
Disconnect a worker between calculating and dispatching; the next attempt must know
which effects already happened.

### 4. Break the physical assumptions

Simulate stale meter readings, a disappearing transformer and a reconnect storm.
Arm a recording around the incident, then replay it without the live depot.

## Build the first vertical slice

This version is intentionally one process: the lock, capacity ledger and room live
with the application. It is enough to prove the important behavior before adding a
database. Save it as `app.py`.

```python title="app.py"
import asyncio
from dataclasses import dataclass, field
from typing import Annotated

from wreath import Request, Wreath
from wreath.binding import Body
from wreath.exceptions import Conflict
from wreath.protobuf import encode, field as wire_field, message
from wreath.rooms import RoomRegistry
from wreath.websocket import WebSocket


@dataclass
class AllocationRequest:
    charger_id: str
    kilowatts: float


@message
class AllocationUpdate:
    charger_id: str = wire_field(1)
    kilowatts: float = wire_field(2)
    allocated_kw: float = wire_field(3)
    headroom_kw: float = wire_field(4)


@dataclass
class Depot:
    capacity_kw: float
    allocations: dict[str, float] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def allocate(self, charger_id: str, kilowatts: float) -> dict:
        async with self.lock:
            proposed = sum(self.allocations.values())
            proposed -= self.allocations.get(charger_id, 0.0)
            proposed += kilowatts
            if proposed > self.capacity_kw:
                raise Conflict(
                    f"{proposed:g} kW requested; depot limit is {self.capacity_kw:g} kW"
                )
            self.allocations[charger_id] = kilowatts
            return {
                "charger_id": charger_id,
                "kilowatts": kilowatts,
                "allocated_kw": proposed,
                "headroom_kw": self.capacity_kw - proposed,
            }


app = Wreath()
rooms = RoomRegistry()
depots = {"north": Depot(capacity_kw=100.0)}


@app.post("/depots/{depot_id}/allocations")
async def allocate(
    request: Request,
    depot_id: str,
    command: Annotated[AllocationRequest, Body()],
) -> dict:
    result = await depots[depot_id].allocate(command.charger_id, command.kilowatts)
    await rooms.broadcast(
        f"depot:{depot_id}",
        encode(AllocationUpdate(**result)),
    )
    return result


@app.websocket("/depots/{depot_id}/live")
async def live_depot(socket: WebSocket) -> None:
    room = f"depot:{socket.path_params['depot_id']}"
    await socket.accept()
    await rooms.join(room, socket)
    try:
        async for _ in socket:
            pass
    finally:
        await rooms.leave(room, socket)
```

The lock makes the capacity check and write one operation. The room sees only the
accepted result; a rejected allocation is never broadcast as if it happened.

### Prove the race, not just the happy path

```python title="test_app.py"
import asyncio

from wreath.protobuf import decode
from wreath.testing import TestClient

from app import AllocationUpdate, app, depots


async def test_two_chargers_cannot_spend_the_same_headroom() -> None:
    depots["north"].allocations.clear()
    async with TestClient(app) as client:
        first, second = await asyncio.gather(
            client.post(
                "/depots/north/allocations",
                json={"charger_id": "c-1", "kilowatts": 70},
            ),
            client.post(
                "/depots/north/allocations",
                json={"charger_id": "c-2", "kilowatts": 70},
            ),
        )

    assert sorted((first.status, second.status)) == [200, 409]
    assert sum(depots["north"].allocations.values()) == 70


async def test_an_operator_sees_the_accepted_allocation() -> None:
    depots["north"].allocations.clear()
    async with TestClient(app) as client:
        async with client.websocket("/depots/north/live") as socket:
            response = await client.post(
                "/depots/north/allocations",
                json={"charger_id": "c-7", "kilowatts": 24},
            )
            event = decode(AllocationUpdate, await socket.receive_bytes())

    assert response.status == 200
    assert event.headroom_kw == 76
```

Run it:

```bash
uv run wreath test -k depot
uv run wreath dev app:app
```

## Lift it across workers

An `asyncio.Lock` protects one process. Production contention needs a shared owner,
and production fan-out needs a bus. The application owns both declarations:

```python title="infrastructure.py"
from dataclasses import dataclass

from wreath import Wreath
from wreath.config import Environment, read_osenv
from wreath.rooms import RoomRegistry


@dataclass(frozen=True)
class Settings:
    database_url: str


settings = Environment(read_osenv()).bind(Settings)

app = Wreath()
app.postgres("main", dsn=settings.database_url)
bus = app.messaging("depot", database="main")
entities = app.entities(database="main", bus="depot", lease=15)
dispatch = app.jobs("dispatch", database="main", concurrency=16, lease=30)
rooms = RoomRegistry(bus)


@entities.answers("charger")
async def answer_charger(charger_id: str, payload: dict) -> dict:
    return await connected_chargers[charger_id].request(payload)


@dispatch.task("apply_allocation", retries=8, timeout=20)
async def apply_allocation(ctx, charger_id: str, kilowatts: float) -> None:
    await entities.ask(
        "charger",
        charger_id,
        {"op": "set_limit", "kilowatts": kilowatts, "fence": ctx.fence},
        timeout=10,
    )
```

The handler stays idempotent because jobs are at-least-once. The fence travels to the
device so an old worker cannot apply a command after its lease has moved. Generate and
apply Wreath's owned tables before starting more than one worker:

```bash
uv run wreath schema sql app:app > schema.sql
psql "$DATABASE_URL" -f schema.sql
uv run wreath run app:app --loop metal --workers 4
```

## What this story proves

Realtime does not mean putting every event on a socket. It means deciding which state
is authoritative, making contention explicit, and then delivering accepted changes
quickly. Wreath lets the room, job, lease and series share that model instead of each
inventing a slightly different depot.

Next: [turn the same ownership model into a fleet of computers](agent-fleet.md), or
[browse the Wreath surface](../reference/index.md).
