"""Group parameters and Python-facing types for edwards25519 and NIST P-256.

The curve library implements point validation, encoding, addition, public
double-scalar multiplication, and fixed-shape secret-scalar multiplication.
This module supplies the standard parameters and preserves tuple-shaped points
for the JWT, DKIM, WebAuthn, and Web Push APIs.
"""

from __future__ import annotations

from typing import Final

from ._native import _core

__all__ = [
    "ED_D",
    "ED_L",
    "ED_NEUTRAL",
    "ED_P",
    "EdPoint",
    "P256_A",
    "P256_B",
    "P256_G",
    "P256_N",
    "P256_P",
    "ed_add",
    "ed_base",
    "ed_decode_point",
    "ed_double_scalarmult_public",
    "ed_encode_point",
    "ed_equal",
    "ed_negate",
    "ed_recover_x",
    "ed_scalarmult_public",
    "ed_scalarmult_secret",
    "p256_double_scalarmult_public",
    "p256_on_curve",
    "p256_scalarmult_secret",
]

ED_P: Final = 2**255 - 19
ED_L: Final = 2**252 + 27742317777372353535851937790883648493
ED_D: Final = 37095705934669439343138083508754565189542113879843219016388785533085940283555
_ED_I: Final = 19681161376707505956807079304988542015446066515923890162744021073123829784752
_ED_SECRET_BITS: Final = 254

type EdPoint = tuple[int, int, int, int]

ED_NEUTRAL: Final[EdPoint] = (0, 1, 1, 0)
_ED_BASE: Final[EdPoint] = (
    15112221349535400772501151409588531511454012693041857206046113283949847762202,
    46316835694926478169428394003475163141307993866256225615783033603165251855960,
    1,
    46827403850823179245072216630277197565144205554125654976674165829533817101731,
)


def ed_add(p: EdPoint, q: EdPoint) -> EdPoint:
    """Add two edwards25519 points in extended coordinates."""
    return _core.curve_ed_add(p, q)


def ed_negate(p: EdPoint) -> EdPoint:
    """Return the additive inverse of an edwards25519 point."""
    return _core.curve_ed_negate(p)


def ed_recover_x(y: int, sign: int) -> int | None:
    """Recover the x coordinate with the requested low bit."""
    return _core.curve_ed_recover_x(y, sign)


def ed_decode_point(data: bytes) -> EdPoint | None:
    """Decode an RFC 8032 point encoding."""
    return _core.curve_ed_decode(data)


def ed_encode_point(p: EdPoint) -> bytes:
    """Encode an edwards25519 point in RFC 8032 form."""
    return _core.curve_ed_encode(p)


def ed_base() -> EdPoint:
    """Return the RFC 8032 base point."""
    return _ED_BASE


def ed_equal(p: EdPoint, q: EdPoint) -> bool:
    """Compare two extended-coordinate points projectively."""
    return _core.curve_ed_equal(p, q)


def ed_scalarmult_public(k: int, point: EdPoint) -> EdPoint:
    """Multiply by a public scalar."""
    return _core.curve_ed_scalar(k, point, False)


def ed_double_scalarmult_public(
    k1: int, p1: EdPoint, k2: int, p2: EdPoint
) -> EdPoint:
    """Return ``[k1]p1 + [k2]p2`` for public scalars."""
    return _core.curve_ed_double_scalar(k1, p1, k2, p2)


def ed_scalarmult_secret(k: int, point: EdPoint) -> EdPoint:
    """Multiply by a secret scalar with a fixed operation shape."""
    return _core.curve_ed_scalar(k, point, True)


P256_P: Final = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
P256_A: Final = P256_P - 3
P256_B: Final = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
P256_N: Final = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_GX: Final = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY: Final = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
P256_G: Final[tuple[int, int]] = (_P256_GX, _P256_GY)
def p256_on_curve(x: int, y: int) -> bool:
    """Return whether ``(x, y)`` is an affine P-256 point."""
    return _core.curve_p256_on_curve(x, y)


def p256_double_scalarmult_public(
    k1: int, p1: tuple[int, int], k2: int, p2: tuple[int, int]
) -> tuple[int, int] | None:
    """Return affine ``[k1]p1 + [k2]p2`` for public scalars."""
    return _core.curve_p256_double_scalar(k1, p1, k2, p2)


def p256_scalarmult_secret(
    k: int, point: tuple[int, int]
) -> tuple[int, int] | None:
    """Multiply an affine P-256 point by a secret scalar."""
    return _core.curve_p256_scalar(k, point)
