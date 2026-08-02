"""The AES-NI/PCLMULQDQ path and the pure twin produce identical bytes.

`wreath._webpush` has two AES-128-GCM implementations: one written in Python
because CPython ships no AES, and one in `src/wreath/_native/aesgcm.c` that runs
the block cipher on `aesenc` and GHASH on `pclmulqdq`. A machine without those
instructions runs the first; every other machine runs the second. Both ship.

That is the shape a differential test exists for. Crypto passes its own tests
while being wrong, and a *pair* of implementations can be wrong in two different
ways at once -- so this file compares them to each other byte for byte, and
`tests/test_aesgcm_vectors.py` compares both to something written by somebody
else. Neither file on its own would be enough:

* agreement without an external reference proves only that two implementations
  share an author's misreading of SP 800-38D;
* known answers without agreement proves only that whichever path this CPU
  happens to run is correct, and says nothing about the other one.

The lengths are chosen around the block boundaries, because that is where the
implementations differ structurally: the C path steps four blocks at a time,
then one, then a zero-padded tail, and the Python path steps sixteen bytes at a
time throughout. An off-by-one lives at 15/16/17, 31/32/33 and 63/64/65 and
nowhere else.
"""

from __future__ import annotations

import random

import pytest

from wreath._native import _core
from wreath._webpush import (
    MAX_GCM_PLAINTEXT_BYTES,
    TAG_BYTES,
    PushError,
    _aes128gcm_decrypt_pure,
    _aes128gcm_encrypt_pure,
    aes128gcm_decrypt,
    aes128gcm_encrypt,
)

#: An empty tuple means this CPU (or this build) has no AES-NI/PCLMULQDQ, so
#: there is no second implementation to differ from -- a capability of the
#: machine, not of Wreath.
ARMS: tuple[str, ...] = (
    () if _core is None else tuple(getattr(_core, "aesgcm_arms", tuple)())
)

needs_hardware = pytest.mark.skipif(
    not ARMS,
    reason="no AES-NI/PCLMULQDQ on this CPU or in this build; only the pure twin exists",
)

KEY = bytes(range(16))
NONCE = bytes(range(100, 112))

#: Every block boundary either implementation has a branch for, plus the sizes
#: RFC 8291 actually produces.
LENGTHS = [
    *range(0, 81),
    100,
    127,
    128,
    129,
    255,
    256,
    257,
    1000,
    4000,
    4079,
    4096,
]

AAD_LENGTHS = [0, 1, 15, 16, 17, 31, 32, 33, 63, 64, 65, 1000]


def _body(length: int) -> bytes:
    """`length` bytes that are not all the same, so a misplaced block shows."""
    return bytes((index * 7 + 3) & 0xFF for index in range(length))


def _native_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    return _core.aes128gcm_encrypt(key, nonce, plaintext, aad)


def _native_decrypt(key: bytes, nonce: bytes, message: bytes, aad: bytes) -> bytes | None:
    return _core.aes128gcm_decrypt(key, nonce, message, aad)


@needs_hardware
@pytest.mark.parametrize("length", LENGTHS)
def test_native_and_pure_agree_on_every_block_boundary(length: int) -> None:
    plaintext = _body(length)
    assert _native_encrypt(KEY, NONCE, plaintext, b"") == _aes128gcm_encrypt_pure(
        KEY, NONCE, plaintext, b""
    )


@needs_hardware
@pytest.mark.parametrize("aad_length", AAD_LENGTHS)
def test_native_and_pure_agree_on_additional_data(aad_length: int) -> None:
    """AAD is hashed but not encrypted, so a wrong length block shows up here.

    The failure this catches is specific: the length block at the end of GHASH
    carries the AAD's bit count and the ciphertext's, and swapping them, or
    zero-padding the AAD to the wrong multiple, produces a tag that is wrong
    only when both are non-empty and of different lengths.
    """
    aad = bytes((index * 13 + 1) & 0xFF for index in range(aad_length))
    for length in (0, 1, 16, 17, 4000):
        plaintext = _body(length)
        assert _native_encrypt(KEY, NONCE, plaintext, aad) == _aes128gcm_encrypt_pure(
            KEY, NONCE, plaintext, aad
        )


@needs_hardware
def test_native_and_pure_agree_under_a_seeded_fuzz() -> None:
    """Random keys, nonces, lengths and AAD, from a seed so a failure repeats."""
    rng = random.Random(0xAE5C_C4)
    for _ in range(400):
        key = bytes(rng.randrange(256) for _ in range(16))
        nonce = bytes(rng.randrange(256) for _ in range(12))
        plaintext = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 2000)))
        aad = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200)))
        native = _native_encrypt(key, nonce, plaintext, aad)
        pure = _aes128gcm_encrypt_pure(key, nonce, plaintext, aad)
        assert native == pure, (
            f"disagreed on {len(plaintext)} bytes with {len(aad)} bytes of AAD"
        )
        assert _native_decrypt(key, nonce, native, aad) == plaintext
        assert _aes128gcm_decrypt_pure(key, nonce, pure, aad) == plaintext


@needs_hardware
def test_the_tag_is_where_the_two_agree_last() -> None:
    """Stated separately because the ciphertext and the tag fail apart.

    A wrong GHASH leaves the ciphertext byte-identical and only the last 16
    bytes different, which an assertion on the whole message reports as a
    difference somewhere -- true, and useless when the question is which half is
    broken.
    """
    plaintext = bytes(range(256)) * 4
    native = _native_encrypt(KEY, NONCE, plaintext, b"aad")
    pure = _aes128gcm_encrypt_pure(KEY, NONCE, plaintext, b"aad")
    assert native[:-TAG_BYTES] == pure[:-TAG_BYTES], "the counter-mode halves differ"
    assert native[-TAG_BYTES:] == pure[-TAG_BYTES:], "GHASH differs"


# --- refusals ----------------------------------------------------------------


@needs_hardware
@pytest.mark.parametrize("length", [0, 1, 16, 17, 64, 4000])
def test_both_refuse_a_tag_altered_in_any_bit(length: int) -> None:
    """Every bit of the tag, not a representative one.

    A comparison that reads fifteen of the sixteen bytes accepts a forgery one
    time in 256 and passes a test that flips the first byte.
    """
    plaintext = _body(length)
    message = _native_encrypt(KEY, NONCE, plaintext, b"")
    for index in range(TAG_BYTES):
        for bit in (0x01, 0x40, 0x80):
            altered = bytearray(message)
            altered[len(message) - TAG_BYTES + index] ^= bit
            assert _native_decrypt(KEY, NONCE, bytes(altered), b"") is None
            assert _aes128gcm_decrypt_pure(KEY, NONCE, bytes(altered), b"") is None


@needs_hardware
def test_both_refuse_an_altered_ciphertext() -> None:
    plaintext = bytes(range(64))
    message = _native_encrypt(KEY, NONCE, plaintext, b"")
    for index in range(len(plaintext)):
        altered = bytearray(message)
        altered[index] ^= 0x01
        assert _native_decrypt(KEY, NONCE, bytes(altered), b"") is None
        assert _aes128gcm_decrypt_pure(KEY, NONCE, bytes(altered), b"") is None


@needs_hardware
def test_both_refuse_additional_data_that_changed_in_flight() -> None:
    """AAD is not carried in the message, so this is the only thing binding it."""
    message = _native_encrypt(KEY, NONCE, b"payload", b"origin=a")
    assert _native_decrypt(KEY, NONCE, message, b"origin=b") is None
    assert _aes128gcm_decrypt_pure(KEY, NONCE, message, b"origin=b") is None
    assert _native_decrypt(KEY, NONCE, message, b"") is None


@needs_hardware
@pytest.mark.parametrize("key_length", [0, 15, 17, 32])
def test_the_native_path_refuses_a_key_that_is_not_128_bits(key_length: int) -> None:
    """The facade checks first, so this is the C's own guard being exercised."""
    with pytest.raises(ValueError, match="16-byte key"):
        _core.aes128gcm_encrypt(bytes(key_length), NONCE, b"x", b"")


@needs_hardware
@pytest.mark.parametrize("nonce_length", [0, 11, 13, 16])
def test_the_native_path_refuses_a_nonce_that_is_not_96_bits(nonce_length: int) -> None:
    with pytest.raises(ValueError, match="96-bit nonce"):
        _core.aes128gcm_encrypt(KEY, bytes(nonce_length), b"x", b"")


@needs_hardware
def test_the_native_path_refuses_a_message_shorter_than_its_tag() -> None:
    with pytest.raises(ValueError, match="at least its 16-byte tag"):
        _core.aes128gcm_decrypt(KEY, NONCE, bytes(15), b"")


# --- the seam ----------------------------------------------------------------


def test_the_facade_binds_the_hardware_path_exactly_when_the_cpu_has_it() -> None:
    """The one place the two implementations are chosen between."""
    import wreath._webpush as webpush

    if ARMS:
        assert webpush._native_gcm_encrypt is not None
        assert webpush._native_gcm_decrypt is not None
        assert set(ARMS) == {"aesni", "pclmul"}
    else:
        assert webpush._native_gcm_encrypt is None
        assert webpush._native_gcm_decrypt is None


def test_the_facade_actually_calls_the_bound_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both paths return the same bytes, so agreement cannot prove which ran.

    Without this, deleting the dispatch and always running the pure twin is
    invisible: every other test in this file still passes, and the 4000x the
    hardware path is worth is silently gone. So the call is observed rather than
    inferred.
    """
    import wreath._webpush as webpush

    seen: list[tuple[str, bytes, bytes, bytes, bytes]] = []

    def encrypt_recorder(key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        seen.append(("encrypt", key, nonce, data, aad))
        return b"\x00" * (len(data) + TAG_BYTES)

    def decrypt_recorder(key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        seen.append(("decrypt", key, nonce, data, aad))
        return b"recovered"

    monkeypatch.setattr(webpush, "_native_gcm_encrypt", encrypt_recorder)
    monkeypatch.setattr(webpush, "_native_gcm_decrypt", decrypt_recorder)
    assert aes128gcm_encrypt(KEY, NONCE, b"body", b"extra") == b"\x00" * (4 + TAG_BYTES)
    assert aes128gcm_decrypt(KEY, NONCE, bytes(TAG_BYTES + 4), b"extra") == b"recovered"
    assert seen == [
        ("encrypt", KEY, NONCE, b"body", b"extra"),
        ("decrypt", KEY, NONCE, bytes(TAG_BYTES + 4), b"extra"),
    ]


def test_the_facade_falls_back_to_the_pure_twin_when_nothing_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What a machine without AES-NI runs, exercised on a machine that has it."""
    import wreath._webpush as webpush

    monkeypatch.setattr(webpush, "_native_gcm_encrypt", None)
    monkeypatch.setattr(webpush, "_native_gcm_decrypt", None)
    message = aes128gcm_encrypt(KEY, NONCE, b"body", b"extra")
    assert message == _aes128gcm_encrypt_pure(KEY, NONCE, b"body", b"extra")
    assert aes128gcm_decrypt(KEY, NONCE, message, b"extra") == b"body"
    with pytest.raises(PushError, match="does not authenticate"):
        aes128gcm_decrypt(KEY, NONCE, message[:-1] + bytes(1), b"extra")


# --- the facade's own contract ------------------------------------------------


@pytest.mark.parametrize("length", [0, 1, 15, 16, 17, 63, 64, 65, 1000])
def test_the_facade_round_trips_whichever_path_is_bound(length: int) -> None:
    plaintext = _body(length)
    message = aes128gcm_encrypt(KEY, NONCE, plaintext, b"aad")
    assert len(message) == length + TAG_BYTES
    assert aes128gcm_decrypt(KEY, NONCE, message, b"aad") == plaintext


def test_the_facade_refuses_an_altered_message_by_name() -> None:
    message = bytearray(aes128gcm_encrypt(KEY, NONCE, b"body"))
    message[-1] ^= 0x80
    with pytest.raises(PushError, match="does not authenticate this message"):
        aes128gcm_decrypt(KEY, NONCE, bytes(message))


def test_the_facade_refuses_a_message_shorter_than_a_tag() -> None:
    """Distinct from a bad tag: there is no tag to compare at all."""
    with pytest.raises(PushError, match="carries a 16-byte tag"):
        aes128gcm_decrypt(KEY, NONCE, bytes(TAG_BYTES - 1))


def test_a_plaintext_past_the_mode_is_refused_rather_than_encrypted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is real, and this is how it is tested without 68 GB of RAM.

    `MAX_GCM_PLAINTEXT_BYTES` is where AES-GCM's counter wraps and the mode
    stops being AES-GCM. Nothing in this repository can allocate that, so the
    bound is lowered and the *guard* is tested -- which is the part that could
    be missing. It is a `raise` rather than an `assert` because `python -O`
    deletes the second kind and this file's whole subject is what happens when a
    length is wrong.
    """
    import wreath._webpush as webpush

    assert MAX_GCM_PLAINTEXT_BYTES == (1 << 36) - 32
    monkeypatch.setattr(webpush, "MAX_GCM_PLAINTEXT_BYTES", 8)
    assert aes128gcm_encrypt(KEY, NONCE, bytes(8)) is not None
    with pytest.raises(PushError, match="at most 8 bytes under one key and nonce"):
        aes128gcm_encrypt(KEY, NONCE, bytes(9))
    with pytest.raises(PushError, match="at most 8 bytes under one key and nonce"):
        aes128gcm_decrypt(KEY, NONCE, bytes(TAG_BYTES + 9))
