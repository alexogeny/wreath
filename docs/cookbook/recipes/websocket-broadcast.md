# Broadcast to many WebSocket clients

A live feed, a chat room, a dashboard — many sockets, one stream of updates. The
trick is to keep a set of the accepted connections and write to each when
something happens. Register a handler with `@app.websocket`, accept the
connection, add it to the set, then read until the client goes away:

```python
from wreath.websocket import WebSocket, WebSocketDisconnect

clients: set[WebSocket] = set()

@app.websocket("/feed")
async def feed(connection: WebSocket) -> None:
    await connection.accept()
    clients.add(connection)
    try:
        while True:
            await connection.receive_text()      # keep the connection alive
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(connection)
```

To push an update to everyone, iterate the set and `send_text` — dropping any
connection that has since disconnected:

```python
async def broadcast(message: str) -> None:
    for connection in list(clients):
        try:
            await connection.send_text(message)
        except WebSocketDisconnect:
            clients.discard(connection)
```

The disconnect surfaces as a `WebSocketDisconnect` wherever you happen to be
awaiting — the `finally` is what keeps the set from leaking closed sockets.

`WebSocket` sends text with `send_text` and bytes with `send_bytes` (or `send`,
which picks by type) — there is no `send_json`, so serialize your own payload
first, e.g. `connection.send_text(dumps(payload))`. The native server bounds how
many messages one connection may accumulate, so a single client can't ask for
unbounded work.
