"""JWT verification: correctness and the adversarial/negative suite.

HS* tokens are built with the stdlib (no external dependency). RS*/PS* tokens are
signed with ``cryptography`` (a dev dependency) as an external oracle — exactly
the arrangement the design mandates for a security component. This is the
first leg of the eventual three-legged harness; the review fork should add
Project Wycheproof vectors before production trust.

NOTE FOR THE REVIEW FORK: these tests exercise the Python facade + stdlib
fallbacks. To also cover the native jose.c path they must run once with the
compiled _core present (default) and once with WREATH_PURE=1.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from wreath.auth import (
    JwtVerifier,
    RsaPublicKey,
    SymmetricKey,
    UnsupportedAlgorithm,
    key_from_pem,
)

SECRET = b"a-shared-secret-of-reasonable-length"
_HS_DIGEST = {"HS256": "sha256", "HS384": "sha384", "HS512": "sha512"}


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _segments(header: dict, claims: dict) -> tuple[str, str, bytes]:
    hb = _b64u(json.dumps(header).encode())
    pb = _b64u(json.dumps(claims).encode())
    return hb, pb, f"{hb}.{pb}".encode("ascii")


def _hs(claims: dict, *, secret: bytes = SECRET, alg: str = "HS256", header_extra: dict | None = None) -> str:
    header = {"alg": alg, "typ": "JWT", **(header_extra or {})}
    hb, pb, signing_input = _segments(header, claims)
    sig = hmac.new(secret, signing_input, _HS_DIGEST[alg]).digest()
    return f"{hb}.{pb}.{_b64u(sig)}"


def _claims(**overrides) -> dict:
    base = {"sub": "user-123", "exp": int(time.time()) + 3600, "iss": "https://issuer.example", "aud": "my-api"}
    base.update(overrides)
    return base


def _verifier(**overrides) -> JwtVerifier:
    kwargs = dict(
        algorithms=("HS256",),
        key=SymmetricKey(SECRET),
        issuer="https://issuer.example",
        audience="my-api",
        leeway=0,
    )
    kwargs.update(overrides)
    return JwtVerifier(**kwargs)


# ---- happy path -----------------------------------------------------------


def test_valid_hs256_returns_identity():
    identity = _verifier()(_hs(_claims(roles=["admin", "ops"])))
    assert identity is not None
    assert identity.id == "user-123"
    assert identity.roles == frozenset({"admin", "ops"})


def test_cognito_groups_map_to_roles():
    token = _hs(_claims(**{"cognito:groups": ["fleet-admin"]}))
    identity = _verifier()(token)
    assert identity is not None
    assert identity.roles == frozenset({"fleet-admin"})


# ---- adversarial / negative ----------------------------------------------


def test_alg_none_is_rejected():
    # A classic downgrade: alg "none" with an empty signature.
    hb, pb, _ = _segments({"alg": "none", "typ": "JWT"}, _claims())
    assert _verifier()(f"{hb}.{pb}.") is None
    # ...and even with junk in the signature slot.
    assert _verifier()(f"{hb}.{pb}.{_b64u(b'junk')}") is None


def test_unlisted_algorithm_is_rejected():
    # Token signed HS384 but the verifier only allows HS256.
    assert _verifier(algorithms=("HS256",))(_hs(_claims(), alg="HS384")) is None


def test_rs_hs_key_confusion_is_rejected():
    # Attack: sign an HS256 token using the RSA *public key* bytes as the HMAC
    # secret and present it to an RS256 verifier. The token's alg (HS256) is not
    # in the RS256 allow-list, so it never reaches a verify.
    pub_pem = b"-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n-----END PUBLIC KEY-----"
    verifier = JwtVerifier(
        algorithms=("RS256",),
        key=RsaPublicKey(n=0xC0FFEE, e=65537),
        issuer="https://issuer.example",
        audience="my-api",
    )
    forged = _hs(_claims(), secret=pub_pem, alg="HS256")
    assert verifier(forged) is None


def test_tampered_signature_is_rejected():
    token = _hs(_claims())
    head, _, _sig = token.rpartition(".")
    assert _verifier()(f"{head}.{_b64u(b'0' * 32)}") is None


def test_wrong_secret_is_rejected():
    assert _verifier()(_hs(_claims(), secret=b"the-wrong-secret")) is None


def test_expired_token_is_rejected():
    assert _verifier()(_hs(_claims(exp=int(time.time()) - 10))) is None


def test_not_yet_valid_token_is_rejected():
    assert _verifier()(_hs(_claims(nbf=int(time.time()) + 3600))) is None


def test_wrong_audience_is_rejected():
    assert _verifier()(_hs(_claims(aud="some-other-api"))) is None


def test_wrong_issuer_is_rejected():
    assert _verifier()(_hs(_claims(iss="https://evil.example"))) is None


def test_missing_sub_is_rejected():
    claims = _claims()
    del claims["sub"]
    assert _verifier()(_hs(claims)) is None


@pytest.mark.parametrize("token", ["", "a", "a.b", "a.b.c.d", "....", "not-a-jwt"])
def test_malformed_tokens_are_rejected(token):
    assert _verifier()(token) is None


def test_oversized_token_is_rejected():
    huge = _hs(_claims(blob="x" * (200 * 1024)))
    assert _verifier()(huge) is None


# ---- construction-time guards --------------------------------------------


def test_symmetric_key_rejects_rsa_algorithm():
    with pytest.raises(UnsupportedAlgorithm):
        JwtVerifier(algorithms=("RS256",), key=SymmetricKey(SECRET))


def test_es256_is_a_loud_unsupported_error():
    with pytest.raises(UnsupportedAlgorithm):
        JwtVerifier(algorithms=("ES256",), key=SymmetricKey(SECRET))


def test_eddsa_is_a_loud_unsupported_error():
    with pytest.raises(UnsupportedAlgorithm):
        JwtVerifier(algorithms=("EdDSA",), key=SymmetricKey(SECRET))


# ---- RSA via the cryptography oracle -------------------------------------


@pytest.fixture(scope="module")
def rsa_keypair():
    crypto = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, pem


def _rs_token(private, claims: dict, *, alg: str = "RS256") -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    hashes_by_alg = {
        "RS256": hashes.SHA256, "RS384": hashes.SHA384, "RS512": hashes.SHA512,
        "PS256": hashes.SHA256, "PS384": hashes.SHA384, "PS512": hashes.SHA512,
    }
    algo = hashes_by_alg[alg]()
    header = {"alg": alg, "typ": "JWT"}
    hb, pb, signing_input = _segments(header, claims)
    if alg.startswith("PS"):
        pad = padding.PSS(mgf=padding.MGF1(algo), salt_length=algo.digest_size)
    else:
        pad = padding.PKCS1v15()
    sig = private.sign(signing_input, pad, algo)
    return f"{hb}.{pb}.{_b64u(sig)}"


@pytest.mark.parametrize("alg", ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512"])
def test_rsa_valid_token_verifies(rsa_keypair, alg):
    private, pem = rsa_keypair
    verifier = JwtVerifier(
        algorithms=(alg,), key=key_from_pem(pem),
        issuer="https://issuer.example", audience="my-api", leeway=0,
    )
    identity = verifier(_rs_token(private, _claims(), alg=alg))
    assert identity is not None
    assert identity.id == "user-123"


def test_rsa_tampered_signature_is_rejected(rsa_keypair):
    private, pem = rsa_keypair
    verifier = JwtVerifier(
        algorithms=("RS256",), key=key_from_pem(pem),
        issuer="https://issuer.example", audience="my-api",
    )
    token = _rs_token(private, _claims(), alg="RS256")
    head, _, _sig = token.rpartition(".")
    bad_sig = _b64u(b"\x00" * 256)
    assert verifier(f"{head}.{bad_sig}") is None


def test_pem_and_jwk_agree(rsa_keypair):
    private, pem = rsa_keypair
    from wreath.auth import key_from_jwk

    numbers = private.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "n": _b64u(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64u(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }
    from_pem = key_from_pem(pem)
    from_jwk = key_from_jwk(jwk)
    assert (from_pem.n, from_pem.e) == (from_jwk.n, from_jwk.e)
