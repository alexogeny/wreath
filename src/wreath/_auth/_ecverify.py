"""Zero-dependency public-key signature verification for JWT ES256 and EdDSA.

CPython's stdlib exposes hashes/HMAC (`hashlib`) and big-integer `pow` — enough
for HS/RS/PS — but no elliptic-curve signature verification. Rather than take a
third-party runtime dependency (forbidden) these are implemented directly:

* `verify_es256` — ECDSA over NIST P-256 (secp256r1), FIPS 186-4 / SEC1.
* `verify_ed25519` — EdDSA over edwards25519, RFC 8032 §5.1.7.

Verify-only: no key generation or signing lives here, so there is no private-key
or nonce-generation surface to get wrong. The group arithmetic underneath both
now lives in `wreath._curves`, which `_dkim` and `_webpush` share — this module
kept private copies for a long time precisely *because* it is verify-only, and
that is what forced the two signers to write their own. The split that replaced
it is by secrecy of the scalar rather than by module: everything here verifies a
published signature against a published key, so it calls the `_public` scalar
multiplications, which are variable-time on purpose. A signer must not.

Correctness is pinned against the RFC 8032 and NIST CAVP/Wycheproof
known-answer vectors in `tests/compliance/test_jwt_ec.py`, and against the
pre-projective implementation and `cryptography` in `tests/test_ec_p256.py` and
`tests/test_ec_ed25519.py`.
"""
from __future__ import annotations

import hashlib

from .._curves import (
    ED_L,
    P256_G,
    P256_N,
    ed_base,
    ed_decode_point,
    ed_double_scalarmult_public,
    ed_equal,
    ed_negate,
    p256_double_scalarmult_public,
    p256_on_curve,
)

__all__ = ["on_p256_curve", "verify_ed25519", "verify_es256"]

# --- NIST P-256 (secp256r1) -------------------------------------------------


def on_p256_curve(x: int, y: int) -> bool:
    """Whether `(x, y)` is a point on P-256 (and not the point at infinity).

    A public key is attacker-supplied whenever it comes from a JWKS, and a point
    that is not on the curve is not a public key -- it is a value that makes the
    group arithmetic below mean something other than ECDSA.
    """
    return p256_on_curve(x, y)


def verify_es256(x: int, y: int, signing_input: bytes, signature: bytes) -> bool:
    """Verify an ES256 (ECDSA/P-256/SHA-256) JWS signature.

    `x`/`y` are the public-key affine coordinates; `signature` is the
    fixed-width `r || s` (64 bytes) JOSE form.
    """
    if len(signature) != 64:
        return False
    if not p256_on_curve(x, y):
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    # FIPS 186-4 §6.4.2 step 1. `wreath mutant` reports the `r` half as a
    # survivor and it is kept deliberately: `r >= n` is already impossible at the
    # final comparison, which tests `point[0] % n == r` against a value below
    # `n`, and `r == 0` would need a point with `x == 0` whose discrete logarithm
    # the caller knows -- so no test can drive it, and no test should have to. The
    # `s` half is load-bearing (`pow(s, -1, n)` raises on zero) and is killed.
    if not (1 <= r < P256_N and 1 <= s < P256_N):
        return False
    digest = hashlib.sha256(signing_input).digest()
    z = int.from_bytes(digest, "big")  # P-256 order is 256 bits: no truncation
    w = pow(s, -1, P256_N)
    u1 = z * w % P256_N
    u2 = r * w % P256_N
    # [u1]G + [u2]Q, interleaved, in Jacobian coordinates. Both scalars are
    # derived from published values, so the variable-time form is the right one.
    point = p256_double_scalarmult_public(u1, P256_G, u2, (x, y))
    if point is None:
        return False
    return point[0] % P256_N == r


# --- Ed25519 (edwards25519) -- RFC 8032 §5.1 --------------------------------


def verify_ed25519(public: bytes, message: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 (EdDSA) signature per RFC 8032 §5.1.7."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a_point = ed_decode_point(public)
    if a_point is None:
        return False
    r_bytes = signature[:32]
    r_point = ed_decode_point(r_bytes)
    if r_point is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= ED_L:  # RFC 8032: S must be reduced (rejects malleability)
        return False
    h = int.from_bytes(
        hashlib.sha512(r_bytes + public + message).digest(), "little"
    ) % ED_L
    # RFC 8032's check is [S]B == R + [h]A. Written as [S]B + [h](-A) == R it is
    # one interleaved double-scalar multiplication rather than two independent
    # ones, which shares the doublings and drops the step count by two fifths.
    # Negating A rather than using [L - h]A keeps this exact for a public key
    # with a torsion component, which cofactorless verification accepts.
    left = ed_double_scalarmult_public(s, ed_base(), h, ed_negate(a_point))
    return ed_equal(left, r_point)
