# Server-Sent Events

When the browser only needs to *listen* — a progress bar, a live feed, a token stream — a full WebSocket is more machinery than the job wants. Server-Sent Events are a plain HTTP response that never quite ends, and `SSEResponse` frames them for you.

`SSEResponse` is the transport. It sets the `text/event-stream` content type and the no-buffering headers (`cache-control: no-cache`, `x-accel-buffering: no`) that keep proxies from swallowing the stream, then frames each item your async iterator yields. What you send over it — a progress convention, an event schema — is yours to define.

## User story: a live notifications feed

> *As an API author, my web app has a notification bell that should light up the
> moment something happens — a mention, a finished export. The browser only needs
> to listen, so I want a plain `EventSource` endpoint that pushes typed events as
> they occur and reconnects on its own.*

```python
from wreath.response import SSEResponse, ServerSentEvent

@app.get("/notifications")
async def notifications(request):
    async def stream():
        async for note in notifications_for(request.identity.id):
            yield ServerSentEvent(data=note.text, event=note.kind, id=note.id)
    return SSEResponse(stream())
```

The browser opens it with `new EventSource("/notifications")` and can listen per
`event` type; the `id` on each event lets it resume with `Last-Event-ID` after a
dropped connection. No polling, no WebSocket — just an HTTP response that stays
open.

## An event stream

```python
import asyncio
from wreath.response import SSEResponse, ServerSentEvent

@app.get("/events")
async def events(request):
    async def stream():
        for i in range(100):
            yield ServerSentEvent(data=str(i), event="tick", id=str(i))
            await asyncio.sleep(1)
    return SSEResponse(stream())
```

Each `ServerSentEvent` field is optional. `data` may be multi-line (each line is framed as its own `data:` field); `event` names the event type an `EventSource` listener can filter on; `id` lets the browser resume with `Last-Event-ID`; `retry` sets the client's reconnection delay.

A comment-only event is a keep-alive — send one periodically so intermediaries don't time the connection out:

```python
yield ServerSentEvent(comment="ping")
```

A bare `str` or `bytes` you yield is treated as the `data` field, and a mapping is unpacked into the event fields — so the simplest stream is just:

```python
return SSEResponse(f"line {i}" async for i in source())
```

For duplex communication — the client needs to *send* as well as receive — reach for [WebSockets](websockets.md) instead.
