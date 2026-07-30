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


# --- the session backend's ordering requirement -------------------------------
#
# `SessionIdentityBackend` reads `request.state.session`, which `SessionMiddleware`
# publishes. Route middleware runs *after* authorization, so registering the two
# in the obvious way authenticated every caller as anonymous and answered 401 to
# a valid session cookie -- silently, and identically to a genuine anonymous
# request. These pin the refusal that replaced it.


def _session_app(*, global_scope: bool) -> Wreath:
    from wreath.auth import SessionIdentityBackend
    from wreath.middleware import SessionMiddleware

    app = Wreath()
    app.configure_auth(SessionIdentityBackend())
    middleware = SessionMiddleware(secret="x" * 32, secure=False)
    if global_scope:
        app.add_global_middleware(middleware)
    else:
        app.add_middleware(middleware)

    @app.get("/me")
    @authenticated()
    async def me(request: Any) -> dict[str, Any]:
        return {"id": request.identity.id}

    return app


def test_a_session_backend_refuses_route_scoped_session_middleware() -> None:
    app = _session_app(global_scope=False)
    with pytest.raises(TypeError) as caught:
        app._compile_routes()
    message = str(caught.value)
    # The remedy has to be in the message: the failure it replaces was a 401,
    # which says nothing about middleware ordering.
    assert "add_global_middleware" in message
    assert "SessionMiddleware" in message
    assert "SessionIdentityBackend" in message


def test_the_correct_registration_is_not_refused() -> None:
    """Otherwise the refusal above could pass by refusing everything."""
    app = _session_app(global_scope=True)
    app._compile_routes()


def test_a_composite_backend_propagates_the_session_requirement() -> None:
    """A wrapper must not hide the requirement its members carry."""
    from wreath.auth import CompositeBackend, SessionIdentityBackend

    bearer = BearerTokenBackend({"t": Identity(id="bo", type="User")})
    assert not getattr(bearer, "requires_session", False)
    assert CompositeBackend(bearer, SessionIdentityBackend()).requires_session
    assert not CompositeBackend(bearer).requires_session


def test_a_bearer_only_app_may_still_use_route_scoped_sessions() -> None:
    """The refusal is about the *backend's* need, not about sessions at all.

    A session used only by handlers -- a flash message, a wizard step -- has no
    ordering requirement, and route scope is the cheaper registration because a
    miss or a static file never decodes the cookie. Refusing that too would have
    made the check a blanket ban rather than a statement about ordering.
    """
    from wreath.middleware import SessionMiddleware

    app = Wreath()
    app.configure_auth(BearerTokenBackend({"t": Identity(id="bo", type="User")}))
    app.add_middleware(SessionMiddleware(secret="x" * 32, secure=False))

    @app.get("/me")
    @authenticated()
    async def me(request: Any) -> dict[str, Any]:
        return {"id": request.identity.id}

    app._compile_routes()


# --- one rule, two enforcers -------------------------------------------------


def _step_up_app() -> tuple[Wreath, Any]:
    """One route carrying a *bare* second-factor requirement, exposed as a tool.

    Bare meaning `authenticated` is not set beside it, which no decorator
    produces -- `@second_factor` sets both. Constructed directly here because
    that is the case the two enforcers used to answer differently, and the next
    feature that builds an `AuthRequirement` itself is the one that would meet
    it.
    """
    from wreath._auth.requirements import set_requirement
    from wreath.authorization import AuthRequirement
    from wreath.mcp import MCP, expose_routes

    async def verify(token: str) -> Identity | None:
        # An identity that never proved a second factor: no `second_factor_at`
        # claim, which `second_factor_age` reads as a refusal rather than a zero.
        return Identity(id="bo", type="User", claims={}) if token == "t" else None

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/wipe", tags=("danger",))
    async def wipe(request: Any) -> dict[str, Any]:
        """Delete everything, irreversibly."""
        return {"wiped": True}

    set_requirement(wipe, AuthRequirement(second_factor=300.0))
    mcp = MCP(app, name="t", version="1.0.0", path="/mcp")
    expose_routes(mcp, app, tags=("danger",))
    return app, mcp


@pytest.mark.asyncio
async def test_a_bare_second_factor_requirement_is_refused_over_http() -> None:
    """The HTTP pipeline's half of the agreement: an identity that has not
    proved a factor lately is refused, and an anonymous caller is challenged
    rather than admitted because the requirement never set `authenticated`."""
    app, _ = _step_up_app()

    refused = await invoke(app, "/wipe", authorization=b"Bearer t")
    assert refused[0]["status"] == 403
    assert b"second_factor_required" in refused[1]["body"]

    anonymous = await invoke(app, "/wipe")
    assert anonymous[0]["status"] == 401


@pytest.mark.asyncio
async def test_the_same_requirement_is_refused_by_mcp() -> None:
    """MCP's half. `_authorize` used to skip the whole decision on
    `access_level == 0`, which a bare second-factor requirement was -- so the
    identical declaration refused an HTTP caller and admitted a model."""
    from wreath.mcp import PROTOCOL_VERSION
    from wreath.testing import TestClient

    app, _ = _step_up_app()
    headers = {"authorization": "Bearer t"}
    async with TestClient(app) as client:
        opened = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION},
            },
            headers=headers,
        )
        session = dict(opened.headers)[b"mcp-session-id"].decode()
        answer = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "wipe", "arguments": {}},
            },
            headers={**headers, "mcp-session-id": session},
        )
    body = answer.json()
    assert "error" in body, body
    assert "second factor" in body["error"]["message"], body


@pytest.mark.asyncio
async def test_the_two_enforcers_ask_the_same_question_of_a_requirement() -> None:
    """The shared definition itself, over every field that can refuse a caller.

    Both enforcers gate on `access_level`, so a field it forgets is a field one
    of them skips: `second_factor` was missing from it, and MCP read it while
    the HTTP pipeline read `authenticated` instead. Asserted per field rather
    than per enforcer, because the defect is a field going unnamed.
    """
    from wreath._auth.requirements import PolicyRequirement, SetRequirement
    from wreath.authorization import AuthRequirement

    admin = SetRequirement(frozenset({"admin"}), "all")
    assert AuthRequirement().access_level == 0
    assert AuthRequirement(identify=True).access_level == 0   # asks nothing of the caller
    assert AuthRequirement(role_checks=(admin,)).access_level == 2
    for requirement in (
        AuthRequirement(authenticated=True),
        AuthRequirement(second_factor=300.0),
        AuthRequirement(role_checks=(SetRequirement(frozenset({"staff"}), "all"),)),
        AuthRequirement(permission_checks=(SetRequirement(frozenset({"p"}), "any"),)),
        AuthRequirement(policies=(PolicyRequirement("Thing::wipe", None),)),
    ):
        assert requirement.access_level > 0, requirement
        assert requirement.needs_backend, requirement
