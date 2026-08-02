"""P-256 group arithmetic, three ways it can be wrong, and one it must not be.

`wreath._curves` replaced affine P-256 addition -- one `pow(x, -1, p)` per point
addition, 766 of them per verification -- with Jacobian coordinates that invert
once at the end. That is a rewrite of the arithmetic underneath every ES256
bearer token, every WebAuthn assertion and every Web Push, so it is checked
against three things that are not it:

1. **RFC 6979 §A.2.5 known answers**, as literal data. The RFC fixes the
   keypair, the message and the resulting `(r, s)`, so these pin the primitive
   to the standard rather than to any implementation of it.
2. **The affine implementation this replaced**, transcribed verbatim below as
   `_AffineOracle` and driven over a seeded corpus. The rewrite must return the
   same verdict for every input.
3. **`cryptography`**, a declared dev/test dependency, which is a wholly
   independent implementation. It is imported here and must never be imported
   by anything under `src/wreath`.

**The rejections are tested harder than the acceptances.** A curve rewrite that
starts accepting an off-curve point, the identity, or an out-of-range `s` has
failed in the direction that matters, and a corpus of valid signatures cannot
see it.
"""

from __future__ import annotations

import hashlib
import random

import pytest

from wreath import _curves
from wreath._auth._ecverify import on_p256_curve, verify_es256

P = _curves.P256_P
N = _curves.P256_N
A = _curves.P256_A
B = _curves.P256_B
G = _curves.P256_G


# --- the implementation this replaced, kept as a differential oracle --------


class _AffineOracle:
    """P-256 exactly as `_ecverify` had it before Jacobian coordinates.

    Transcribed unchanged from the pre-rewrite `_ecverify.py` so that the corpus
    below compares two genuinely different pieces of arithmetic rather than one
    piece of arithmetic against itself. It is slow -- that is the entire point of
    having replaced it -- so the corpus is sized for it, not for the fast path.
    """

    @staticmethod
    def add(
        p: tuple[int, int] | None, q: tuple[int, int] | None
    ) -> tuple[int, int] | None:
        if p is None:
            return q
        if q is None:
            return p
        x1, y1 = p
        x2, y2 = q
        if x1 == x2 and (y1 + y2) % P == 0:
            return None
        if x1 == x2 and y1 == y2:
            lam = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
        else:
            lam = (y2 - y1) * pow((x2 - x1) % P, -1, P) % P
        x3 = (lam * lam - x1 - x2) % P
        y3 = (lam * (x1 - x3) - y1) % P
        return (x3, y3)

    @classmethod
    def mul(cls, k: int, point: tuple[int, int] | None) -> tuple[int, int] | None:
        result: tuple[int, int] | None = None
        addend = point
        while k:
            if k & 1:
                result = cls.add(result, addend)
            addend = cls.add(addend, addend)
            k >>= 1
        return result

    @staticmethod
    def on_curve(x: int, y: int) -> bool:
        if not (0 <= x < P and 0 <= y < P):
            return False
        return (y * y - (x * x * x + A * x + B)) % P == 0

    @classmethod
    def verify(cls, x: int, y: int, signing_input: bytes, signature: bytes) -> bool:
        if len(signature) != 64:
            return False
        if not cls.on_curve(x, y):
            return False
        r = int.from_bytes(signature[:32], "big")
        s = int.from_bytes(signature[32:], "big")
        if not (1 <= r < N and 1 <= s < N):
            return False
        z = int.from_bytes(hashlib.sha256(signing_input).digest(), "big")
        w = pow(s, -1, N)
        u1 = z * w % N
        u2 = r * w % N
        point = cls.add(cls.mul(u1, G), cls.mul(u2, (x, y)))
        if point is None:
            return False
        return point[0] % N == r


def _sign(private: int, message: bytes, nonce: int) -> bytes:
    """A textbook ECDSA signature, for building corpus entries.

    Deliberately the naive form -- no hedging, no low-S -- because the corpus
    wants coverage of the whole `(r, s)` space including the high half, which a
    normalising signer would never produce.
    """
    z = int.from_bytes(hashlib.sha256(message).digest(), "big")
    point = _AffineOracle.mul(nonce, G)
    assert point is not None
    r = point[0] % N
    s = pow(nonce, -1, N) * (z + r * private) % N
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


# --- 1. known answers from RFC 6979 -----------------------------------------

#: RFC 6979 §A.2.5: P-256 with SHA-256, the key `x = C9AF...6721`.
_RFC6979_UX = 0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6
_RFC6979_UY = 0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299
_RFC6979_X = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721


@pytest.mark.parametrize(
    "message, r, s",
    [
        (
            b"sample",
            0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
            0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8,
        ),
        (
            b"test",
            0xF1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367,
            0x019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083,
        ),
    ],
)
def test_rfc6979_known_answers(message: bytes, r: int, s: int) -> None:
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    assert verify_es256(_RFC6979_UX, _RFC6979_UY, message, signature)


def test_rfc6979_public_key_is_the_stated_private_scalar_times_g() -> None:
    """The vector's own consistency, which also exercises the secret ladder."""
    assert _curves.p256_scalarmult_secret(_RFC6979_X, G) == (
        _RFC6979_UX,
        _RFC6979_UY,
    )


@pytest.mark.parametrize(
    "message, r, s",
    [
        (
            b"samplE",  # one bit of the message
            0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
            0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8,
        ),
        (
            b"sample",  # one bit of r
            0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3717,
            0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8,
        ),
        (
            b"sample",  # one bit of s
            0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
            0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA9,
        ),
        (
            b"sample",  # s negated: valid ECDSA maths, wrong signature
            0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3717,
            N - 0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8,
        ),
    ],
)
def test_rfc6979_known_answers_reject_when_tampered(
    message: bytes, r: int, s: int
) -> None:
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    assert not verify_es256(_RFC6979_UX, _RFC6979_UY, message, signature)


def test_low_s_and_high_s_are_both_accepted() -> None:
    """`s` and `n - s` are both valid ECDSA and ES256 does not normalise.

    Worth pinning because the Jacobian rewrite changed how the result reaches the
    `x mod n == r` comparison. JOSE has no low-S rule; `_webpush` chooses to emit
    the low form, and a verifier that refused the high one would reject
    signatures every other implementation makes.
    """
    signature = _sign(_RFC6979_X, b"sample", 0x1234567890ABCDEF)
    r, s = signature[:32], int.from_bytes(signature[32:], "big")
    flipped = r + (N - s).to_bytes(32, "big")
    assert verify_es256(_RFC6979_UX, _RFC6979_UY, b"sample", signature)
    assert verify_es256(_RFC6979_UX, _RFC6979_UY, b"sample", flipped)


# --- 2. differential against the affine implementation ----------------------


def _corpus(seed: int, count: int) -> list[tuple[int, int, bytes, bytes]]:
    """`count` valid `(x, y, message, signature)` quadruples, seeded."""
    rng = random.Random(seed)
    out = []
    for index in range(count):
        private = rng.randrange(1, N)
        public = _AffineOracle.mul(private, G)
        assert public is not None
        message = f"corpus entry {index}".encode() + rng.randbytes(rng.randrange(0, 40))
        out.append(
            (public[0], public[1], message, _sign(private, message, rng.randrange(1, N)))
        )
    return out


def test_valid_corpus_agrees_with_the_affine_implementation() -> None:
    for x, y, message, signature in _corpus(0xC0FFEE, 12):
        assert _AffineOracle.verify(x, y, message, signature)
        assert verify_es256(x, y, message, signature)


def _mutations(
    rng: random.Random, x: int, y: int, message: bytes, signature: bytes
) -> list[tuple[int, int, bytes, bytes]]:
    """Every way this corpus entry can be broken, one at a time."""
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")

    def sig(r_: int, s_: int) -> bytes:
        return (r_ % 2**256).to_bytes(32, "big") + (s_ % 2**256).to_bytes(32, "big")

    return [
        # r and s at and around the boundaries of [1, n).
        (x, y, message, sig(0, s)),
        (x, y, message, sig(r, 0)),
        (x, y, message, sig(0, 0)),
        (x, y, message, sig(N, s)),
        (x, y, message, sig(r, N)),
        (x, y, message, sig(N + 1, s)),
        (x, y, message, sig(r, N + 1)),
        (x, y, message, sig(N - 1, s)),
        (x, y, message, sig(r, N - 1)),
        (x, y, message, sig(2**256 - 1, s)),
        (x, y, message, sig(r, 2**256 - 1)),
        # A single flipped bit somewhere in the signature.
        (x, y, message, _flip(rng, signature)),
        # A tampered message.
        (x, y, message + b"\x00", signature),
        (x, y, b"", signature),
        # The point at infinity, and points that are not on the curve.
        (0, 0, message, signature),
        (x, (y + 1) % P, message, signature),
        ((x + 1) % P, y, message, signature),
        (x, P - y, message, signature),  # on the curve, but the wrong key
        # Coordinates outside the field, which have no reduction that is correct.
        (x + P, y, message, signature),
        (x, y + P, message, signature),
        (-1, y, message, signature),
        (x, -1, message, signature),
        # Wrong-length signatures: the JOSE form is fixed-width, so a 63- or
        # 65-byte value is not a short integer, it is a malformed input.
        (x, y, message, signature[:63]),
        (x, y, message, signature + b"\x00"),
        (x, y, message, b""),
    ]


def _flip(rng: random.Random, data: bytes) -> bytes:
    index = rng.randrange(len(data))
    return data[:index] + bytes([data[index] ^ (1 << rng.randrange(8))]) + data[index + 1 :]


def test_every_mutation_is_refused_by_both_implementations() -> None:
    rng = random.Random(0xBADF00D)
    checked = 0
    for x, y, message, signature in _corpus(0xC0FFEE, 4):
        for mx, my, mm, ms in _mutations(rng, x, y, message, signature):
            assert not _AffineOracle.verify(mx, my, mm, ms), (mx, my, mm, ms)
            assert not verify_es256(mx, my, mm, ms), (mx, my, mm, ms)
            checked += 1
    assert checked == 100, "the mutation table changed size; update this count"


def test_a_63_byte_signature_is_refused_by_length_and_not_by_the_maths() -> None:
    """The length check is load-bearing, and only a constructed input shows it.

    Slicing a 63-byte value gives a 32-byte `r` and a 31-byte `s`, and when the
    real `s` is under `2**248` its big-endian encoding has a leading zero byte --
    so dropping that byte yields *the same integer*. The arithmetic then accepts
    it, and only `len(signature) != 64` refuses. JOSE fixes the width precisely so
    one signature has one encoding; without this guard a caller could re-present
    the same signature in a second form, which defeats any replay ledger keyed on
    the bytes.

    A random corpus never finds this -- it needs an `s` whose top byte is zero,
    which is one signature in 256 -- so it is searched for rather than sampled.
    """
    for nonce in range(2, 4000):
        signature = _sign(_RFC6979_X, b"sample", nonce)
        s = int.from_bytes(signature[32:], "big")
        if s >> 248 == 0:
            break
    else:  # pragma: no cover - 4000 nonces without a small s is impossible
        raise AssertionError("no signature with a leading zero byte in s")
    shortened = signature[:32] + signature[33:]
    assert len(shortened) == 63
    assert int.from_bytes(shortened[32:], "big") == s, "same integer, fewer bytes"
    assert verify_es256(_RFC6979_UX, _RFC6979_UY, b"sample", signature)
    assert not verify_es256(_RFC6979_UX, _RFC6979_UY, b"sample", shortened)
    assert not _AffineOracle.verify(_RFC6979_UX, _RFC6979_UY, b"sample", shortened)


def test_a_signature_driving_the_sum_to_infinity_is_refused() -> None:
    """`[u1]G + [u2]Q` really can be the identity, on input anyone can build.

    Choose `r = -z/d mod n` for a key whose private scalar `d` you know and any
    `s`: then `u1 + u2*d == 0 mod n`, so the two multiples cancel exactly and the
    verification equation has no affine `x` to compare. No random signature ever
    lands here, so without this input the `point is None` branch is dead code
    that a mutant deletes for free -- and deleting it turns a refusal into a
    `TypeError` on `None`, which is a 500 rather than a 401.
    """
    private = _RFC6979_X
    z = int.from_bytes(hashlib.sha256(b"infinity").digest(), "big")
    r = -z * pow(private, -1, N) % N
    s = 12345
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    w = pow(s, -1, N)
    assert (
        _curves.p256_double_scalarmult_public(
            z * w % N, G, r * w % N, (_RFC6979_UX, _RFC6979_UY)
        )
        is None
    ), "the construction no longer reaches the identity"
    assert not verify_es256(_RFC6979_UX, _RFC6979_UY, b"infinity", signature)
    assert not _AffineOracle.verify(_RFC6979_UX, _RFC6979_UY, b"infinity", signature)


def test_on_curve_agrees_with_the_affine_predicate_including_infinity() -> None:
    """`on_p256_curve` differs from the raw curve equation at exactly one point.

    `(0, 0)` satisfies neither `y^2 = x^3 + ax + b` nor anything else -- it is
    the conventional affine spelling of the identity -- so both must refuse it,
    and the oracle's plain equation is checked against that separately.
    """
    rng = random.Random(0x5EED)
    for _ in range(30):
        private = rng.randrange(1, N)
        point = _AffineOracle.mul(private, G)
        assert point is not None
        assert on_p256_curve(*point)
        assert _AffineOracle.on_curve(*point)
        assert not on_p256_curve(point[0], (point[1] + 1) % P)
        assert not _AffineOracle.on_curve(point[0], (point[1] + 1) % P)
    assert not on_p256_curve(0, 0)
    assert not _AffineOracle.on_curve(0, 0)  # (0,0) is not on P-256 either way


# --- 3. cross-check against `cryptography` ----------------------------------

cryptography = pytest.importorskip(
    "cryptography",
    reason=(
        "cryptography is a declared dev-group dependency; if it is missing the "
        "venv was reconciled with a bare `uv sync --group X`, which evicts it"
    ),
)


def test_cryptography_signatures_verify_and_tampered_ones_do_not() -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    rng = random.Random(0x0DDBA11)
    for index in range(8):
        key = ec.generate_private_key(ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        message = f"cross-check {index}".encode()
        r, s = utils.decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        assert verify_es256(numbers.x, numbers.y, message, signature)
        assert not verify_es256(numbers.x, numbers.y, message + b"!", signature)
        assert not verify_es256(numbers.x, numbers.y, message, _flip(rng, signature))


def test_wreath_signatures_verify_under_cryptography() -> None:
    """The other direction: what `_webpush` emits must satisfy a real verifier."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    from wreath._webpush import _ecdsa_sign

    for index in range(6):
        private = random.Random(0xF00D + index).randrange(1, N)
        point = _curves.p256_scalarmult_secret(private, G)
        assert point is not None
        message = f"emitted {index}".encode()
        digest = hashlib.sha256(message).digest()
        raw = _ecdsa_sign(private, digest)
        der = utils.encode_dss_signature(
            int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        )
        public = ec.EllipticCurvePublicNumbers(
            point[0], point[1], ec.SECP256R1()
        ).public_key()
        public.verify(der, message, ec.ECDSA(hashes.SHA256()))
        with pytest.raises(InvalidSignature):
            public.verify(der, message + b"!", ec.ECDSA(hashes.SHA256()))


# --- the group law itself ---------------------------------------------------


def test_curve_parameters_match_the_standard_and_the_doubling_shortcut() -> None:
    """`_jac_double` expands `3X^2 + aZ^4` as `3(X - Z^2)(X + Z^2)`.

    That identity holds only for `a == -3`, which is a property of P-256 rather
    than of the code, so it is checked once here instead of in the hot function.
    """
    assert A == P - 3
    assert (A - (-3)) % P == 0
    assert _curves.p256_on_curve(*G)
    assert (B * B * B * 4 + 27 * B * B) % P != 0  # non-singular
    # G has order n: [n]G is the identity, so [n-1]G is -G.
    assert _curves.p256_double_scalarmult_public(N - 1, G, 0, G) == (G[0], P - G[1])


def test_zero_is_refused_by_the_curve_equation_itself() -> None:
    """Why `p256_on_curve` has no `(0, 0)` clause, recorded so none comes back.

    An explicit clause sat here and three mutants survived on it: removing it,
    and dropping either half of it, changed no answer any test could see. The
    reason is algebraic rather than accidental -- `(0, 0)` on the curve would need
    `0 == b`, and P-256's `b` is not zero -- so the clause was two spellings of
    one condition, which is how they drift apart later.

    The second half of the test is the trap that makes the first half worth
    stating: `x == 0` is *not* an invalid coordinate. `b` is a quadratic residue
    mod `p`, so there are two real curve points with `x == 0`, and a guard written
    as `x == 0` rather than `x == 0 and y == 0` would reject them.
    """
    assert not on_p256_curve(0, 0)
    assert not _AffineOracle.on_curve(0, 0)
    assert (0 - (0 + 0 + B)) % P != 0
    root = pow(B, (P + 1) // 4, P)
    assert root * root % P == B % P, "b is a QR mod p, so x == 0 has real points"
    assert on_p256_curve(0, root)
    assert on_p256_curve(0, P - root)


def test_doubling_infinity_needs_no_special_case() -> None:
    """Why `_jac_double` has no `z1 == 0` guard.

    `Z3 = (Y1 + Z1)^2 - Y1^2 - Z1^2` collapses to zero whenever `Z1` is zero, so
    the unguarded formula already maps every point at infinity to a point at
    infinity, and the canonical representative to itself. A guard survived as a
    mutant for exactly that reason. Driven over non-canonical infinities too,
    because the canonical one alone cannot distinguish the two versions.
    """
    assert _curves._jac_double(_curves._JAC_INFINITY) == _curves._JAC_INFINITY
    rng = random.Random(0x1FF1)
    for _ in range(40):
        junk = (rng.randrange(P), rng.randrange(P), 0)
        assert _curves._jac_double(junk)[2] == 0
        assert _curves._jac_to_affine(_curves._jac_double(junk)) is None


def test_jacobian_addition_matches_affine_addition_over_a_corpus() -> None:
    rng = random.Random(0xACE)
    for _ in range(25):
        p = _AffineOracle.mul(rng.randrange(1, N), G)
        q = _AffineOracle.mul(rng.randrange(1, N), G)
        assert p is not None and q is not None
        expected = _AffineOracle.add(p, q)
        got = _curves._jac_to_affine(_curves._jac_add_affine((p[0], p[1], 1), q))
        assert got == expected


def test_jacobian_addition_handles_doubling_and_the_identity() -> None:
    """The three cases a general addition formula gets wrong if it forgets them."""
    p = _AffineOracle.mul(7, G)
    assert p is not None
    doubled = _curves._jac_to_affine(_curves._jac_add_affine((p[0], p[1], 1), p))
    assert doubled == _AffineOracle.add(p, p)
    negated = (p[0], P - p[1])
    assert _curves._jac_to_affine(_curves._jac_add_affine((p[0], p[1], 1), negated)) is None
    from_infinity = _curves._jac_add_affine(_curves._JAC_INFINITY, p)
    assert _curves._jac_to_affine(from_infinity) == p


def test_double_scalarmult_matches_two_separate_multiplications() -> None:
    rng = random.Random(0xB0A7)
    q = _AffineOracle.mul(rng.randrange(1, N), G)
    assert q is not None
    for k1, k2 in [
        (0, 0),
        (1, 0),
        (0, 1),
        (1, 1),
        (N - 1, N - 1),
        (rng.randrange(1, N), rng.randrange(1, N)),
        (rng.randrange(1, N), rng.randrange(1, N)),
    ]:
        expected = _AffineOracle.add(_AffineOracle.mul(k1, G), _AffineOracle.mul(k2, q))
        assert _curves.p256_double_scalarmult_public(k1, G, k2, q) == expected


def test_double_scalarmult_survives_the_two_points_cancelling() -> None:
    """`p1 + p2` is the identity when `p2 == -p1`, and the digit-3 table entry
    is then unrepresentable in affine form. ECDSA verification never reaches it
    -- `p1` is the base point and `p2` an independent key -- but the function is
    general, and the alternative to handling it is a wrong answer rather than an
    error."""
    minus_g = (G[0], P - G[1])
    for k1, k2 in [(3, 3), (0xFFFF, 0xFFFF), (0b1011, 0b1011)]:
        expected = _AffineOracle.add(
            _AffineOracle.mul(k1, G), _AffineOracle.mul(k2, minus_g)
        )
        assert _curves.p256_double_scalarmult_public(k1, G, k2, minus_g) == expected


def test_secret_scalarmult_matches_the_affine_one() -> None:
    rng = random.Random(0xDEC0DE)
    scalars = [1, 2, 3, N - 1, N - 2, 2**255, *(rng.randrange(1, N) for _ in range(8))]
    for k in scalars:
        assert _curves.p256_scalarmult_secret(k, G) == _AffineOracle.mul(k, G)


def test_secret_scalarmult_refuses_a_scalar_outside_the_group_order() -> None:
    """A real `raise`, not an `assert`: `python -O` would strip an assert.

    The bound is public -- it is the curve order -- so refusing loudly leaks
    nothing, and the alternative is a ladder whose length depends on how far out
    of range the caller went.
    """
    for k in (0, -1, N, N + 1, 2**300):
        with pytest.raises(ValueError, match=r"P-256 scalar is in \[1, n\)"):
            _curves.p256_scalarmult_secret(k, G)


# --- the shape of the secret path -------------------------------------------


def _group_operation_trace(run) -> list[str]:
    """Record the sequence of P-256 group operations `run()` performs.

    Each call gets its own `MonkeyPatch` context. Sharing the test's fixture
    across several calls would wrap the already-wrapped function and count every
    operation once per call made so far, which reads as a variable-shape ladder
    and is really a leaky harness.
    """
    trace: list[str] = []
    with pytest.MonkeyPatch.context() as patch:
        for name in ("_jac_double", "_jac_add_affine_jac", "_jac_add_affine"):
            original = getattr(_curves, name)

            def counted(*args, _name=name, _original=original, **kwargs):
                trace.append(_name)
                return _original(*args, **kwargs)

            patch.setattr(_curves, name, counted)
        run()
    return trace


def test_the_secret_ladder_runs_the_same_operations_for_every_scalar() -> None:
    """The property `p256_scalarmult_secret` exists to have.

    The old `while k: if k & 1:` leaked the scalar's bit length through the
    iteration count and its Hamming weight through the branch. This asserts the
    replacement is free of both: the *sequence* of group operations, not merely
    its length, is identical for a 1-bit scalar, an all-ones scalar and `n - 1`.

    It does not assert constant time, which pure Python cannot deliver --
    CPython's big-integer multiply is faster on smaller operands. See
    `wreath._curves`'s module docstring.
    """
    traces = {
        k: _group_operation_trace(lambda k=k: _curves.p256_scalarmult_secret(k, G))
        for k in (1, 2, (1 << 255) - 1, N - 1, 0x5EED5EED, N // 2)
    }
    reference = traces[1]
    assert len(reference) == 2 * 256 + 1  # one doubling to start, then 256 steps
    for k, trace in traces.items():
        assert trace == reference, f"scalar {k:#x} took a different path"


def test_the_public_path_is_allowed_to_vary_with_its_scalar() -> None:
    """The other half of the contract, asserted so the two cannot be confused.

    `p256_double_scalarmult_public` skips zero digits on purpose: its scalars are
    `u1` and `u2` from an ECDSA verification, both derived from published values.
    If this ever stops varying, someone has made verification pay for a guarantee
    it does not need -- and if `p256_scalarmult_secret` ever starts varying, the
    test above goes red.
    """
    q = _AffineOracle.mul(9, G)
    assert q is not None
    sparse = _group_operation_trace(
        lambda: _curves.p256_double_scalarmult_public(1 << 200, G, 0, q)
    )
    dense = _group_operation_trace(
        lambda: _curves.p256_double_scalarmult_public((1 << 201) - 1, G, 0, q)
    )
    assert len(sparse) < len(dense)


def test_verification_no_longer_inverts_once_per_addition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the rewrite, asserted rather than benchmarked.

    A benchmark would be a claim about a machine. This is a claim about the
    algorithm: one ES256 verification performs a bounded, tiny number of modular
    inversions -- two, one to normalise the joint table entry and one to leave
    Jacobian coordinates -- instead of one per point addition. The affine
    original did 775 for this same input, which is what made it 8x slower.
    """
    inversions = 0
    real_pow = pow

    def counting_pow(base, exponent, modulus=None):
        nonlocal inversions
        if exponent == -1:
            inversions += 1
        return real_pow(base, exponent, modulus)

    monkeypatch.setattr(_curves, "pow", counting_pow, raising=False)
    signature = (
        (0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716).to_bytes(
            32, "big"
        )
        + (0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8).to_bytes(
            32, "big"
        )
    )
    assert verify_es256(_RFC6979_UX, _RFC6979_UY, b"sample", signature)
    assert inversions == 2
