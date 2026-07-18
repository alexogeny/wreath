from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, authorize


class Engine:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def is_authorized(self, **request: object) -> bool:
        self.calls.append(request)
        return request["principal"] == "User::alice" and request["resource"] == "Document::42"


async def invoke(app: Wreath, token: str) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": "/documents/42",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        },
        receive,
        send,
    )
    return sent


@pytest.mark.asyncio
async def test_cedar_adapter_is_final_authorization_after_coarse_route_pruning() -> None:
    engine = Engine()

    async def verify(token: str) -> Identity | None:
        return Identity(token) if token in {"alice", "bob"} else None

    authorizer = CedarAuthorizer(
        engine=engine,
        principal=lambda identity: f"User::{identity.id}",
        action=lambda action, request: action,
        resource=lambda resource, request: f"Document::{resource}",
        entities=lambda request: (),
        context=lambda request: {"method": request.method},
    )
    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify), authorizer)

    @app.get("/documents/{document_id}")
    @authorize(action="Document::read", resource=lambda request: request.path_params["document_id"])
    async def document(request):
        return "allowed"

    allowed = await invoke(app, "alice")
    denied = await invoke(app, "bob")

    assert allowed[0]["status"] == 200
    assert denied[0]["status"] == 403
    assert engine.calls[0]["context"] == {"method": "GET"}
