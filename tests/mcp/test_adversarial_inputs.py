from __future__ import annotations

import asyncio

import pytest

from wreath import Depends, Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.mcp import MCP, PROTOCOL_VERSION, expose_routes
from wreath.response import Response
from wreath.testing import TestClient, TestResponse

STREAM = {"accept": "text/event-stream"}


def header(response: TestResponse, name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in response.headers:
        if key == wanted:
            return value.decode("latin-1")
    return None


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def application_authenticated() -> tuple[Wreath, MCP]:
    """An MCP endpoint on an app whose *own* backend identifies the caller.

    No `MCPAuth`: this is the second of the two supported ways to protect the
    surface, and the one `expose_routes` exists for -- a route behind the
    application's session or bearer backend, exposed as a tool.
    """
    app = Wreath()
    # The token *is* the subject, so two callers are two identities without a
    # token minting oracle in the test.
    app.configure_auth(BearerTokenBackend(lambda token: Identity(token)), None)
    mcp = MCP(app, name="camera-trap", version="1.0.0")

    @mcp.tool(description="Says who the server thinks is asking.")
    async def whoami(request) -> dict:
        return {"caller": None if request.identity is None else request.identity.id}

    return app, mcp


async def initialize(client: TestClient, token: str) -> str:
    opened = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION},
        },
        headers=bearer(token),
    )
    assert opened.status == 200
    return header(opened, "mcp-session-id") or ""


async def test_another_caller_cannot_drive_the_session_ada_opened() -> None:
    app, mcp = application_authenticated()
    async with TestClient(app) as client:
        session = await initialize(client, "ada")

        stolen = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers=bearer("bob") | {"mcp-session-id": session},
        )
        assert stolen.status == 401
        # And Ada's own session still works, so the refusal is about the caller
        # rather than about the session having been invalidated by the attempt.
        mine = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "ping"},
            headers=bearer("ada") | {"mcp-session-id": session},
        )
        assert mine.status == 200
        assert mine.json()["result"] == {}


async def test_another_caller_cannot_end_the_session_ada_opened() -> None:
    app, mcp = application_authenticated()
    async with TestClient(app) as client:
        session = await initialize(client, "ada")
        ended = await client.delete("/mcp", headers=bearer("bob") | {"mcp-session-id": session})
        assert ended.status == 401
        assert mcp.sessions == 1


async def test_another_caller_cannot_open_the_stream_of_ada_s_session() -> None:
    app, mcp = application_authenticated()
    async with TestClient(app) as client:
        session = await initialize(client, "ada")
        try:
            # Bounded, because the failure this guards against is not a wrong
            # status: it is a stream that *opens*. An opened stream emits a
            # keep-alive comment every fifteen seconds and never ends on its
            # own, so an unbounded `await` here hangs the suite instead of
            # reporting the hole -- which is precisely how it presented the
            # first time it was written.
            async with asyncio.timeout(5.0):
                opened = await client.get(
                    "/mcp", headers=bearer("bob") | STREAM | {"mcp-session-id": session}
                )
        except TimeoutError:
            pytest.fail(
                "a second caller's GET opened a notification stream on the "
                "session another caller had initialized"
            )
        assert opened.status == 401


async def test_an_unprotected_endpoint_still_needs_no_identity() -> None:
    app = Wreath()
    mcp = MCP(app, name="camera-trap", version="1.0.0")

    @mcp.tool(description="Anyone may call this.")
    async def ping_tool(request) -> dict:
        return {"ok": True}

    async with TestClient(app) as client:
        opened = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION},
            },
        )
        session = header(opened, "mcp-session-id") or ""
        answered = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers={"mcp-session-id": session},
        )
        assert answered.status == 200


async def test_a_route_s_dependencies_are_not_silently_dropped() -> None:
    ran: list[str] = []

    async def require_api_key(request) -> None:
        ran.append("dependency")
        raise PermissionError("no api key")

    app = Wreath()

    @app.get("/admin/purge", tags=("ops",), dependencies=[Depends(require_api_key)])
    async def purge(request) -> dict:
        """Delete every sighting."""
        ran.append("handler")
        return {"purged": True}

    mcp = MCP(app, name="camera-trap", version="1.0.0")
    with pytest.raises(ValueError) as caught:
        expose_routes(mcp, app, tags=("ops",))
    message = str(caught.value)
    assert "/admin/purge" in message
    assert "dependencies" in message
    assert mcp.tools == ()
    assert ran == []


async def test_a_route_s_middleware_is_not_silently_dropped() -> None:
    ran: list[str] = []

    class Blocker:
        async def before(self, request):
            ran.append("middleware")
            return Response(b"", status=403)

    app = Wreath()

    @app.get("/admin/wipe", tags=("ops",), middleware=[Blocker()])
    async def wipe(request) -> dict:
        """Wipe everything."""
        ran.append("handler")
        return {"wiped": True}

    mcp = MCP(app, name="camera-trap", version="1.0.0")

    async with TestClient(app) as client:
        refused = await client.get("/admin/wipe")
    assert refused.status == 403
    assert ran == ["middleware"]
    ran.clear()

    with pytest.raises(ValueError) as caught:
        expose_routes(mcp, app, tags=("ops",))
    message = str(caught.value)
    assert "/admin/wipe" in message
    assert "middleware" in message
    assert mcp.tools == ()
    assert ran == []


async def test_an_unguarded_route_is_still_exposed() -> None:
    app = Wreath()

    @app.get("/sightings", tags=("ops",))
    async def list_sightings(request) -> dict:
        """List recent sightings."""
        return {"items": []}

    mcp = MCP(app, name="camera-trap", version="1.0.0")
    (tool,) = expose_routes(mcp, app, tags=("ops",))
    assert tool.name == "list_sightings"
