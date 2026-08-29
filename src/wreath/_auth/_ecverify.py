"""Zero-dependency public-key signature verification for JWT ES256 and EdDSA.

CPython's stdlib exposes hashes/HMAC (`hashlib`) but no elliptic-curve signature
verification. Wreath supplies the missing fixed-width curve operations itself:

* `verify_es256` — ECDSA over NIST P-256 (secp256r1), FIPS 186-4 / SEC1.
* `verify_ed25519` — EdDSA over edwards25519, RFC 8032 §5.1.7.

Verify-only: no key generation or signing lives here. The curve library is
shared with `_dkim` and `_webpush`, and separates variable-time operations on
published scalars from fixed-shape operations on secret scalars.

Correctness is pinned against RFC 8032 and NIST CAVP/Wycheproof known-answer
vectors, plus the independent `cryptography` test dependency.
"""

from __future__ import annotations

import hashlib

from .._curves import p256_on_curve
from .._native import _core

__all__ = ["on_p256_curve", "verify_ed25519", "verify_es256"]


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
    digest = hashlib.sha256(signing_input).digest()
    return _core.curve_p256_verify(x, y, digest, signature)


def verify_ed25519(public: bytes, message: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 (EdDSA) signature per RFC 8032 §5.1.7."""
    if len(public) != 32 or len(signature) != 64:
        return False
    r_bytes = signature[:32]
    digest = hashlib.sha512(r_bytes + public + message).digest()
    return _core.curve_ed_verify(public, digest, signature)
