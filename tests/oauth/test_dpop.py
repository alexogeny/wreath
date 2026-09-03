from __future__ import annotations

import hashlib
import json
import time

import pytest

from wreath._b64 import b64url_decode, b64url_encode
from wreath._webpush import _ecdsa_sign
from wreath.dpop import DPoPRefusal, DPoPVerifier, _target_uri
from wreath.oauth import AuthorizationServer, ClientRegistration, Es256Signer, OAuthRefusal


def _proof(
    signer: Es256Signer,
    *,
    method: str = "POST",
    uri: str = "https://server.example/token",
    jti: str = "proof-1",
    issued_at: int = 1_000,
    access_token: str | None = None,
    nonce: str | None = None,
    private_jwk: bool = False,
    header_overrides: dict[str, object] | None = None,
    claim_overrides: dict[str, object] | None = None,
) -> str:
    jwk = dict(signer.public_jwks()[0])
    jwk.pop("alg")
    jwk.pop("use")
    jwk.pop("kid")
    if private_jwk:
        jwk["d"] = b64url_encode(signer.private_bytes)
    header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk}
    if header_overrides is not None:
        header.update(header_overrides)
    claims: dict[str, object] = {
        "jti": jti,
        "htm": method,
        "htu": uri,
        "iat": issued_at,
    }
    if access_token is not None:
        claims["ath"] = b64url_encode(hashlib.sha256(access_token.encode("ascii")).digest())
    if nonce is not None:
        claims["nonce"] = nonce
    if claim_overrides is not None:
        claims.update(claim_overrides)
    encoded_header = b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    encoded_claims = b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = _ecdsa_sign(signer.private, hashlib.sha256(signing_input).digest())
    return f"{encoded_header}.{encoded_claims}.{b64url_encode(signature)}"


@pytest.fixture
def signer() -> Es256Signer:
    return Es256Signer.generate()


@pytest.fixture
def verifier() -> DPoPVerifier:
    return DPoPVerifier(max_entries=8, max_age=300, clock_skew=30)


def test_target_uri_brackets_only_ipv6_hosts() -> None:
    assert _target_uri("https://server.example/token", proof=False) == (
        "https://server.example/token"
    )
    assert _target_uri("https://[2001:db8::1]/token", proof=False) == (
        "https://[2001:db8::1]/token"
    )


def test_a_valid_proof_returns_the_public_key_thumbprint(verifier, signer) -> None:
    validated = verifier.verify(
        _proof(signer),
        method="POST",
        uri="https://server.example/token?ignored=yes",
        now=1_000,
    )
    assert validated.jti == "proof-1"
    assert validated.jkt
    assert validated.jwk["kty"] == "EC"


def test_a_proof_is_single_use(verifier, signer) -> None:
    proof = _proof(signer)
    verifier.verify(proof, method="POST", uri="https://server.example/token", now=1_000)
    with pytest.raises(DPoPRefusal, match="already been used") as raised:
        verifier.verify(proof, method="POST", uri="https://server.example/token", now=1_001)
    assert raised.value.reason == "replayed-proof"


@pytest.mark.parametrize(
    ("method", "uri", "message"),
    [
        ("GET", "https://server.example/token", "HTTP method"),
        ("POST", "https://other.example/token", "target URI"),
        ("POST", "https://server.example:8443/token", "target URI"),
        ("POST", "https://server.example/other", "target URI"),
    ],
)
def test_a_proof_is_bound_to_one_method_and_uri(verifier, signer, method, uri, message) -> None:
    with pytest.raises(DPoPRefusal, match=message):
        verifier.verify(_proof(signer), method=method, uri=uri, now=1_000)


@pytest.mark.parametrize(
    "uri",
    [
        "ftp://server.example/token",
        "https:///token",
        "https://user@server.example/token",
        "https://user:password@server.example/token",
        "https://server.example:invalid/token",
        "https://server.example/token?query=yes",
        "https://server.example/token#fragment",
    ],
)
def test_a_proof_target_must_be_an_absolute_credential_free_uri(verifier, signer, uri) -> None:
    with pytest.raises(DPoPRefusal) as raised:
        verifier.verify(_proof(signer, uri=uri), method="POST", uri=uri, now=1_000)
    assert raised.value.reason == "invalid-target-uri"


@pytest.mark.parametrize(
    ("proof_uri", "request_uri"),
    [
        ("http://server.example:80/token", "http://server.example/token"),
        ("https://server.example:443/token", "https://server.example/token"),
        ("https://server.example:8443/token", "https://server.example:8443/token"),
        ("https://[2001:db8::1]/token", "https://[2001:db8::1]/token"),
        ("https://server.example", "https://server.example/"),
    ],
)
def test_equivalent_target_uri_forms_are_normalized(
    verifier, signer, proof_uri, request_uri
) -> None:
    validated = verifier.verify(
        _proof(signer, uri=proof_uri), method="POST", uri=request_uri, now=1_000
    )
    assert validated.jti == "proof-1"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_age": 0}, "max_age must be positive"),
        ({"clock_skew": -1}, "clock_skew must not be negative"),
        ({"algorithms": ()}, "at least one asymmetric"),
        ({"algorithms": ("HS256",)}, "supported asymmetric"),
        ({"algorithms": ("not-an-algorithm",)}, "supported asymmetric"),
    ],
    ids=["max-age", "clock-skew", "empty-algorithms", "symmetric", "unknown"],
)
def test_verifier_configuration_refuses_unsafe_values(options, message) -> None:
    with pytest.raises(ValueError, match=message):
        DPoPVerifier(**options)


@pytest.mark.parametrize(
    ("header_overrides", "reason"),
    [
        ({"typ": "JWT"}, "wrong-type"),
        ({"alg": 7}, "unsupported-algorithm"),
        ({"alg": []}, "unsupported-algorithm"),
        ({"alg": "RS256"}, "unsupported-algorithm"),
        ({"jwk": []}, "missing-key"),
    ],
    ids=["typ", "alg-type", "alg-unhashable", "unconfigured-alg", "jwk-type"],
)
def test_invalid_protected_headers_are_refused(verifier, signer, header_overrides, reason) -> None:
    with pytest.raises(DPoPRefusal) as raised:
        verifier.verify(
            _proof(signer, header_overrides=header_overrides),
            method="POST",
            uri="https://server.example/token",
            now=1_000,
        )
    assert raised.value.reason == reason


def test_a_malformed_compact_proof_is_refused(verifier) -> None:
    with pytest.raises(DPoPRefusal) as raised:
        verifier.verify(
            "not-a-compact-jwt",
            method="POST",
            uri="https://server.example/token",
            now=1_000,
        )
    assert raised.value.reason == "malformed-proof"


def test_an_invalid_public_key_is_refused(verifier, signer) -> None:
    with pytest.raises(DPoPRefusal) as raised:
        verifier.verify(
            _proof(signer, header_overrides={"jwk": {"kty": "EC"}}),
            method="POST",
            uri="https://server.example/token",
            now=1_000,
        )
    assert raised.value.reason == "invalid-key"


def test_a_proof_signature_must_match_its_public_key(verifier, signer) -> None:
    encoded_header, encoded_claims, encoded_signature = _proof(signer).split(".")
    signature = bytearray(b64url_decode(encoded_signature))
    signature[-1] ^= 1
    proof = f"{encoded_header}.{encoded_claims}.{b64url_encode(bytes(signature))}"
    with pytest.raises(DPoPRefusal) as raised:
        verifier.verify(
            proof,
            method="POST",
            uri="https://server.example/token",
            now=1_000,
        )
    assert raised.value.reason == "invalid-signature"


@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"jti": None},
        {"jti": 7},
        {"jti": ""},
        {"htm": None},
        {"htm": 7},
        {"htm": ""},
        {"htu": None},
        {"htu": 7},
        {"htu": ""},
        {"iat": True},
        {"iat": "1000"},
    ],
    ids=[
        "jti-type",
        "jti-number",
        "jti-empty",
        "htm-type",
        "htm-number",
        "htm-empty",
        "htu-type",
        "htu-number",
        "htu-empty",
        "iat-bool",
        "iat-type",
    ],
)
def test_required_claims_have_exact_types(verifier, signer, claim_overrides) -> None:
    with pytest.raises(DPoPRefusal) as raised:
        verifier.verify(
            _proof(signer, claim_overrides=claim_overrides),
            method="POST",
            uri="https://server.example/token",
            now=1_000,
        )
    assert raised.value.reason == "missing-claim"


def test_resource_proof_binds_the_access_token_and_key(verifier, signer) -> None:
    proof = _proof(signer, access_token="access")
    validated = verifier.verify(
        proof,
        method="POST",
        uri="https://server.example/token",
        access_token="access",
        now=1_000,
    )
    second = DPoPVerifier(max_entries=8)
    assert second.verify(
        _proof(signer, access_token="access", jti="proof-2"),
        method="POST",
        uri="https://server.example/token",
        access_token="access",
        expected_jkt=validated.jkt,
        now=1_000,
    ).jkt == validated.jkt


def test_a_wrong_access_token_is_refused(verifier, signer) -> None:
    with pytest.raises(DPoPRefusal, match="access token hash") as raised:
        verifier.verify(
            _proof(signer, access_token="access"),
            method="POST",
            uri="https://server.example/token",
            access_token="different",
            now=1_000,
        )
    assert raised.value.reason == "access-token-mismatch"


def test_a_private_jwk_is_refused(verifier, signer) -> None:
    with pytest.raises(DPoPRefusal, match="private key") as raised:
        verifier.verify(
            _proof(signer, private_jwk=True),
            method="POST",
            uri="https://server.example/token",
            now=1_000,
        )
    assert raised.value.reason == "private-key"


def test_a_stale_proof_is_refused(verifier, signer) -> None:
    with pytest.raises(DPoPRefusal, match="creation time") as raised:
        verifier.verify(
            _proof(signer, issued_at=600),
            method="POST",
            uri="https://server.example/token",
            now=1_000,
        )
    assert raised.value.reason == "stale-proof"


def test_a_proof_too_far_in_the_future_is_refused(verifier, signer) -> None:
    with pytest.raises(DPoPRefusal) as raised:
        verifier.verify(
            _proof(signer, issued_at=1_031),
            method="POST",
            uri="https://server.example/token",
            now=1_000,
        )
    assert raised.value.reason == "stale-proof"


def test_a_proof_can_use_the_current_clock(verifier, signer) -> None:
    now = int(time.time())
    validated = verifier.verify(
        _proof(signer, issued_at=now),
        method="POST",
        uri="https://server.example/token",
    )
    assert validated.issued_at == now


def test_nonce_is_required_when_the_server_issued_one(verifier, signer) -> None:
    with pytest.raises(DPoPRefusal, match="nonce") as raised:
        verifier.verify(
            _proof(signer),
            method="POST",
            uri="https://server.example/token",
            nonce="server-nonce",
            now=1_000,
        )
    assert raised.value.reason == "nonce-mismatch"


def test_an_unrequested_nonce_does_not_invalidate_a_proof(verifier, signer) -> None:
    validated = verifier.verify(
        _proof(signer, nonce="client-nonce"),
        method="POST",
        uri="https://server.example/token",
        now=1_000,
    )
    assert validated.jti == "proof-1"


def test_a_requested_nonce_can_match(verifier, signer) -> None:
    validated = verifier.verify(
        _proof(signer, nonce="server-nonce"),
        method="POST",
        uri="https://server.example/token",
        nonce="server-nonce",
        now=1_000,
    )
    assert validated.jti == "proof-1"


def test_access_token_hash_must_be_a_string(verifier, signer) -> None:
    with pytest.raises(DPoPRefusal) as raised:
        verifier.verify(
            _proof(signer, claim_overrides={"ath": 7}),
            method="POST",
            uri="https://server.example/token",
            access_token="access",
            now=1_000,
        )
    assert raised.value.reason == "access-token-mismatch"


def test_access_token_must_be_ascii(verifier, signer) -> None:
    with pytest.raises(DPoPRefusal) as raised:
        verifier.verify(
            _proof(signer),
            method="POST",
            uri="https://server.example/token",
            access_token="café",
            now=1_000,
        )
    assert raised.value.reason == "invalid-access-token"


def test_expected_key_thumbprint_must_match(verifier, signer) -> None:
    with pytest.raises(DPoPRefusal) as raised:
        verifier.verify(
            _proof(signer),
            method="POST",
            uri="https://server.example/token",
            expected_jkt="different-thumbprint",
            now=1_000,
        )
    assert raised.value.reason == "key-mismatch"


def test_replay_capacity_is_distinct_from_a_replayed_proof(signer) -> None:
    verifier = DPoPVerifier(max_entries=1)
    verifier.verify(
        _proof(signer), method="POST", uri="https://server.example/token", now=1_000
    )
    with pytest.raises(DPoPRefusal) as raised:
        verifier.verify(
            _proof(signer, jti="proof-2"),
            method="POST",
            uri="https://server.example/token",
            now=1_000,
        )
    assert raised.value.reason == "replay-capacity"


def test_authorization_server_advertises_dpop_and_binds_an_access_token(signer) -> None:
    server = AuthorizationServer(
        issuer="https://server.example",
        signer=signer,
    )
    proof = DPoPVerifier(max_entries=8).verify(
        _proof(signer),
        method="POST",
        uri="https://server.example/token",
        now=1_000,
    )
    token = server.issue_access(
        subject="user",
        audience="api",
        dpop_jkt=proof.jkt,
        now=1_000,
    )
    claims = json.loads(b64url_decode(token.access_token.split(".")[1]))
    assert claims["cnf"] == {"jkt": proof.jkt}
    assert token.token_type == "DPoP"
    assert server.metadata()["dpop_signing_alg_values_supported"] == ["ES256", "EdDSA"]


def test_a_client_can_require_dpop_at_every_token_request(signer) -> None:
    client = ClientRegistration(
        client_id="public",
        redirect_uris=("https://client.example/cb",),
        dpop_bound_access_tokens=True,
    )
    server = AuthorizationServer(issuer="https://server.example", clients=(client,))
    with pytest.raises(OAuthRefusal, match="requires a DPoP proof") as raised:
        server.issue_access(subject="user", audience="public", client_id="public")
    assert raised.value.reason == "invalid-dpop-proof"


def test_a_dpop_bound_refresh_token_can_only_rotate_with_the_same_key(signer) -> None:
    server = AuthorizationServer(issuer="https://server.example")
    first = server.issue_access(
        subject="user",
        audience="public",
        dpop_jkt="bound-thumbprint",
        with_refresh=True,
    )
    with pytest.raises(OAuthRefusal, match="same DPoP key") as raised:
        server.rotate(first.refresh_token, dpop_jkt="different-thumbprint")
    assert raised.value.reason == "invalid-dpop-proof"
    rotated = server.rotate(first.refresh_token, dpop_jkt="bound-thumbprint")
    claims = json.loads(b64url_decode(rotated.access_token.split(".")[1]))
    assert claims["cnf"] == {"jkt": "bound-thumbprint"}
    assert rotated.token_type == "DPoP"
