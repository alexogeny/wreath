"""Zero-dependency public-key signature verification for JWT ES256 and EdDSA.

CPython's stdlib exposes hashes/HMAC (``hashlib``) and big-integer ``pow`` — enough
for HS/RS/PS — but no elliptic-curve signature verification. Rather than take a
third-party runtime dependency (forbidden) these are implemented directly:

* :func:`verify_es256` — ECDSA over NIST P-256 (secp256r1), FIPS 186-4 / SEC1.
* :func:`verify_ed25519` — EdDSA over edwards25519, RFC 8032 §5.1.7.

Verify-only: no key generation or signing lives here, so there is no private-key
or nonce-generation surface to get wrong. Correctness is pinned against the RFC
8032 and NIST CAVP/Wycheproof known-answer vectors in
``tests/compliance/test_jwt_ec.py``. These run in constant *code* paths but are
not written for side-channel resistance; that is irrelevant for verifying a
public signature with public inputs.
"""
from __future__ import annotations

import hashlib

# --- NIST P-256 (secp256r1) -------------------------------------------------

_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_A = _P256_P - 3
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5


def _inv_mod(value: int, modulus: int) -> int:
    return pow(value % modulus, -1, modulus)


def _p256_add(
    p: tuple[int, int] | None, q: tuple[int, int] | None
) -> tuple[int, int] | None:
    """Add two points on P-256 in affine coordinates (None is the identity)."""
    if p is None:
        return q
    if q is None:
        return p
    x1, y1 = p
    x2, y2 = q
    if x1 == x2 and (y1 + y2) % _P256_P == 0:
        return None
    if x1 == x2 and y1 == y2:
        lam = (3 * x1 * x1 + _P256_A) * _inv_mod(2 * y1, _P256_P) % _P256_P
    else:
        lam = (y2 - y1) * _inv_mod((x2 - x1) % _P256_P, _P256_P) % _P256_P
    x3 = (lam * lam - x1 - x2) % _P256_P
    y3 = (lam * (x1 - x3) - y1) % _P256_P
    return (x3, y3)


def _p256_mul(k: int, point: tuple[int, int] | None) -> tuple[int, int] | None:
    """Scalar multiplication by double-and-add."""
    result: tuple[int, int] | None = None
    addend = point
    while k:
        if k & 1:
            result = _p256_add(result, addend)
        addend = _p256_add(addend, addend)
        k >>= 1
    return result


def _p256_on_curve(x: int, y: int) -> bool:
    if not (0 <= x < _P256_P and 0 <= y < _P256_P):
        return False
    return (y * y - (x * x * x + _P256_A * x + _P256_B)) % _P256_P == 0


def on_p256_curve(x: int, y: int) -> bool:
    """Whether ``(x, y)`` is a point on P-256 (and not the point at infinity).

    A public key is attacker-supplied whenever it comes from a JWKS, and a point
    that is not on the curve is not a public key -- it is a value that makes the
    group arithmetic below mean something other than ECDSA.
    """
    if not (0 <= x < _P256_P and 0 <= y < _P256_P):
        return False
    if x == 0 and y == 0:
        return False
    return (y * y - (x * x * x + _P256_A * x + _P256_B)) % _P256_P == 0


def verify_es256(x: int, y: int, signing_input: bytes, signature: bytes) -> bool:
    """Verify an ES256 (ECDSA/P-256/SHA-256) JWS signature.

    ``x``/``y`` are the public-key affine coordinates; ``signature`` is the
    fixed-width ``r || s`` (64 bytes) JOSE form.
    """
    if len(signature) != 64:
        return False
    if not _p256_on_curve(x, y):
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (1 <= r < _P256_N and 1 <= s < _P256_N):
        return False
    digest = hashlib.sha256(signing_input).digest()
    z = int.from_bytes(digest, "big")  # P-256 order is 256 bits: no truncation
    w = _inv_mod(s, _P256_N)
    u1 = z * w % _P256_N
    u2 = r * w % _P256_N
    point = _p256_add(
        _p256_mul(u1, (_P256_GX, _P256_GY)), _p256_mul(u2, (x, y))
    )
    if point is None:
        return False
    return point[0] % _P256_N == r


# --- Ed25519 (edwards25519) -- RFC 8032 §5.1 --------------------------------

_ED_P = 2**255 - 19
_ED_L = 2**252 + 27742317777372353535851937790883648493
_ED_D = (-121665 * pow(121666, -1, _ED_P)) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)  # sqrt(-1)


def _ed_recover_x(y: int, sign: int) -> int | None:
    if y >= _ED_P:
        return None
    xx = (y * y - 1) * pow(_ED_D * y * y + 1, -1, _ED_P) % _ED_P
    x = pow(xx, (_ED_P + 3) // 8, _ED_P)
    if (x * x - xx) % _ED_P != 0:
        x = x * _ED_I % _ED_P
    if (x * x - xx) % _ED_P != 0:
        return None
    if x & 1 != sign:
        x = _ED_P - x
    return x


def _ed_point_add(
    p: tuple[int, int, int, int], q: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """Extended-coordinate (X, Y, Z, T) addition on edwards25519."""
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _ED_P
    b = (y1 + x1) * (y2 + x2) % _ED_P
    c = 2 * t1 * t2 * _ED_D % _ED_P
    dd = 2 * z1 * z2 % _ED_P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_scalarmult(k: int, point: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    result = (0, 1, 1, 0)  # neutral element
    addend = point
    while k:
        if k & 1:
            result = _ed_point_add(result, addend)
        addend = _ed_point_add(addend, addend)
        k >>= 1
    return result


def _ed_decode_point(data: bytes) -> tuple[int, int, int, int] | None:
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _ed_recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _ED_P)


def _ED_B() -> tuple[int, int, int, int]:
    by = 4 * pow(5, -1, _ED_P) % _ED_P
    bx = _ed_recover_x(by, 0)
    assert bx is not None
    return (bx, by, 1, bx * by % _ED_P)


def _ed_equal(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> bool:
    # Compare in affine form: X1/Z1 == X2/Z2 and Y1/Z1 == Y2/Z2.
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    return (x1 * z2 - x2 * z1) % _ED_P == 0 and (y1 * z2 - y2 * z1) % _ED_P == 0


def verify_ed25519(public: bytes, message: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 (EdDSA) signature per RFC 8032 §5.1.7."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a_point = _ed_decode_point(public)
    if a_point is None:
        return False
    r_bytes = signature[:32]
    r_point = _ed_decode_point(r_bytes)
    if r_point is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _ED_L:  # RFC 8032: S must be reduced (rejects malleability)
        return False
    h = int.from_bytes(
        hashlib.sha512(r_bytes + public + message).digest(), "little"
    ) % _ED_L
    # Check [S]B == R + [h]A.
    sb = _ed_scalarmult(s, _ED_B())
    rha = _ed_point_add(r_point, _ed_scalarmult(h, a_point))
    return _ed_equal(sb, rha)
