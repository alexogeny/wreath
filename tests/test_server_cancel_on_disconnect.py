"""A client that goes away stops the work it asked for -- on safe methods.

Layer 1 of the cancellation chain. `http.disconnect` is a *passive* ASGI
message: a handler parked on a database query is not awaiting `receive()`, so
queueing it stops nothing. These tests hold the server to also cancelling the
application task, and to the line it draws about when.

The line is RFC 9110's: `GET`, `HEAD` and `OPTIONS` have no intended effect on
the server, so abandoning one can lose nothing but work. Everything else is left
running, because a `POST` unwound mid-flight rolls its transaction back cleanly
and still leaves whatever it did *outside* the transaction half-done. A route
overrides either default with `cancel_on_disconnect=`.

`tests/test_disconnect_cancels_query.py` carries the same disconnect through to
a live PostgreSQL backend.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import wreath
from wreath.router import RouteDefinition
from wreath.server import ServerConfig, _select_protocol, serve

#: How long a cancellation is given to arrive before the test calls it absent.
_PATIENCE = 5.0

@pytest.fixture
def protocol() -> type:
    """The HTTP/1 protocol class, resolved the way `Server` resolves it."""
    return _select_protocol()


class _Watch:
    """What one handler run observed, so an assertion can name the outcome."""

    __slots__ = ("cancelled", "completed", "release", "started")

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.completed = asyncio.Event()
        self.release = asyncio.Event()


async def _serve(app: Any, **config: Any) -> Any:
    server = await serve(
        app,
        ServerConfig(host="127.0.0.1", port=0, lifespan="off", **config),
    )
    disconnected = asyncio.Event()

    class _ObservedProtocol(_select_protocol()):
        def connection_lost(self, error: BaseException | None) -> None:
            try:
                super().connection_lost(error)
            finally:
                disconnected.set()

    server._protocol_cls = _ObservedProtocol
    server._test_disconnected = disconnected
    return server


def _port(server: Any) -> int:
    return server.sockets[0].getsockname()[1]


async def _send_and_drop(
    port: int, request: bytes, watch: _Watch, *, abort: bool = False
) -> None:
    """Send `request`, wait for the handler to start, then lose the client."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        await asyncio.wait_for(watch.started.wait(), timeout=_PATIENCE)
    finally:
        if abort:
            writer.transport.abort()
        else:
            writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass
    del reader


async def _release_after_disconnect(server: Any, watch: _Watch) -> None:
    """Prove the handler can advance after the peer is known to be gone.

    A fixed grace period only proves that cancellation did not arrive inside an
    arbitrary window. The protocol removes itself from the server's registry
    after it has delivered the disconnect, so wait for that positive signal,
    release the parked handler, and require the handler to complete instead.
    """
    await asyncio.wait_for(server._test_disconnected.wait(), timeout=_PATIENCE)
    watch.release.set()
    await asyncio.wait_for(watch.completed.wait(), timeout=_PATIENCE)
    assert not watch.cancelled.is_set()


class _Everywhere:
    """A global middleware that runs on every route. Selects `_handle_http`."""

    def before_sync(self, request: Any) -> None:
        return None

    def after_inplace(self, request: Any, response: Any) -> None:
        return None


class _Scoped(_Everywhere):
    """A global middleware some routes decline. Selects the compartment path."""

    def __init__(self, only: str) -> None:
        self.only = only

    def applies_to(self, method: str, path: str) -> bool:
        return path == self.only


def _slow_app(shape: str = "plain") -> tuple[wreath.Wreath, dict[str, _Watch]]:
    """One app with a slow handler per route shape under test.

    `shape` selects which of the three dispatchers `_select_dispatch` will pick,
    which is the thing that has to be covered rather than assumed: the seam that
    honours `cancel_on_disconnect=` lives in each of them, and one missing copy
    is a declaration silently ignored for a whole class of application.
    """
    app = wreath.Wreath()
    if shape == "general":
        app.add_global_middleware(_Everywhere())
    elif shape == "compartment":
        app.add_global_middleware(_Everywhere())
        app.add_global_middleware(_Scoped(only="/get"))
    watches = {
        name: _Watch()
        for name in ("get", "post", "post-optin", "get-optout", "streaming")
    }

    async def park(watch: _Watch) -> None:
        watch.started.set()
        try:
            await watch.release.wait()
        except asyncio.CancelledError:
            # Recorded and re-raised. Swallowing it would leave the request
            # neither cancelled nor finished, which is worse than either.
            watch.cancelled.set()
            raise
        watch.completed.set()

    @app.get("/get")
    async def slow_get(request: wreath.Request) -> wreath.Response:
        await park(watches["get"])
        return wreath.Response(b"late")

    @app.post("/post")
    async def slow_post(request: wreath.Request) -> wreath.Response:
        await park(watches["post"])
        return wreath.Response(b"late")

    @app.post("/post-optin", cancel_on_disconnect=True)
    async def opt_in(request: wreath.Request) -> wreath.Response:
        await park(watches["post-optin"])
        return wreath.Response(b"late")

    @app.get("/get-optout", cancel_on_disconnect=False)
    async def opt_out(request: wreath.Request) -> wreath.Response:
        await park(watches["get-optout"])
        return wreath.Response(b"late")

    @app.get("/streaming")
    async def streaming(request: wreath.Request) -> wreath.response.StreamingResponse:
        watch = watches["streaming"]

        async def body() -> Any:
            yield b"first"
            await park(watch)
            yield b"second"

        return wreath.response.StreamingResponse(body())

    return app, watches


def _get(path: str) -> bytes:
    return f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()


def _post(path: str) -> bytes:
    return (
        f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n"
    ).encode()


# --- the declaration --------------------------------------------------------


def test_the_route_default_is_undeclared() -> None:
    """`None` means "let the method decide", which is not the same as False."""
    definition = RouteDefinition("/x", ("GET",), lambda request: None)
    assert definition.cancel_on_disconnect is None


def test_a_route_records_what_it_declared() -> None:
    app = wreath.Wreath()

    @app.get("/audit", cancel_on_disconnect=False)
    async def audit(request: wreath.Request) -> str:
        return "ok"

    @app.post("/import", cancel_on_disconnect=True)
    async def importer(request: wreath.Request) -> str:
        return "ok"

    declared = {route.path: route.cancel_on_disconnect for route in app._routes}
    assert declared == {"/audit": False, "/import": True}


def test_the_declaration_is_reachable_by_wreath_mutant(tmp_path: Any) -> None:
    """A control nobody can remove is a control nobody has verified.

    `cancel_on_disconnect=` rides `**metadata` into `RouteDefinition`, which is
    where `_route_metadata` finds it -- but the declaration operator only offers
    a keyword that `CONTROL_KEYWORDS` names, so being a field is not enough.
    Without both, a suite that would not notice the keyword's loss reports clean.
    """
    import ast
    import textwrap

    from wreath._mutant import operators

    path = tmp_path / "routes_factory.py"
    path.write_text(
        textwrap.dedent(
            """
            from wreath import Wreath


            def build_app():
                app = Wreath()

                @app.get("/audit", cancel_on_disconnect=False)
                async def audit(request) -> dict:
                    \"\"\"Every audit entry.\"\"\"
                    return {}

                return app
            """
        ),
        encoding="utf-8",
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    operators.tag(tree)
    offered = {
        candidate.control
        for candidate in operators.scan(tree, None)
        if candidate.operator == "declaration.drop-keyword"
    }
    assert offered == {
        "`cancel_on_disconnect=` on `app.get(...)` (it falls back to the default)"
    }


def test_a_router_route_records_what_it_declared() -> None:
    router = wreath.router.Router(prefix="/v1")

    @router.get("/audit", cancel_on_disconnect=False)
    async def audit(request: wreath.Request) -> str:
        return "ok"

    assert router.routes[0].cancel_on_disconnect is False


# --- the defaults -----------------------------------------------------------


async def test_a_disconnected_get_cancels_the_handler(protocol: type) -> None:
    """The default for a safe method. Nobody is waiting for the answer."""
    app, watches = _slow_app()
    server = await _serve(app)
    try:
        await _send_and_drop(_port(server), _get("/get"), watches["get"])
        await asyncio.wait_for(watches["get"].cancelled.wait(), timeout=_PATIENCE)
    finally:
        await server.close()
    assert not watches["get"].completed.is_set()


async def test_a_reset_get_cancels_the_handler(protocol: type) -> None:
    """An RST rather than a FIN: connection_lost, not eof_received."""
    app, watches = _slow_app()
    server = await _serve(app)
    try:
        await _send_and_drop(
            _port(server), _get("/get"), watches["get"], abort=True
        )
        await asyncio.wait_for(watches["get"].cancelled.wait(), timeout=_PATIENCE)
    finally:
        await server.close()


async def test_a_disconnected_post_is_left_running(protocol: type) -> None:
    """The default for an unsafe method, and the reason the line is drawn.

    A `POST` unwound mid-flight rolls its transaction back cleanly. What it does
    *not* roll back is the job it enqueued, the card it charged or the mail it
    sent, and the client is gone and cannot be told which happened.
    """
    app, watches = _slow_app()
    server = await _serve(app)
    try:
        await _send_and_drop(_port(server), _post("/post"), watches["post"])
        await _release_after_disconnect(server, watches["post"])
    finally:
        await server.close()


# --- the overrides ----------------------------------------------------------


async def test_a_post_can_opt_in(protocol: type) -> None:
    app, watches = _slow_app()
    server = await _serve(app)
    try:
        await _send_and_drop(
            _port(server), _post("/post-optin"), watches["post-optin"]
        )
        await asyncio.wait_for(
            watches["post-optin"].cancelled.wait(), timeout=_PATIENCE
        )
    finally:
        await server.close()


async def test_a_get_can_opt_out(protocol: type) -> None:
    app, watches = _slow_app()
    server = await _serve(app)
    try:
        await _send_and_drop(
            _port(server), _get("/get-optout"), watches["get-optout"]
        )
        await _release_after_disconnect(server, watches["get-optout"])
    finally:
        await server.close()


# --- the boundary -----------------------------------------------------------


async def test_a_disconnect_after_the_response_started_does_not_cancel(
    protocol: type,
) -> None:
    """The status line is already on the wire; there is nothing left to save.

    Cancelling here would truncate a body rather than avoid a scan. The handler
    still unwinds -- the next `send` raises the server's disconnect error -- but
    it is not torn out from underneath, so its own cleanup runs where it stands.
    """
    app, watches = _slow_app()
    watch = watches["streaming"]
    server = await _serve(app)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", _port(server))
        writer.write(_get("/streaming"))
        await writer.drain()
        head = await asyncio.wait_for(reader.read(65536), timeout=_PATIENCE)
        assert b"first" in head
        await asyncio.wait_for(watch.started.wait(), timeout=_PATIENCE)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass
        await _release_after_disconnect(server, watch)
    finally:
        await server.close()


@pytest.mark.parametrize(
    ("shape", "dispatcher"),
    [
        ("plain", "_handle_http_plain"),
        ("general", "_handle_http"),
        ("compartment", "_handle_http_compartment"),
    ],
)
async def test_the_override_is_honoured_by_every_dispatcher(
    protocol: type, shape: str, dispatcher: str
) -> None:
    """An application's shape decides which dispatcher runs it, at compile time.

    `_select_dispatch` picks between three, and a route's declaration is only
    read where the route is matched -- so a seam written into the general
    dispatcher alone leaves `cancel_on_disconnect=` inert for an application
    with no global middleware, which is both the leanest shape and the one most
    likely to have a slow read behind a `GET`. The dispatcher is asserted rather
    than assumed, because a change in `_select_dispatch`'s conditions would
    otherwise turn this into three copies of one test.
    """
    app, watches = _slow_app(shape)
    app._compile_routes()
    assert app._dispatch_http.__name__ == dispatcher

    server = await _serve(app)
    try:
        # `=False` on a GET: without the seam the server's safe-method default
        # cancels this, so the assertion fails rather than passing vacuously.
        await _send_and_drop(
            _port(server), _get("/get-optout"), watches["get-optout"]
        )
        await _release_after_disconnect(server, watches["get-optout"])
        # `=True` on a POST: the opposite direction, so a seam that armed
        # everything rather than reading the declaration is caught too.
        await _send_and_drop(
            _port(server), _post("/post-optin"), watches["post-optin"]
        )
        await asyncio.wait_for(
            watches["post-optin"].cancelled.wait(), timeout=_PATIENCE
        )
        # And a route in the same application that declared *nothing* still
        # takes the server's default. A seam that armed every matched route
        # with whatever the table returned would disarm this one, since a
        # missing entry reads as "do not cancel".
        await _send_and_drop(_port(server), _get("/get"), watches["get"])
        await asyncio.wait_for(watches["get"].cancelled.wait(), timeout=_PATIENCE)
    finally:
        await server.close()


@pytest.mark.parametrize(
    ("shape", "dispatcher"),
    [
        ("plain", "_handle_http_plain"),
        ("general", "_handle_http"),
        ("compartment", "_handle_http_compartment"),
    ],
)
async def test_an_application_that_declares_nothing_is_served_normally(
    protocol: type, shape: str, dispatcher: str
) -> None:
    """The empty-table branch, on every dispatcher.

    `_cancel_on_disconnect` is left `None` rather than an empty dict precisely
    so the per-request cost is one member read; the guard that makes that safe
    is only exercised by an application with no declarations at all, which the
    tests above -- all of which declare something -- never reach.
    """
    app = wreath.Wreath()
    if shape == "general":
        app.add_global_middleware(_Everywhere())
    elif shape == "compartment":
        app.add_global_middleware(_Everywhere())
        app.add_global_middleware(_Scoped(only="/brief"))

    @app.get("/brief")
    async def brief(request: wreath.Request) -> wreath.Response:
        return wreath.Response(b"done")

    @app.get("/other")
    async def other(request: wreath.Request) -> wreath.Response:
        return wreath.Response(b"other")

    app._compile_routes()
    assert app._dispatch_http.__name__ == dispatcher
    assert app._cancel_on_disconnect is None, (
        "an application with no declarations must not build an override table"
    )

    server = await _serve(app)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", _port(server))
        writer.write(_get("/brief"))
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(65536), timeout=_PATIENCE)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass
    finally:
        await server.close()
    assert b" 200 " in answer and b"done" in answer, answer


def test_only_declaring_routes_enter_the_override_table() -> None:
    """The table holds declarations, not one entry per route.

    An entry per route would make the `declared is not None` test dead and put a
    dict lookup on every request of every application, which is the cost this
    design exists to avoid.
    """
    app = wreath.Wreath()

    @app.get("/plain")
    async def plain(request: wreath.Request) -> str:
        return "ok"

    @app.get("/audit", cancel_on_disconnect=False)
    async def audit(request: wreath.Request) -> str:
        return "ok"

    app._compile_routes()
    table = app._cancel_on_disconnect
    assert table is not None
    assert list(table.values()) == [False]


async def test_an_undisturbed_get_still_completes(protocol: type) -> None:
    """The control. Without it every assertion above could pass vacuously."""
    app = wreath.Wreath()
    ran = asyncio.Event()

    @app.get("/brief")
    async def brief(request: wreath.Request) -> wreath.Response:
        await asyncio.sleep(0.05)
        ran.set()
        return wreath.Response(b"done")

    server = await _serve(app)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", _port(server))
        writer.write(_get("/brief"))
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(65536), timeout=_PATIENCE)
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass
    finally:
        await server.close()
    assert b"done" in answer
    assert ran.is_set()
