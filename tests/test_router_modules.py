from __future__ import annotations

from typing import Any

import pytest

from wreath import Depends, Router, Wreath
from wreath.auth import Identity
from wreath.testing import TestClient
from wreath.websocket import WebSocket


class HeaderIdentityBackend:
    def challenge(self, request: Any) -> str:
        return "Bearer"

    async def authenticate(self, request: Any) -> Identity | None:
        value = request.header("authorization")
        if value is None:
            return None
        permissions = frozenset(value.removeprefix("Bearer ").split(","))
        return Identity("test-user", permissions=permissions)


@pytest.mark.asyncio
async def test_nested_router_flattens_prefixes_and_inherits_metadata() -> None:
    calls: list[str] = []

    async def app_dependency(request: Any) -> None:
        calls.append("app")

    async def parent_dependency(request: Any) -> None:
        calls.append("parent")

    async def child_dependency(request: Any) -> None:
        calls.append("child")

    parent = Router(
        prefix="/admin",
        tags=("admin",),
        dependencies=(Depends(parent_dependency),),
        permissions=("admin:access",),
    )
    child = Router(
        prefix="/users",
        tags=("users",),
        dependencies=(Depends(child_dependency),),
        permissions=("users:read",),
    )

    @child.get("/{user_id}")
    async def user(request: Any, user_id: int) -> dict[str, int]:
        return {"id": user_id}

    parent.include_router(child)
    app = Wreath()
    app.configure_auth(HeaderIdentityBackend())
    app.include_router(
        parent,
        prefix="/v1",
        tags=("v1",),
        dependencies=(Depends(app_dependency),),
        permissions=("api:access",),
    )

    async with TestClient(app) as client:
        missing = await client.get("/v1/admin/users/7")
        assert missing.status == 401
        denied = await client.get(
            "/v1/admin/users/7",
            headers={"authorization": "Bearer api:access,admin:access"},
        )
        assert denied.status == 403
        response = await client.get(
            "/v1/admin/users/7",
            headers={"authorization": "Bearer api:access,admin:access,users:read"},
        )

    assert response.status == 200
    assert response.json() == {"id": 7}
    assert calls == ["app", "parent", "child"]
    definition = app._routes[0]
    assert definition.path == "/v1/admin/users/{user_id}"
    assert definition.tags == ("v1", "admin", "users")
    assert definition.requirement.authenticated is True


@pytest.mark.asyncio
async def test_router_dependencies_share_request_cache_with_handler_dependencies() -> None:
    calls = 0

    async def shared(request: Any) -> object:
        nonlocal calls
        calls += 1
        return object()

    router = Router(dependencies=(Depends(shared),))

    @router.get("/cached")
    async def cached(request: Any, value: object = Depends(shared)) -> dict[str, bool]:
        return {"ok": value is not None}

    app = Wreath()
    app.include_router(router)
    async with TestClient(app) as client:
        response = await client.get("/cached")

    assert response.status == 200
    assert response.json() == {"ok": True}
    assert calls == 1


def test_router_snapshots_included_routes() -> None:
    child = Router(prefix="/child")

    @child.get("/first")
    async def first(request: Any) -> str:
        return "first"

    parent = Router(prefix="/parent")
    parent.include_router(child)

    @child.get("/second")
    async def second(request: Any) -> str:
        return "second"

    assert [route.path for route in parent.routes] == ["/parent/child/first"]


def test_router_preserves_response_and_security_metadata() -> None:
    router = Router()

    @router.post(
        "/items",
        status_code=201,
        responses={409: dict},
        security={"bearer": ("items:write",)},
    )
    async def create(request: Any) -> dict:
        return {}

    definition = router.routes[0]
    assert definition.status_code == 201
    assert definition.responses == ((409, dict),)
    assert definition.security == (("bearer", ("items:write",)),)


def test_router_permission_requirement_demands_an_identity() -> None:
    router = Router(permissions=("items:read",))

    @router.get("/items")
    async def list_items(request: Any) -> list[object]:
        return []

    assert router.routes[0].requirement.authenticated is True


@pytest.mark.asyncio
async def test_nested_router_websocket_composes_prefixes_and_permissions() -> None:
    child = Router(prefix="/stream", permissions=("stream:read",))

    @child.websocket("/{channel}")
    async def stream(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.close()

    parent = Router(prefix="/llama", permissions=("trek:enter",))
    parent.include_router(child)
    app = Wreath(hardening="off")
    app.configure_auth(HeaderIdentityBackend())
    app.include_router(parent, prefix="/v1", permissions=("api:use",))

    async def drive(authorization: bytes | None) -> list[dict[str, Any]]:
        incoming = iter(({"type": "websocket.connect"},))
        sent: list[dict[str, Any]] = []
        headers = [] if authorization is None else [(b"authorization", authorization)]

        async def receive() -> dict[str, Any]:
            return next(incoming)

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await app(
            {
                "type": "websocket",
                "path": "/v1/llama/stream/ridge",
                "query_string": b"",
                "headers": headers,
                "subprotocols": [],
            },
            receive,
            send,
        )
        return sent

    denied = await drive(b"Bearer api:use,trek:enter")
    allowed = await drive(b"Bearer api:use,trek:enter,stream:read")

    assert denied == [{"type": "websocket.close", "code": 1008}]
    assert allowed[0]["type"] == "websocket.accept"
    assert allowed[-1] == {"type": "websocket.close", "code": 1000, "reason": ""}
    assert app._ws_routes[0][0] == "/v1/llama/stream/{channel}"


@pytest.mark.parametrize("status", [99, 600])
def test_router_refuses_an_invalid_status_code(status: int) -> None:
    router = Router()
    with pytest.raises(ValueError, match="status_code"):
        router.get("/items", status_code=status)
