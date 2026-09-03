from __future__ import annotations

import json
import math
from dataclasses import dataclass

import pytest

from wreath._b64 import b64url_decode
from wreath.auth import JwtVerifier, SymmetricKey
from wreath.oauth import (
    TOKEN_INTROSPECTION_JWT_MEDIA_TYPE,
    AuthorizationServer,
    ClientRegistration,
    OAuthRefusal,
)


@pytest.mark.parametrize("field", ("lifetime", "code_ttl", "refresh_ttl"))
@pytest.mark.parametrize("value", (math.nan, math.inf, -math.inf))
def test_authorization_server_refuses_nonfinite_grant_lifetimes(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=field):
        AuthorizationServer(
            issuer="https://issuer.example",
            **{field: value},
        )


CLIENT_SECRET = b"console-secret-material" * 2
CLIENT = ClientRegistration(
    client_id="console",
    redirect_uris=("https://app.example/cb",),
    scopes=("read", "write"),
    confidential=True,
    client_secret=CLIENT_SECRET,
)
PUBLIC = ClientRegistration(
    client_id="spa", redirect_uris=("https://app.example/spa",), scopes=("read",)
)
RESOURCE_SECRET = b"resource-server-secret" * 2
RESOURCE = ClientRegistration(
    client_id="resource-api",
    confidential=True,
    client_secret=RESOURCE_SECRET,
    scopes=("read",),
    introspection_signed_response_alg="HS256",
)


@dataclass(frozen=True, slots=True)
class AccountInformation:
    actions: list[str]
    locations: list[str]


@pytest.fixture
def server() -> AuthorizationServer:
    return AuthorizationServer(
        issuer="https://app.example", secret=b"a" * 32, clients=(CLIENT, PUBLIC)
    )


def test_metadata_names_the_issuer_and_its_endpoints(server) -> None:
    metadata = server.metadata()
    assert metadata["issuer"] == "https://app.example"
    assert metadata["token_endpoint"].endswith("/oauth/token")
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["authorization_response_iss_parameter_supported"] is True


def test_jwt_introspection_exposes_its_registered_media_type() -> None:
    assert TOKEN_INTROSPECTION_JWT_MEDIA_TYPE == b"application/token-introspection+jwt"


def test_jwt_introspection_is_authenticated_audience_bound_and_cross_jwt_safe() -> None:
    introspection_server = AuthorizationServer(
        issuer="https://app.example",
        secret=b"a" * 32,
        clients=(RESOURCE,),
    )
    issued = introspection_server.issue_access(
        subject="user-1",
        audience="resource-api",
        scope=("read", "write"),
        client_id="resource-api",
        now=1000,
    )
    response = introspection_server.introspection_jwt(
        issued.access_token,
        client_id="resource-api",
        client_secret=RESOURCE_SECRET,
        now=1001,
    )
    header_segment, payload_segment, _signature = response.split(".")
    header = json.loads(b64url_decode(header_segment))
    payload = json.loads(b64url_decode(payload_segment))
    assert header == {"alg": "HS256", "typ": "token-introspection+jwt"}
    assert payload["iss"] == "https://app.example"
    assert payload["aud"] == "resource-api"
    assert payload["iat"] == 1001
    assert "sub" not in payload
    assert "exp" not in payload
    assert payload["token_introspection"]["active"] is True
    assert payload["token_introspection"]["sub"] == "user-1"
    assert payload["token_introspection"]["client_id"] == "resource-api"
    assert payload["token_introspection"]["scope"] == "read"
    metadata = introspection_server.metadata()
    assert metadata["introspection_endpoint"].endswith("/oauth/introspect")
    assert metadata["introspection_signing_alg_values_supported"] == ["HS256"]


def test_jwt_introspection_discloses_nothing_for_the_wrong_resource_server() -> None:
    other_secret = b"other-resource-secret" * 2
    other = ClientRegistration(
        client_id="other-api",
        confidential=True,
        client_secret=other_secret,
        introspection_signed_response_alg="HS256",
    )
    introspection_server = AuthorizationServer(
        issuer="https://app.example",
        secret=b"a" * 32,
        clients=(RESOURCE, other),
    )
    issued = introspection_server.issue_access(subject="user-1", audience="resource-api", now=1000)
    response = introspection_server.introspection_jwt(
        issued.access_token,
        client_id="other-api",
        client_secret=other_secret,
        now=1001,
    )
    payload = json.loads(b64url_decode(response.split(".")[1]))
    assert payload["token_introspection"] == {"active": False}


def test_jwt_introspection_uses_the_servers_asymmetric_signer() -> None:
    from wreath._auth.jwt import _parse_compact, _verify_signature
    from wreath.oauth import Es256Signer

    signer = Es256Signer.generate()
    resource = ClientRegistration(
        client_id="resource-api",
        confidential=True,
        client_secret=RESOURCE_SECRET,
        introspection_signed_response_alg="ES256",
    )
    introspection_server = AuthorizationServer(
        issuer="https://app.example",
        signer=signer,
        clients=(resource,),
    )
    issued = introspection_server.issue_access(subject="user-1", audience="resource-api", now=1000)
    response = introspection_server.introspection_jwt(
        issued.access_token,
        client_id="resource-api",
        client_secret=RESOURCE_SECRET,
        now=1001,
    )
    header, _claims, signing_input, signature = _parse_compact(response)
    assert header == {
        "alg": "ES256",
        "typ": "token-introspection+jwt",
        "kid": signer.kid,
    }
    assert _verify_signature("ES256", signer.verifying_key(), signing_input, signature)
    assert introspection_server.metadata()["introspection_signing_alg_values_supported"] == [
        "ES256"
    ]


def test_jwt_introspection_requires_registered_resource_server_authentication() -> None:
    introspection_server = AuthorizationServer(
        issuer="https://app.example",
        secret=b"a" * 32,
        clients=(RESOURCE,),
    )
    with pytest.raises(OAuthRefusal, match="secret") as raised:
        introspection_server.introspection_jwt(
            "not-a-token",
            client_id="resource-api",
            client_secret=b"wrong" * 8,
        )
    assert raised.value.reason == "invalid-client"


def test_introspection_encryption_is_refused_at_registration() -> None:
    encrypted = ClientRegistration(
        client_id="resource-api",
        confidential=True,
        client_secret=RESOURCE_SECRET,
        introspection_signed_response_alg="HS256",
        introspection_encrypted_response_alg="RSA-OAEP",
        introspection_encrypted_response_enc="A128CBC-HS256",
    )
    with pytest.raises(ValueError, match="introspection response encryption.*not supported"):
        AuthorizationServer(issuer="https://app.example", clients=(encrypted,))


def test_authorization_success_response_identifies_its_issuer(server) -> None:
    assert server.authorization_response(code="code", state="state") == {
        "code": "code",
        "state": "state",
        "iss": "https://app.example",
    }


def test_authorization_error_response_identifies_its_issuer(server) -> None:
    assert server.authorization_response(error="access_denied", state="state") == {
        "error": "access_denied",
        "state": "state",
        "iss": "https://app.example",
    }


def test_authorization_response_refuses_ambiguous_success_and_error(server) -> None:
    with pytest.raises(ValueError, match="exactly one of code or error"):
        server.authorization_response(code="code", error="access_denied")


def test_the_jwks_is_empty_for_an_hmac_signer_and_that_is_a_fact(server) -> None:
    assert server.jwks() == {"keys": []}


def test_rich_authorization_details_are_validated_and_carried_by_the_token() -> None:
    client = ClientRegistration(
        client_id="rich-client",
        redirect_uris=("https://app.example/cb",),
        authorization_detail_types=("account_information",),
    )
    rich = AuthorizationServer(
        issuer="https://app.example",
        secret=b"a" * 32,
        clients=(client,),
        authorization_detail_types={"account_information": AccountInformation},
    )
    verifier, challenge = _pkce()
    code = rich.issue_code(
        client_id="rich-client",
        subject="u",
        challenge=challenge,
        redirect_uri="https://app.example/cb",
        authorization_details=json.dumps(
            [
                {
                    "type": "account_information",
                    "actions": ["list_accounts"],
                    "locations": ["https://api.example/accounts"],
                }
            ]
        ),
    )
    token = rich.redeem(
        code,
        verifier=verifier,
        client_id="rich-client",
        redirect_uri="https://app.example/cb",
    )
    claims = json.loads(b64url_decode(token.access_token.split(".")[1]))
    assert token.authorization_details == (
        {
            "type": "account_information",
            "actions": ["list_accounts"],
            "locations": ["https://api.example/accounts"],
        },
    )
    assert claims["authorization_details"] == list(token.authorization_details)
    assert rich.metadata()["authorization_details_types_supported"] == ["account_information"]


@pytest.mark.parametrize(
    "details",
    [
        [{"type": "unknown", "actions": []}],
        [{"type": "account_information", "actions": [], "locations": [], "extra": True}],
        [{"type": "account_information", "actions": "read", "locations": []}],
        [{"type": "account_information", "actions": []}],
        {"type": "account_information"},
    ],
)
def test_invalid_rich_authorization_details_are_refused(details) -> None:
    rich = AuthorizationServer(
        issuer="https://app.example",
        authorization_detail_types={"account_information": AccountInformation},
    )
    with pytest.raises(OAuthRefusal, match="authorization_details") as raised:
        rich.validate_authorization_details(details)
    assert raised.value.reason == "invalid-authorization-details"


def test_client_registration_refuses_an_unknown_authorization_detail_type() -> None:
    client = ClientRegistration(
        client_id="rich-client",
        authorization_detail_types=("not-registered",),
    )
    with pytest.raises(ValueError, match="not-registered.*authorization detail type"):
        AuthorizationServer(issuer="https://app.example", clients=(client,))


def test_a_redirect_uri_is_matched_exactly_and_never_by_prefix(server) -> None:
    with pytest.raises(OAuthRefusal, match="matched exactly") as raised:
        server.authorize(client_id="console", redirect_uri="https://app.example.evil/cb")
    assert raised.value.reason == "redirect_uri-mismatch"


def test_the_registered_redirect_uri_is_accepted(server) -> None:
    assert (
        server.authorize(client_id="console", redirect_uri="https://app.example/cb").client_id
        == "console"
    )


def test_the_plain_pkce_method_is_refused_at_the_authorization_endpoint(server) -> None:
    with pytest.raises(OAuthRefusal, match="protects nothing") as raised:
        server.authorize(
            client_id="console", redirect_uri="https://app.example/cb", challenge_method="plain"
        )
    assert raised.value.reason == "weak-pkce"


def test_an_unknown_client_is_refused(server) -> None:
    with pytest.raises(OAuthRefusal, match="no client registered"):
        server.authorize(client_id="ghost", redirect_uri="https://app.example/cb")


def test_a_scope_the_client_was_not_registered_for_is_refused(server) -> None:
    _, challenge = _pkce()
    with pytest.raises(OAuthRefusal, match="not registered for scope") as raised:
        server.issue_code(
            client_id="console",
            subject="u",
            scope=("admin",),
            challenge=challenge,
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "invalid-scope"


def test_a_code_can_be_redeemed_once(server) -> None:
    verifier, _ = _pkce()
    code = _issued(server)
    token = server.redeem(
        code,
        verifier=verifier,
        client_id="console",
        client_secret=CLIENT_SECRET,
        redirect_uri="https://app.example/cb",
    )
    assert token.subject == "u"
    assert token.scope == ("read",)


def test_a_confidential_client_must_authenticate_when_redeeming_a_code(server) -> None:
    verifier, _ = _pkce()
    code = _issued(server)
    with pytest.raises(OAuthRefusal, match="client secret") as raised:
        server.redeem(
            code,
            verifier=verifier,
            client_id="console",
            client_secret=b"wrong" * 8,
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "invalid-client"


def test_redeeming_a_code_twice_revokes_the_token_the_first_redemption_issued(
    server,
) -> None:
    verifier, _ = _pkce()
    code = _issued(server)
    redemption = {
        "verifier": verifier,
        "client_id": "console",
        "client_secret": CLIENT_SECRET,
        "redirect_uri": "https://app.example/cb",
    }
    first = server.redeem(code, **redemption)
    assert not server.is_revoked(first.access_token)
    with pytest.raises(OAuthRefusal, match="has been revoked") as raised:
        server.redeem(code, **redemption)
    assert raised.value.reason == "code-replayed"
    assert server.is_revoked(first.access_token)


def test_a_code_redeemed_with_the_wrong_pkce_verifier_is_refused(server) -> None:
    code = _issued(server)
    with pytest.raises(OAuthRefusal, match="does not match the challenge") as raised:
        server.redeem(
            code,
            verifier="a-different-one",
            client_id="console",
            client_secret=CLIENT_SECRET,
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "pkce-mismatch"


def test_the_matching_verifier_is_accepted(server) -> None:
    verifier, _ = _pkce()
    code = _issued(server)
    assert (
        server.redeem(
            code,
            verifier=verifier,
            client_id="console",
            client_secret=CLIENT_SECRET,
            redirect_uri="https://app.example/cb",
        ).subject
        == "u"
    )


def test_a_refresh_token_rotates(server) -> None:
    first = server.issue_refresh(subject="u")
    rotated = server.rotate(first)
    assert rotated.refresh_token
    assert rotated.refresh_token != first.token


def test_presenting_a_rotated_refresh_token_again_is_refused(server) -> None:
    first = server.issue_refresh(subject="u")
    server.rotate(first)
    with pytest.raises(OAuthRefusal, match="already been rotated") as raised:
        server.rotate(first)
    assert raised.value.reason == "refresh-reused"


def test_revoking_the_chain_revokes_every_access_token_it_issued(server) -> None:
    first = server.issue_refresh(subject="u")
    second = server.rotate(first)
    assert server.revoke_chain(first.chain) >= 1
    assert server.is_revoked(second.access_token)


def test_a_client_credentials_grant_cannot_carry_a_subject(server) -> None:
    with pytest.raises(OAuthRefusal, match="no resource owner") as raised:
        server.client_credentials(client_id="console", subject="u")
    assert raised.value.reason == "subject-on-client-credentials"


def test_a_public_client_cannot_use_the_client_credentials_grant(server) -> None:
    with pytest.raises(OAuthRefusal, match="cannot keep a secret") as raised:
        server.client_credentials(client_id="spa")
    assert raised.value.reason == "public-client"


def test_a_confidential_client_gets_a_subjectless_token(server) -> None:
    token = server.client_credentials(client_id="console", client_secret=CLIENT_SECRET)
    assert token.subject is None


def test_client_credentials_requires_the_confidential_clients_secret(server) -> None:
    with pytest.raises(OAuthRefusal, match="client secret") as raised:
        server.client_credentials(client_id="console", client_secret=b"wrong" * 8)
    assert raised.value.reason == "invalid-client"


def test_client_credentials_cannot_escalate_past_registered_scopes(server) -> None:
    with pytest.raises(OAuthRefusal, match="not registered for scope") as raised:
        server.client_credentials(
            client_id="console",
            client_secret=CLIENT_SECRET,
            scope=("admin",),
        )
    assert raised.value.reason == "invalid-scope"


def test_hmac_authorization_server_refuses_a_short_signing_secret() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        AuthorizationServer(issuer="https://app.example", secret=b"short")


@pytest.mark.parametrize(
    "issuer",
    [
        "http://app.example",
        "https://user:password@app.example",
        "https://app.example?tenant=acme",
        "https://app.example#issuer",
    ],
)
def test_authorization_server_refuses_an_insecure_issuer(issuer: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTPS URL"):
        AuthorizationServer(issuer=issuer, secret=b"a" * 32)


def test_refresh_tokens_expire() -> None:
    expiring = AuthorizationServer(
        issuer="https://app.example",
        secret=b"a" * 32,
        refresh_ttl=60,
    )
    refresh = expiring.issue_refresh(subject="u", now=1000)
    with pytest.raises(OAuthRefusal, match="expired") as raised:
        expiring.rotate(refresh, now=1061)
    assert raised.value.reason == "refresh-expired"


def test_a_refresh_preserves_the_original_grant_limits(server) -> None:
    verifier, _ = _pkce()
    code = _issued(server, tenant="acme")
    first = server.redeem(
        code,
        verifier=verifier,
        client_id="console",
        client_secret=CLIENT_SECRET,
        redirect_uri="https://app.example/cb",
    )

    rotated = server.rotate(
        first.refresh_token,
        client_id="console",
        client_secret=CLIENT_SECRET,
    )

    assert rotated.audience == "console"
    assert rotated.scope == ("read",)
    assert rotated.tenant == "acme"


def test_a_refresh_cannot_change_audience_or_bypass_client_authentication(server) -> None:
    verifier, _ = _pkce()
    code = _issued(server)
    first = server.redeem(
        code,
        verifier=verifier,
        client_id="console",
        client_secret=CLIENT_SECRET,
        redirect_uri="https://app.example/cb",
    )

    with pytest.raises(OAuthRefusal, match="client secret") as wrong_secret:
        server.rotate(
            first.refresh_token,
            client_id="console",
            client_secret=b"wrong" * 8,
        )
    assert wrong_secret.value.reason == "invalid-client"

    with pytest.raises(OAuthRefusal, match="audience") as changed_audience:
        server.rotate(
            first.refresh_token,
            client_id="console",
            client_secret=CLIENT_SECRET,
            audience="admin-service",
        )
    assert changed_audience.value.reason == "audience-mismatch"

    assert (
        server.rotate(
            first.refresh_token,
            client_id="console",
            client_secret=CLIENT_SECRET,
        ).audience
        == "console"
    )


def test_a_token_carries_the_tenant_it_was_minted_in(server) -> None:
    verifier, _ = _pkce()
    code = _issued(server, tenant="acme")
    assert (
        server.redeem(
            code,
            verifier=verifier,
            client_id="console",
            client_secret=CLIENT_SECRET,
            redirect_uri="https://app.example/cb",
        ).tenant
        == "acme"
    )


def test_a_minted_token_is_accepted_by_wreaths_own_verifier(server) -> None:
    token = server.issue_access(subject="u-9", audience="https://api.example", scope=("read",))
    verifier = JwtVerifier(
        algorithms=["HS256"],
        key=SymmetricKey(secret=server.secret),
        issuer="https://app.example",
        audience="https://api.example",
    )
    identity = verifier(token.access_token)
    # `Identity.id`, not `.subject`: wreath's identity carries `id`/`type`, and
    # the `sub` claim is what `default_identity` maps onto it.
    assert identity.id == "u-9"


def test_a_token_minted_for_another_audience_is_refused_by_that_verifier(
    server,
) -> None:
    token = server.issue_access(subject="u", audience="https://other.example")
    verifier = JwtVerifier(
        algorithms=["HS256"],
        key=SymmetricKey(secret=server.secret),
        issuer="https://app.example",
        audience="https://api.example",
    )
    # `None`, not an exception: `JwtVerifier` is a `Verifier` callable and a
    # token that does not verify is "nobody", which is what
    # `BearerTokenBackend` turns into a 401.
    assert verifier(token.access_token) is None


def test_a_token_signed_with_another_secret_is_refused(server) -> None:
    token = server.issue_access(subject="u", audience="https://api.example")
    verifier = JwtVerifier(
        algorithms=["HS256"],
        key=SymmetricKey(secret=b"b" * 32),
        issuer="https://app.example",
        audience="https://api.example",
    )
    assert verifier(token.access_token) is None


# Added after the module docstring claimed asymmetric signing would need a
# `cryptography` dependency. It does not: `wreath._webpush` already signs ES256
# with a hedged nonce over the standard library, and `JwtVerifier` already
# verifies ES256. Both halves were in the tree and the claim was wrong.


def _es256_server() -> tuple[AuthorizationServer, object]:
    from wreath.oauth import Es256Signer

    signer = Es256Signer.generate()
    return AuthorizationServer(
        issuer="https://app.example", clients=(CLIENT,), signer=signer
    ), signer


def test_the_es256_signer_publishes_a_real_key_set() -> None:
    server, signer = _es256_server()
    keys = server.jwks()["keys"]
    assert len(keys) == 1
    assert keys[0]["kty"] == "EC" and keys[0]["crv"] == "P-256"
    assert keys[0]["kid"] == signer.kid


def test_the_published_key_set_cannot_contain_the_private_half() -> None:
    server, signer = _es256_server()
    entry = server.jwks()["keys"][0]
    assert "d" not in entry and "k" not in entry
    assert signer.private_bytes.hex() not in str(entry)


def test_a_token_signed_with_es256_verifies_against_the_published_key() -> None:
    server, signer = _es256_server()
    token = server.issue_access(subject="u-9", audience="https://api.example")
    verifier = JwtVerifier(
        algorithms=["ES256"],
        key=signer.verifying_key(),
        issuer="https://app.example",
        audience="https://api.example",
    )
    assert verifier(token.access_token).id == "u-9"


def test_an_es256_token_names_its_key_id_so_rotation_is_possible() -> None:
    import json
    from base64 import urlsafe_b64decode

    server, signer = _es256_server()
    token = server.issue_access(subject="u", audience="a")
    raw = token.access_token.split(".")[0]
    header = json.loads(urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert header["alg"] == "ES256"
    assert header["kid"] == signer.kid


def test_a_token_signed_by_another_es256_key_is_refused() -> None:
    from wreath.oauth import Es256Signer

    server, _ = _es256_server()
    token = server.issue_access(subject="u", audience="https://api.example")
    verifier = JwtVerifier(
        algorithms=["ES256"],
        key=Es256Signer.generate().verifying_key(),
        issuer="https://app.example",
        audience="https://api.example",
    )
    assert verifier(token.access_token) is None


def test_a_signer_round_trips_through_its_private_bytes() -> None:
    from wreath.oauth import Es256Signer

    signer = Es256Signer.generate()
    restored = Es256Signer.from_bytes(signer.private_bytes, kid=signer.kid)
    assert restored.public_jwks() == signer.public_jwks()


def test_signing_cost_is_counted_so_the_ceiling_can_be_watched() -> None:
    server, _ = _es256_server()
    for _ in range(5):
        server.issue_access(subject="u", audience="a")
    counters = server.counters()
    assert counters.values["tokens_issued"] == 5
    assert counters.values["signing_nanoseconds"] > 0


def test_the_hmac_path_counts_tokens_but_not_signing_time(server) -> None:
    server.issue_access(subject="u", audience="a")
    counters = server.counters()
    assert counters.subsystem == "oauth"
    assert counters.values == {"tokens_issued": 1, "signing_nanoseconds": 0}


# Each of these is a check RFC 6749 §4.1.3 requires of the token endpoint and
# that `redeem` did not make. They are grouped because they share one shape: a
# field the code carries, stored and then never read.


def _pkce() -> tuple[str, str]:
    """A verifier and its S256 challenge."""
    import hashlib
    from base64 import urlsafe_b64encode

    verifier = "the-real-one-and-it-is-long-enough-to-be-worth-something"
    challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _issued(server: AuthorizationServer, **overrides) -> str:
    verifier, challenge = _pkce()
    options = {
        "client_id": "console",
        "subject": "u",
        "scope": ("read",),
        "challenge": challenge,
        "redirect_uri": "https://app.example/cb",
    }
    options.update(overrides)
    return server.issue_code(**options)


def test_a_code_cannot_be_minted_without_a_pkce_challenge(server) -> None:
    with pytest.raises(OAuthRefusal, match="code_challenge") as raised:
        server.issue_code(
            client_id="console",
            subject="u",
            challenge="",
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "weak-pkce"


def test_a_challenge_that_is_not_an_s256_digest_is_refused(server) -> None:
    with pytest.raises(OAuthRefusal, match="code_challenge") as raised:
        server.issue_code(
            client_id="console",
            subject="u",
            challenge="short",
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "weak-pkce"


def test_redeeming_without_a_verifier_is_refused(server) -> None:
    code = _issued(server)
    with pytest.raises(OAuthRefusal, match="does not match the challenge") as raised:
        server.redeem(
            code,
            verifier="",
            client_id="console",
            client_secret=CLIENT_SECRET,
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "pkce-mismatch"


def test_a_code_is_redeemable_only_by_the_client_it_was_issued_to(server) -> None:
    verifier, _ = _pkce()
    code = _issued(server)
    with pytest.raises(OAuthRefusal, match="issued to a different client") as raised:
        server.redeem(
            code,
            verifier=verifier,
            client_id="spa",
            redirect_uri="https://app.example/cb",
        )
    assert raised.value.reason == "client-mismatch"


def test_a_code_is_redeemable_only_against_the_redirect_uri_it_was_issued_for(
    server,
) -> None:
    verifier, _ = _pkce()
    code = _issued(server)
    with pytest.raises(OAuthRefusal, match="redirect_uri") as raised:
        server.redeem(
            code,
            verifier=verifier,
            client_id="console",
            client_secret=CLIENT_SECRET,
            redirect_uri="https://app.example/other",
        )
    assert raised.value.reason == "redirect_uri-mismatch"


def test_an_authorization_code_expires(server) -> None:
    verifier, _ = _pkce()
    code = _issued(server, now=1000.0)
    with pytest.raises(OAuthRefusal, match="expired") as raised:
        server.redeem(
            code,
            verifier=verifier,
            client_id="console",
            client_secret=CLIENT_SECRET,
            redirect_uri="https://app.example/cb",
            now=1000.0 + 61.0,
        )
    assert raised.value.reason == "code-expired"


def test_a_code_inside_its_window_still_redeems(server) -> None:
    verifier, _ = _pkce()
    code = _issued(server, now=1000.0)
    token = server.redeem(
        code,
        verifier=verifier,
        client_id="console",
        client_secret=CLIENT_SECRET,
        redirect_uri="https://app.example/cb",
        now=1000.0 + 30.0,
    )
    assert token.subject == "u"


def test_refresh_reuse_revokes_the_chain_it_says_it_revokes(server) -> None:
    first = server.issue_refresh(subject="u")
    stolen = server.rotate(first)
    further = server.rotate(stolen.refresh_token)

    with pytest.raises(OAuthRefusal, match="already been rotated"):
        server.rotate(first)

    assert server.is_revoked(stolen.access_token)
    assert server.is_revoked(further.access_token)
    with pytest.raises(OAuthRefusal, match="already been rotated"):
        server.rotate(further.refresh_token)


def test_revoking_a_chain_covers_the_access_token_minted_beside_its_first_refresh(
    server,
) -> None:
    verifier, _ = _pkce()
    code = _issued(server)
    token = server.redeem(
        code,
        verifier=verifier,
        client_id="console",
        client_secret=CLIENT_SECRET,
        redirect_uri="https://app.example/cb",
    )
    assert token.refresh_token
    with pytest.raises(OAuthRefusal, match="already been rotated"):
        server.rotate(
            token.refresh_token,
            client_id="console",
            client_secret=CLIENT_SECRET,
        )
        server.rotate(
            token.refresh_token,
            client_id="console",
            client_secret=CLIENT_SECRET,
        )
    assert server.is_revoked(token.access_token)
