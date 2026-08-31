from __future__ import annotations

from typing import Any

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity, authenticated, public
from wreath.authorization import AuthorizationVocabulary, authorize, roles
from wreath.policy import HttpPolicy
from wreath.request import Request


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
async def test_strict_access_declarations_refuse_an_undeclared_route() -> None:
    app = Wreath(require_access_declarations=True)

    @app.get("/forgotten")
    async def forgotten(request: Any) -> str:
        return "open"

    with pytest.raises(RuntimeError, match=r"GET /forgotten.*@public"):
        await invoke(app, "/forgotten")


@pytest.mark.asyncio
async def test_public_is_an_explicit_declaration_without_backend_work() -> None:
    app = Wreath(require_access_declarations=True)

    @app.get("/health")
    @public()
    async def health(request: Any) -> str:
        return "ok"

    sent = await invoke(app, "/health")
    assert sent[0]["status"] == 200


def test_public_cannot_be_stacked_with_a_guard() -> None:
    with pytest.raises(ValueError, match=r"public.*authentication"):

        @public()
        @authenticated()
        async def contradictory(request: Any) -> str:
            return "never"


def test_a_typed_vocabulary_catches_an_unknown_route_action_at_compile() -> None:
    from enum import StrEnum

    class Actions(StrEnum):
        READ = "Document::read"

    app = Wreath()
    app.configure_auth(BearerTokenBackend({}), vocabulary=AuthorizationVocabulary(Actions))

    @app.get("/documents")
    @authorize(action="Document::delete", resource="Document::all")
    async def documents(request: Any) -> list[Any]:
        return []

    with pytest.raises(RuntimeError, match="Document::delete"):
        app._compile_routes()


def test_a_strenum_action_is_the_route_s_wire_vocabulary() -> None:
    from enum import StrEnum

    from wreath._auth.requirements import requirement_for

    class Actions(StrEnum):
        READ = "Document::read"

    @authorize(action=Actions.READ, resource="Document::all")
    async def documents(request: Any) -> list[Any]:
        return []

    assert requirement_for(documents).policies[0].action == "Document::read"


def test_strict_access_declarations_cover_websockets() -> None:
    app = Wreath(require_access_declarations=True)

    @app.websocket("/events")
    async def events(websocket: Any) -> None:
        await websocket.accept()

    with pytest.raises(RuntimeError, match="WEBSOCKET /events"):
        app._compile_routes()


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
async def test_bearer_scheme_is_case_insensitive_and_other_schemes_are_refused() -> None:
    seen: list[str] = []

    async def verify(token: str) -> Identity | None:
        seen.append(token)
        return Identity("user-1") if token == "valid" else None

    app = Wreath()
    app.configure_auth(BearerTokenBackend(verify))

    @app.get("/private")
    @authenticated()
    async def private(request):
        return request.identity.id

    lowercase = await invoke(app, "/private", authorization=b"bearer valid")
    foreign = await invoke(app, "/private", authorization=b"Basic valid")
    empty = await invoke(app, "/private", authorization=b"Bearer ")

    assert lowercase[0]["status"] == 200
    assert foreign[0]["status"] == 401
    assert empty[0]["status"] == 401
    assert seen == ["valid"]


@pytest.mark.asyncio
async def test_bearer_backend_uses_the_native_token_seam_without_reading_headers() -> None:
    class NativeContext:
        def _bearer_token(self) -> str:
            return "native-token"

        @property
        def headers(self) -> list[tuple[bytes, bytes]]:
            raise AssertionError("native bearer extraction materialized all headers")

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(NativeContext(), receive)
    backend = BearerTokenBackend(
        lambda token: Identity("native") if token == "native-token" else None
    )

    assert await backend.authenticate(request) == Identity("native")


@pytest.mark.asyncio
async def test_bearer_backend_subclass_keeps_its_authenticate_override() -> None:
    calls = 0

    class CustomBearerBackend(BearerTokenBackend):
        async def authenticate(self, request: Request) -> Identity | None:
            nonlocal calls
            calls += 1
            return Identity("custom")

    app = Wreath()
    app.configure_auth(CustomBearerBackend(lambda token: Identity(token)))

    @app.get("/private")
    @authenticated()
    async def private(request: Request) -> str:
        return request.identity.id

    sent = await invoke(app, "/private")

    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"custom"
    assert calls == 1


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


# `SessionIdentityBackend` reads `request.state.session`, which `SessionPolicy`
# publishes. Route middleware runs *after* authorization, so registering the two
# in the obvious way authenticated every caller as anonymous and answered 401 to
# a valid session cookie -- silently, and identically to a genuine anonymous
# request. These pin the refusal that replaced it.


def _session_app(*, global_scope: bool) -> Wreath:
    from wreath.auth import SessionIdentityBackend
    from wreath.policy import SessionPolicy

    app = Wreath()
    app.configure_auth(SessionIdentityBackend())
    policy = SessionPolicy(secret="x" * 32, secure=False)
    if global_scope:
        app.configure_http_policy(HttpPolicy(session=policy))
    else:
        app.add_middleware(policy)

    @app.get("/me")
    @authenticated()
    async def me(request: Any) -> dict[str, Any]:
        return {"id": request.identity.id}

    return app


def test_a_session_backend_refuses_route_scoped_session_middleware() -> None:
    with pytest.raises(TypeError) as caught:
        _session_app(global_scope=False)
    message = str(caught.value)
    assert "configure_http_policy" in message
    assert "HttpPolicy" in message
    assert "SessionPolicy" in message


def test_the_correct_registration_is_not_refused() -> None:
    app = _session_app(global_scope=True)
    app._compile_routes()


def test_a_composite_backend_propagates_the_session_requirement() -> None:
    from wreath.auth import CompositeBackend, SessionIdentityBackend

    bearer = BearerTokenBackend({"t": Identity(id="bo", type="User")})
    assert not getattr(bearer, "requires_session", False)
    assert CompositeBackend(bearer, SessionIdentityBackend()).requires_session
    assert not CompositeBackend(bearer).requires_session


class _SessionRequest:
    """A request carrying whatever `request.state.session` a case needs.

    `session` is a sentinel-free positional: passing nothing leaves the
    attribute *absent*, which is the shape a route sees when the session
    middleware is not installed and is not the same as an empty session.
    """

    def __init__(self, *session: Any) -> None:
        state = type("state", (), {})()
        if session:
            state.session = session[0]
        self.state = state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session", "why"),
    [
        ((), "no session published at all"),
        ((None,), "a session attribute that is None"),
        (("principal",), "a session that is not a mapping"),
        (({},), "a session with no principal"),
        (({"principal": "ada"},), "a principal that is not a mapping"),
        (({"principal": {}},), "a principal with no subject"),
        (({"principal": {"sub": ""}},), "an empty subject"),
        (({"principal": {"sub": 12345}},), "a subject that is not a string"),
        (({"principal": {"sub": True}},), "a subject that is a bool"),
    ],
)
async def test_a_session_that_names_nobody_is_anonymous(session, why) -> None:
    from wreath.auth import SessionIdentityBackend

    assert await SessionIdentityBackend().authenticate(_SessionRequest(*session)) is None, why


@pytest.mark.asyncio
async def test_a_boolean_expiry_is_not_an_expiry() -> None:
    from wreath.auth import SessionIdentityBackend

    backend = SessionIdentityBackend()
    for flag in (True, False):
        identity = await backend.authenticate(
            _SessionRequest({"principal": {"sub": "ada", "exp": flag}})
        )
        assert identity is not None and identity.id == "ada"
    # A real expiry in the past is still honoured, so the clause above is not
    # simply turning the whole check off.
    import time

    expired = await backend.authenticate(
        _SessionRequest({"principal": {"sub": "ada", "exp": time.time() - 1}})
    )
    assert expired is None


class _Answering:
    """A backend with a fixed answer that records having been asked.

    The composite's whole contract is *which* member gets asked and in what
    order, and neither shipped backend can express "asked and declined"
    distinguishably from "never asked" -- so the stand-in counts.
    """

    requires_session = False

    def __init__(
        self,
        identity: Identity | None = None,
        *,
        offers: str | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._identity = identity
        self._offers = offers
        self._raises = raises
        self.asked = 0

    async def authenticate(self, request: Any) -> Identity | None:
        self.asked += 1
        if self._raises is not None:
            raise self._raises
        return self._identity

    def challenge(self, request: Any) -> str | None:
        return self._offers


@pytest.mark.asyncio
async def test_a_composite_backend_stops_at_the_first_identity() -> None:
    from wreath.auth import CompositeBackend

    first = _Answering(Identity(id="from-bearer", type="User"))
    second = _Answering(Identity(id="from-session", type="User"))

    identity = await CompositeBackend(first, second).authenticate(object())

    assert identity is not None
    assert identity.id == "from-bearer"
    assert (first.asked, second.asked) == (1, 0)


@pytest.mark.asyncio
async def test_a_composite_backend_walks_past_a_backend_that_declines() -> None:
    from wreath.auth import CompositeBackend

    first = _Answering(None)
    second = _Answering(Identity(id="from-session", type="User"))

    identity = await CompositeBackend(first, second).authenticate(object())

    assert identity is not None
    assert identity.id == "from-session"
    assert (first.asked, second.asked) == (1, 1)


@pytest.mark.asyncio
async def test_a_composite_backend_is_anonymous_when_every_member_declines() -> None:
    from wreath.auth import CompositeBackend

    first, second = _Answering(None), _Answering(None)

    assert await CompositeBackend(first, second).authenticate(object()) is None
    assert (first.asked, second.asked) == (1, 1)


@pytest.mark.asyncio
async def test_a_composite_backend_lets_a_failing_backend_stop_the_walk() -> None:
    from wreath.auth import CompositeBackend

    first = _Answering(raises=RuntimeError("token store unreachable"))
    second = _Answering(Identity(id="from-session", type="User"))

    with pytest.raises(RuntimeError, match="token store unreachable"):
        await CompositeBackend(first, second).authenticate(object())

    assert second.asked == 0


def test_a_composite_backend_advertises_the_first_challenge_offered() -> None:
    from wreath.auth import CompositeBackend

    silent, bearer = _Answering(), _Answering(offers='Bearer realm="api"')

    assert CompositeBackend(silent, bearer).challenge(object()) == 'Bearer realm="api"'
    assert CompositeBackend(bearer, silent).challenge(object()) == 'Bearer realm="api"'
    assert CompositeBackend(silent, silent).challenge(object()) is None


def test_an_empty_composite_backend_is_refused() -> None:
    from wreath.auth import CompositeBackend

    with pytest.raises(ValueError, match="at least one backend"):
        CompositeBackend()


def test_an_unnamed_authorization_action_is_refused() -> None:
    from wreath.authorization import authorize

    with pytest.raises(ValueError, match="action is required"):
        authorize(action="", resource="User")


def test_a_bearer_only_app_may_still_use_route_scoped_sessions() -> None:
    from wreath.policy import SessionPolicy

    app = Wreath()
    app.configure_auth(BearerTokenBackend({"t": Identity(id="bo", type="User")}))
    app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="x" * 32, secure=False)))

    @app.get("/me")
    @authenticated()
    async def me(request: Any) -> dict[str, Any]:
        return {"id": request.identity.id}

    app._compile_routes()


def _step_up_app() -> tuple[Wreath, Any]:
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
    app, _ = _step_up_app()

    refused = await invoke(app, "/wipe", authorization=b"Bearer t")
    assert refused[0]["status"] == 403
    assert b"second_factor_required" in refused[1]["body"]

    anonymous = await invoke(app, "/wipe")
    assert anonymous[0]["status"] == 401


@pytest.mark.asyncio
async def test_the_same_requirement_is_refused_by_mcp() -> None:
    from wreath.mcp import PROTOCOL_VERSION
    from wreath.testing import TestClient

    app, _ = _step_up_app()
    headers = {"authorization": "Bearer t"}
    async with TestClient(app) as client:
        opened = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION},
            },
            headers=headers,
        )
        session = dict(opened.headers)[b"mcp-session-id"].decode()
        answer = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "wipe", "arguments": {}},
            },
            headers={**headers, "mcp-session-id": session},
        )
    body = answer.json()
    assert "error" in body, body
    assert "second factor" in body["error"]["message"], body


@pytest.mark.asyncio
async def test_the_two_enforcers_ask_the_same_question_of_a_requirement() -> None:
    from wreath._auth.requirements import PolicyRequirement, SetRequirement
    from wreath.authorization import AuthRequirement

    admin = SetRequirement(frozenset({"admin"}), "all")
    assert AuthRequirement().access_level == 0
    identify = AuthRequirement(identify=True)
    assert identify.access_level == 0  # loads the caller without requiring one
    assert identify.needs_backend
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
