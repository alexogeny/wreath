"""In-memory test client: drive a Wreath (or any ASGI) app without a server.

    async with TestClient(app) as client:          # runs lifespan
        response = await client.get("/items", params={"q": "bolt"})
        assert response.status == 200
        assert response.json() == {...}

        async with client.websocket("/ws") as ws:  # performs connect/accept
            await ws.send_text("hi")
            assert await ws.receive_text() == "hi"

Requests execute the application directly — no sockets, no threads — so
tests observe exactly the ASGI messages a server would.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote, urlencode

from ._headers import find_header
from ._json import dumps as _json_dumps
from ._json import loads as _json_loads

Message = dict[str, Any]

_DEFAULT_ASGI = {"version": "3.0", "spec_version": "2.5"}


class TestResponse:
    __test__ = False  # not a pytest collection target despite the name
    __slots__ = ("body", "headers", "status")

    def __init__(self, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        return _json_loads(self.body)

    def header(self, name: str, default: str | None = None) -> str | None:
        value = find_header(self.headers, name.lower().encode("latin-1"))
        return value.decode("latin-1") if value is not None else default


class WebSocketTestSession:
    """Client side of an in-memory WebSocket connection."""

    def __init__(self, app: Any, scope: dict[str, Any]) -> None:
        self._app = app
        self._scope = scope
        self._to_app: asyncio.Queue[Message] = asyncio.Queue()
        self._from_app: asyncio.Queue[Message] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.accepted: Message | None = None

    async def __aenter__(self) -> WebSocketTestSession:
        async def receive() -> Message:
            return await self._to_app.get()

        async def send(message: Message) -> None:
            await self._from_app.put(message)

        self._task = asyncio.get_running_loop().create_task(
            self._app(self._scope, receive, send)
        )
        self._to_app.put_nowait({"type": "websocket.connect"})
        first = await self._from_app.get()
        if first["type"] == "websocket.close":
            await self._finish()
            raise ConnectionError(f"websocket rejected: {first.get('code', 1000)}")
        if first["type"] != "websocket.accept":
            await self._finish()
            raise RuntimeError(f"expected websocket.accept, got {first['type']!r}")
        self.accepted = first
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._to_app.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await self._finish()

    async def _finish(self) -> None:
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            finally:
                self._task = None

    async def send_text(self, data: str) -> None:
        await self._to_app.put({"type": "websocket.receive", "text": data})

    async def send_bytes(self, data: bytes) -> None:
        await self._to_app.put({"type": "websocket.receive", "bytes": data})

    async def receive(self) -> Message:
        return await self._from_app.get()

    async def receive_text(self) -> str:
        message = await self.receive()
        if message["type"] != "websocket.send" or message.get("text") is None:
            raise RuntimeError(f"expected a text message, got {message!r}")
        return message["text"]

    async def receive_bytes(self) -> bytes:
        message = await self.receive()
        if message["type"] != "websocket.send" or message.get("bytes") is None:
            raise RuntimeError(f"expected a binary message, got {message!r}")
        return message["bytes"]


class TestClient:
    __test__ = False  # not a pytest collection target despite the name
    __slots__ = ("_lifespan_from_app", "_lifespan_task", "_lifespan_to_app", "app")

    def __init__(self, app: Any) -> None:
        self.app = app
        self._lifespan_task: asyncio.Task[None] | None = None
        self._lifespan_to_app: asyncio.Queue[Message] | None = None
        self._lifespan_from_app: asyncio.Queue[Message] | None = None

    # --- lifespan ----------------------------------------------------------

    async def __aenter__(self) -> TestClient:
        to_app: asyncio.Queue[Message] = asyncio.Queue()
        from_app: asyncio.Queue[Message] = asyncio.Queue()

        async def receive() -> Message:
            return await to_app.get()

        async def send(message: Message) -> None:
            await from_app.put(message)

        self._lifespan_to_app = to_app
        self._lifespan_from_app = from_app
        self._lifespan_task = asyncio.get_running_loop().create_task(
            self.app({"type": "lifespan", "asgi": _DEFAULT_ASGI}, receive, send)
        )
        to_app.put_nowait({"type": "lifespan.startup"})
        reply = await self._reply("startup")
        if reply["type"] == "lifespan.startup.failed":
            raise RuntimeError(f"lifespan startup failed: {reply.get('message', '')}")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        assert self._lifespan_to_app is not None
        assert self._lifespan_from_app is not None
        self._lifespan_to_app.put_nowait({"type": "lifespan.shutdown"})
        reply = await self._reply("shutdown")
        if self._lifespan_task is not None:
            await asyncio.wait_for(self._lifespan_task, timeout=5)
            self._lifespan_task = None
        if reply["type"] == "lifespan.shutdown.failed":
            raise RuntimeError(f"lifespan shutdown failed: {reply.get('message', '')}")

    async def _reply(self, phase: str) -> Message:
        """Wait for a lifespan reply, or for the app to die trying.

        Waiting on the queue alone deadlocks whenever the app raises instead of
        replying: the exception is held by the lifespan task, nothing is ever
        put on the queue, and the ``async with`` blocks forever. An application
        that fails to start is the *normal* way to arrive here -- a bad route
        annotation, a missing table -- so the failure has to come back as the
        error it is rather than as a hang with no output.
        """
        assert self._lifespan_task is not None
        assert self._lifespan_from_app is not None
        reply = asyncio.ensure_future(self._lifespan_from_app.get())
        try:
            await asyncio.wait(
                (reply, self._lifespan_task), return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            reply.cancel()
            raise
        if reply.done():
            # A reply beats a finished task: an app may legitimately send its
            # reply and return in the same step.
            return reply.result()
        reply.cancel()
        error = self._lifespan_task.exception()
        if error is not None:
            raise RuntimeError(f"the app raised during lifespan {phase}") from error
        raise RuntimeError(
            f"the app's lifespan ended without a {phase} reply; it must send "
            f"lifespan.{phase}.complete or lifespan.{phase}.failed"
        )

    # --- HTTP ---------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        content: bytes = b"",
        json: Any = None,
    ) -> TestResponse:
        raw_headers = [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in (headers or {}).items()
        ]
        if json is not None:
            content = _json_dumps(json)
            raw_headers.append((b"content-type", b"application/json"))
        if content:
            raw_headers.append((b"content-length", str(len(content)).encode()))
        path_part, _, existing_query = path.partition("?")
        query = existing_query.encode("ascii")
        if params:
            encoded = urlencode(params).encode("ascii")
            query = query + b"&" + encoded if query else encoded
        scope = {
            "type": "http",
            "asgi": _DEFAULT_ASGI,
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path_part,
            "raw_path": quote(path_part).encode("ascii"),
            "query_string": query,
            "headers": raw_headers,
            "server": ("testclient", 80),
            "client": ("testclient", 50000),
            "root_path": "",
        }

        sent: list[Message] = []
        body = content

        async def receive() -> Message:
            nonlocal body
            chunk, body = body, b""
            return {"type": "http.request", "body": chunk, "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        await self.app(scope, receive, send)
        if not sent:
            raise RuntimeError("application sent no response")
        first = sent[0]
        if first["type"] == "wreath.response":
            return TestResponse(first["status"], list(first["headers"]), first.get("body", b""))
        payload = b"".join(
            m.get("body", b"") for m in sent if m["type"] == "http.response.body"
        )
        return TestResponse(first["status"], list(first["headers"]), payload)

    async def get(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("PUT", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("DELETE", path, **kwargs)

    async def options(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("OPTIONS", path, **kwargs)

    async def head(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("HEAD", path, **kwargs)

    # --- WebSocket ------------------------------------------------------------

    def websocket(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        subprotocols: list[str] | None = None,
    ) -> WebSocketTestSession:
        raw_headers = [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in (headers or {}).items()
        ]
        path_part, _, query = path.partition("?")
        scope = {
            "type": "websocket",
            "asgi": _DEFAULT_ASGI,
            "http_version": "1.1",
            "scheme": "ws",
            "path": path_part,
            "raw_path": quote(path_part).encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": raw_headers,
            "server": ("testclient", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "subprotocols": subprotocols or [],
        }
        return WebSocketTestSession(self.app, scope)


__all__ = ["TestClient", "TestResponse", "WebSocketTestSession"]
