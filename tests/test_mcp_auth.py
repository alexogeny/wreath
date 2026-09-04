from __future__ import annotations

import base64
import hmac
import json
import time

import pytest

from wreath import Wreath
from wreath.auth import BearerTokenBackend, Identity, JwtVerifier, SymmetricKey
from wreath.authorization import CedarAuthorizer
from wreath.mcp import MCP, PROTOCOL_VERSION, MCPAuth, ToolRateLimit
from wreath.testing import TestClient, TestResponse

SECRET = b"a-shared-secret-of-entirely-reasonable-length"
ISSUER = "https://idp.example"
RESOURCE = "https://api.example.com/mcp"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
METADATA_URL = "https://api.example.com" + METADATA_PATH


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def token(*, audience: str | list[str] = RESOURCE, subject: str = "ada") -> str:
    """One HS256 token, minted with the stdlib so no oracle is needed here."""
    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = _b64u(
        json.dumps(
            {
                "sub": subject,
                "iss": ISSUER,
                "aud": audience,
                "exp": int(time.time()) + 3600,
            }
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    signature = hmac.new(SECRET, signing_input, "sha256").digest()
    return f"{header}.{claims}.{_b64u(signature)}"


def verifier(**overrides) -> JwtVerifier:
    kwargs: dict = {
        "algorithms": ("HS256",),
        "key": SymmetricKey(SECRET),
        "issuer": ISSUER,
        # Deliberately *not* audience-bound here. `MCPAuth` must bind the
        # audience itself: a deployment that forgot this argument has to fail
        # closed, because the alternative is an endpoint that looks configured
        # and is not.
        "audience": None,
        "leeway": 0,
    }
    kwargs.update(overrides)
    return JwtVerifier(**kwargs)


def protection(**overrides) -> MCPAuth:
    kwargs: dict = {
        "resource": RESOURCE,
        "authorization_servers": (ISSUER,),
        "verifier": verifier(),
        "scopes_supported": ("mcp:tools",),
    }
    kwargs.update(overrides)
    return MCPAuth(**kwargs)


def header(response: TestResponse, name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in response.headers:
        if key == wanted:
            return value.decode("latin-1")
    return None


def build(**overrides) -> tuple[Wreath, MCP]:
    app = Wreath()
    mcp = MCP(app, name="camera-trap", version="1.0.0", auth=protection(), **overrides)

    @mcp.tool(description="Says hello to whoever is allowed to ask.")
    async def greet(request) -> dict:
        return {"caller": request.identity.id}

    return app, mcp


async def initialize(client: TestClient, bearer: str | None) -> TestResponse:
    headers = {} if bearer is None else {"authorization": f"Bearer {bearer}"}
    return await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION},
        },
        headers=headers,
    )


async def session_for(client: TestClient, bearer: str) -> str:
    opened = await initialize(client, bearer)
    assert opened.status == 200
    return header(opened, "mcp-session-id") or ""


async def call(client: TestClient, session: str, bearer: str, params: dict) -> TestResponse:
    return await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params},
        headers={"mcp-session-id": session, "authorization": f"Bearer {bearer}"},
    )


async def test_protected_resource_metadata_is_served_without_a_token() -> None:
    app, mcp = build()
    assert mcp.metadata_path == METADATA_PATH
    assert mcp.metadata_url == METADATA_URL
    async with TestClient(app) as client:
        response = await client.get(METADATA_PATH)
        assert response.status == 200
        document = response.json()
        assert document["resource"] == RESOURCE
        assert document["authorization_servers"] == [ISSUER]
        assert document["scopes_supported"] == ["mcp:tools"]
        # A token in a query string lands in access logs and referrer headers.
        assert document["bearer_methods_supported"] == ["header"]


async def test_metadata_omits_the_scope_list_when_there_is_none() -> None:
    app = Wreath()
    MCP(app, name="camera-trap", version="1.0.0", auth=protection(scopes_supported=()))
    async with TestClient(app) as client:
        document = (await client.get(METADATA_PATH)).json()
        assert "scopes_supported" not in document
        # The unconditional keys are still there, so this is not an empty document.
        assert document["resource"] == RESOURCE
        assert document["bearer_methods_supported"] == ["header"]


async def test_an_audience_claim_that_is_a_list_is_read_as_a_set() -> None:
    app, _ = build()
    bearer = token(audience=[RESOURCE, "https://other.example/api"])
    async with TestClient(app) as client:
        accepted = await call(client, await session_for(client, bearer), bearer, {"name": "greet"})
        assert "error" not in accepted.json(), accepted.json()


async def test_an_audience_claim_of_the_wrong_shape_matches_nothing() -> None:
    from wreath._mcp.auth import _audience_of

    assert _audience_of({"aud": [RESOURCE, 7, None]}) == frozenset((RESOURCE,))
    assert _audience_of({"aud": (RESOURCE,)}) == frozenset((RESOURCE,))
    assert _audience_of({"aud": RESOURCE}) == frozenset((RESOURCE,))
    assert _audience_of({"aud": {RESOURCE: 1}}) == frozenset()
    assert _audience_of({"aud": 7}) == frozenset()
    assert _audience_of({}) == frozenset()
    assert _audience_of(object()) == frozenset()


async def test_the_metadata_path_follows_the_endpoint_path() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0", path="/tools/mcp", auth=protection())
    assert mcp.metadata_path == "/.well-known/oauth-protected-resource/tools/mcp"
    async with TestClient(app) as client:
        assert (await client.get(mcp.metadata_path)).status == 200


async def test_an_unprotected_server_publishes_no_metadata() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0")
    assert mcp.metadata_url == ""
    async with TestClient(app) as client:
        assert (await client.get(mcp.metadata_path)).status == 404


def test_metadata_needs_somewhere_to_send_a_client() -> None:
    with pytest.raises(ValueError, match="authorization_servers"):
        MCPAuth(resource=RESOURCE, authorization_servers=())
    with pytest.raises(ValueError, match="resource"):
        MCPAuth(resource="", authorization_servers=(ISSUER,))


@pytest.mark.parametrize(
    "resource",
    (
        "api.example.com/mcp",
        "http://api.example.com/mcp",
        "https:///mcp",
        "https://operator@api.example.com/mcp",
        "https://api.example.com:0/mcp",
        "https://api.example.com:invalid/mcp",
        "https://api.example.com/mcp#fragment",
        "https://api.exa\tmple.com/mcp",
        "https://api.example.com/mcp\x80suffix",
    ),
)
def test_resource_identifier_is_a_canonical_https_url(resource: str) -> None:
    with pytest.raises(ValueError, match="resource.*absolute HTTPS URL"):
        MCPAuth(resource=resource, authorization_servers=(ISSUER,))


@pytest.mark.parametrize(
    "issuer",
    (
        "idp.example",
        "http://idp.example",
        "https:///issuer",
        "https://operator@idp.example",
        "https://idp.example:0",
        "https://idp.example:invalid",
        "https://idp.example/issuer#fragment",
        "https://idp.exa\tmple",
        "https://idp.example/issuer\x80suffix",
    ),
)
def test_authorization_server_identifier_is_a_canonical_https_url(issuer: str) -> None:
    with pytest.raises(ValueError, match="authorization server.*absolute HTTPS URL"):
        MCPAuth(resource=RESOURCE, authorization_servers=(issuer,))


def test_mcp_auth_url_fields_refuse_non_text_and_non_idna_hosts() -> None:
    with pytest.raises(ValueError, match="resource.*absolute HTTPS URL"):
        protection(resource=7)
    with pytest.raises(ValueError, match="authorization server.*absolute HTTPS URL"):
        protection(authorization_servers=(7,))
    with pytest.raises(ValueError, match="resource.*absolute HTTPS URL"):
        MCPAuth(resource="https://" + "\ud800" + "/mcp", authorization_servers=(ISSUER,))


async def test_a_missing_token_gets_a_challenge_naming_the_metadata_url() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        response = await initialize(client, None)
        assert response.status == 401
        challenge = header(response, "www-authenticate") or ""
        assert challenge.startswith("Bearer")
        assert f'resource_metadata="{METADATA_URL}"' in challenge
        # RFC 6750 §3.1: nothing was presented, so there is no token for an
        # error code to describe.
        assert "error=" not in challenge


async def test_an_unverifiable_token_is_named_as_such() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        response = await initialize(client, "not.a.token")
        assert response.status == 401
        challenge = header(response, "www-authenticate") or ""
        assert 'error="invalid_token"' in challenge
        assert f'resource_metadata="{METADATA_URL}"' in challenge


async def test_comma_combined_authorization_is_refused_before_verification() -> None:
    verified: list[str] = []

    def verify(value: str) -> Identity:
        verified.append(value)
        return Identity("ada", claims={"aud": RESOURCE})

    app = Wreath()
    MCP(
        app,
        name="camera-trap",
        version="1.0.0",
        auth=protection(verifier=verify),
    )
    async with TestClient(app) as client:
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION},
            },
            headers={"authorization": "Bearer valid, Bearer attacker"},
        )

    assert response.status == 401
    assert verified == []


async def test_the_challenge_is_on_every_method() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        for response in (
            await client.get("/mcp"),
            await client.delete("/mcp"),
        ):
            assert response.status == 401
            assert f'resource_metadata="{METADATA_URL}"' in (
                header(response, "www-authenticate") or ""
            )


async def test_a_token_minted_for_another_resource_is_rejected() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        response = await initialize(client, token(audience="https://other.example/mcp"))
        assert response.status == 401
        assert "audience" in response.json()["error"]["message"]
        assert 'error="invalid_token"' in (header(response, "www-authenticate") or "")


async def test_a_token_with_no_audience_at_all_is_rejected() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        response = await initialize(client, token(audience=[]))
        assert response.status == 401
        assert "audience" in response.json()["error"]["message"]


async def test_a_token_naming_this_resource_among_several_is_accepted() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        response = await initialize(client, token(audience=["https://other.example/mcp", RESOURCE]))
        assert response.status == 200


async def test_a_bound_token_reaches_the_tool_as_an_identity() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        bearer = token()
        session = await session_for(client, bearer)
        response = await call(client, session, bearer, {"name": "greet"})
        assert response.json()["result"]["structuredContent"] == {"caller": "ada"}


async def test_the_audience_may_differ_from_the_resource_identifier() -> None:
    app = Wreath()
    mcp = MCP(
        app,
        name="x",
        version="1.0.0",
        auth=protection(audience="api://camera-trap"),
    )

    @mcp.tool(description="Anything.")
    async def noop(request) -> dict:
        return {}

    async with TestClient(app) as client:
        assert (await initialize(client, token(audience="api://camera-trap"))).status == 200
        assert (await initialize(client, token(audience=RESOURCE))).status == 401


async def test_a_protected_server_with_no_verifier_admits_nobody() -> None:
    app = Wreath()
    MCP(
        app,
        name="x",
        version="1.0.0",
        auth=MCPAuth(resource=RESOURCE, authorization_servers=(ISSUER,)),
    )
    async with TestClient(app) as client:
        response = await initialize(client, token())
        assert response.status == 401
        assert "verifier" in response.json()["error"]["message"]


async def test_a_session_belongs_to_the_subject_that_opened_it() -> None:
    app, _ = build()
    async with TestClient(app) as client:
        session = await session_for(client, token(subject="ada"))
        stolen = await call(client, session, token(subject="grace"), {"name": "greet"})
        assert stolen.status == 401
        assert "did not open this MCP session" in stolen.json()["error"]["message"]


class Engine:
    """Permits `Sighting::find` and refuses everything else."""

    def __init__(self) -> None:
        self.calls = 0

    def is_authorized(self, **request: object) -> bool:
        self.calls += 1
        return request["action"] == "Sighting::find"


#: Distinguishes "no authorizer at all" from "the default one", without a
#: mutable default argument every test in this file would then share.
_DEFAULT = object()


def gated(engine: object = _DEFAULT) -> tuple[Wreath, MCP, list[str]]:
    if engine is _DEFAULT:
        engine = Engine()
    app = Wreath()
    ran: list[str] = []
    authorizer = (
        None
        if engine is None
        else CedarAuthorizer(
            engine=engine,
            principal=lambda identity: f"User::{identity.id}",
            action=lambda action, request: action,
            resource=lambda resource, request: resource,
            entities=lambda request: (),
            context=lambda request: {},
        )
    )
    app.configure_auth(BearerTokenBackend(lambda _t: Identity("unused")), authorizer)
    mcp = MCP(app, name="camera-trap", version="1.0.0", auth=protection())

    @mcp.tool(description="Find sightings.", action="Sighting::find", resource="all")
    async def find_sightings(request) -> dict:
        ran.append("find_sightings")
        return {"ok": True}

    @mcp.tool(description="Delete a camera.", action="Camera::delete", resource="all")
    async def delete_camera(request) -> dict:
        ran.append("delete_camera")
        return {"ok": True}

    @mcp.tool(description="Ungated, and says so.")
    async def ping_tool(request) -> dict:
        ran.append("ping_tool")
        return {"ok": True}

    return app, mcp, ran


async def test_a_permitted_tool_runs() -> None:
    app, mcp, ran = gated()
    async with TestClient(app) as client:
        bearer = token()
        session = await session_for(client, bearer)
        response = await call(client, session, bearer, {"name": "find_sightings"})
        assert response.json()["result"]["isError"] is False
        assert ran == ["find_sightings"]
        assert mcp.unauthorized_calls == 0


async def test_a_denied_tool_never_runs_and_counts_as_a_refusal() -> None:
    app, mcp, ran = gated()
    async with TestClient(app) as client:
        bearer = token()
        session = await session_for(client, bearer)
        response = await call(client, session, bearer, {"name": "delete_camera"})
        error = response.json()["error"]
        assert error["code"] == -32001
        assert "Camera::delete" in error["message"]
        assert ran == []
        # A refused call is not a failed one. Conflating them would hide which
        # half of a deployment is broken -- the same reason `MessageBus` keeps
        # `doorbell_reconnects` apart from `handler_errors`.
        assert mcp.unauthorized_calls == 1
        assert mcp.tool_errors == 0
        assert mcp.tool_calls == 0
        assert mcp.schema_rejections == 0


async def test_an_ungated_tool_never_reaches_the_authorizer() -> None:
    engine = Engine()
    app, _, ran = gated(engine)
    async with TestClient(app) as client:
        bearer = token()
        session = await session_for(client, bearer)
        await call(client, session, bearer, {"name": "ping_tool"})
        assert ran == ["ping_tool"]
        assert engine.calls == 0


async def test_a_gated_tool_with_no_authorizer_fails_closed() -> None:
    app, mcp, ran = gated(None)
    async with TestClient(app) as client:
        bearer = token()
        session = await session_for(client, bearer)
        response = await call(client, session, bearer, {"name": "find_sightings"})
        message = response.json()["error"]["message"]
        assert "configure_auth" in message
        assert ran == []
        assert mcp.unauthorized_calls == 1


async def test_a_resource_resolver_sees_the_call_arguments() -> None:
    seen: list[object] = []

    class Recording:
        def is_authorized(self, **request: object) -> bool:
            seen.append(request["resource"])
            return True

    app = Wreath()
    app.configure_auth(
        BearerTokenBackend(lambda _t: Identity("unused")),
        CedarAuthorizer(
            engine=Recording(),
            principal=lambda identity: identity.id,
            action=lambda action, request: action,
            resource=lambda resource, request: resource,
            entities=lambda request: (),
            context=lambda request: {},
        ),
    )
    mcp = MCP(app, name="x", version="1.0.0", auth=protection())

    @mcp.tool(
        description="Retires one camera.",
        action="Camera::retire",
        resource=lambda request: request.state.mcp.arguments.get("camera_id"),
    )
    async def retire_camera(request, camera_id: str) -> dict:
        return {"retired": camera_id}

    async with TestClient(app) as client:
        bearer = token()
        session = await session_for(client, bearer)
        response = await call(
            client,
            session,
            bearer,
            {"name": "retire_camera", "arguments": {"camera_id": "ridge-2"}},
        )
        assert response.json()["result"]["structuredContent"] == {"retired": "ridge-2"}
        assert seen == ["ridge-2"]


async def test_a_malformed_call_is_rejected_before_any_policy_is_consulted() -> None:
    engine = Engine()
    app, mcp, _ = gated(engine)

    @mcp.tool(description="Needs an argument.", action="Sighting::find", resource="all")
    async def find_one(request, species: str) -> dict:
        return {}

    async with TestClient(app) as client:
        bearer = token()
        session = await session_for(client, bearer)
        response = await call(client, session, bearer, {"name": "find_one"})
        assert response.json()["error"]["code"] == -32602
        assert mcp.schema_rejections == 1
        assert engine.calls == 0


async def test_declared_actions_names_every_model_callable_action() -> None:
    _, mcp, _ = gated()
    assert mcp.declared_actions() == {
        "Camera": ("Camera::delete",),
        "Sighting": ("Sighting::find",),
    }


def test_a_resource_without_an_action_gates_nothing_and_says_so() -> None:
    mcp = MCP(name="x", version="1.0.0")
    with pytest.raises(ValueError, match="resource"):

        @mcp.tool(description="Half a policy.", resource="all")
        async def half(request) -> dict:
            return {}


async def test_a_tool_is_bounded_per_caller() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0", auth=protection())
    ran = 0

    @mcp.tool(description="Cheap, but not free.", rate_limit=ToolRateLimit(2, 60.0))
    async def scan(request) -> dict:
        nonlocal ran
        ran += 1
        return {}

    @mcp.tool(description="Its own bucket.")
    async def other(request) -> dict:
        return {}

    async with TestClient(app) as client:
        bearer = token(subject="ada")
        session = await session_for(client, bearer)
        for _ in range(2):
            assert (await call(client, session, bearer, {"name": "scan"})).json()["result"]
        refused = await call(client, session, bearer, {"name": "scan"})
        error = refused.json()["error"]
        assert error["code"] == -32003
        assert error["data"]["retryAfter"] > 0
        assert ran == 2
        assert mcp.throttled == 1
        # A throttled call is neither a failure nor a refusal on the merits.
        assert mcp.tool_errors == 0
        assert mcp.unauthorized_calls == 0
        # An unbounded tool keeps working while a bounded one is exhausted.
        assert (await call(client, session, bearer, {"name": "other"})).json()["result"]


async def test_one_caller_cannot_spend_another_callers_allowance() -> None:
    app = Wreath()
    mcp = MCP(app, name="x", version="1.0.0", auth=protection())

    @mcp.tool(description="Bounded.", rate_limit=ToolRateLimit(1, 60.0))
    async def scan(request) -> dict:
        return {}

    async with TestClient(app) as client:
        ada, grace = token(subject="ada"), token(subject="grace")
        ada_session = await session_for(client, ada)
        grace_session = await session_for(client, grace)
        assert (await call(client, ada_session, ada, {"name": "scan"})).json()["result"]
        assert "error" in (await call(client, ada_session, ada, {"name": "scan"})).json()
        assert (await call(client, grace_session, grace, {"name": "scan"})).json()["result"]


def test_a_rate_limit_that_is_not_a_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="limit"):
        ToolRateLimit(0)
    with pytest.raises(ValueError, match="window"):
        ToolRateLimit(1, 0.0)


@pytest.mark.parametrize("window", [float("nan"), float("inf")])
def test_a_tool_rate_limit_window_must_be_finite(window: float) -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        ToolRateLimit(1, window)


def test_a_burst_below_one_is_refused_and_a_burst_below_the_limit_is_not() -> None:
    with pytest.raises(ValueError, match="burst must be at least 1"):
        ToolRateLimit(5, 60.0, 0)
    with pytest.raises(ValueError, match="burst must be at least 1"):
        ToolRateLimit(5, 60.0, -1)
    assert ToolRateLimit(5, 60.0, 1).capacity == 1.0
    assert ToolRateLimit(5, 60.0).capacity == 5.0
