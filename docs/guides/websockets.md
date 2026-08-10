# WebSockets

Some conversations don't fit the request-and-response shape — a live feed, a
chat, a game. For those, Wreath speaks WebSocket. You declare a handler with the
`websocket` decorator and work with a `WebSocket` connection that you accept,
then read from and write to for as long as the connection lasts.

```python
from wreath.websocket import WebSocket, WebSocketDisconnect

@app.websocket("/ws")
async def ws(connection: WebSocket) -> None:
    await connection.accept()
    try:
        while True:
            message = await connection.receive_text()
            await connection.send_text(f"echo: {message}")
    except WebSocketDisconnect:
        pass
```

The loop runs until the client goes away, which arrives as a
`WebSocketDisconnect` you can catch and clean up around. Because a single
long-lived connection can otherwise ask for unbounded work, the native server
bounds how many messages and fragments one connection may accumulate — see
`ServerConfig` in the [Native server](server.md) guide. You can drive a WebSocket
handler in a test with `WebSocketTestSession` from
[`wreath.testing`](../reference/testing.md), no server required.

`send_json(value)` and `receive_json()` use Wreath's JSON codec when the
application protocol is JSON. Routers carry WebSocket routes too, including
their inherited permissions:

```python
from wreath import Router
from wreath.websocket import WebSocket

streams = Router(prefix="/streams", permissions=("stream:read",))

@streams.websocket("/{name}")
async def stream(connection: WebSocket) -> None:
    await connection.accept()
    request = await connection.receive_json()
    await connection.send_json({"accepted": request["name"]})
```

## Bounded connection ownership

For a large, long-lived connection population, register a `WebSocketService`.
It reserves capacity before accepting, gives every peer a bounded outbound
queue, serializes writes, drains at application shutdown, and exposes counters
through `snapshot`:

```python
from wreath.websocket import Heartbeat, WebSocket, WebSocketService

connections = app.service(
    "streams",
    WebSocketService(
        max_connections=10_000,
        queue_capacity=64,
        overflow="reject",
        heartbeat=Heartbeat(
            frame='{"type":"ping"}',
            acknowledge=lambda frame: frame == '{"type":"pong"}',
        ),
    ),
)

@app.websocket("/streams/{name}")
async def stream(connection: WebSocket) -> None:
    async def handle(frame: str | bytes) -> str | bytes | None:
        return frame

    await connections.serve(
        connection,
        handle,
        key=connection.path_params["name"],
    )
```

The overflow choice is an operational contract: `reject` raises
`ConnectionBackpressure` immediately, `backpressure` waits only up to
`enqueue_timeout`, and `disconnect` closes a peer whose queue is full. All three
remain memory-bounded. Inbound messages are handled sequentially, providing
natural backpressure rather than spawning an unbounded task per frame.

`Heartbeat` is protocol-supplied: Wreath schedules and bounds it but does not
guess a ping message or acknowledgement shape. Acknowledgements are consumed by
default, a missed deadline closes once with code 1011, and
`snapshot.heartbeat_timeouts` records the outcome.

Outbound wire time is recorded using the existing Flight Recorder WebSocket
fan-out phase. There is no second telemetry or comparison pipeline to deploy.

## User story: a live feed the client subscribes to

> *As an API author, my dashboard opens a socket and I want to push it live
> updates — but first it tells me which channel it cares about. I need to read
> that opening message, then stream until the tab closes.*

```python
from wreath.websocket import WebSocket, WebSocketDisconnect

@app.websocket("/live")
async def live(connection: WebSocket) -> None:
    await connection.accept()
    channel = await connection.receive_text()   # first message: the channel name
    try:
        async for update in updates_for(channel):
            await connection.send_text(update)
    except WebSocketDisconnect:
        pass
```

You accept, read the opening message, then write for as long as the client
stays. The disconnect surfaces as a `WebSocketDisconnect` wherever you happen to
be awaiting — inside the `async for` above included — so cleanup is a plain
`except`.

## Request/response on a frame pipe

A socket delivers frames in order and pairs nothing with anything. Most
protocols built on one want a request that gets *an answer*, and every one of
them grows the same three pieces by hand: an identifier on the way out, a map of
what is outstanding, and a deadline so a peer that never answers cannot pin a
caller forever.

`Calls` is those three. The protocol is two functions:

```python
from wreath.websocket import Calls

calls = Calls(
    ws,
    reply_to=lambda message: message.get("reply_to"),   # None means "a request"
    label=lambda identifier, payload: {"id": identifier, **payload},
)

@calls.on_request
async def handle(message: dict) -> dict | None:
    return {"reply_to": message["id"], "ok": True}      # None sends nothing

async with calls:
    answer = await calls.call({"op": "read"}, timeout=seconds(30))
```

Neither `reply_to` nor `label` has a default, and that is deliberate: a guessed
correlation field is a protocol this class does not know it is implementing.

`Calls` **is** the single reader `WebSocket` documents, so a handler using it
must not also iterate the socket. Sends take a lock, because an outgoing request
and an outgoing reply genuinely do race.

Four behaviours are worth knowing before you need them:

| Situation | What happens |
| --- | --- |
| A reply names a call that already timed out | Ignored. A shared pipe carries other callers' frames; this is ordinary, not an error. |
| A second reply to one call | Dropped. The first answer stands. |
| A frame that will not decode | Skipped, and the loop continues — the calls still outstanding on that socket are still answerable. |
| The socket closes with calls outstanding | Every one fails with `CallsClosed`, rather than each waiting out its own deadline for an answer that provably cannot arrive. |

`max_pending` bounds the correlation map, because it is memory the *peer*
controls; a call past it raises rather than queueing, since a queue in front of
a full map is an unbounded one with extra steps. Refusals are counted on
`calls.refusals`.

## Browser-origin and handshake security

CORS does not protect WebSockets, and a browser attaches matching cookies to the
handshake. Install an exact origin allowlist for every browser-facing socket:

```python
from wreath.policy import HttpPolicy, WebSocketOriginPolicy

app.configure_http_policy(HttpPolicy(
    websocket_origin=WebSocketOriginPolicy(["https://app.example"]),
))
```

Missing, malformed, repeated, and unlisted `Origin` values are refused before
`accept`. Handshake-safe global middleware also runs before WebSocket auth:
`ProxyPolicy`, `TrustedHostPolicy`, and a global
`SessionPolicy` therefore apply on both HTTP and WebSocket paths. Encoded
slashes and backslashes are refused before WebSocket routing just as they are
before HTTP routing.

**Reference:** [`wreath.websocket`](../reference/websocket.md).
