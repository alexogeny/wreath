from __future__ import annotations

import hashlib
import hmac
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

import wreath.oauth as oauth
from wreath import JSONResponse, Wreath
from wreath._auth.oauth2 import bearer_challenge, register_oauth2_login
from wreath._auth.oidc import OidcProvider
from wreath.oauth import AuthorizationServer, ClientRegistration, Es256Signer, OAuthRefusal
from wreath.policy import HttpPolicy, SessionPolicy
from wreath.testing import TestClient

CLIENT_SECRET = b"confidential-client-secret-material"
CLIENT = ClientRegistration(
    client_id="confidential",
    redirect_uris=("https://client.example/callback",),
    scopes=("read", "write"),
    confidential=True,
    client_secret=CLIENT_SECRET,
)
PUBLIC = ClientRegistration(
    client_id="public",
    redirect_uris=("https://client.example/public",),
)


@dataclass(frozen=True, slots=True)
class PaymentDetail:
    amount: int


def _server(**options: Any) -> AuthorizationServer:
    arguments: dict[str, Any] = {
        "issuer": "https://issuer.example",
        "secret": b"s" * 32,
        "clients": (CLIENT, PUBLIC),
    }
    arguments.update(options)
    return AuthorizationServer(**arguments)


def _claims(token: str) -> dict[str, object]:
    payload = token.split(".")[1]
    return json.loads(urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _segment(value: object) -> str:
    encoded = urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
    return encoded.rstrip(b"=").decode()


def _signed_token(
    server: AuthorizationServer,
    claims: dict[str, object],
    *,
    algorithm: str = "HS256",
    typ: str = "JWT",
) -> str:
    signing_input = f"{_segment({'alg': algorithm, 'typ': typ})}.{_segment(claims)}"
    signature = hmac.new(server.secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def _challenge(verifier: str) -> str:
    import hashlib

    return (
        urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def test_a_bare_bearer_challenge_has_no_trailing_separator() -> None:
    assert bearer_challenge() == b"Bearer"


def _login_app(provider: OidcProvider, *, seed: bool = False) -> Wreath:
    app = Wreath()
    if seed:
        app.configure_http_policy(HttpPolicy(session=SessionPolicy(secret="s" * 32, secure=False)))

        @app.get("/seed")
        async def seed_session(request):
            request.state.session.update(
                {
                    "_oidc_state_idp": "issued",
                    "_oidc_verifier_idp": "verifier",
                }
            )
            return JSONResponse({})

    register_oauth2_login(
        app,
        "idp",
        provider=provider,
        client_id="client",
        client_secret="secret",
        redirect_uri="https://app.example/auth/callback",
    )
    return app


def _oidc_provider(http_client: object) -> OidcProvider:
    return OidcProvider(
        "idp",
        issuer="https://idp.example",
        audience="client",
        http_client=http_client,
    )


def _callback_with_recording_client() -> tuple[Any, Any]:
    class Response:
        status = 500
        body = b""

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, *args: Any, **kwargs: Any) -> Response:
            self.calls += 1
            return Response()

    class App:
        def __init__(self) -> None:
            self.routes: dict[str, Any] = {}

        def get(self, path: str) -> Any:
            def register(endpoint: Any) -> Any:
                self.routes[path] = endpoint
                return endpoint

            return register

    client = Client()
    provider = _oidc_provider(client)
    provider.authorization_endpoint = "https://idp.example/authorize"
    provider.token_endpoint = "https://idp.example/token"
    app = App()
    register_oauth2_login(
        app,
        "idp",
        provider=provider,
        client_id="client",
        client_secret="secret",
        redirect_uri="https://app.example/auth/callback",
    )
    return app.routes["/auth/callback"], client


@pytest.mark.parametrize(
    "query",
    [
        b"code=first&code=second&state=issued",
        b"code=code&state=issued&state=other",
    ],
)
async def test_callback_refuses_duplicate_security_parameters(query: bytes) -> None:
    callback, client = _callback_with_recording_client()
    session = {
        "_oidc_state_idp": "issued",
        "_oidc_verifier_idp": "verifier",
        "_oidc_nonce_idp": "nonce",
    }

    response = await callback(
        SimpleNamespace(query_string=query, state=SimpleNamespace(session=session))
    )

    assert response.status == 400
    assert response.body == b'{"error":"invalid_state"}'
    assert client.calls == 0


@pytest.mark.parametrize("query", [b"state=issued", b"code=code"])
async def test_incomplete_callback_does_not_consume_the_pending_flow(query: bytes) -> None:
    callback, client = _callback_with_recording_client()
    session = {
        "_oidc_state_idp": "issued",
        "_oidc_verifier_idp": "verifier",
        "_oidc_nonce_idp": "nonce",
    }
    original = dict(session)

    response = await callback(
        SimpleNamespace(query_string=query, state=SimpleNamespace(session=session))
    )

    assert response.status == 400
    assert session == original
    assert client.calls == 0


async def test_login_refuses_a_provider_without_an_authorization_endpoint() -> None:
    app = _login_app(_oidc_provider(object()))

    async with TestClient(app) as client:
        response = await client.get("/auth/login")

    assert response.status == 503
    assert response.json() == {"error": "provider_not_discovered"}


async def test_callback_refuses_a_provider_without_a_token_endpoint() -> None:
    provider = _oidc_provider(object())
    provider.authorization_endpoint = "https://idp.example/authorize"
    app = _login_app(provider, seed=True)

    async with TestClient(app) as client:
        seeded = await client.get("/seed")
        cookie = seeded.header("set-cookie")
        assert cookie is not None
        response = await client.get(
            "/auth/callback?code=code&state=issued",
            headers={"cookie": cookie.split(";", 1)[0]},
        )

    assert response.status == 503
    assert response.json() == {"error": "provider_not_discovered"}


async def test_successful_token_exchange_reaches_the_id_token_validation() -> None:
    class Response:
        status = 200
        body = b"{}"

    class Client:
        async def post(self, *args, **kwargs) -> Response:
            return Response()

    provider = _oidc_provider(Client())
    provider.authorization_endpoint = "https://idp.example/authorize"
    provider.token_endpoint = "https://idp.example/token"
    app = _login_app(provider, seed=True)

    async with TestClient(app) as client:
        seeded = await client.get("/seed")
        cookie = seeded.header("set-cookie")
        assert cookie is not None
        response = await client.get(
            "/auth/callback?code=code&state=issued",
            headers={"cookie": cookie.split(";", 1)[0]},
        )

    assert response.status == 401
    assert response.json() == {"error": "missing_id_token"}


@pytest.mark.parametrize(
    "options",
    [
        {"introspection_encrypted_response_enc": "A256GCM"},
        {"client_secret": b"x" * 32},
        {"confidential": True},
        {"confidential": True, "client_secret": b"short"},
    ],
)
def test_client_registration_refuses_each_invalid_secret_or_encryption_shape(
    options: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        ClientRegistration(client_id="invalid", **options)


@pytest.mark.parametrize("detail_name", [None, 1, b"payment", ""])
def test_authorization_detail_registry_requires_nonempty_text_names(
    detail_name: object,
) -> None:
    with pytest.raises(ValueError, match="type names must be non-empty strings"):
        _server(authorization_detail_types={detail_name: PaymentDetail})


@pytest.mark.parametrize("model", [None, 1, object(), PaymentDetail(1), dict])
def test_authorization_detail_registry_requires_dataclass_types(model: object) -> None:
    with pytest.raises(ValueError, match="must map to a dataclass type"):
        _server(authorization_detail_types={"payment": model})


def test_authorization_response_omits_absent_state_on_each_branch() -> None:
    server = _server()

    assert server.authorization_response(code="code") == {
        "iss": "https://issuer.example",
        "code": "code",
    }
    assert server.authorization_response(error="denied") == {
        "iss": "https://issuer.example",
        "error": "denied",
    }


def _detail_server() -> AuthorizationServer:
    return _server(authorization_detail_types={"payment": PaymentDetail})


def test_authorization_details_refuse_oversized_json_before_parsing() -> None:
    with pytest.raises(OAuthRefusal, match="exceeds the 64 KiB") as raised:
        _detail_server().validate_authorization_details(" " * (64 * 1024 + 1))
    assert raised.value.reason == "invalid-authorization-details"


@pytest.mark.parametrize("details", [None, 1, object(), {"type": "payment"}])
def test_authorization_details_require_a_list_or_tuple(details: object) -> None:
    with pytest.raises(OAuthRefusal, match="JSON array of objects") as raised:
        _detail_server().validate_authorization_details(details)
    assert raised.value.reason == "invalid-authorization-details"


def test_authorization_details_refuse_more_than_64_objects() -> None:
    details = [{"type": "payment", "amount": 1}] * 65

    with pytest.raises(OAuthRefusal, match="at most 64 objects"):
        _detail_server().validate_authorization_details(details)


@pytest.mark.parametrize("detail", [None, 1, "payment", ["payment"]])
def test_authorization_details_require_each_entry_to_be_an_object(detail: object) -> None:
    with pytest.raises(OAuthRefusal, match=r"authorization_details\[0\] must be an object"):
        _detail_server().validate_authorization_details([detail])


@pytest.mark.parametrize("detail_type", [None, 1, b"payment", ""])
def test_authorization_details_require_nonempty_text_type(detail_type: object) -> None:
    with pytest.raises(OAuthRefusal, match=r"\[0\]\.type must be a non-empty string"):
        _detail_server().validate_authorization_details([{"type": detail_type, "amount": 1}])


def test_authorization_details_name_unknown_type_exactly() -> None:
    with pytest.raises(OAuthRefusal, match="names unknown type 'unknown'"):
        _detail_server().validate_authorization_details([{"type": "unknown", "amount": 1}])


@pytest.mark.parametrize("details", [None, (), []])
def test_client_authorization_details_accept_each_empty_shape(details: object) -> None:
    assert _detail_server()._client_authorization_details(PUBLIC, details) == ()


def test_client_authorization_details_refuse_type_outside_registration() -> None:
    with pytest.raises(OAuthRefusal, match="not registered.*payment"):
        _detail_server()._client_authorization_details(
            PUBLIC,
            [{"type": "payment", "amount": 1}],
        )


def _introspection_server() -> AuthorizationServer:
    resource = ClientRegistration(
        client_id="resource",
        confidential=True,
        client_secret=b"r" * 32,
        scopes=("read",),
        introspection_signed_response_alg="HS256",
    )
    return _server(clients=(resource,))


def _active_claims(**changes: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": "https://issuer.example",
        "aud": "resource",
        "exp": 2000,
        "scope": "read write",
    }
    claims.update(changes)
    return claims


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other.example"},
        {"exp": True},
        {"exp": "2000"},
        {"exp": None},
        {"exp": 1000},
        {"aud": "other"},
    ],
)
def test_introspection_refuses_each_invalid_signed_claim(changes: dict[str, object]) -> None:
    server = _introspection_server()
    token = _signed_token(server, _active_claims(**changes))

    assert server._introspection_claims(
        token,
        audience="resource",
        scopes=("read",),
        now=1000,
    ) == {"active": False}


@pytest.mark.parametrize(
    ("algorithm", "typ"),
    [("HS512", "JWT"), ("HS256", "access+jwt")],
)
def test_introspection_refuses_each_invalid_protected_header(
    algorithm: str,
    typ: str,
) -> None:
    server = _introspection_server()
    token = _signed_token(server, _active_claims(), algorithm=algorithm, typ=typ)

    assert server._introspection_claims(
        token,
        audience="resource",
        scopes=("read",),
        now=1000,
    ) == {"active": False}


def test_introspection_refuses_revoked_signed_token() -> None:
    server = _introspection_server()
    token = _signed_token(server, _active_claims())
    server._revoked.add(token)

    assert server._introspection_claims(
        token,
        audience="resource",
        scopes=("read",),
        now=1000,
    ) == {"active": False}


def test_introspection_preserves_scope_when_resource_has_no_scope_filter() -> None:
    server = _introspection_server()
    token = _signed_token(server, _active_claims())

    claims = server._introspection_claims(token, audience="resource", scopes=(), now=1000)

    assert claims["active"] is True
    assert claims["scope"] == "read write"


def test_introspection_preserves_non_text_scope() -> None:
    server = _introspection_server()
    token = _signed_token(server, _active_claims(scope=["read"]))

    claims = server._introspection_claims(
        token,
        audience="resource",
        scopes=("read",),
        now=1000,
    )

    assert claims["active"] is True
    assert claims["scope"] == ["read"]


def test_introspection_refuses_boolean_expiry_even_before_epoch() -> None:
    server = _introspection_server()
    token = _signed_token(server, _active_claims(exp=False))

    assert server._introspection_claims(
        token,
        audience="resource",
        scopes=("read",),
        now=-1,
    ) == {"active": False}


def test_introspection_jwt_requires_registered_response_algorithm() -> None:
    client = ClientRegistration(
        client_id="ordinary-resource",
        confidential=True,
        client_secret=b"o" * 32,
    )
    server = _server(clients=(client,))

    with pytest.raises(OAuthRefusal, match="not registered for JWT token introspection"):
        server.introspection_jwt(
            "token",
            client_id="ordinary-resource",
            client_secret=b"o" * 32,
        )


def test_introspection_jwt_uses_current_time_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _introspection_server()
    monkeypatch.setattr(oauth.time, "time", lambda: 1234.75)

    response = server.introspection_jwt(
        "not-a-token",
        client_id="resource",
        client_secret=b"r" * 32,
    )

    assert _claims(response)["iat"] == 1234


def test_introspection_reports_missing_verifying_key() -> None:
    server = _introspection_server()
    token = _signed_token(server, _active_claims())
    server._verifying_key = None

    with pytest.raises(RuntimeError, match="without a verifying key"):
        server._introspection_claims(
            token,
            audience="resource",
            scopes=("read",),
            now=1000,
        )


def test_introspection_refuses_invalid_signature() -> None:
    server = _introspection_server()
    token = _signed_token(server, _active_claims())
    head, body, signature = token.split(".")
    replacement = ("A" if signature[0] != "A" else "B") + signature[1:]

    assert server._introspection_claims(
        f"{head}.{body}.{replacement}",
        audience="resource",
        scopes=("read",),
        now=1000,
    ) == {"active": False}


def test_pkce_challenge_must_be_ascii_even_at_the_exact_digest_length() -> None:
    with pytest.raises(OAuthRefusal) as raised:
        _server().issue_code(
            client_id="public",
            subject="user",
            challenge="é" * 43,
            redirect_uri="https://client.example/public",
        )
    assert raised.value.reason == "weak-pkce"


def test_es256_zero_scalar_has_no_public_point() -> None:
    with pytest.raises(ValueError, match=r"P-256 scalar is in \[1, n\)"):
        Es256Signer.from_bytes(bytes(32)).public_jwks()


@pytest.mark.parametrize(
    "issuer",
    [
        "https:///missing-host",
        "https://user@issuer.example",
        "https://:password@issuer.example",
    ],
)
def test_issuer_requires_a_host_and_refuses_each_credential_form(issuer: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTPS URL without credentials"):
        AuthorizationServer(issuer=issuer, secret=b"s" * 32)


def test_string_and_bytes_signing_secrets_are_preserved() -> None:
    text = "s" * 32
    assert _server(secret=text).secret == text.encode()
    assert _server(secret=text.encode()).secret == text.encode()


def test_missing_signing_secret_is_generated(monkeypatch: pytest.MonkeyPatch) -> None:
    generated = b"g" * 32
    monkeypatch.setattr(oauth.secrets, "token_bytes", lambda size: generated)
    server = AuthorizationServer(issuer="https://issuer.example")
    assert server.secret == generated


@pytest.mark.parametrize("refresh_ttl", [0, -1])
def test_refresh_lifetime_must_be_positive(refresh_ttl: float) -> None:
    with pytest.raises(ValueError, match="refresh_ttl must be positive"):
        _server(refresh_ttl=refresh_ttl)


def test_public_client_refuses_a_presented_secret() -> None:
    with pytest.raises(OAuthRefusal) as raised:
        _server()._authenticate_client("public", b"unexpected")
    assert raised.value.reason == "invalid-client"


def test_confidential_client_accepts_its_text_secret() -> None:
    text_secret = "x" * 32
    client = ClientRegistration(
        client_id="text-secret",
        confidential=True,
        client_secret=text_secret,
    )
    server = _server(clients=(client,))
    assert server._authenticate_client("text-secret", text_secret) is client


@pytest.mark.parametrize("malformed", [None, object()])
def test_confidential_client_secret_shape_is_refused_as_oauth(malformed: object) -> None:
    client = ClientRegistration(
        client_id="malformed",
        confidential=True,
        client_secret=b"x" * 32,
    )
    if malformed is not None:
        object.__setattr__(client, "client_secret", malformed)
        supplied: object = b"x" * 32
    else:
        supplied = None
    server = _server(clients=(client,))
    with pytest.raises(OAuthRefusal) as raised:
        server._authenticate_client("malformed", supplied)
    assert raised.value.reason == "invalid-client"


def test_issue_code_rechecks_the_exact_redirect_uri() -> None:
    with pytest.raises(OAuthRefusal) as raised:
        _server().issue_code(
            client_id="public",
            subject="user",
            challenge=_challenge("verifier"),
            redirect_uri="https://client.example/public/extra",
        )
    assert raised.value.reason == "redirect_uri-mismatch"


def test_unknown_authorization_code_is_refused() -> None:
    with pytest.raises(OAuthRefusal) as raised:
        _server().redeem(
            "unknown",
            verifier="verifier",
            client_id="public",
            redirect_uri="https://client.example/public",
        )
    assert raised.value.reason == "unknown-code"


def test_access_token_uses_supplied_time_and_omits_absent_optional_claims() -> None:
    server = _server()
    token = server.issue_access(subject=None, audience="api", now=1234.75)
    claims = _claims(token.access_token)
    assert claims["iat"] == 1234
    assert claims["exp"] == 4834
    assert "sub" not in claims
    assert "scope" not in claims
    assert "tenant" not in claims


def test_access_token_includes_present_optional_claims() -> None:
    token = _server().issue_access(
        subject="user",
        audience="api",
        scope=("read", "write"),
        tenant="acme",
    )
    claims = _claims(token.access_token)
    assert claims["sub"] == "user"
    assert claims["scope"] == "read write"
    assert claims["tenant"] == "acme"


def test_refresh_is_minted_only_when_requested_for_a_subject() -> None:
    server = _server()
    assert server.issue_access(subject="user", audience="api").refresh_token == ""
    assert (
        server.issue_access(
            subject=None,
            audience="api",
            with_refresh=True,
        ).refresh_token
        == ""
    )
    assert server.revoke_chain("") == 0


def test_client_credentials_defaults_to_registered_scopes() -> None:
    server = _server()
    token = server.client_credentials(
        client_id="confidential",
        client_secret=CLIENT_SECRET,
    )
    assert token.scope == ("read", "write")
    selected = server.client_credentials(
        client_id="confidential",
        client_secret=CLIENT_SECRET,
        scope=("read",),
    )
    assert selected.scope == ("read",)


def test_refresh_defaults_its_audience_to_the_issuer() -> None:
    refresh = _server().issue_refresh(subject="user")
    assert refresh.audience == "https://issuer.example"


def test_reused_bound_refresh_authenticates_before_revoking() -> None:
    server = _server()
    first = server.issue_refresh(
        subject="user",
        audience="api",
        client_id="confidential",
    )
    rotated = server.rotate(
        first,
        client_id="confidential",
        client_secret=CLIENT_SECRET,
    )
    with pytest.raises(OAuthRefusal) as raised:
        server.rotate(
            first,
            client_id="confidential",
            client_secret=b"wrong" * 8,
        )
    assert raised.value.reason == "invalid-client"
    assert not server.is_revoked(rotated.access_token)


def test_refresh_accepts_its_existing_audience_and_supplied_time() -> None:
    server = _server(refresh_ttl=60)
    refresh = server.issue_refresh(subject="user", audience="api", now=1000)
    rotated = server.rotate(refresh, audience="api", now=1030)
    assert rotated.audience == "api"
    assert rotated.expires_at == 4630


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [("confidential", None), (None, CLIENT_SECRET), ("confidential", CLIENT_SECRET)],
)
def test_unbound_refresh_refuses_any_client_credentials(
    client_id: str | None,
    client_secret: bytes | None,
) -> None:
    server = _server()
    refresh = server.issue_refresh(subject="user")
    with pytest.raises(OAuthRefusal) as raised:
        server.rotate(refresh, client_id=client_id, client_secret=client_secret)
    assert raised.value.reason == "invalid-client"


def test_bound_refresh_refuses_a_different_client_id() -> None:
    server = _server()
    refresh = server.issue_refresh(subject="user", client_id="confidential")
    with pytest.raises(OAuthRefusal) as raised:
        server.rotate(refresh, client_id="public", client_secret=CLIENT_SECRET)
    assert raised.value.reason == "invalid-client"


def test_revoking_one_chain_preserves_another_active_and_spent_chain() -> None:
    server = _server()
    first = server.issue_refresh(subject="first", audience="api", now=1000)
    other = server.issue_refresh(subject="other", audience="api", now=1000)
    first_rotated = server.rotate(first, now=1001)
    other_rotated = server.rotate(other, now=1001)

    server.revoke_chain(first.chain)

    assert server.rotate(other_rotated.refresh_token, now=1002).subject == "other"
    with pytest.raises(OAuthRefusal):
        server.rotate(other, now=1002)
    assert server.is_revoked(other_rotated.access_token)
    with pytest.raises(OAuthRefusal) as raised:
        server.rotate(first, client_id="public", now=1002)
    assert raised.value.reason == "refresh-reused"
    assert server.is_revoked(first_rotated.access_token)


def test_expired_spent_refresh_evidence_is_reclaimed() -> None:
    server = _server(refresh_ttl=10)
    first = server.issue_refresh(subject="user", audience="api", now=0)
    second = server.rotate(first, now=1)
    third = server.rotate(second.refresh_token, now=9)

    assert first.token in server._spent
    server.rotate(third.refresh_token, now=11)

    assert first.token not in server._spent


def test_expired_access_revocations_do_not_exhaust_pending_grant_capacity() -> None:
    server = _server(lifetime=1, max_pending_grants=4)
    for index in range(4):
        issued = server.issue_access(subject=f"user-{index}", audience="api", now=0)
        server._revoke_access(issued.access_token)

    code = server.issue_code(
        client_id="public",
        subject="user",
        challenge="x" * 43,
        redirect_uri="https://client.example/public",
        now=10_001,
    )

    assert code in server._codes
    assert server._revoked == set()
    assert server._revoked_ids == set()


@pytest.mark.parametrize("expires_at", [False, float("nan"), float("inf")])
def test_non_finite_or_boolean_token_expiry_is_not_used_for_revocation_cleanup(
    expires_at: object,
) -> None:
    server = _server()
    token = _signed_token(
        server,
        {"jti": "revoked", "exp": expires_at},
    )

    server._revoke_access(token)

    assert token not in server._revoked_expiries
    assert token in server._revoked
