from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity, authenticated
from wreath.authorization import roles


async def invoke(
    app: Wreath, path: str, *, authorization: bytes | None = None
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    headers = [] if authorization is None else [(b"authorization", authorization)]

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {"type": "http", "method": "GET", "path": path, "headers": headers},
        receive,
        send,
    )
    return sent


@pytest.mark.asyncio
async def test_public_route_does_not_invoke_authentication_backend() -> None:
    calls = 0

    async def verify(token: str) -> Identity | None:
        nonlocal calls
        calls += 1
        return Identity(token)

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/public")
    async def public(request):
        return "public"

    sent = await invoke(app, "/public")

    assert sent[0]["status"] == 200
    assert calls == 0


@pytest.mark.asyncio
async def test_authenticated_route_challenges_then_exposes_identity() -> None:
    async def verify(token: str) -> Identity | None:
        return Identity("user-1") if token == "valid" else None

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/private")
    @authenticated()
    async def private(request):
        return request.identity.id

    missing = await invoke(app, "/private")
    allowed = await invoke(app, "/private", authorization=b"Bearer valid")

    assert missing[0]["status"] == 401
    assert (b"www-authenticate", b"Bearer") in missing[0]["headers"]
    assert allowed[1]["body"] == b"user-1"


@pytest.mark.asyncio
async def test_admin_route_is_pruned_for_non_admin_and_allowed_for_admin() -> None:
    async def verify(token: str) -> Identity | None:
        if token == "admin":
            return Identity("admin-1", roles=frozenset({"admin"}))
        if token == "user":
            return Identity("user-1", roles=frozenset({"user"}))
        return None

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/admin")
    @roles("admin")
    async def admin(request):
        return "admin"

    denied = await invoke(app, "/admin", authorization=b"Bearer user")
    allowed = await invoke(app, "/admin", authorization=b"Bearer admin")

    assert denied[0]["status"] == 403
    assert allowed[0]["status"] == 200
