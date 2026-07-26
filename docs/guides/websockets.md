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

**Reference:** [`wreath.websocket`](../reference/websocket.md).
