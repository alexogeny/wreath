"""edwards25519, and the one substitution that would have quietly broken it.

Ed25519 verification was already inversion-free -- extended coordinates, no
`pow(x, -1, p)` in the loop -- so the change here is smaller than the P-256 one:
`[S]B == R + [h]A` became `[S]B + [h](-A) == R`, which shares one sequence of
doublings between the two scalar multiplications instead of running two. The
step count falls; the arithmetic does not change.

**The hazard that shaped this file.** The obvious way to write that rewrite is
`[S]B + [L - h]A`, because `-[h]A == [L - h]A` whenever `A` has order dividing
`L`. RFC 8032 cofactorless verification *accepts a public key with a torsion
component*, for which the two are different points, so the substitution would
silently change which signatures verify -- accepting or rejecting where the
shipped implementation did the opposite. `ed_negate` is exact for every point,
and `test_torsion_keys_agree_with_the_two_multiplication_form` is what holds the
line: it drives small-order and mixed-order keys, where a wrong rewrite differs,
through both forms.

Checked against three things that are not this code: the RFC 8032 §7.1 vectors
as literal data, the two-multiplication implementation that shipped before,
and `cryptography` (a dev/test dependency, never importable from `src/wreath`).
"""

from __future__ import annotations

import hashlib
import random

import pytest

from wreath import _curves
from wreath._auth._ecverify import verify_ed25519
from wreath._dkim import _ed25519_sign, ed25519_public_key

P = _curves.ED_P
L = _curves.ED_L


# --- the implementation this replaced, kept as a differential oracle --------


def _verify_two_multiplications(public: bytes, message: bytes, signature: bytes) -> bool:
    """`verify_ed25519` exactly as it read before the interleaved rewrite.

    Two independent scalar multiplications and an equality, transcribed from the
    pre-rewrite `_ecverify.py`. Everything else in this file compares against it.
    """
    if len(public) != 32 or len(signature) != 64:
        return False
    a_point = _curves.ed_decode_point(public)
    if a_point is None:
        return False
    r_bytes = signature[:32]
    r_point = _curves.ed_decode_point(r_bytes)
    if r_point is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    h = int.from_bytes(hashlib.sha512(r_bytes + public + message).digest(), "little") % L
    sb = _curves.ed_scalarmult_public(s, _curves.ed_base())
    rha = _curves.ed_add(r_point, _curves.ed_scalarmult_public(h, a_point))
    return _curves.ed_equal(sb, rha)


# --- 1. known answers from RFC 8032 §7.1 ------------------------------------

_RFC8032 = [
    # (seed, public, message, signature) -- TEST 1, 2, 3 and SHA(abc).
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590"
        "a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e"
        "15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d"
        "16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
    (
        "833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
        "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
        "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a27"
        "4fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
        "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac"
        "90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704",
    ),
]


@pytest.mark.parametrize(("seed", "public", "message", "signature"), _RFC8032)
def test_rfc8032_vectors_verify(
    seed: str, public: str, message: str, signature: str
) -> None:
    assert verify_ed25519(
        bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)
    )


@pytest.mark.parametrize(("seed", "public", "message", "signature"), _RFC8032)
def test_rfc8032_vectors_are_reproduced_by_the_signer(
    seed: str, public: str, message: str, signature: str
) -> None:
    """`_dkim` signs with `ed_scalarmult_secret`, so this pins the ladder too.

    A signing known-answer test is strictly stronger than a verifying one: it
    fixes every intermediate, so a scalar multiplication that is wrong anywhere
    produces different bytes rather than a result that still satisfies the
    verification equation.
    """
    assert ed25519_public_key(bytes.fromhex(seed)).hex() == public
    assert _ed25519_sign(bytes.fromhex(seed), bytes.fromhex(message)).hex() == signature


@pytest.mark.parametrize(("seed", "public", "message", "signature"), _RFC8032)
def test_rfc8032_vectors_reject_a_tampered_message(
    seed: str, public: str, message: str, signature: str
) -> None:
    assert not verify_ed25519(
        bytes.fromhex(public),
        bytes.fromhex(message) + b"\x00",
        bytes.fromhex(signature),
    )


# --- 2. differential against the two-multiplication form --------------------


def _corpus(seed: int, count: int) -> list[tuple[bytes, bytes, bytes]]:
    rng = random.Random(seed)
    out = []
    for index in range(count):
        key = rng.randbytes(32)
        message = f"corpus {index}".encode() + rng.randbytes(rng.randrange(0, 40))
        out.append((ed25519_public_key(key), message, _ed25519_sign(key, message)))
    return out


def test_valid_corpus_agrees_with_the_two_multiplication_form() -> None:
    for public, message, signature in _corpus(0xE0FEED, 10):
        assert _verify_two_multiplications(public, message, signature)
        assert verify_ed25519(public, message, signature)


def _mutations(
    rng: random.Random, public: bytes, message: bytes, signature: bytes
) -> list[tuple[bytes, bytes, bytes]]:
    s = int.from_bytes(signature[32:], "little")
    r_half = signature[:32]

    def sig(r: bytes, s_: int) -> bytes:
        return r + (s_ % 2**256).to_bytes(32, "little")

    return [
        # S at and past the reduction bound. RFC 8032 requires S < L, and
        # accepting S + L would make every signature malleable.
        (public, message, sig(r_half, s + L)),
        (public, message, sig(r_half, L)),
        (public, message, sig(r_half, L + 1)),
        (public, message, sig(r_half, 2**256 - 1)),
        (public, message, sig(r_half, s + 1)),
        # A flipped bit anywhere in the signature or the key.
        (public, message, _flip(rng, signature)),
        (_flip(rng, public), message, signature),
        # A tampered message.
        (public, message + b"\x00", signature),
        (public, b"", signature),
        # Non-canonical and impossible point encodings in R and in A. `y >= p`
        # names no point; `ed_recover_x` must refuse rather than reduce.
        (public, message, sig((P + 1).to_bytes(32, "little"), s)),
        (public, message, sig((2**255 - 1).to_bytes(32, "little"), s)),
        ((P + 1).to_bytes(32, "little"), message, signature),
        (bytes(32), message, signature),  # y = 0, which is not on the curve
        # Wrong lengths, in both arguments.
        (public, message, signature[:63]),
        (public, message, signature + b"\x00"),
        (public, message, b""),
        (public[:31], message, signature),
        (public + b"\x00", message, signature),
    ]


def _flip(rng: random.Random, data: bytes) -> bytes:
    index = rng.randrange(len(data))
    return data[:index] + bytes([data[index] ^ (1 << rng.randrange(8))]) + data[index + 1 :]


def test_every_mutation_is_refused_by_both_implementations() -> None:
    rng = random.Random(0xFEEDBEE)
    checked = 0
    for public, message, signature in _corpus(0xE0FEED, 4):
        for mp, mm, ms in _mutations(rng, public, message, signature):
            assert not _verify_two_multiplications(mp, mm, ms), (mp, mm, ms)
            assert not verify_ed25519(mp, mm, ms), (mp, mm, ms)
            checked += 1
    assert checked == 72, "the mutation table changed size; update this count"


def test_a_31_byte_public_key_is_refused_by_length_and_not_by_the_maths() -> None:
    """The length check on the *key* is load-bearing, and needs a forged input.

    A public key whose last byte is zero decodes identically with that byte
    removed -- the encoding is little-endian, so the final byte carries the sign
    bit and the top bits of `y`. Truncating a real key does not produce a valid
    signature, because the key goes into the challenge hash and the hash changes.
    But an attacker signs *for the short form*, and then the arithmetic accepts
    it: only `len(public) != 32` refuses.

    That matters because the key is the identity. Two encodings of one key mean
    two spellings of one principal, and anything that pins, caches, or revokes by
    the key bytes sees them as different.
    """
    for index in range(3000):
        seed = hashlib.sha256(b"seed %d" % index).digest()
        public = ed25519_public_key(seed)
        if public[31] == 0:
            break
    else:  # pragma: no cover - one key in 256 ends in a zero byte
        raise AssertionError("no public key ending in a zero byte")
    short = public[:31]
    assert _curves.ed_equal(
        _curves.ed_decode_point(public), _curves.ed_decode_point(short + b"\x00")
    )

    # Sign for the short form: the same algorithm with the 31-byte key in the
    # challenge hash, which is what an attacker presenting one would compute.
    digest = hashlib.sha512(seed).digest()
    scalar = (int.from_bytes(digest[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    message = b"short key"
    nonce = int.from_bytes(
        hashlib.sha512(digest[32:] + message).digest(), "little"
    ) % L
    r_bytes = _curves.ed_encode_point(
        _curves.ed_scalarmult_secret(nonce, _curves.ed_base())
    )
    challenge = int.from_bytes(
        hashlib.sha512(r_bytes + short + message).digest(), "little"
    ) % L
    forged = r_bytes + ((nonce + challenge * scalar) % L).to_bytes(32, "little")

    a_point = _curves.ed_decode_point(short)
    assert a_point is not None
    assert _curves.ed_equal(
        _curves.ed_double_scalarmult_public(
            int.from_bytes(forged[32:], "little"),
            _curves.ed_base(),
            challenge,
            _curves.ed_negate(a_point),
        ),
        _curves.ed_decode_point(forged[:32]),
    ), "the forgery no longer satisfies the equation, so this proves nothing"
    assert not verify_ed25519(short, message, forged)
    assert not _verify_two_multiplications(short, message, forged)


def _small_order_points() -> list[bytes]:
    """The eight points of the torsion subgroup, as RFC 8032 encodings.

    Derived rather than tabulated: `[L]Q` lands in the torsion subgroup for any
    `Q`, because the curve has order `8L`, so decoding random 32-byte strings and
    multiplying by `L` enumerates all eight. Deriving them means the list cannot
    drift away from the constants it belongs to -- a tabulated one would still
    look right after someone changed `ED_L`.
    """
    rng = random.Random(0x707510)
    found: dict[bytes, int] = {}
    while len(found) < 8:
        point = _curves.ed_decode_point(rng.randbytes(32))
        if point is None:
            continue
        torsion = _curves.ed_scalarmult_public(L, point)
        found[_curves.ed_encode_point(torsion)] = _order_of(torsion)
    assert sorted(set(found.values())) == [1, 2, 4, 8], found
    return sorted(found)


def _order_of(point: _curves.EdPoint) -> int:
    """The order of a torsion point, which is 1, 2, 4 or 8 by construction."""
    accumulated = _curves.ED_NEUTRAL
    for index in range(1, 9):
        accumulated = _curves.ed_add(accumulated, point)
        if _curves.ed_equal(accumulated, _curves.ED_NEUTRAL):
            return index
    raise AssertionError("a point of order greater than 8 is not torsion")


def _forge_small_order_signature(public: bytes) -> tuple[bytes, bytes] | None:
    """A `(message, signature)` a small-order `public` actually verifies.

    This is what makes the torsion test bite. Comparing two implementations on
    random signatures under a torsion key compares reject against reject and
    proves nothing; a rewrite could be arbitrarily wrong and still agree. So the
    signature is constructed to be *accepted*: with `S = 0` the check collapses
    to `R == [-h]A`, and since `A` has order at most 8 there are only that many
    candidate `R` values. Trying messages until the hash lands on the right one
    takes a handful of iterations.

    Returns `None` for the identity, whose whole subgroup is one point, so no
    signature can distinguish anything.
    """
    point = _curves.ed_decode_point(public)
    assert point is not None
    order = _order_of(point)
    if order == 1:
        return None
    candidates = []
    accumulated = _curves.ED_NEUTRAL
    for multiple in range(order):
        candidates.append((multiple, _curves.ed_encode_point(accumulated)))
        accumulated = _curves.ed_add(accumulated, point)
    for attempt in range(5000):
        message = b"forged small-order %d" % attempt
        for multiple, r_bytes in candidates:
            h = int.from_bytes(
                hashlib.sha512(r_bytes + public + message).digest(), "little"
            ) % L
            if (multiple + h) % order == 0:
                return message, r_bytes + bytes(32)
    raise AssertionError("no forgery found; the search above is broken")


def _verify_with_the_unsafe_shortcut(
    public: bytes, message: bytes, signature: bytes
) -> bool:
    """`[S]B + [L - h]A == R` -- the tempting rewrite, which is wrong.

    Present so the test below can show it differs, rather than only asserting
    that the shipped code agrees with itself. Never called by anything shipped.
    """
    a_point = _curves.ed_decode_point(public)
    r_point = _curves.ed_decode_point(signature[:32])
    if a_point is None or r_point is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    h = int.from_bytes(
        hashlib.sha512(signature[:32] + public + message).digest(), "little"
    ) % L
    left = _curves.ed_double_scalarmult_public(
        s, _curves.ed_base(), (L - h) % L, a_point
    )
    return _curves.ed_equal(left, r_point)


def test_torsion_keys_agree_with_the_two_multiplication_form() -> None:
    """The test this file exists for, on inputs where a wrong rewrite differs.

    A public key of small order has `[L]A != identity`, so `[L - h]A` and
    `-[h]A` are different points. RFC 8032 cofactorless verification accepts such
    a key -- a JWKS entry or a WebAuthn credential can carry any 32 bytes that
    decode -- so a rewrite reaching for the first would change the verdict on
    attacker-chosen input.

    The assertion is *agreement with what shipped*, not acceptance: whether
    cofactorless Ed25519 ought to accept these is RFC 8032's decision and this
    work does not touch it. What must not change is which side of it wreath
    lands on.
    """
    distinguished = 0
    for public in _small_order_points():
        forged = _forge_small_order_signature(public)
        if forged is None:
            continue
        message, signature = forged
        assert _verify_two_multiplications(public, message, signature)
        assert verify_ed25519(public, message, signature)
        if not _verify_with_the_unsafe_shortcut(public, message, signature):
            distinguished += 1
    assert distinguished == 7, (
        "these inputs no longer separate ed_negate from the [L - h] shortcut, so "
        "this test would pass for an implementation using the wrong one"
    )


def test_torsion_keys_agree_on_signatures_that_are_simply_wrong() -> None:
    """The same keys with random signatures: both must refuse, in step."""
    rng = random.Random(0x717D)
    checked = 0
    for public in _small_order_points():
        for index in range(3):
            message = f"torsion {index}".encode()
            signature = rng.randbytes(32) + rng.randrange(0, L).to_bytes(32, "little")
            assert not verify_ed25519(public, message, signature)
            assert not _verify_two_multiplications(public, message, signature)
            checked += 1
    assert checked == 24


def test_a_genuine_signature_does_not_verify_under_a_torsion_shifted_key() -> None:
    """Key substitution: `A + T` must not inherit `A`'s signatures.

    This does not separate the two rewrite forms -- the hash binds the public key,
    so shifting it changes `h` and the equation fails for both -- and it is here
    because it is the property an attacker would actually want. Stated plainly so
    nobody later reads it as the differential test; that one is above.
    """
    seed = bytes(range(32))
    genuine = ed25519_public_key(seed)
    a_point = _curves.ed_decode_point(genuine)
    assert a_point is not None
    checked = 0
    for encoded in _small_order_points():
        torsion = _curves.ed_decode_point(encoded)
        assert torsion is not None
        shifted = _curves.ed_encode_point(_curves.ed_add(a_point, torsion))
        for index in range(3):
            message = f"substituted {index}".encode()
            signature = _ed25519_sign(seed, message)
            assert verify_ed25519(genuine, message, signature)
            expected = _verify_two_multiplications(shifted, message, signature)
            assert verify_ed25519(shifted, message, signature) == expected
            assert expected == (shifted == genuine)
            checked += 1
    assert checked == 24


# --- 3. cross-check against `cryptography` ----------------------------------

cryptography = pytest.importorskip(
    "cryptography",
    reason=(
        "cryptography is a declared dev-group dependency; if it is missing the "
        "venv was reconciled with a bare `uv sync --group X`, which evicts it"
    ),
)


def test_cryptography_signatures_verify_and_tampered_ones_do_not() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    rng = random.Random(0xC0FFEE)
    for index in range(8):
        key = ed25519.Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        message = f"cross-check {index}".encode()
        signature = key.sign(message)
        assert verify_ed25519(public, message, signature)
        assert not verify_ed25519(public, message + b"!", signature)
        assert not verify_ed25519(public, message, _flip(rng, signature))


def test_wreath_signatures_verify_under_cryptography() -> None:
    """The other direction: what `_dkim`'s constant-shape ladder emits is real."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ed25519

    for index in range(6):
        seed = random.Random(0xBEEF + index).randbytes(32)
        message = f"emitted {index}".encode()
        signature = _ed25519_sign(seed, message)
        public = ed25519.Ed25519PublicKey.from_public_bytes(ed25519_public_key(seed))
        public.verify(signature, message)
        with pytest.raises(InvalidSignature):
            public.verify(signature, message + b"!")


# --- the group law itself ---------------------------------------------------


def test_curve_parameters_match_rfc_8032() -> None:
    assert P == 2**255 - 19
    assert L == 2**252 + 27742317777372353535851937790883648493
    assert _curves.ED_D == (-121665 * pow(121666, -1, P)) % P
    assert _curves._ED_I * _curves._ED_I % P == P - 1  # sqrt(-1)
    base = _curves.ed_base()
    assert _curves.ed_encode_point(base) == (
        (4 * pow(5, -1, P) % P) | 0
    ).to_bytes(32, "little")
    # B has order L, so [L]B is the neutral element.
    assert _curves.ed_equal(
        _curves.ed_scalarmult_public(L, base), _curves.ED_NEUTRAL
    )


def test_the_secret_ladder_length_is_the_one_the_constants_imply() -> None:
    """`k % L + 2L` has exactly 254 bits for every `k`, which is the whole trick.

    If `2L < 2**253` or `3L >= 2**254` the reduction would not pin the length and
    the iteration count would leak the scalar again, so the two inequalities are
    asserted against the constant rather than trusted.
    """
    assert 2 * L >= 2**253
    assert 3 * L < 2**254
    for k in (0, 1, L - 1, L, L + 1, 2**255 - 1):
        assert (k % L + 2 * L).bit_length() == _curves._ED_SECRET_BITS


def test_secret_and_public_scalarmult_agree() -> None:
    rng = random.Random(0x5CA1AB1E)
    base = _curves.ed_base()
    for k in [0, 1, 2, L - 1, L, L + 5, *(rng.randrange(0, L) for _ in range(8))]:
        assert _curves.ed_equal(
            _curves.ed_scalarmult_secret(k, base),
            _curves.ed_scalarmult_public(k % L, base),
        )


def test_negation_is_exact_for_torsion_points_where_the_shortcut_is_not() -> None:
    """`-[h]A == [L - h]A` only when `[L]A` is the neutral element.

    Asserted directly, so the reason `ed_negate` exists is written down as a
    property rather than only as a comment. The first half proves the identity
    holds for a genuine key; the second proves it fails for a torsion one, which
    is what makes the shortcut unsafe.
    """
    base = _curves.ed_base()
    h = 0x1234567890ABCDEF
    genuine = _curves.ed_scalarmult_public(12345, base)
    assert _curves.ed_equal(
        _curves.ed_scalarmult_public(h, _curves.ed_negate(genuine)),
        _curves.ed_scalarmult_public((L - h) % L, genuine),
    )
    disagreed = 0
    for encoded in _small_order_points():
        point = _curves.ed_decode_point(encoded)
        assert point is not None
        if not _curves.ed_equal(
            _curves.ed_scalarmult_public(h, _curves.ed_negate(point)),
            _curves.ed_scalarmult_public((L - h) % L, point),
        ):
            disagreed += 1
    assert disagreed, "no torsion point distinguished the two forms; the derivation broke"


def test_double_scalarmult_matches_two_separate_multiplications() -> None:
    rng = random.Random(0xD0AB1E)
    base = _curves.ed_base()
    other = _curves.ed_scalarmult_public(777, base)
    for k1, k2 in [
        (0, 0),
        (1, 0),
        (0, 1),
        (1, 1),
        (L - 1, L - 1),
        *[(rng.randrange(0, L), rng.randrange(0, L)) for _ in range(4)],
    ]:
        expected = _curves.ed_add(
            _curves.ed_scalarmult_public(k1, base),
            _curves.ed_scalarmult_public(k2, other),
        )
        assert _curves.ed_equal(
            _curves.ed_double_scalarmult_public(k1, base, k2, other), expected
        )


def test_the_addition_law_is_complete() -> None:
    """One formula for doubling, the neutral element, and a point plus its negation.

    The secret ladder calls `ed_add` unconditionally and would be wrong if any of
    these needed a special case.
    """
    base = _curves.ed_base()
    assert _curves.ed_equal(
        _curves.ed_add(base, base), _curves.ed_scalarmult_public(2, base)
    )
    assert _curves.ed_equal(_curves.ed_add(base, _curves.ED_NEUTRAL), base)
    assert _curves.ed_equal(_curves.ed_add(_curves.ED_NEUTRAL, base), base)
    assert _curves.ed_equal(
        _curves.ed_add(base, _curves.ed_negate(base)), _curves.ED_NEUTRAL
    )
    assert _curves.ed_equal(
        _curves.ed_add(_curves.ED_NEUTRAL, _curves.ED_NEUTRAL), _curves.ED_NEUTRAL
    )


def test_base_point_failure_is_a_raise_and_not_an_assert() -> None:
    """`python -O` strips `assert`, and this invariant used to depend on one.

    `_ED_B()` in the old `_ecverify` read `assert bx is not None`. Under `-O`
    that vanishes and a `None` propagates into the arithmetic as a `TypeError`
    somewhere else entirely. Driving the real failure means breaking the curve
    constant, so the check is that the guard is a `raise` reached through a
    poisoned `ed_recover_x`.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_curves, "_ED_BASE", None)
        patch.setattr(_curves, "ed_recover_x", lambda y, sign: None)
        with pytest.raises(ValueError, match="base point has no x coordinate"):
            _curves.ed_base()
    assert _curves.ed_base() is not None  # and the cache is intact afterwards


# --- the shape of the secret path -------------------------------------------


def _addition_trace(run) -> int:
    """How many `ed_add` calls `run()` makes, in its own patch context."""
    calls = 0
    with pytest.MonkeyPatch.context() as patch:
        original = _curves.ed_add

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        patch.setattr(_curves, "ed_add", counted)
        run()
    return calls


def test_the_secret_ladder_adds_the_same_number_of_times_for_every_scalar() -> None:
    """No branch on a secret bit, so no Hamming-weight leak.

    `_dkim` multiplies two secrets per signature -- the clamped key and the
    per-message nonce -- and the old `while k: if k & 1:` leaked the weight of
    both. Not a constant-time claim: see `wreath._curves`'s module docstring.
    """
    base = _curves.ed_base()
    counts = {
        k: _addition_trace(lambda k=k: _curves.ed_scalarmult_secret(k, base))
        for k in (0, 1, 2, (1 << 253) - 1, L - 1, 0xDEADBEEF)
    }
    assert set(counts.values()) == {2 * _curves._ED_SECRET_BITS}


def test_verification_is_one_interleaved_multiplication_not_two() -> None:
    """The step count, asserted rather than benchmarked.

    Two independent multiplications over 253-bit scalars cost about 760
    additions; interleaving them shares the doublings and costs about 440. This
    pins the shape so a later edit cannot quietly put the second loop back.
    """
    public, message, signature = _corpus(0xE0FEED, 1)[0]
    interleaved = _addition_trace(lambda: verify_ed25519(public, message, signature))
    separate = _addition_trace(
        lambda: _verify_two_multiplications(public, message, signature)
    )
    assert interleaved < separate * 0.65
