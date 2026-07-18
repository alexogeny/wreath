from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath._pure.dtrouter import DecisionRouteTable as PureDecisionRouteTable
from wreath.auth import BearerTokenBackend, Identity
from wreath.authorization import CedarAuthorizer, authorize, roles

try:
    from wreath._native import _core
except ImportError:  # pragma: no cover
    _core = None


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


@pytest.mark.parametrize(
    "table_type",
    [
        pytest.param(PureDecisionRouteTable, id="pure"),
        pytest.param(
            None if _core is None else _core.DecisionRouteTable,
            id="native",
            marks=pytest.mark.skipif(_core is None, reason="native extension unavailable"),
        ),
    ],
)
def test_probe_classifies_public_protected_and_missing_paths(table_type: type) -> None:
    authenticated = 1
    table = table_type()
    public_handler = object()
    private_handler = object()
    table.add("/public", "GET", public_handler, (0,))
    table.add("/private/{item}", "GET", private_handler, (authenticated,))

    assert table.probe("GET", "/public", authenticated) == (
        1,
        (public_handler, None),
    )
    assert table.probe("GET", "/private/42", authenticated) == (2, None)
    assert table.probe("GET", "/missing", authenticated) == (0, None)


@pytest.mark.parametrize(
    "table_type",
    [
        pytest.param(PureDecisionRouteTable, id="pure"),
        pytest.param(
            None if _core is None else _core.DecisionRouteTable,
            id="native",
            marks=pytest.mark.skipif(_core is None, reason="native extension unavailable"),
        ),
    ],
)
def test_single_pass_classification_resolves_protected_ticket(table_type: type) -> None:
    authenticated = 1
    table = table_type()
    private_handler = object()
    table.add("/private/{item}", "GET", private_handler, (authenticated,))

    classification, ticket = table.classify("GET", "/private/42")

    assert classification == 2
    assert table.resolve(ticket, 0) is None
    assert table.resolve(ticket, authenticated) == (
        private_handler,
        {"item": "42"},
    )


@pytest.mark.parametrize(
    "table_type",
    [
        pytest.param(PureDecisionRouteTable, id="pure"),
        pytest.param(
            None if _core is None else _core.DecisionRouteTable,
            id="native",
            marks=pytest.mark.skipif(_core is None, reason="native extension unavailable"),
        ),
    ],
)
def test_probe_preserves_public_dynamic_fallback_behind_protected_static(
    table_type: type,
) -> None:
    table = table_type()
    protected_handler = object()
    fallback_handler = object()
    table.add("/reports/current", "GET", protected_handler, (1,))
    table.add("/reports/{name}", "GET", fallback_handler, (0,))

    assert table.probe("GET", "/reports/current", 1) == (
        1,
        (fallback_handler, {"name": "current"}),
    )


@pytest.mark.asyncio
async def test_true_404_does_not_authenticate() -> None:
    calls = 0

    async def verify(token: str) -> Identity | None:
        nonlocal calls
        calls += 1
        return Identity(token)

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/private")
    @roles("admin")
    async def private(request):
        return "private"

    sent = await invoke(app, "/missing", authorization=b"Bearer user")

    assert sent[0]["status"] == 404
    assert calls == 0


@pytest.mark.asyncio
async def test_local_rbac_denial_never_calls_cedar() -> None:
    engine_calls = 0

    class Engine:
        def is_authorized(self, **request: object) -> bool:
            nonlocal engine_calls
            engine_calls += 1
            return True

    async def verify(token: str) -> Identity | None:
        return Identity(token, roles=frozenset({"user"}))

    authorizer = CedarAuthorizer(
        engine=Engine(),
        principal=lambda identity: identity.id,
        action=lambda action, request: action,
        resource=lambda resource, request: resource,
        entities=lambda request: (),
        context=lambda request: {},
    )
    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify), authorizer)

    @app.get("/admin")
    @roles("admin")
    @authorize(action="read", resource="admin")
    async def admin(request):
        return "admin"

    sent = await invoke(app, "/admin", authorization=b"Bearer user")

    assert sent[0]["status"] == 403
    assert engine_calls == 0
