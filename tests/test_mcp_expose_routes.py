"""`expose_routes`: an opt-in adapter with no way to say "all of them".

The design decisions being pinned here are the ones that make this safe to
ship at all. There is no `all=True`, so nobody converts an application's whole
HTTP surface -- including its destructive half -- into model-callable actions in
one line. A route with no description is refused. And an exposed route keeps
whatever was in front of it: the same Cedar decision, the same rate limiting,
the same Flight Recorder marker as a hand-declared tool, because a route
exposed as a tool that skipped any of those would be a hole rather than a
feature.
"""

from __future__ import annotations

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, authorize, permissions
from wreath.mcp import MCP, PROTOCOL_VERSION, ToolRateLimit, ToolSignatureError, expose_routes
from wreath.router import Router
from wreath.testing import TestClient, TestResponse


def header(response: TestResponse, name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in response.headers:
        if key == wanted:
            return value.decode("latin-1")
    return None


async def initialize(client: TestClient) -> str:
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION},
        },
    )
    return header(response, "mcp-session-id") or ""


async def call(client: TestClient, session: str, payload: dict) -> dict:
    response = await client.post("/mcp", json=payload, headers={"mcp-session-id": session})
    return response.json()


def build() -> tuple[Wreath, MCP]:
    app = Wreath()

    @app.get("/sightings", tags=("sightings",))
    async def list_sightings(request, species: str = "any", limit: int = 20) -> dict:
        """List recent sightings, newest first."""
        return {"species": species, "limit": limit}

    @app.post("/sightings", tags=("sightings",))
    async def record_sighting(request, species: str) -> dict:
        """Record a new sighting."""
        return {"recorded": species}

    @app.get("/health", tags=("ops",))
    async def health(request) -> dict:
        """Report whether the service is up."""
        return {"ok": True}

    mcp = MCP(app, name="camera-trap", version="1.0.0")
    return app, mcp


def test_there_is_no_way_to_expose_everything() -> None:
    """The decision the whole adapter turns on, asserted rather than described."""
    app, mcp = build()
    with pytest.raises(ValueError) as caught:
        expose_routes(mcp, app)
    message = str(caught.value)
    assert "selector" in message
    assert "tags=" in message
    with pytest.raises(TypeError):
        expose_routes(mcp, app, all=True)  # type: ignore[call-arg]


def test_a_tag_selects_and_everything_else_stays_unreachable() -> None:
    app, mcp = build()
    declared = expose_routes(mcp, app, tags=("sightings",))
    assert [tool.name for tool in declared] == ["list_sightings", "record_sighting"]
    assert [tool.name for tool in mcp.tools] == ["list_sightings", "record_sighting"]
    # The MCP endpoint's own routes carry the `mcp` tag and are never candidates
    # for a selector that did not name it.
    assert "health" not in [tool.name for tool in mcp.tools]


def test_a_path_and_a_predicate_are_the_other_two_selectors() -> None:
    app, mcp = build()
    expose_routes(mcp, app, include=("/health",))
    assert [tool.name for tool in mcp.tools] == ["health"]

    _, other = build()
    expose_routes(other, predicate=lambda route: "GET" in route.methods
                  and route.path == "/sightings")
    assert [tool.name for tool in other.tools] == ["list_sightings"]


def test_a_selector_that_matches_nothing_is_an_error() -> None:
    app, mcp = build()
    with pytest.raises(ValueError, match="matched no route"):
        expose_routes(mcp, app, tags=("sitings",))


async def test_an_exposed_route_is_callable_with_its_own_schema() -> None:
    app, mcp = build()
    expose_routes(mcp, app, tags=("sightings",))
    async with TestClient(app) as client:
        session = await initialize(client)
        listed = await call(
            client, session, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        entry = next(t for t in listed["result"]["tools"] if t["name"] == "list_sightings")
        # The description is the handler's docstring, which is the same text the
        # OpenAPI document carries as the operation description.
        assert entry["description"] == "List recent sightings, newest first."
        assert entry["inputSchema"]["properties"]["limit"]["default"] == 20

        called = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "list_sightings",
                    "arguments": {"species": "fox", "limit": 5},
                },
            },
        )
        assert called["result"]["structuredContent"] == {"species": "fox", "limit": 5}
        assert mcp.tool_calls == 1


async def test_a_route_with_no_docstring_is_refused_by_name() -> None:
    app = Wreath()

    @app.get("/quiet", tags=("public",))
    async def quiet(request) -> dict:
        return {}

    mcp = MCP(app, name="x", version="1.0.0")
    with pytest.raises(ValueError) as caught:
        expose_routes(mcp, app, tags=("public",))
    message = str(caught.value)
    assert "/quiet" in message
    assert "docstring" in message


async def test_a_path_parameter_is_refused_naming_the_route_and_the_parameter() -> None:
    """The refusal route-derived tools hit far more often than declared ones."""
    app = Wreath()

    @app.get("/sightings/{sighting_id}", tags=("public",))
    async def show_sighting(request, sighting_id: int) -> dict:
        """Show one sighting."""
        return {"id": sighting_id}

    mcp = MCP(app, name="x", version="1.0.0")
    with pytest.raises(ToolSignatureError) as caught:
        expose_routes(mcp, app, tags=("public",))
    message = str(caught.value)
    assert "/sightings/{sighting_id}" in message
    assert "sighting_id" in message
    assert "path placeholder" in message
    # And it says what to do instead, because "no" on its own is not actionable.
    assert "declare a tool of your own" in message


async def test_a_dependency_is_refused_naming_the_route() -> None:
    """The route's `dependencies=` tuple, which a tool call would not replay.

    The handler's signature is deliberately clean. A `Depends(...)` *parameter*
    is refused earlier, by the schema derivation, for an unrelated reason -- so
    a handler carrying one never reaches `_check_carryable` and cannot prove
    anything about it. This test names the route-level control, so it declares
    the route-level control and nothing else.
    """
    from wreath.binding import Depends

    ran: list[str] = []

    async def require_api_key(request) -> None:
        ran.append("dependency")
        raise PermissionError("no api key")

    app = Wreath()

    @app.get("/counted", tags=("public",), dependencies=[Depends(require_api_key)])
    async def counted(request) -> dict:
        """Count something."""
        ran.append("handler")
        return {"count": 1}

    mcp = MCP(app, name="x", version="1.0.0")
    with pytest.raises(ValueError) as caught:
        expose_routes(mcp, app, tags=("public",))
    message = str(caught.value)
    assert "/counted" in message
    assert "dependencies" in message
    assert mcp.tools == ()
    assert ran == []


async def test_a_dependency_parameter_is_refused_naming_the_route() -> None:
    """The other refusal, kept: a `Depends(...)` parameter names its route too.

    This is the body the test above used to have. It exercises the schema
    derivation's refusal rather than `_check_carryable`'s, which is why it now
    has a name that says so.
    """
    from wreath.binding import Depends

    async def provide() -> int:
        return 1

    app = Wreath()

    @app.get("/counted", tags=("public",))
    async def counted(request, count: int = Depends(provide)) -> dict:
        """Count something."""
        return {"count": count}

    mcp = MCP(app, name="x", version="1.0.0")
    with pytest.raises(ToolSignatureError) as caught:
        expose_routes(mcp, app, tags=("public",))
    assert "/counted" in str(caught.value)
    assert "Depends" in str(caught.value)


# -- the controls come with the route ----------------------------------------


class Engine:
    """Permits `Sighting::read` and refuses everything else."""

    def __init__(self) -> None:
        self.asked: list[object] = []

    def is_authorized(self, **request: object) -> bool:
        self.asked.append(request["action"])
        return request["action"] == "Sighting::read"


def gated(identity: Identity) -> tuple[Wreath, MCP, Engine, list[str]]:
    engine = Engine()
    ran: list[str] = []
    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda _t: identity),
        CedarAuthorizer(
            engine=engine,
            principal=lambda identity: f"User::{identity.id}",
            action=lambda action, request: action,
            resource=lambda resource, request: resource,
            entities=lambda request: (),
            context=lambda request: {},
        ),
    )

    @app.get("/sightings", tags=("sightings",))
    @authorize(action="Sighting::read", resource="all")
    async def list_sightings(request) -> dict:
        """List recent sightings."""
        ran.append("list_sightings")
        return {"ok": True}

    @app.delete("/sightings", tags=("sightings",))
    @authorize(action="Sighting::purge", resource="all")
    async def purge_sightings(request) -> dict:
        """Delete every sighting."""
        ran.append("purge_sightings")
        return {"ok": True}

    @app.get("/staff", tags=("sightings",))
    @permissions("staff::read")
    async def staff_only(request) -> dict:
        """Something only staff may see."""
        ran.append("staff_only")
        return {"ok": True}

    mcp = MCP(app, name="x", version="1.0.0")
    expose_routes(mcp, app, tags=("sightings",))
    return app, mcp, engine, ran


async def test_a_route_s_cedar_policy_comes_with_it() -> None:
    """The assertion the whole adapter has to earn."""
    app, mcp, engine, ran = gated(Identity("ada", permissions=frozenset({"staff::read"})))
    assert mcp.declared_actions() == {
        "Sighting": ("Sighting::purge", "Sighting::read"),
    }
    async with TestClient(app, headers={"authorization": "Bearer t"}) as client:
        session = await initialize(client)
        allowed = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_sightings"},
            },
        )
        assert allowed["result"]["isError"] is False

        denied = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "purge_sightings"},
            },
        )
        assert denied["error"]["code"] == -32001
        assert "Sighting::purge" in denied["error"]["message"]
        assert ran == ["list_sightings"]
        assert engine.asked == ["Sighting::read", "Sighting::purge"]
        assert mcp.unauthorized_calls == 1
        assert mcp.tool_errors == 0


async def test_a_route_s_permissions_come_with_it_too() -> None:
    _, mcp, _, ran = gated(Identity("ada", permissions=frozenset()))
    app, *_ = gated(Identity("ada", permissions=frozenset()))
    async with TestClient(app, headers={"authorization": "Bearer t"}) as client:
        session = await initialize(client)
        refused = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "staff_only"},
            },
        )
        assert refused["error"]["code"] == -32001
        assert "permissions" in refused["error"]["message"]


async def test_a_protected_route_exposed_on_an_unauthenticated_endpoint_refuses() -> None:
    """Fail closed, and say which of the two boundaries is missing."""
    app = Wreath()

    @app.get("/sightings", tags=("sightings",))
    @authorize(action="Sighting::read", resource="all")
    async def list_sightings(request) -> dict:
        """List recent sightings."""
        return {"ok": True}

    mcp = MCP(app, name="x", version="1.0.0")
    expose_routes(mcp, app, tags=("sightings",))
    async with TestClient(app) as client:
        session = await initialize(client)
        body = await call(
            client,
            session,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_sightings"},
            },
        )
        assert body["error"]["code"] == -32001
        assert "MCPAuth" in body["error"]["message"]
        assert mcp.unauthorized_calls == 1


async def test_a_rate_limit_applies_to_every_route_this_call_exposes() -> None:
    app, mcp = build()
    expose_routes(mcp, app, tags=("ops",), rate_limit=ToolRateLimit(limit=1, window=60.0))
    async with TestClient(app) as client:
        session = await initialize(client)
        first = await call(
            client,
            session,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "health"}},
        )
        assert first["result"]["isError"] is False
        second = await call(
            client,
            session,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "health"}},
        )
        assert second["error"]["code"] == -32003
        assert mcp.throttled == 1


async def test_a_router_s_inherited_permissions_reach_the_tool() -> None:
    """Inheritance folds into the definition, so the tool must see it folded."""
    app = Wreath()
    internal = Router(prefix="/internal", tags=("internal",), permissions=["internal::use"])

    @internal.get("/stats")
    async def stats(request) -> dict:
        """Internal statistics."""
        return {"ok": True}

    app.include_router(internal)
    app.configure_auth(BearerTokenBackend(lambda _t: Identity("ada")), None)
    mcp = MCP(app, name="x", version="1.0.0")
    (tool,) = expose_routes(mcp, app, tags=("internal",))
    assert tool.route == "/internal/stats"
    assert tool.requirement.permission_checks

    async with TestClient(app, headers={"authorization": "Bearer t"}) as client:
        session = await initialize(client)
        body = await call(
            client,
            session,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "stats"}},
        )
        assert body["error"]["code"] == -32001


async def test_an_exposed_route_leaves_the_same_record_a_declared_tool_does() -> None:
    from wreath import _flight_schema as fs
    from wreath import logging as log

    app, mcp = build()
    expose_routes(mcp, app, tags=("ops",))
    with log.testing_runtime() as records, log.request_scope(request_id=7):
        async with TestClient(app) as client:
            session = await initialize(client)
            await call(
                client,
                session,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "health"},
                },
            )
        markers = [
            log.attributes(cell)
            for cell in records
            if not cell.flags & fs.LOG_FLAG_EVENT_FIELDS
        ]
    # The identical marker a declared tool leaves: an exposed route is not a
    # second dispatch path, so it cannot have a second (or no) audit trail.
    assert [(m["tool"], m["outcome"]) for m in markers] == [("health", "ok")]


def test_a_prefix_keeps_two_applications_from_colliding() -> None:
    app, mcp = build()
    expose_routes(mcp, app, tags=("ops",), prefix="reserve_")
    assert [tool.name for tool in mcp.tools] == ["reserve_health"]


def test_a_derived_name_that_collides_is_refused() -> None:
    app, mcp = build()

    @mcp.tool(description="Declared by hand first.")
    async def health(request) -> dict:
        return {}

    with pytest.raises(ValueError, match="already registered"):
        expose_routes(mcp, app, tags=("ops",))
