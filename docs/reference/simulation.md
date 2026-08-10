# Transport simulation

`wreath.simulation` runs an interactive peer against Wreath's real server
protocol without opening a socket. It complements [`wreath.replay`](replay.md):
replay re-drives a fixed recording, while a simulator can observe one response
and choose its next transport event.

The layer is deliberately thin:

- `TransportSimulator` feeds bytes through the native or pure HTTP protocol and
  captures exactly what its fake transport writes;
- `WebSocketSimulator` adds a real HTTP upgrade plus masked RFC 6455 client
  frames, then decodes the server's wire frames;
- virtual time and fault coordinates are the existing `VirtualClock` and
  `FaultSchedule` from `wreath.replay`;
- a supplied native Flight Recorder is passed into the native protocol, so
  HTTP and whole-WebSocket-session cells are emitted by the existing C path;
- route handlers, `Calls`, `WebSocketService` capacity, outbound backpressure,
  heartbeat handling, authorization, and application state remain the real
  application components.

There is no scenario DSL or second scheduler. Ordinary async Python is the
scenario language, so conditions, loops, seeded choices, and hundreds of peers
compose with `TaskGroup` instead of being reimplemented as simulator concepts.

## An interactive WebSocket peer

```python
from wreath.simulation import WebSocketSimulator

peer = WebSocketSimulator(
    app,
    "/streams/camera-7",
    subprotocols=("camera-trap.v1",),
)
await peer.start()

reply = await peer.send_text('{"kind":"sighting","count":3}', offset_us=10_000)
assert reply[0].text() == '{"accepted":true}'

result = await peer.close()
assert result.segments_fed == 3  # upgrade, data frame, close frame
```

Each `send_*` call is one stable transport segment. `offset_us` advances the
virtual clock and may not move backwards. The result retains the full raw
handshake/frame response, normalized response bytes, terminal disposition,
write count, and number of input segments actually fed.

The client frame mask and handshake key are deterministic because this is a
socket-free reproduction surface, not a network client. Handshake validation
still checks the server's accept value and refuses a selected subprotocol the
peer did not offer. Extra headers are allowed, but the upgrade, connection,
host, key, version, and subprotocol fields remain owned by the simulator so a
scenario cannot accidentally construct a different handshake while appearing
to use the helper.

## Reuse the fault corpus

```python
from wreath.replay import FaultDescriptor, FaultKind, FaultSchedule
from wreath.simulation import WebSocketSimulator

schedule = FaultSchedule((
    # Segment 0 is the upgrade; segment 1 is the first client frame.
    FaultDescriptor(int(FaultKind.RESET), segment_index=1),
))

peer = WebSocketSimulator(
    app,
    "/treks/ridge",
    subprotocols=("llama-trek.v1",),
    faults=schedule,
)
await peer.start()
await peer.send_text("depart")
result = await peer.close()
```

This is the same reset the fixed-recording replayer uses. `SPLIT`, short read,
truncate, duplicate, half-close, clock jump, and the protocol's own deadline
fault therefore retain one taxonomy and one set of recovery properties. A
reset is delivered after the selected segment, matching transport replay: if a
complete message reaches the handler before the reset, its reply may be written
first.

## Many peers and bounded application flow

```python
import asyncio

from wreath.simulation import WebSocketSimulator

async def walk(name: str) -> None:
    peer = WebSocketSimulator(
        app,
        f"/treks/{name}",
        subprotocols=("llama-trek.v1",),
    )
    await peer.start()
    await peer.send_text("depart")
    await peer.close()

async with asyncio.TaskGroup() as peers:
    for number in range(500):
        peers.create_task(walk(f"ridge-{number}"))
```

If the route delegates to `WebSocketService`, those 500 sessions hit its real
admission limit and bounded per-peer queues. The simulator does not grow a
parallel queue in front of them. This makes queue refusal, disconnect-on-
overflow, heartbeat timeout, shutdown drain, and Flight's WebSocket fan-out
phase observable in the same run.

## Flight Recorder integration

Pass the recorder owned by a running `wreath.server.Server` as `recorder=`. The
native protocol receives that exact object and emits its ordinary completion,
correlation, and phase cells. A WebSocket produces one completion for the whole
session, with protocol `WEBSOCKET`, handshake status, bytes in/out, route id,
and terminal status—the same C code used by a live listener.

The pure HTTP protocol accepts the uniform constructor argument and ignores it,
as it does when selected by the server. Flight is native; there is intentionally
no Python recorder twin.

Fixed recordings accept the same option:

```python
from wreath.replay import replay_transport

result = await replay_transport(app, recording, recorder=server.recorder)
```

::: wreath.simulation.TransportSimulator

::: wreath.simulation.WebSocketSimulator

::: wreath.simulation.SimulatedWebSocketFrame

::: wreath.simulation.SimulationError
