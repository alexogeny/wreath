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
