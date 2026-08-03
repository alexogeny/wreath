---
description: Addressing a thing that lives on one worker — leased ownership of a name, and a question that reaches whoever holds it.
keywords: websocket gateway, sticky session, actor, virtual actor, connection affinity, stateful worker, device fleet
---
# Entities and addressing

A WebSocket gateway is stateful and sits behind a stateless load balancer. The
socket for device `abc` lives on exactly one worker; the request that needs to
talk to it arrives on any of them. So does the next one, and it is usually a
different worker.

Every system with that shape grows the same three things by hand:

1. a channel on a broker, one per connection, so a message can be aimed;
2. an ownership table with a heartbeat, so two workers do not both believe they
   hold the socket after a reconnect;
3. a correlation map with a timeout, so a reply can be matched to its question.

`wreath.entity` is those three, once.

```python
from wreath.temporal import seconds

app.postgres("main", dsn=...)
app.messaging("events", database="main")
entities = app.entities(database="main", bus="events")

@app.websocket("/device/{name}")
async def device_socket(ws, name: str) -> None:
    if await entities.hold("device", name) is None:
        await ws.close(code=1013)      # another worker has it
        return
    await ws.accept()
    SOCKETS[name] = ws
    try:
        async for message in ws:
            ...
    finally:
        SOCKETS.pop(name, None)
        await entities.release("device", name)

@entities.answers("device")
async def talk_to_device(name: str, payload: dict) -> dict:
    return await SOCKETS[name].request(payload)   # local: we hold this one

# ... and from any worker, in an ordinary handler:
@app.post("/device/{name}/read")
async def read(request, name: str) -> dict:
    return await entities.ask("device", name, {"op": "read"}, timeout=seconds(30))
```

The handler doing the asking never learns which worker answered, and does not
have to.

## Ownership is a lease and a fence

`hold` is one `INSERT ... ON CONFLICT DO UPDATE ... WHERE`, so **a row coming
back is the claim**. There is no read-then-write window in which two workers
both conclude they were first — the same argument
[`wreath.store`](../reference/store.md) already makes for keyed state, applied
to names.

The `fence` moves when a name **changes hands** and not when the same holder
renews. That is [`wreath.jobs`](jobs.md)' rule, and for the same reason: a fence
that moved on renewal would invalidate the holder's own in-flight work. Read it
before you write anything a superseded holder must not land.

```python
lease = await entities.hold("device", "abc")
if lease is not None:
    await record(reading, fence=lease.fence)   # a stale holder's write matches nothing
```

Two failure modes are worth stating plainly rather than discovering:

- **A lease does not stop the world.** There is no heartbeat, so a holder still
  running when its lease expires is superseded by whoever takes the name next.
  The fence stops the loser's *bookkeeping* landing; it cannot stop the loser's
  side effects.
- **`release` is scoped to the owner.** A worker whose lease already lapsed
  cannot delete its successor's row while tidying up on the way out, which is
  the shutdown race a naive `DELETE` loses.

## Holding is a block, not a pair of calls

`hold`/`release` are there, and `holding` is what you reach for:

```python
async with entities.holding("device", name, grace=seconds(2)) as lease:
    if lease is None:
        return                      # another worker has it
    ...
```

Three things come with it, and each is a loop an application would otherwise
write once per name:

- **Renewal.** A held name joins the batch the registry's tick extends. That
  tick is **one statement however many names a worker holds** — `WHERE owner =
  $1` is the whole selector — because a statement per name per tick is a round
  trip per entity per tick, which is the cost that stops this scaling.
- **Loss.** There is no heartbeat, so the renewal result is the only way to
  learn a name was taken. Names missing from it are dropped locally (so a later
  `release` cannot delete the new holder's row) and counted on
  `registry.lost`. Pass `on_lost=` to react. **Non-zero `lost` is the number
  that says a gateway is quietly shedding ownership** — otherwise it looks
  exactly like clients reconnecting.
- **Grace.** `grace=` keeps the lease for a moment after the block exits, so a
  caller that reconnects inside the window *renews* instead of re-acquiring —
  no handover, no fence bump, for something that never moved. It is applied by
  the same tick, not by a timer per name.

The registry is a `wreath.services.Service`, so `app.entities()` starts the tick
after the bus it asks over and drains before it. Draining **releases everything
in one statement**: a rolling deploy that waited out the leases would park every
name for a full lease, and the lease is sized for crash detection, where waiting
is the only option. A clean shutdown has better information.

## Asking, and the two ways it fails

`ask` refuses in two distinguishable ways, because they want different operator
responses:

| Raised | Means |
| --- | --- |
| `NotHeld` | nothing live holds the name — a missing device, or one whose worker died |
| `Unanswered` | a holder existed and did not reply inside the deadline — a wedged worker |

`NotHeld` is decided **before** publishing, by reading the ownership table. An
ephemeral publish to nobody is a silent no-op, and waiting the full deadline to
learn a simple fact is a bad diagnosis.

The transport is [`wreath.messaging`](../reference/messaging.md)'s ephemeral
fan-out — a `NOTIFY`, at-most-once. That is the right tier for a question with a
deadline: a durable queue would replay a question whose asker gave up minutes
ago. It also means **a timeout is an ordinary outcome**, not a surprise.

One channel carries the whole registry rather than one per name, because a
channel per entity is a `LISTEN` per entity and the entire point is the case
where there are a hundred thousand of them. Every worker therefore sees every
message; filtering is what makes that correct, and it is tested rather than
assumed.

## What it does not do

**It does not replace `SingletonRunner`.** An advisory lock releases the instant
its connection drops, which is a *better* failure detector than a lease — there
is no expiry to wait out. It costs a held connection per lock, which is right
for "one process runs this loop" and wrong for a hundred thousand devices. Two
mechanisms for two shapes; folding the better one into the weaker one to save a
class would be a downgrade wearing a tidy-up's clothes. See
[Distributed locks](distributed-locks.md).

**It is not a general actor runtime.** There is no mailbox queue per entity, no
supervision tree, no state persistence, and no automatic migration of an entity
to a new worker. What is here is the addressing: who holds a name, and how to
reach them.

**The pending map is bounded.** `max_pending` caps questions in flight and an
`ask` past it refuses immediately, because that map is memory a remote caller
would otherwise control. Refusals are counted on `registry.refusals`, and
questions that reached no holder on `registry.unrouted`.
