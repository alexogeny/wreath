"""Attacks on the MCP surface, each written to fail before it was fixed.

Two holes, and both are the same shape: a control that is real on one path and
absent on the path beside it, so the docstring above it reads as true and the
deployment that took a different-but-supported route has none of it.

* **A session was bound to its opener only when `MCPAuth` was the thing doing
  the authenticating.** `Session.principal` says a leaked `Mcp-Session-Id` must
  not be a credential in its own right, and `_owns` enforces exactly that -- but
  the principal was recorded from the identity `MCPAuth` had published, and an
  application that authenticates with `app.configure_auth(...)` instead
  publishes nothing at `initialize`. Every session on such an endpoint was
  therefore unbound, and a second verified caller who learned an id could drive
  another caller's session: cancel its calls, end it, and open its
  server-to-client stream, which is where a tool's `elicitation/create` form and
  its progress reports travel.
* **`expose_routes` carried a route's `AuthRequirement` and dropped everything
  else in front of it.** A route's own `middleware=` and `dependencies=` are
  controls a person put on that route; the adapter read `definition.requirement`
  and nothing else, so a route guarded by `Depends(require_api_key)` became a
  tool with no key check and a route behind a refusing middleware became a tool
  that runs the handler.

Both are checked here rather than in `tests/test_mcp_auth.py` and
`tests/test_mcp_expose_routes.py` so a red-team round does not collide with the
suites that describe the intended behaviour.
"""

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


# -- a session is a credential for one subject --------------------------------


async def test_another_caller_cannot_drive_the_session_ada_opened() -> None:
    """The attack: Bob learns Ada's `Mcp-Session-Id` and uses it.

    A session id travels in a header, is logged by intermediaries that log
    headers, and is held by whatever client Ada is running -- so "it is secret"
    is not the control. The control is that it names Ada, and a request from
    anybody else is refused whatever it carries.
    """
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
    """`DELETE` is the cheapest cross-caller attack: one request, no body."""
    app, mcp = application_authenticated()
    async with TestClient(app) as client:
        session = await initialize(client, "ada")
        ended = await client.delete(
            "/mcp", headers=bearer("bob") | {"mcp-session-id": session}
        )
        assert ended.status == 401
        assert mcp.sessions == 1


async def test_another_caller_cannot_open_the_stream_of_ada_s_session() -> None:
    """The stream is where a tool's questions to Ada travel.

    `notifications/progress`, a subscribed resource changing, and above all the
    `elicitation/create` form a tool puts in front of the person at the other
    end -- all of it goes onto the session's `GET` stream. A second caller who
    could open it would read Ada's prompts and be able to answer them.
    """
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
    """The guard must not invent a requirement where there is no subject.

    An endpoint with neither `MCPAuth` nor an application backend has nobody to
    bind a session to, and binding one to `None` would refuse every request on
    it. This is the case that makes the fix's condition load-bearing rather
    than a blanket refusal.
    """
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


# -- a route's controls come with it, or the route is not exposed -------------


async def test_a_route_s_dependencies_are_not_silently_dropped() -> None:
    """The attack: put the API-key check in `dependencies=` and expose the route.

    `Depends(...)` as a *parameter* is already refused by name. The same marker
    passed to `app.get(dependencies=[...])` is not a parameter, it is the shape
    a guard is usually written in -- resolved before the handler runs, free to
    refuse -- and it was read by nothing in the adapter.
    """
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
    """The same attack one layer out: the refusal lives in route middleware.

    A `before` hook that returns a response is how Wreath spells "this request
    stops here", and a route carrying one is a route somebody guarded.
    """
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
    """The refusals must not swallow the ordinary case they sit beside."""
    app = Wreath()

    @app.get("/sightings", tags=("ops",))
    async def list_sightings(request) -> dict:
        """List recent sightings."""
        return {"items": []}

    mcp = MCP(app, name="camera-trap", version="1.0.0")
    (tool,) = expose_routes(mcp, app, tags=("ops",))
    assert tool.name == "list_sightings"
