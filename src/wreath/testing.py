"""In-memory test client: drive a Wreath (or any ASGI) app without a server.

The client is **asynchronous**. Enter it with `async with` and await every
request; there is no synchronous mode.

    async with TestClient(app) as client:          # runs lifespan
        response = await client.get("/items", params={"q": "bolt"})
        assert response.status == 200
        assert response.json() == {...}

        async with client.websocket("/ws") as ws:  # performs connect/accept
            await ws.send_text("hi")
            assert await ws.receive_text() == "hi"

Requests execute the application directly — no sockets, no threads — so
tests observe exactly the ASGI messages a server would.

Two differences from httpx and requests catch people out. The status is
`response.status`, never `response.status_code`; and an error body is RFC 9457
`application/problem+json` (`type`/`title`/`status`/`detail`), never
`{"detail": ...}`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote, urlencode

from ._headers import find_header
from ._json import dumps as _json_dumps
from ._json import loads as _json_loads

Message = dict[str, Any]

_DEFAULT_ASGI = {"version": "3.0", "spec_version": "2.5"}


class TestResponse:
    """One completed response, assembled from the ASGI messages the app sent.

    The status code is `status` — **not** `status_code`. A `TestResponse` is not
    an httpx or requests response, so `response.status_code` raises
    `AttributeError`, which is the single most common surprise when porting a
    test suite.

    Three attributes carry the whole response, and nothing is lazily fetched:

    - `status` — the integer status code.
    - `headers` — the raw ASGI header list, lowercase-name byte pairs, in the
    order the application emitted them.
    - `body` — the complete body as bytes, with every `http.response.body`
    chunk already joined.

    An error response carries an RFC 9457 problem document under
    `application/problem+json`, not `{"detail": ...}`. A 500 reads
    `{"type": "about:blank", "title": "Internal Server Error", "status": 500,
    "detail": "Internal Server Error"}`, so assert on `body["title"]` or
    `body["detail"]` rather than on a `detail` string alone.
    """

    __test__ = False  # not a pytest collection target despite the name
    __slots__ = ("body", "headers", "status")

    def __init__(self, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        """The body decoded as UTF-8, raising `UnicodeDecodeError` if it is not."""
        return self.body.decode("utf-8")

    def json(self) -> Any:
        """Decode the body as JSON.

        The response's `Content-Type` is not consulted, so this also decodes a
        `problem+json` error body.

        Returns:
            The decoded JSON value.

        Raises:
            ValueError: The body is not valid JSON; an empty body is a ValueError too.
        """
        return _json_loads(self.body)

    def header(self, name: str, default: str | None = None) -> str | None:
        """Return one response header as text, or `default` when it is absent.

        The name is matched case-insensitively and the **first** match wins, so
        this cannot read the second of a repeated header such as `Set-Cookie`;
        read `headers` directly for those. Values are decoded as latin-1, the
        HTTP wire encoding.
        """
        value = find_header(self.headers, name.lower().encode("latin-1"))
        return value.decode("latin-1") if value is not None else default


class WebSocketTestSession:
    """Client side of an in-memory WebSocket connection.

    `TestClient.websocket()` returns one of these, already carrying the scope
    but **not yet connected**: the endpoint does not run until the session is
    entered. Entering it starts the application as a task, sends
    `websocket.connect`, and waits for the reply, so the handshake has completed
    by the time the body of the `async with` runs.

        async with client.websocket("/ws") as ws:
            await ws.send_text("hi")
            assert await ws.receive_text() == "hi"

    Entering raises `ConnectionError` when the application closes instead of
    accepting — a rejected handshake, an authorization failure — with the close
    code in the message, and `RuntimeError` when the first message is neither an
    accept nor a close. The accepted handshake message itself is kept on
    `accepted`, which is how a test reads back a negotiated subprotocol or the
    headers the endpoint accepted with; it is `None` before the session is
    entered.

    Exiting sends `websocket.disconnect` with code 1000 and then awaits the
    application task, so an exception raised inside the endpoint surfaces from
    the `async with` rather than being swallowed. An endpoint that never returns
    after a disconnect makes exit raise `TimeoutError` after five seconds
    instead of hanging the test session.
    """

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

        self._task = asyncio.get_running_loop().create_task(self._app(self._scope, receive, send))
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
        """Deliver `data` to the endpoint as a text frame."""
        await self._to_app.put({"type": "websocket.receive", "text": data})

    async def send_bytes(self, data: bytes) -> None:
        """Deliver `data` to the endpoint as a binary frame."""
        await self._to_app.put({"type": "websocket.receive", "bytes": data})

    async def receive(self) -> Message:
        """Wait for the next raw ASGI message the endpoint sends.

        This is the unfiltered form: the message may be a `websocket.send` or a
        `websocket.close`, and reading a close is how a test observes an
        endpoint hanging up. It waits indefinitely, so a test that expects a
        message the endpoint never sends hangs rather than failing — bound it
        with `asyncio.timeout` when that is a real possibility.

        Returns:
            The ASGI message dict, exactly as the endpoint sent it.
        """
        return await self._from_app.get()

    async def receive_text(self) -> str:
        """Wait for the next text frame from the endpoint.

        Returns:
            The frame's text payload.

        Raises:
            RuntimeError: The next message was a close or carried bytes rather than text.
        """
        message = await self.receive()
        if message["type"] != "websocket.send" or message.get("text") is None:
            raise RuntimeError(f"expected a text message, got {message!r}")
        return message["text"]

    async def receive_bytes(self) -> bytes:
        """Wait for the next binary frame from the endpoint.

        Returns:
            The frame's binary payload.

        Raises:
            RuntimeError: The next message was a close or carried text rather than bytes.
        """
        message = await self.receive()
        if message["type"] != "websocket.send" or message.get("bytes") is None:
            raise RuntimeError(f"expected a binary message, got {message!r}")
        return message["bytes"]


class _ScopeIdentityBackend:
    """Authenticates from the scope, so a test can *be* someone in one line.

    Installed only when `TestClient.acting_as()` is used. It reads the identity
    the client put on the request scope, which keeps concurrent `admin_client` /
    `rider_client` requests independent -- the identity rides the request, not
    the backend.

    `TestClient._restore_auth_backend` puts the application's own backend back
    when the client exits, but only when there *was* one: an application with no
    configured backend keeps this one, because `configure_auth` has no way to
    express "no backend".
    """

    __slots__ = ()
    scheme = "Bearer"

    def challenge(self, request: Any) -> str:
        return "Bearer"

    async def authenticate(self, request: Any) -> Any:
        return request.scope.get(_SCOPE_IDENTITY)


#: Where `acting_as` puts the identity for `_ScopeIdentityBackend` to find.
_SCOPE_IDENTITY = "wreath.test.identity"

#: "Nothing was saved", distinct from "None was saved" (an app with no backend).
_UNSET: Any = object()


class TestClient:
    """Drive an ASGI application in-process, with no server and no socket.

    **The client is asynchronous.** Enter it with `async with` and await every
    request — there is no synchronous mode, and calling `client.get("/")`
    without awaiting it returns a coroutine, not a response:

        async with TestClient(app) as client:
            response = await client.get("/items", params={"q": "bolt"})
            assert response.status == 200

    Entering the context manager runs the application's **lifespan startup** and
    exiting runs its shutdown, so anything opened at startup — a database pool,
    a background worker, a warmed route table — is live for the requests in
    between. A startup or shutdown that fails, or an application that raises
    instead of replying, is turned into a `RuntimeError` naming the phase rather
    than a hang. A client used without `async with` still serves requests, but
    the lifespan never runs and startup state is missing.

    Requests call the application object directly. Nothing is serialized onto a
    socket, so a test sees the exact ASGI messages the app produced, and a
    handler's exception is converted by the application's own error handling
    into a response — an RFC 9457 problem document — not re-raised at the
    call site. Responses are `TestResponse`, whose status is `.status`.

    `app` may be any ASGI 3 application; nothing here is Wreath-specific except
    `acting_as()`, which needs an application that exposes `configure_auth`.

    `identity` is the mechanism behind `acting_as()` and does nothing on its
    own — the identity is placed on the request scope, and only the backend
    that `acting_as()` installs ever reads it. Call `acting_as()` instead.

    Args:
        app: The ASGI application to drive.
        headers: Sent on every request; a per-request `headers=` entry wins on collision.
        identity: The identity acting_as puts on the scope; setting it alone does nothing.
    """

    __test__ = False  # not a pytest collection target despite the name
    __slots__ = (
        "_headers",
        "_identity",
        "_lifespan_from_app",
        "_lifespan_task",
        "_lifespan_to_app",
        "_restore_backend",
        "_root",
        "app",
    )

    def __init__(
        self,
        app: Any,
        *,
        headers: dict[str, str] | None = None,
        identity: Any = None,
    ) -> None:
        self.app = app
        self._headers = dict(headers or {})
        self._identity = identity
        self._lifespan_task: asyncio.Task[None] | None = None
        self._lifespan_to_app: asyncio.Queue[Message] | None = None
        self._lifespan_from_app: asyncio.Queue[Message] | None = None
        self._restore_backend: Any = _UNSET
        self._root: TestClient = self

    def acting_as(
        self,
        principal: Any,
        *,
        roles: Iterable[str] = (),
        permissions: Iterable[str] = (),
        type: str = "User",
    ) -> TestClient:
        """A client whose every HTTP request arrives authenticated as `principal`.

        The point is to test *authorization* without re-deriving a token for
        every case:

            async with TestClient(app) as client:
                admin  = client.acting_as("root", roles=["admin"])
                rider  = client.acting_as("bo", roles=["rider"])

                assert (await admin.delete("/llamas/7")).status == 204
                assert (await rider.delete("/llamas/7")).status == 403

        `principal` is a `wreath.auth.Identity`, or an id to build one from with
        `roles`/`permissions`. Passing both a complete `Identity` and roles or
        permissions raises `TypeError`, because two sources for one fact is how
        a test ends up lying about itself.

        Derived clients share the application, the lifespan, and the default
        headers of the client they came from, so make as many as there are
        roles. Do not enter one with `async with` — it is already live, and
        entering it would start a second lifespan against the same app.

        The identity rides the *request scope*, not the backend, so requests
        from two acting-as clients may be in flight at once without seeing each
        other's identity. It reaches HTTP requests only: `websocket()` builds
        its scope from scratch and carries neither the identity nor the client's
        default headers.

        Authentication runs only for routes that declare a requirement, so a
        deliberately open route still sees `request.identity is None` under an
        acting-as client. That is the application's rule, not the client's.

        **This bypasses authentication.** While any acting-as client exists the
        application's authentication backend is replaced with one that trusts
        the scope, and the original is put back when the client exits — unless
        the application had no backend configured, in which case the scope
        backend stays installed on that application object for its lifetime.
        Bypassing is the right trade for an authorization test and the wrong one
        for a test *of* authentication; use a real token there.

        Args:
            principal: A `wreath.auth.Identity`, or an id string to build one from.
            roles: Roles for the built identity; rejected when principal is an Identity.
            permissions: Permissions for the built identity; rejected the same way.
            type: Principal type recorded on a built identity, matching the policy vocabulary.

        Returns:
            A derived client sharing this one's application and lifespan.

        Raises:
            TypeError: A complete Identity was passed together with roles or permissions.
        """
        from ._auth.models import Identity

        if isinstance(principal, str):
            principal = Identity(
                principal,
                type=type,
                roles=frozenset(roles),
                permissions=frozenset(permissions),
            )
        elif roles or permissions:
            raise TypeError("pass roles/permissions with an id, or a complete Identity -- not both")
        derived = TestClient(self.app, headers=self._headers, identity=principal)
        derived._root = self._root
        derived._lifespan_task = self._lifespan_task
        derived._lifespan_to_app = self._lifespan_to_app
        derived._lifespan_from_app = self._lifespan_from_app
        self._root._install_scope_backend()
        return derived

    def with_headers(self, **headers: str) -> TestClient:
        """A client that sends `headers` on every request, plus this client's own.

        Header names arrive as keyword arguments and are sent verbatim, so an
        underscore is *not* rewritten to a hyphen: `with_headers(authorization=...)`
        is fine, and a hyphenated name such as `x-trace-id` cannot be spelled
        here at all — pass those in a request's `headers=` argument instead. On
        a collision the new value wins.

        The derived client shares this one's application, lifespan, and acting-as
        identity; do not enter it with `async with`. Like `acting_as()`, the
        headers apply to HTTP requests only, not to `websocket()`.

        Returns:
            A derived client sharing this one's application and lifespan.
        """
        derived = TestClient(
            self.app, headers={**self._headers, **headers}, identity=self._identity
        )
        derived._root = self._root
        derived._lifespan_task = self._lifespan_task
        derived._lifespan_to_app = self._lifespan_to_app
        derived._lifespan_from_app = self._lifespan_from_app
        return derived

    def _install_scope_backend(self) -> None:
        if self._restore_backend is not _UNSET:
            return
        self._restore_backend = getattr(self.app, "_auth_backend", None)
        configure = getattr(self.app, "configure_auth", None)
        if configure is not None:
            configure(_ScopeIdentityBackend(), getattr(self.app, "_authorizer", None))

    def _restore_auth_backend(self) -> None:
        if self._restore_backend is _UNSET:
            return
        configure = getattr(self.app, "configure_auth", None)
        if configure is not None and self._restore_backend is not None:
            configure(self._restore_backend, getattr(self.app, "_authorizer", None))
        self._restore_backend = _UNSET

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
        self._restore_auth_backend()
        if self._lifespan_to_app is None or self._lifespan_from_app is None:
            raise RuntimeError("TestClient lifespan has not been started")
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
        put on the queue, and the `async with` blocks forever. An application
        that fails to start is the *normal* way to arrive here -- a bad route
        annotation, a missing table -- so the failure has to come back as the
        error it is rather than as a hang with no output.
        """
        if self._lifespan_task is None or self._lifespan_from_app is None:
            raise RuntimeError(f"TestClient lifespan {phase} has not been started")
        reply = asyncio.ensure_future(self._lifespan_from_app.get())
        try:
            await asyncio.wait((reply, self._lifespan_task), return_when=asyncio.FIRST_COMPLETED)
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
        """Send one request through the application and collect the response.

        The verb helpers — `get()`, `post()`, `put()`, `patch()`, `delete()`,
        `options()`, `head()` — forward here, so every keyword documented below
        works on all of them.

        A query string already present in `path` is kept and `params` is
        appended to it, so `get("/items?page=2", params={"q": "bolt"})` sends
        both. Passing `json=` encodes the value, sets `Content-Type` to
        `application/json`, and overwrites `content`; a `json=None` is
        indistinguishable from passing nothing, so a literal JSON `null` body
        must go through `content=b"null"`. `Content-Length` is set only when
        there is a body.

        The body is delivered as a single `http.request` message with
        `more_body` false, which is what a streaming handler sees.

        Args:
            method: HTTP method, uppercased for you.
            path: Request path, optionally already carrying a query string.
            headers: Merged over the client's default headers for this request only.
            params: Query parameters, urlencoded and appended to any existing query.
            content: Raw request body, ignored when json is given.
            json: Value to encode as a JSON body, setting the content type.

        Returns:
            The completed response.

        Raises:
            RuntimeError: The application returned without sending any response message.
        """
        scope, content = self._scope(
            method, path, headers=headers, params=params, content=content, json=json
        )

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
        payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return TestResponse(first["status"], list(first["headers"]), payload)

    def _scope(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        content: bytes = b"",
        json: Any = None,
    ) -> tuple[dict[str, Any], bytes]:
        """The ASGI scope for one request, and the body to feed it.

        Split out of `request()` so a caller that must consume the response
        *incrementally* -- an SSE stream that never ends on its own is the case
        that needs it -- can drive `self.app` itself without rebuilding a scope
        by hand. `request()` collects, which is right for every assertion in a
        test and wrong for a stream something is reading as it arrives.
        """
        raw_headers = [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in {**self._headers, **(headers or {})}.items()
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
        scope: dict[str, Any] = {
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
        if self._identity is not None:
            # On the scope rather than on the backend, so two clients acting as
            # different people can have requests in flight at the same time.
            scope[_SCOPE_IDENTITY] = self._identity
        return scope, content

    async def get(self, path: str, **kwargs: Any) -> TestResponse:
        """Send a GET request; keywords are those of `request()`."""
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> TestResponse:
        """Send a POST request; keywords are those of `request()`."""
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> TestResponse:
        """Send a PUT request; keywords are those of `request()`."""
        return await self.request("PUT", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> TestResponse:
        """Send a PATCH request; keywords are those of `request()`."""
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> TestResponse:
        """Send a DELETE request; keywords are those of `request()`."""
        return await self.request("DELETE", path, **kwargs)

    async def options(self, path: str, **kwargs: Any) -> TestResponse:
        """Send an OPTIONS request; keywords are those of `request()`."""
        return await self.request("OPTIONS", path, **kwargs)

    async def head(self, path: str, **kwargs: Any) -> TestResponse:
        """Send a HEAD request; keywords are those of `request()`."""
        return await self.request("HEAD", path, **kwargs)

    def websocket(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        subprotocols: list[str] | None = None,
    ) -> WebSocketTestSession:
        """Build an unconnected `WebSocketTestSession` for `path`.

        Nothing runs until the returned session is entered with `async with`;
        that is what performs the connect/accept handshake. A query string in
        `path` is passed through to the scope.

        The scope is built from scratch: unlike the HTTP verbs, this carries
        **neither** the client's default headers **nor** an `acting_as()`
        identity, so a WebSocket endpoint that authenticates must be given its
        credentials through `headers` here.

        Args:
            path: Connection path, optionally carrying a query string.
            headers: Handshake headers; the client's default headers are not added.
            subprotocols: Offered subprotocols, placed on the scope for the endpoint to pick.

        Returns:
            A session to enter with `async with`.
        """
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
