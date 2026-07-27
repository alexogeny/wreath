"""WebSocket routes and the authentication pipeline (report 23: R-43).

`_handle_websocket` matches a route and calls the handler. Nothing else in the
request pipeline runs: no global middleware, and no route requirement. A
`@authenticated()` WebSocket handler therefore accepts an anonymous connection,
which is the opposite of what the decorator means on an HTTP route.
"""

from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.auth import Identity, authenticated
from wreath.testing import TestClient


class _RejectingBackend:
    """Authenticates nobody, so any enforced route must refuse."""

    async def authenticate(self, request):
        return None

    def challenge(self, request):
        return "Bearer"


class _AcceptingBackend:
    async def authenticate(self, request):
        return Identity(id="u1", roles=frozenset({"member"}))

    def challenge(self, request):
        return "Bearer"


class _Recorder:
    """Global middleware that records whether it saw a request at all."""

    global_scope = True

    def __init__(self) -> None:
        self.seen = 0

    async def before(self, request):
        self.seen += 1
        return None



async def test_authenticated_websocket_route_refuses_an_anonymous_connection():
    app = Wreath()
    app.configure_auth(_RejectingBackend())
    opened = 0

    @app.websocket("/ws")
    @authenticated()
    async def socket(websocket):
        nonlocal opened
        opened += 1
        await websocket.accept()
        await websocket.close()

    async with TestClient(app) as client:
        with pytest.raises(ConnectionError):
            async with client.websocket("/ws"):
                pass

    assert opened == 0, "the handler ran despite an enforced auth requirement"



async def test_authenticated_websocket_route_admits_an_authenticated_caller():
    app = Wreath()
    app.configure_auth(_AcceptingBackend())

    @app.websocket("/ws")
    @authenticated()
    async def socket(websocket):
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    async with TestClient(app) as client:
        async with client.websocket("/ws") as session:
            assert await session.receive_text() == "hello"



async def test_unenforced_websocket_route_is_unaffected():
    app = Wreath()

    @app.websocket("/open")
    async def socket(websocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    async with TestClient(app) as client:
        async with client.websocket("/open") as session:
            assert await session.receive_text() == "hi"
