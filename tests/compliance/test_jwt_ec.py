"""JWT ES256 / EdDSA verification, pinned two ways:

1. Dependency-free known-answer vectors (RFC 6979 P-256, RFC 8032 Ed25519) that
   run everywhere and lock the primitives to the standards.
2. A differential oracle against `cryptography` (a test-only dependency, skipped
   if absent) that generates fresh keypairs, signs real JWTs, and verifies them
   end-to-end through wreath's JwtVerifier — including tamper rejection.
"""
from __future__ import annotations

import base64
import json

import pytest

from wreath._auth._ecverify import verify_ed25519, verify_es256
from wreath._auth.jwt import JwtVerifier, key_from_jwk


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# --- dependency-free known-answer vectors -----------------------------------


def test_rfc6979_p256_sha256_known_answer() -> None:
    # RFC 6979 §A.2.5 — P-256/SHA-256, message "sample".
    ux = 0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6
    uy = 0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299
    r = 0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716
    s = 0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    assert verify_es256(ux, uy, b"sample", sig)
    assert not verify_es256(ux, uy, b"samplE", sig)   # message tamper
    assert not verify_es256(ux, uy, b"sample", bytes(64))  # zero signature


@pytest.mark.parametrize("public_hex, msg_hex, sig_hex", [
    # RFC 8032 §7.1 TEST 1 (empty message) and TEST 2 (one byte).
    ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a"
     "33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15"
     "996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
])
def test_rfc8032_ed25519_known_answers(public_hex: str, msg_hex: str, sig_hex: str) -> None:
    public, msg, sig = bytes.fromhex(public_hex), bytes.fromhex(msg_hex), bytes.fromhex(sig_hex)
    assert verify_ed25519(public, msg, sig)
    assert not verify_ed25519(public, msg + b"\x00", sig)  # message tamper


def test_deferred_ec_curves_and_algs_are_refused() -> None:
    from wreath._auth.jwt import UnsupportedAlgorithm

    with pytest.raises(UnsupportedAlgorithm):
        key_from_jwk({"kty": "EC", "crv": "P-384", "x": "AA", "y": "AA"})
    with pytest.raises(UnsupportedAlgorithm):
        key_from_jwk({"kty": "OKP", "crv": "Ed448", "x": "AA"})


# --- cryptography differential oracle ---------------------------------------

crypto = pytest.importorskip("cryptography", reason="oracle differential test")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, utils  # noqa: E402


def _make_jwt(alg: str, signing_input: bytes, signature: bytes) -> str:
    return signing_input.decode("ascii") + "." + _b64u(signature)


def _signing_input(alg: str, claims: dict) -> bytes:
    header = _b64u(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    payload = _b64u(json.dumps(claims).encode())
    return f"{header}.{payload}".encode("ascii")


@pytest.mark.parametrize("n", range(25))
def test_es256_roundtrip_and_tamper_against_oracle(n: int) -> None:
    sk = ec.generate_private_key(ec.SECP256R1())
    numbers = sk.public_key().public_numbers()
    jwk = {"kty": "EC", "crv": "P-256",
           "x": _b64u(numbers.x.to_bytes(32, "big")),
           "y": _b64u(numbers.y.to_bytes(32, "big"))}
    verifier = JwtVerifier(
        algorithms=["ES256"], key=key_from_jwk(jwk), audience=None, required=()
    )

    signing_input = _signing_input("ES256", {"sub": f"user-{n}"})
    der = sk.sign(bytes(signing_input), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    token = _make_jwt("ES256", signing_input, r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    identity = verifier(token)
    assert identity is not None and identity.id == f"user-{n}"
    # A single flipped signature byte must fail closed (returns None, never raises).
    assert verifier(token[:-2] + ("A" if token[-1] != "A" else "B")) is None


@pytest.mark.parametrize("n", range(25))
def test_ed25519_roundtrip_and_tamper_against_oracle(n: int) -> None:
    sk = ed25519.Ed25519PrivateKey.generate()
    public = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": _b64u(public)}
    verifier = JwtVerifier(
        algorithms=["EdDSA"], key=key_from_jwk(jwk), audience=None, required=()
    )

    signing_input = _signing_input("EdDSA", {"sub": f"user-{n}"})
    token = _make_jwt("EdDSA", signing_input, sk.sign(bytes(signing_input)))

    identity = verifier(token)
    assert identity is not None and identity.id == f"user-{n}"
    assert verifier(token[:-2] + ("A" if token[-1] != "A" else "B")) is None


def test_wrong_key_family_is_rejected() -> None:
    # An Ed25519 verifier must not accept an ES256 token, and vice versa.
    ec_sk = ec.generate_private_key(ec.SECP256R1())
    numbers = ec_sk.public_key().public_numbers()
    ec_key = key_from_jwk({"kty": "EC", "crv": "P-256",
                           "x": _b64u(numbers.x.to_bytes(32, "big")),
                           "y": _b64u(numbers.y.to_bytes(32, "big"))})
    from wreath._auth.jwt import UnsupportedAlgorithm
    with pytest.raises(UnsupportedAlgorithm):
        JwtVerifier(algorithms=["EdDSA"], key=ec_key, audience=None, required=())
