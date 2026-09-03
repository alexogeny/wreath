from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from orm.conftest import FakeDatabase, Post, User

from wreath import Wreath
from wreath._auth.requirements import requirement_for
from wreath.auth import Identity
from wreath.authorization import (
    CedarAuthorizer,
    CedarPolicies,
    EntityUid,
    authorize,
    declared_actions,
    permissions_router,
)
from wreath.graphql import GraphQL
from wreath.grpc import GrpcService
from wreath.mcp import MCP
from wreath.orm.registry import Registry
from wreath.protobuf import field, message
from wreath.testing import TestClient

POLICIES = """
    permit(principal in Role::"ranger", action == Action::"Collar::read", resource);
    permit(principal in Role::"ranger", action == Action::"Camera::read", resource);
    permit(principal in Role::"ranger", action == Action::"read", resource == User::"id");
    permit(principal in Role::"ranger", action == Action::"read", resource == Query::"user");
"""


class _Backend:
    """Authenticates `Bearer <name>:<role>,<role>`; roles drive the policies."""

    scheme = "Bearer"

    def challenge(self, request: Any) -> str:
        return "Bearer"

    async def authenticate(self, request: Any) -> Identity | None:
        header = request.header("authorization")
        if not header or not header.startswith("Bearer "):
            return None
        name, _, roles = header[7:].partition(":")
        return Identity(
            name,
            roles=frozenset(r for r in roles.split(",") if r),
            permissions=frozenset({"graphql"}),
        )


@message
class Position:
    collar_id: int = field(1)


def _four_protocol_app() -> tuple[Wreath, MCP, GraphQL]:
    """One application declaring a control on each of the four surfaces."""
    app = Wreath()
    authorizer = CedarAuthorizer(engine=CedarPolicies(POLICIES))
    app.configure_auth(backend=_Backend(), authorizer=authorizer)

    # 1. REST.
    @app.get("/treks/{trek_id}")
    @authorize(action="Trek::read", resource="Trek")
    async def read_trek(request) -> dict:
        return {}

    # 2. gRPC: the same `action=` keyword, on a method rather than a route.
    tracker = GrpcService("camera.Tracker")

    @tracker.unary(
        request=Position,
        response=Position,
        action="Collar::read",
        resource=EntityUid("Collar", "7"),
    )
    async def GetPosition(request, incoming: Position) -> Position:
        return incoming

    app.include_router(tracker.router())

    # 3. MCP: the same `action=` keyword again, on a tool.
    mcp = MCP(app, name="camera-trap", version="1.0.0", path="/mcp")

    @mcp.tool(action="Camera::read", description="Read a camera.")
    async def read_camera(request) -> dict:
        return {}

    # 4. GraphQL: a field policy, whose resource is the field itself.
    graphql = GraphQL(
        Registry(FakeDatabase(), [User, Post]),
        models=[User, Post],
        authorizer=authorizer,
    )
    # Mounted behind a permission, per the guide: a GraphQL endpoint is exactly
    # as public as the route it sits on, and an anonymous caller would reach
    # every field policy as `anonymous` rather than as themselves.
    app.include_router(graphql.router(), permissions=("graphql",))
    return app, mcp, graphql


def test_a_rest_route_is_in_the_vocabulary() -> None:
    app, _mcp, _graphql = _four_protocol_app()
    assert "Trek::read" in declared_actions(app)["Trek"]


def test_a_grpc_method_is_in_the_vocabulary() -> None:
    app, _mcp, _graphql = _four_protocol_app()
    assert "Collar::read" in declared_actions(app)["Collar"]


def test_an_mcp_tool_is_in_the_vocabulary() -> None:
    app, mcp, _graphql = _four_protocol_app()
    assert "Camera::read" in declared_actions(app)["Camera"]
    assert mcp.declared_actions()["Camera"] == ("Camera::read",)


def test_a_graphql_field_policy_is_in_the_vocabulary() -> None:
    app, _mcp, _graphql = _four_protocol_app()
    vocabulary = declared_actions(app)
    assert vocabulary["User"] == ("read",)
    assert vocabulary["Query"] == ("read",)


def test_a_graphql_endpoint_with_no_authorizer_contributes_nothing() -> None:
    app = Wreath()
    graphql = GraphQL(Registry(FakeDatabase(), [User, Post]), models=[User, Post])
    app.include_router(graphql.router(public=True))
    assert declared_actions(app) == {}


def test_the_four_surfaces_share_one_dictionary() -> None:
    app, _mcp, _graphql = _four_protocol_app()
    vocabulary = declared_actions(app)
    assert vocabulary["Trek"] == ("Trek::read",)
    assert vocabulary["Collar"] == ("Collar::read",)
    assert vocabulary["Camera"] == ("Camera::read",)
    assert vocabulary["User"] == ("read",)


@pytest.mark.asyncio
async def test_the_manifest_answers_for_every_protocol() -> None:
    app, _mcp, _graphql = _four_protocol_app()
    app.include_router(permissions_router(app))
    async with TestClient(app) as client:
        response = await client.get(
            "/permissions/manifest", headers={"authorization": "Bearer ada:ranger"}
        )
        allowed = response.json()["allowed"]
    assert allowed["Collar"] == ["Collar::read"]
    assert allowed["Camera"] == ["Camera::read"]
    assert "Trek" not in allowed  # no policy permits it


@pytest.mark.asyncio
async def test_the_vocabulary_endpoint_publishes_every_protocol() -> None:
    app, _mcp, _graphql = _four_protocol_app()
    app.include_router(permissions_router(app))
    async with TestClient(app) as client:
        response = await client.get("/permissions", headers={"authorization": "Bearer ada:ranger"})
        resources = response.json()["resources"]
    assert set(resources) >= {"Trek", "Collar", "Camera", "User", "Query"}


@pytest.mark.asyncio
async def test_a_graphql_field_is_answered_exactly_by_the_batch_endpoint() -> None:
    app, _mcp, _graphql = _four_protocol_app()
    app.include_router(permissions_router(app))
    async with TestClient(app) as client:
        response = await client.post(
            "/permissions",
            json={"type": "User", "ids": ["id", "email"]},
            headers={"authorization": "Bearer ada:ranger"},
        )
        permissions = response.json()["permissions"]
    assert permissions["id"] == ["read"]
    assert permissions["email"] == []


@pytest.mark.asyncio
async def test_the_wider_vocabulary_is_still_only_chrome() -> None:
    app, _mcp, graphql = _four_protocol_app()
    app.include_router(permissions_router(app))
    async with TestClient(app) as client:
        manifest = (
            await client.get(
                "/permissions/manifest",
                headers={"authorization": "Bearer ada:ranger"},
            )
        ).json()["allowed"]
        body = (
            await client.post(
                "/graphql",
                json={"query": "{ user(id: 1) { id email } }"},
                headers={"authorization": "Bearer ada:ranger"},
            )
        ).json()
    # The manifest never claimed the field was readable ...
    assert "User" not in manifest
    # ... and the endpoint is what decided, on the request itself.
    assert body["errors"][0]["path"] == ["user", "email"]
    assert graphql.resolver_errors == 0


def test_typegen_emits_permission_types_for_every_protocol() -> None:
    from wreath.typegen.inspect import build_api_model

    app, _mcp, _graphql = _four_protocol_app()
    model = build_api_model(app, allow_unknown=True)
    by_type = {entry.resource_type: entry.actions for entry in model.permissions}
    assert by_type["Trek"] == ("Trek::read",)
    assert by_type["Collar"] == ("Collar::read",)
    assert by_type["Camera"] == ("Camera::read",)
    assert by_type["User"] == ("read",)


def test_a_grpc_method_action_records_the_requirement_authorize_records() -> None:
    service = GrpcService("camera.Tracker")

    @service.unary(request=Position, response=Position, action="Collar::read")
    async def GetPosition(request, incoming: Position) -> Position:
        return incoming

    endpoint = service.router().routes[0].endpoint
    requirement = requirement_for(endpoint)
    assert requirement.authenticated is True
    assert [policy.action for policy in requirement.policies] == ["Collar::read"]


@pytest.mark.asyncio
async def test_a_grpc_method_action_actually_refuses() -> None:
    app, _mcp, _graphql = _four_protocol_app()
    async with TestClient(app) as client:
        anonymous = await client.post("/camera.Tracker/GetPosition", content=b"")
        wrong_role = await client.post(
            "/camera.Tracker/GetPosition",
            content=b"",
            headers={"authorization": "Bearer bo:volunteer"},
        )
    assert anonymous.status == 401
    assert wrong_role.status == 403


def test_a_grpc_resource_without_an_action_is_refused_at_declaration() -> None:
    service = GrpcService("camera.Tracker")
    with pytest.raises(ValueError, match="resource=.*with no .action="):

        @service.unary(request=Position, response=Position, resource="Collar")
        async def GetPosition(request, incoming: Position) -> Position:
            return incoming
