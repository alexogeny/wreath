from __future__ import annotations

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any

import pytest

import wreath.oauth as oauth
from wreath.oauth import AuthorizationServer, ClientRegistration, Es256Signer, OAuthRefusal

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


def _challenge(verifier: str) -> str:
    import hashlib

    return (
        urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


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
