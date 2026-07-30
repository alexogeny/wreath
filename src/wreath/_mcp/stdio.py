"""`wreath mcp stdio`: the same server, behind a pipe.

The supported deployment is a remote HTTP endpoint, which is where authorization
and audit are worth anything. But an editor on someone's laptop usually speaks
only stdio, and telling that person to run a web server and a tunnel to try a
tool out is how a framework loses the first ten minutes.

So this is a **relay and nothing else**. Lines of JSON arrive on stdin and are
POSTed at the application's own MCP route; the reply is written back to stdout.
The session's `GET` stream is opened once and everything on it -- a progress
report, a subscribed resource changing, a `sampling/createMessage` this server
is asking the editor for -- is written to stdout as it arrives. The editor's
answers arrive on stdin as ordinary JSON-RPC responses and are POSTed like
anything else, which is what makes elicitation work here with no extra code.

**There is no second dispatch path, deliberately.** Routing, authentication,
`MCPLimits`, the Flight Recorder marker and the exception boundary are the ones
the HTTP endpoint has, because it *is* the HTTP endpoint: `wreath.testing`'s
in-process client runs the application's lifespan and calls the ASGI callable
directly. A wrapper that parsed the envelope itself and called the registry
would be a second server wearing the first one's name, and the day it drifted
would be the day a tool behaved differently for one transport.

Three details a reader will look for, and the third is the one that bites.

The stream is consumed *incrementally* rather than through
`TestClient.request`, which collects a whole response and would therefore never
return on a stream that stays open. Stdin is read on a worker thread, because a
blocking read on the event loop would stop the stream from being written while
the server waits for a line.

And **each inbound message is dispatched as its own task**, not awaited in turn.
A tool that elicits parks its POST until the client answers, and the client's
answer is the *next line on stdin*: a loop that awaited each POST before reading
again would be waiting for a line it had made itself unable to read. That is the
same reentrancy `_mcp/outbound.py` is about, one layer out, and it deadlocks a
relay that looks obviously correct. `initialize` is the exception and is awaited,
because everything after it needs the session id it returns.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from typing import Any

#: What an SSE `data:` line carries here: one framed JSON-RPC message. The
#: stream's own framing is stripped and the payload passed through untouched, so
#: a client on the far end of the pipe sees exactly the bytes an HTTP client
#: would have seen inside the event.
_DATA = "data: "


async def serve(
    app: Any,
    *,
    path: str = "/mcp",
    stdin: Any = None,
    stdout: Any = None,
) -> int:
    """Relay newline-delimited JSON-RPC between a pipe and `app`'s MCP route.

    Returns:
        A process exit code: 0 when stdin reached end of file cleanly.
    """
    from ..testing import TestClient

    source = sys.stdin.buffer if stdin is None else stdin
    sink = sys.stdout.buffer if stdout is None else stdout
    lock = asyncio.Lock()

    def write(payload: bytes) -> None:
        sink.write(payload.rstrip(b"\r\n") + b"\n")
        sink.flush()

    async with TestClient(app) as client:
        session = ""
        stream: asyncio.Task[None] | None = None
        inflight: set[asyncio.Task[None]] = set()

        async def relay(message: bytes) -> None:
            response = await client.post(
                path, content=message, headers=_headers(session)
            )
            if response.body:
                async with lock:
                    write(response.body)

        try:
            while True:
                line = await asyncio.to_thread(source.readline)
                if not line:
                    return 0
                message = line.strip()
                if not message:
                    continue
                if not session:
                    # Awaited: everything after this needs the session id this
                    # answer carries, and a client sends nothing until it has it.
                    response = await client.post(
                        path, content=message, headers=_headers(session)
                    )
                    session = response.header("mcp-session-id") or ""
                    if response.body:
                        async with lock:
                            write(response.body)
                    if session:
                        # The stream carries everything the server sends unasked,
                        # including the requests it will park a tool on, so it is
                        # opened the moment there is a session to open it for.
                        stream = asyncio.ensure_future(
                            _pump(client, path, session, write, lock)
                        )
                    continue
                task = asyncio.ensure_future(relay(message))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
        finally:
            for task in list(inflight):
                task.cancel()
            if stream is not None:
                stream.cancel()
            await asyncio.gather(*inflight, return_exceptions=True)
            if stream is not None:
                await asyncio.gather(stream, return_exceptions=True)


async def _pump(
    client: Any,
    path: str,
    session: str,
    write: Callable[[bytes], None],
    lock: asyncio.Lock,
) -> None:
    """Write every server-to-client message onto stdout as it arrives.

    Driven off `TestClient._scope` rather than `TestClient.request` because the
    stream does not end until the session does, and a collected response would
    arrive exactly once: too late.
    """
    scope, _body = client._scope(
        "GET",
        path,
        headers={**_headers(session), "accept": "text/event-stream"},
    )
    pending = b""

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal pending
        if message["type"] == "wreath.response":
            body = message.get("body", b"")
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
        else:
            return
        pending += body
        while b"\n" in pending:
            raw, _, pending = pending.partition(b"\n")
            text = raw.decode("utf-8", "replace")
            if text.startswith(_DATA):
                async with lock:
                    write(text[len(_DATA) :].encode("utf-8"))

    await client.app(scope, receive, send)


def _headers(session: str) -> dict[str, str]:
    headers = {"content-type": "application/json", "accept": "application/json"}
    if session:
        headers["mcp-session-id"] = session
    return headers


__all__ = ["serve"]
