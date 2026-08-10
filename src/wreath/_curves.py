"""Group arithmetic for edwards25519 and NIST P-256, shared by every caller.

Three modules needed the same two curves and each had written its own copy:
`_auth/_ecverify` (verify-only, by design, so its arithmetic was private),
`_dkim` (Ed25519 signing, which the private copy could not serve), and
`_webpush` (P-256 signing and ECDH, likewise). `wreath-dup-scan` reported the
edwards25519 half twice and `_dkim` already carried a comment naming this fix.
This module is that fix: the group law lives here once, and the things that are
*not* group law -- clamping, hedged nonce derivation, low-S normalisation,
encoding conventions -- stay with the caller that owns the decision.

## Public scalars and secret scalars are different functions

**This is the security-relevant part of the module and the reason the two
spellings exist.** Read it before choosing a function.

Signature *verification* consumes nothing secret: the public key, the signature
and the message are all attacker-visible already, so a scalar multiplication
whose running time depends on the scalar leaks nothing that was not published.
Those get the `_public` functions, which are as aggressive as correctness
allows -- interleaved (Shamir) double-scalar multiplication, digits skipped when
they are zero, and shapes that vary with the scalar.

Signing and ECDH do not. A DKIM Ed25519 nonce, a VAPID private key and a Web
Push ephemeral scalar are secrets whose disclosure is total: one recovered
ECDSA nonce yields the private key outright. Those get the `_secret`
functions, which run a **fixed number of iterations with a fixed sequence of
group operations**, choose between values with arithmetic masks rather than
`if`, and never index a table with a secret bit.

| scalar | function | shape |
| --- | --- | --- |
| public | `ed_scalarmult_public`, `ed_double_scalarmult_public` | varies with the scalar |
| public | `p256_double_scalarmult_public` | varies with the scalar |
| secret | `ed_scalarmult_secret` | 254 iterations, always |
| secret | `p256_scalarmult_secret` | 257 iterations, always |

**What the `_secret` functions do and do not promise.** They remove the
*structural* leaks: the old `while k: if k & 1:` shape in all three callers
leaked the scalar's bit length through the iteration count and its Hamming
weight through the branch, which is the pair of leaks that a timing or
power-analysis attack on a double-and-add implementation actually reads. What
they cannot remove is CPython's own arithmetic: `int.__mul__` and `int.__mod__`
run schoolbook algorithms whose timing depends on operand magnitude, and a
255-bit product that happens to have a short normalised form is cheaper than one
that does not. **A Python implementation is therefore not constant-time in the
strict sense and this module does not claim to be.** It claims that the
scalar does not steer control flow or memory indexing, which is the part that is
achievable here, and it is a strict improvement on what the three callers
shipped. Anyone who needs a hardened signer wants a hardware token or a library
with field arithmetic in assembly, and that is a deployment decision rather than
something this file can fix.

To make the iteration count fixed, both `_secret` functions rewrite the scalar
`k` as a congruent value with a *constant* bit length: `k mod L + 2L` on
edwards25519, and `k + n` or `k + 2n` (selected by mask) on P-256. Both leave
`[k]P` unchanged for a point `P` of prime order, which is why each function
documents that requirement rather than checking it.

## Coordinates

edwards25519 uses extended coordinates `(X, Y, Z, T)` with `x = X/Z`, `y = Y/Z`
and `T = XY/Z`; the addition law is unified, so one formula covers doubling and
the neutral element and there is no exceptional case to branch on.

P-256 uses Jacobian coordinates `(X, Y, Z)` with `x = X/Z^2` and `y = Y/Z^3`,
where `Z = 0` is the point at infinity. This is the whole point of the module
for that curve: affine addition needs `pow(x, -1, p)` for every slope, which is
11.7 us against 0.27 us for a modular multiplication, and one verification did
766 of them. Jacobian arithmetic inverts **once**, at the end, when the result is
converted back to affine.
"""

from __future__ import annotations

from typing import Final

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

# --- edwards25519 (RFC 8032 §5.1) -------------------------------------------

ED_P: Final = 2**255 - 19
ED_L: Final = 2**252 + 27742317777372353535851937790883648493
ED_D: Final = (-121665 * pow(121666, -1, ED_P)) % ED_P
_ED_I: Final = pow(2, (ED_P - 1) // 4, ED_P)  # sqrt(-1)

#: An extended-coordinate point `(X, Y, Z, T)`.
type EdPoint = tuple[int, int, int, int]

#: The identity of the group, in extended coordinates.
ED_NEUTRAL: Final[EdPoint] = (0, 1, 1, 0)

#: `ed_scalarmult_secret` runs exactly this many iterations. `k % L + 2L` lies
#: in `[2L, 3L)`, and `2L >= 2**253` while `3L < 2**254`, so every such value has
#: exactly 254 bits -- which is what makes the count independent of the scalar.
_ED_SECRET_BITS: Final = 254


def ed_add(p: EdPoint, q: EdPoint) -> EdPoint:
    """Add two edwards25519 points in extended coordinates.

    The twisted-Edwards addition law with `a = -1` and a non-square `d` -- which
    is what edwards25519 has -- is *complete*: this one formula is correct for
    `p == q`, for either argument being `ED_NEUTRAL`, and for a point plus its
    own negation. There is no case to test for, which is why the secret-scalar
    routine below can call it unconditionally.
    """
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % ED_P
    b = (y1 + x1) * (y2 + x2) % ED_P
    c = 2 * t1 * t2 * ED_D % ED_P
    dd = 2 * z1 * z2 % ED_P
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % ED_P, g * h % ED_P, f * g % ED_P, e * h % ED_P)


def ed_negate(p: EdPoint) -> EdPoint:
    """`-p`. Exact for any point, including one outside the prime-order subgroup.

    Verification rewrites `[S]B == R + [h]A` as `[S]B + [h](-A) == R` so both
    scalar multiplications can share one sequence of doublings. Negating `A` is
    what makes that rewrite faithful: substituting `[L - h]A` for `-[h]A` would
    be equivalent only when `A` has order dividing `L`, and RFC 8032 cofactorless
    verification accepts an `A` with a torsion component, so the substitution
    would quietly change which signatures verify.
    """
    x, y, z, t = p
    return ((-x) % ED_P, y, z, (-t) % ED_P)


def ed_recover_x(y: int, sign: int) -> int | None:
    """The `x` with the requested low bit for curve point `y`, or `None`.

    `None` means `y` is out of range or names no point on edwards25519, which is
    a rejection every caller has to make rather than an internal error.
    """
    if y >= ED_P:
        return None
    xx = (y * y - 1) * pow(ED_D * y * y + 1, -1, ED_P) % ED_P
    x = pow(xx, (ED_P + 3) // 8, ED_P)
    if (x * x - xx) % ED_P != 0:
        x = x * _ED_I % ED_P
    if (x * x - xx) % ED_P != 0:
        return None
    if x & 1 != sign:
        x = ED_P - x
    return x


def ed_decode_point(data: bytes) -> EdPoint | None:
    """Decode 32 bytes of RFC 8032 point encoding, or `None` if they name none."""
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = ed_recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % ED_P)


def ed_encode_point(p: EdPoint) -> bytes:
    """The 32-byte RFC 8032 encoding of `p`. One inversion, at the end."""
    x, y, z, _ = p
    zi = pow(z, -1, ED_P)
    x = x * zi % ED_P
    y = y * zi % ED_P
    return ((y & ((1 << 255) - 1)) | ((x & 1) << 255)).to_bytes(32, "little")


_ED_BASE: EdPoint | None = None


def ed_base() -> EdPoint:
    """The RFC 8032 base point `B`, computed once and cached."""
    global _ED_BASE
    if _ED_BASE is None:
        by = 4 * pow(5, -1, ED_P) % ED_P
        bx = ed_recover_x(by, 0)
        if bx is None:
            # A real raise, not an `assert`: `python -O` strips every assert, and
            # this one used to be one. Under `-O` the guard vanished and a `None`
            # propagated into the arithmetic to fail somewhere else entirely.
            raise ValueError("the edwards25519 base point has no x coordinate")
        _ED_BASE = (bx, by, 1, bx * by % ED_P)
    return _ED_BASE


def ed_equal(p: EdPoint, q: EdPoint) -> bool:
    """Whether two extended-coordinate points are the same group element.

    Compared projectively -- `X1/Z1 == X2/Z2` and `Y1/Z1 == Y2/Z2` -- so no
    inversion is needed and two encodings of one point compare equal.
    """
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    return (x1 * z2 - x2 * z1) % ED_P == 0 and (y1 * z2 - y2 * z1) % ED_P == 0


def ed_scalarmult_public(k: int, point: EdPoint) -> EdPoint:
    """`[k]point`, for a scalar that is **not** secret.

    Variable time in `k`: the loop runs once per bit of `k` and adds only where a
    bit is set, so both the length and the Hamming weight of the scalar are
    observable. Use `ed_scalarmult_secret` for anything derived from a private
    key.
    """
    result = ED_NEUTRAL
    addend = point
    while k:
        if k & 1:
            result = ed_add(result, addend)
        addend = ed_add(addend, addend)
        k >>= 1
    return result


def ed_double_scalarmult_public(k1: int, p1: EdPoint, k2: int, p2: EdPoint) -> EdPoint:
    """`[k1]p1 + [k2]p2` with one shared sequence of doublings (Shamir's trick).

    Both scalars must be public. Two separate scalar multiplications double
    twice; interleaving them doubles once, which is where the step count of an
    Ed25519 verification falls by roughly two fifths.
    """
    both = ed_add(p1, p2)
    result = ED_NEUTRAL
    for index in range(max(k1.bit_length(), k2.bit_length()) - 1, -1, -1):
        result = ed_add(result, result)
        digit = ((k1 >> index) & 1) | (((k2 >> index) & 1) << 1)
        if digit == 1:
            result = ed_add(result, p1)
        elif digit == 2:
            result = ed_add(result, p2)
        elif digit == 3:
            result = ed_add(result, both)
    return result


def _ed_select(bit: int, when_zero: EdPoint, when_one: EdPoint) -> EdPoint:
    """`when_one` if `bit` else `when_zero`, without branching on `bit`.

    All four coordinates are already reduced into `[0, ED_P)`, so they are
    non-negative and `a ^ ((a ^ b) & mask)` selects correctly.
    """
    mask = -bit
    return (
        when_zero[0] ^ ((when_zero[0] ^ when_one[0]) & mask),
        when_zero[1] ^ ((when_zero[1] ^ when_one[1]) & mask),
        when_zero[2] ^ ((when_zero[2] ^ when_one[2]) & mask),
        when_zero[3] ^ ((when_zero[3] ^ when_one[3]) & mask),
    )


def ed_scalarmult_secret(k: int, point: EdPoint) -> EdPoint:
    """`[k]point` for a **secret** `k`, in a shape that does not depend on it.

    Double-and-add-always over exactly `_ED_SECRET_BITS` iterations: every
    iteration performs one doubling and one addition whether or not the bit is
    set, and the result is chosen with an arithmetic mask rather than an `if`.
    See the module docstring for what that does and does not promise.

    `point` must have order `ED_L` -- the RFC 8032 base point does. The scalar is
    reduced mod `ED_L` to fix the iteration count, which changes `[k]point` for a
    point with a torsion component.
    """
    m = k % ED_L + 2 * ED_L
    result = ED_NEUTRAL
    for index in range(_ED_SECRET_BITS - 1, -1, -1):
        result = ed_add(result, result)
        stepped = ed_add(result, point)
        result = _ed_select((m >> index) & 1, result, stepped)
    return result


# --- NIST P-256 / secp256r1 (FIPS 186-4, SEC1) ------------------------------

P256_P: Final = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
P256_A: Final = P256_P - 3
P256_B: Final = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
P256_N: Final = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_GX: Final = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY: Final = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5

#: The standard base point, affine.
P256_G: Final[tuple[int, int]] = (_P256_GX, _P256_GY)

#: The point at infinity in Jacobian coordinates. Any `Z == 0` is infinity;
#: this is the representative the routines below produce.
_JAC_INFINITY: Final[tuple[int, int, int]] = (1, 1, 0)

#: `p256_scalarmult_secret` runs exactly this many ladder steps. `k + n` and
#: `k + 2n` both have exactly 257 bits for `k` in `[1, n)` once the right one is
#: selected, so the count does not depend on the scalar.
_P256_SECRET_BITS: Final = 257


def p256_on_curve(x: int, y: int) -> bool:
    """Whether `(x, y)` is an affine point on P-256.

    A public key from a JWKS, a WebAuthn attestation or a browser subscription is
    attacker-supplied, and a point that is not on the curve is not a public key:
    it is a value that makes the group arithmetic below compute in a different
    and much smaller group, which is how an invalid-curve attack recovers a
    private scalar one query at a time.

    `(0, 0)` -- the conventional affine spelling of the identity, and the value
    an all-zero key encodes to -- needs no clause of its own. It fails the curve
    equation outright, because `0 == 0 + 0 + b` would require `b == 0` and P-256's
    `b` is not zero. An explicit `x == 0 and y == 0` check used to sit here; three
    mutants survived on it, and the reason was that it could never change an
    answer. `test_zero_is_refused_by_the_curve_equation_itself` records the proof
    so nobody reinstates it. Note that `x == 0` alone is *not* special: `b` is a
    quadratic residue mod `p`, so two genuine curve points have `x == 0`.
    """
    if not (0 <= x < P256_P and 0 <= y < P256_P):
        return False
    return (y * y - (x * x * x + P256_A * x + P256_B)) % P256_P == 0


def _jac_double(p: tuple[int, int, int]) -> tuple[int, int, int]:
    """Double a Jacobian point, using the `a == -3` shortcut P-256 allows.

    `alpha = 3(X - Z^2)(X + Z^2)` expands to `3X^2 - 3Z^4`, which is
    `3X^2 + a*Z^4` exactly when `a == -3`. That is a property of the curve rather
    than of this code, so it is asserted once, in
    `tests/test_ec_p256.py::test_curve_parameters_match_the_standard_and_the_doubling_shortcut`,
    instead of being re-checked on every one of the 256 calls a verification makes.

    Infinity needs no special case, which is not obvious and was a `if z1 == 0`
    guard until a mutant survived on it. Substitute `z1 = 0` into the `Z3` line:
    `(Y1 + 0)^2 - Y1^2 - 0` is zero for every `Y1`, so doubling any point at
    infinity yields a point at infinity, and the canonical `_JAC_INFINITY` is a
    fixed point exactly. `test_doubling_infinity_needs_no_special_case` records
    that rather than leaving the deletion to be re-litigated.
    """
    x1, y1, z1 = p
    delta = z1 * z1 % P256_P
    gamma = y1 * y1 % P256_P
    beta = x1 * gamma % P256_P
    alpha = 3 * (x1 - delta) * (x1 + delta) % P256_P
    x3 = (alpha * alpha - 8 * beta) % P256_P
    z3 = ((y1 + z1) * (y1 + z1) - gamma - delta) % P256_P
    y3 = (alpha * (4 * beta - x3) - 8 * gamma * gamma) % P256_P
    return (x3, y3, z3)


def _jac_add_affine(p: tuple[int, int, int], q: tuple[int, int]) -> tuple[int, int, int]:
    """Add affine `q` to Jacobian `p` (a mixed addition; `q` is never infinity).

    Taking the second operand affine removes three squarings and two
    multiplications from the general formula, which is why the callers below pay
    one inversion up front to keep their small tables affine.
    """
    x1, y1, z1 = p
    x2, y2 = q
    if z1 == 0:
        return (x2, y2, 1)
    zz = z1 * z1 % P256_P
    u2 = x2 * zz % P256_P
    s2 = y2 * z1 * zz % P256_P
    h = (u2 - x1) % P256_P
    r = (s2 - y1) % P256_P
    if h == 0:
        if r == 0:
            return _jac_double(p)
        return _JAC_INFINITY
    hh = h * h % P256_P
    i = 4 * hh % P256_P
    j = h * i % P256_P
    r = 2 * r % P256_P
    v = x1 * i % P256_P
    x3 = (r * r - j - 2 * v) % P256_P
    y3 = (r * (v - x3) - 2 * y1 * j) % P256_P
    z3 = ((z1 + h) * (z1 + h) - zz - hh) % P256_P
    return (x3, y3, z3)


def _jac_add_affine_jac(
    p: tuple[int, int, int], q: tuple[int, int, int]
) -> tuple[int, int, int]:
    """Add two Jacobian points. Neither ladder register is ever affine.

    Kept separate from `_jac_add_affine` rather than folded into it behind a
    flag. The mixed form is five field multiplications cheaper and is the one
    that runs ~195 times per verification; this general form runs only in the
    secret ladder, where neither operand can be normalised without an inversion
    per step -- which is the cost the whole module exists to avoid.
    """
    x1, y1, z1 = p
    x2, y2, z2 = q
    if z1 == 0:
        return q
    if z2 == 0:
        return p
    z1z1 = z1 * z1 % P256_P
    z2z2 = z2 * z2 % P256_P
    u1 = x1 * z2z2 % P256_P
    u2 = x2 * z1z1 % P256_P
    s1 = y1 * z2 * z2z2 % P256_P
    s2 = y2 * z1 * z1z1 % P256_P
    h = (u2 - u1) % P256_P
    r = (s2 - s1) % P256_P
    if h == 0:
        if r == 0:
            return _jac_double(p)
        return _JAC_INFINITY
    i = 4 * h * h % P256_P
    j = h * i % P256_P
    r = 2 * r % P256_P
    v = u1 * i % P256_P
    x3 = (r * r - j - 2 * v) % P256_P
    y3 = (r * (v - x3) - 2 * s1 * j) % P256_P
    z3 = ((z1 + z2) * (z1 + z2) - z1z1 - z2z2) * h % P256_P
    return (x3, y3, z3)


def _jac_to_affine(p: tuple[int, int, int]) -> tuple[int, int] | None:
    """The affine form of a Jacobian point, or `None` for the point at infinity.

    The single modular inversion of the whole operation happens here.
    """
    x, y, z = p
    if z == 0:
        return None
    zi = pow(z, -1, P256_P)
    zi2 = zi * zi % P256_P
    return (x * zi2 % P256_P, y * zi2 % P256_P * zi % P256_P)


def p256_double_scalarmult_public(
    k1: int, p1: tuple[int, int], k2: int, p2: tuple[int, int]
) -> tuple[int, int] | None:
    """Affine `[k1]p1 + [k2]p2`, or `None` for the point at infinity.

    Both scalars must be public -- this is the ECDSA verification equation, whose
    every input is published. The shape varies with the scalars: one shared
    sequence of doublings (Shamir's trick) and an addition only where a digit is
    non-zero.

    `p1` and `p2` must be affine points on the curve; the identity has no affine
    form and is not accepted. One inversion normalises `p1 + p2` so that every
    addition in the loop is a mixed one, and one more converts the result, so the
    whole operation costs two rather than the 766 the affine version needed.
    """
    both = _jac_to_affine(_jac_add_affine((p1[0], p1[1], 1), p2))
    result = _JAC_INFINITY
    for index in range(max(k1.bit_length(), k2.bit_length()) - 1, -1, -1):
        result = _jac_double(result)
        digit = ((k1 >> index) & 1) | (((k2 >> index) & 1) << 1)
        if digit == 1:
            result = _jac_add_affine(result, p1)
        elif digit == 2:
            result = _jac_add_affine(result, p2)
        elif digit == 3:
            if both is None:
                # p1 == -p2, so their sum is the identity and the digit adds
                # nothing. Unreachable from ECDSA verification, where p1 is the
                # base point and p2 an independent public key, but this function
                # is general and the alternative is a wrong answer.
                continue
            result = _jac_add_affine(result, both)
    return _jac_to_affine(result)


def _jac_select(
    bit: int, when_zero: tuple[int, int, int], when_one: tuple[int, int, int]
) -> tuple[int, int, int]:
    """`when_one` if `bit` else `when_zero`, without branching on `bit`."""
    mask = -bit
    return (
        when_zero[0] ^ ((when_zero[0] ^ when_one[0]) & mask),
        when_zero[1] ^ ((when_zero[1] ^ when_one[1]) & mask),
        when_zero[2] ^ ((when_zero[2] ^ when_one[2]) & mask),
    )


def p256_scalarmult_secret(k: int, point: tuple[int, int]) -> tuple[int, int] | None:
    """Affine `[k]point` for a **secret** `k`, in a shape that does not depend on it.

    A Montgomery ladder over exactly `_P256_SECRET_BITS` steps. The two ladder
    registers differ by `point` at every step, so they are never equal and never
    the identity for any scalar a caller can reach, and the sequence of group
    operations is the same for every scalar. See the module docstring for what
    that does and does not promise.

    `point` must be an affine point on P-256, which has prime order, so replacing
    `k` by a congruent value to fix the ladder length leaves the result alone.

    Raises:
        ValueError: `k` is not in `[1, n)`. A scalar outside that range is a
            caller bug rather than a value to compute on, and the range is
            public (it is the curve order), so refusing it leaks nothing.
    """
    if not 1 <= k < P256_N:
        raise ValueError("a P-256 scalar is in [1, n)")
    # Both branches are computed and one is selected by mask: `k + n` has 257
    # bits when it reaches 2**256 and `k + 2n` has 257 bits when it does not, so
    # the ladder length is the same for every scalar.
    plus_one = k + P256_N
    plus_two = k + 2 * P256_N
    high = (plus_one >> 256) & 1
    m = plus_two ^ ((plus_two ^ plus_one) & -high)
    r0 = (point[0], point[1], 1)
    r1 = _jac_double(r0)
    for index in range(_P256_SECRET_BITS - 2, -1, -1):
        bit = (m >> index) & 1
        a, b = _jac_select(bit, r0, r1), _jac_select(bit, r1, r0)
        b = _jac_add_affine_jac(a, b)
        a = _jac_double(a)
        r0, r1 = _jac_select(bit, a, b), _jac_select(bit, b, a)
    return _jac_to_affine(r0)
