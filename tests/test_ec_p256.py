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


def _sign(private: int, message: bytes, nonce: int) -> bytes:
    """A textbook ECDSA signature, for building corpus entries.

    Deliberately the naive form -- no hedging, no low-S -- because the corpus
    wants coverage of the whole `(r, s)` space including the high half, which a
    normalising signer would never produce.
    """
    z = int.from_bytes(hashlib.sha256(message).digest(), "big")
    point = _curves.p256_scalarmult_secret(nonce, G)
    assert point is not None
    r = point[0] % N
    s = pow(nonce, -1, N) * (z + r * private) % N
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


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
def test_rfc6979_known_answers_reject_when_tampered(message: bytes, r: int, s: int) -> None:
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    assert not verify_es256(_RFC6979_UX, _RFC6979_UY, message, signature)


def test_low_s_and_high_s_are_both_accepted() -> None:
    signature = _sign(_RFC6979_X, b"sample", 0x1234567890ABCDEF)
    r, s = signature[:32], int.from_bytes(signature[32:], "big")
    flipped = r + (N - s).to_bytes(32, "big")
    assert verify_es256(_RFC6979_UX, _RFC6979_UY, b"sample", signature)
    assert verify_es256(_RFC6979_UX, _RFC6979_UY, b"sample", flipped)


def _independent_public(scalar: int) -> tuple[int, int] | None:
    """Return ``[scalar]G`` from the independent test dependency."""
    from cryptography.hazmat.primitives.asymmetric import ec

    scalar %= N
    if scalar == 0:
        return None
    numbers = ec.derive_private_key(scalar, ec.SECP256R1()).public_key().public_numbers()
    return (numbers.x, numbers.y)


def _corpus(seed: int, count: int) -> list[tuple[int, int, bytes, bytes]]:
    """`count` valid `(x, y, message, signature)` quadruples, seeded."""
    rng = random.Random(seed)
    out = []
    for index in range(count):
        private = rng.randrange(1, N)
        public = _independent_public(private)
        assert public is not None
        message = f"corpus entry {index}".encode() + rng.randbytes(rng.randrange(0, 40))
        out.append((public[0], public[1], message, _sign(private, message, rng.randrange(1, N))))
    return out


def test_valid_seeded_corpus_verifies() -> None:
    for x, y, message, signature in _corpus(0xC0FFEE, 12):
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


def test_every_seeded_mutation_is_refused() -> None:
    rng = random.Random(0xBADF00D)
    checked = 0
    for x, y, message, signature in _corpus(0xC0FFEE, 4):
        for mx, my, mm, ms in _mutations(rng, x, y, message, signature):
            assert not verify_es256(mx, my, mm, ms), (mx, my, mm, ms)
            checked += 1
    assert checked == 100, "the mutation table changed size; update this count"


def test_a_63_byte_signature_is_refused_by_length_and_not_by_the_maths() -> None:
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


def test_a_signature_driving_the_sum_to_infinity_is_refused() -> None:
    private = _RFC6979_X
    z = int.from_bytes(hashlib.sha256(b"infinity").digest(), "big")
    r = -z * pow(private, -1, N) % N
    s = 12345
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    w = pow(s, -1, N)
    assert (
        _curves.p256_double_scalarmult_public(z * w % N, G, r * w % N, (_RFC6979_UX, _RFC6979_UY))
        is None
    ), "the construction no longer reaches the identity"
    assert not verify_es256(_RFC6979_UX, _RFC6979_UY, b"infinity", signature)


def test_on_curve_accepts_independent_points_and_refuses_nearby_values() -> None:
    rng = random.Random(0x5EED)
    for _ in range(30):
        private = rng.randrange(1, N)
        point = _independent_public(private)
        assert point is not None
        assert on_p256_curve(*point)
        assert not on_p256_curve(point[0], (point[1] + 1) % P)
    assert not on_p256_curve(0, 0)


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
        public = ec.EllipticCurvePublicNumbers(point[0], point[1], ec.SECP256R1()).public_key()
        public.verify(der, message, ec.ECDSA(hashes.SHA256()))
        with pytest.raises(InvalidSignature):
            public.verify(der, message + b"!", ec.ECDSA(hashes.SHA256()))


def test_curve_parameters_match_the_standard_and_the_doubling_shortcut() -> None:
    assert A == P - 3
    assert (A - (-3)) % P == 0
    assert _curves.p256_on_curve(*G)
    assert (B * B * B * 4 + 27 * B * B) % P != 0  # non-singular
    # G has order n: [n]G is the identity, so [n-1]G is -G.
    assert _curves.p256_double_scalarmult_public(N - 1, G, 0, G) == (G[0], P - G[1])


def test_zero_is_refused_by_the_curve_equation_itself() -> None:
    assert not on_p256_curve(0, 0)
    assert (0 - (0 + 0 + B)) % P != 0
    root = pow(B, (P + 1) // 4, P)
    assert root * root % P == B % P, "b is a QR mod p, so x == 0 has real points"
    assert on_p256_curve(0, root)
    assert on_p256_curve(0, P - root)


def test_double_scalarmult_matches_two_separate_multiplications() -> None:
    rng = random.Random(0xB0A7)
    q_scalar = rng.randrange(1, N)
    q = _independent_public(q_scalar)
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
        expected = _independent_public(k1 + k2 * q_scalar)
        assert _curves.p256_double_scalarmult_public(k1, G, k2, q) == expected


def test_double_scalarmult_survives_the_two_points_cancelling() -> None:
    minus_g = (G[0], P - G[1])
    for k1, k2 in [(3, 3), (0xFFFF, 0xFFFF), (0b1011, 0b1011)]:
        assert k1 == k2
        assert _curves.p256_double_scalarmult_public(k1, G, k2, minus_g) is None


def test_secret_scalarmult_matches_cryptography() -> None:
    rng = random.Random(0xDEC0DE)
    scalars = [1, 2, 3, N - 1, N - 2, 2**255, *(rng.randrange(1, N) for _ in range(8))]
    for k in scalars:
        assert _curves.p256_scalarmult_secret(k, G) == _independent_public(k)


def test_secret_scalarmult_refuses_a_scalar_outside_the_group_order() -> None:
    for k in (0, -1, N, N + 1, 2**300):
        with pytest.raises(ValueError, match=r"P-256 scalar is in \[1, n\)"):
            _curves.p256_scalarmult_secret(k, G)
