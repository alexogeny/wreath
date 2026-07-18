from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import permissions, roles


async def invoke(app: Wreath, path: str, token: str) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        },
        receive,
        send,
    )
    return sent


@pytest.mark.asyncio
async def test_rbac_combines_role_alternatives_and_permission_layers() -> None:
    identities = {
        "support": Identity(
            "support", roles=frozenset({"support"}), permissions=frozenset({"users:read"})
        ),
        "admin": Identity(
            "admin", roles=frozenset({"admin"}), permissions=frozenset({"users:read"})
        ),
        "auditor": Identity(
            "auditor", roles=frozenset({"auditor"}), permissions=frozenset({"users:read"})
        ),
        "limited": Identity("limited", roles=frozenset({"support"})),
    }

    async def verify(token: str) -> Identity | None:
        return identities.get(token)

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/users")
    @roles("admin", "support", mode="any")
    @permissions("users:read")
    async def users(request):
        return "allowed"

    assert (await invoke(app, "/users", "support"))[0]["status"] == 200
    assert (await invoke(app, "/users", "admin"))[0]["status"] == 200
    assert (await invoke(app, "/users", "auditor"))[0]["status"] == 403
    assert (await invoke(app, "/users", "limited"))[0]["status"] == 403
