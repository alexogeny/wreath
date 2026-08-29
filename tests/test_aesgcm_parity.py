from __future__ import annotations

import random

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from wreath._native import _core
from wreath._webpush import (
    MAX_GCM_PLAINTEXT_BYTES,
    TAG_BYTES,
    PushError,
    aes128gcm_decrypt,
    aes128gcm_encrypt,
)

_scalar_encrypt = _core._aes128gcm_encrypt_scalar
_scalar_decrypt = _core._aes128gcm_decrypt_scalar

#: The instruction path selected on this host.
ARMS: tuple[str, ...] = () if _core is None else tuple(getattr(_core, "aesgcm_arms", tuple)())

needs_hardware = pytest.mark.skipif(
    "aesni" not in ARMS,
    reason="no AES-NI/PCLMULQDQ on this CPU or in this build",
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

#: (label, key, iv, plaintext, aad, ciphertext, tag), all hex.
#:
#: The CAVP file publishes AES-128 encryption at PTlen 0/104/128/256/408 bits
#: and AADlen 0/128/160/384/720 bits, so the plaintext sizes below are
#: 0, 13, 16, 32 and 51 bytes -- an empty message, a part block, exactly one
#: block, exactly two, and three blocks plus a three-byte tail.
NIST_VECTORS = [
    (
        "CAVP gcmEncryptExtIV128 [IVlen=96 PTlen=0 AADlen=0] Count=0",
        "11754cd72aec309bf52f7687212e8957",
        "3c819d9a9bed087615030b65",
        "",
        "",
        "",
        "250327c674aaf477aef2675748cf6971",
    ),
    (
        "CAVP gcmEncryptExtIV128 [IVlen=96 PTlen=0 AADlen=128] Count=0",
        "77be63708971c4e240d1cb79e8d77feb",
        "e0e00f19fed7ba0136a797f3",
        "",
        "7a43ec1d9c0a5a78a0b16533a6213cab",
        "",
        "209fcc8d3675ed938e9c7166709dd946",
    ),
    (
        "CAVP gcmEncryptExtIV128 [IVlen=96 PTlen=104 AADlen=0] Count=0",
        "fe9bb47deb3a61e423c2231841cfd1fb",
        "4d328eb776f500a2f7fb47aa",
        "f1cc3818e421876bb6b8bbd6c9",
        "",
        "b88c5c1977b35b517b0aeae967",
        "43fd4727fe5cdb4b5b42818dea7ef8c9",
    ),
    (
        "CAVP gcmEncryptExtIV128 [IVlen=96 PTlen=128 AADlen=0] Count=0",
        "7fddb57453c241d03efbed3ac44e371c",
        "ee283a3fc75575e33efd4887",
        "d5de42b461646c255c87bd2962d3b9a2",
        "",
        "2ccda4a5415cb91e135c2a0f78c9b2fd",
        "b36d1df9b9d5e596f83e8b7f52971cb3",
    ),
    (
        "CAVP gcmEncryptExtIV128 [IVlen=96 PTlen=128 AADlen=160] Count=0",
        "d4a22488f8dd1d5c6c19a7d6ca17964c",
        "f3d5837f22ac1a0425e0d1d5",
        "7b43016a16896497fb457be6d2a54122",
        "f1c5d424b83f96c6ad8cb28ca0d20e475e023b5a",
        "c2bd67eef5e95cac27e3b06e3031d0a8",
        "f23eacf9d1cdf8737726c58648826e9c",
    ),
    (
        "CAVP gcmEncryptExtIV128 [IVlen=96 PTlen=256 AADlen=384] Count=0",
        "48b7f337cdf9252687ecc760bd8ec184",
        "3e894ebb16ce82a53c3e05b2",
        "bb2bac67a4709430c39c2eb9acfabc0d456c80d30aa1734e57997d548a8f0603",
        "7d924cfd37b3d046a96eb5e132042405c8731e06509787bbeb41f2582757"
        "46495e884d69871f77634c584bb007312234",
        "d263228b8ce051f67e9baf1ce7df97d10cd5f3bc972362055130c7d13c3ab2e7",
        "71446737ca1fa92e6d026d7d2ed1aa9c",
    ),
    (
        "CAVP gcmEncryptExtIV128 [IVlen=96 PTlen=408 AADlen=720] Count=0",
        "2c1f21cf0f6fb3661943155c3e3d8492",
        "23cb5ff362e22426984d1907",
        "42f758836986954db44bf37c6ef5e4ac0adaf38f27252a1b82d02ea949c8"
        "a1a2dbc0d68b5615ba7c1220ff6510e259f06655d8",
        "5d3624879d35e46849953e45a32a624d6a6c536ed9857c613b572b0333e7"
        "01557a713e3f010ecdf9a6bd6c9e3e44b065208645aff4aabee611b3915"
        "28514170084ccf587177f4488f33cfb5e979e42b6e1cfc0a60238982a7aec",
        "81824f0e0d523db30d3da369fdc0d60894c7a0a20646dd015073ad2732bd"
        "989b14a222b6ad57af43e1895df9dca2a5344a62cc",
        "57a3ee28136e94c74838997ae9823f3a",
    ),
    (
        # The all-zero case: key, IV, plaintext and AAD are all zero or empty,
        # so the whole answer is E(J0) and nothing masks a buffer that was
        # never written.
        "McGrew-Viega GCM Appendix B Test Case 1",
        "00000000000000000000000000000000",
        "000000000000000000000000",
        "",
        "",
        "",
        "58e2fccefa7e3061367f1d57a4e7455a",
    ),
    (
        "McGrew-Viega GCM Appendix B Test Case 2",
        "00000000000000000000000000000000",
        "000000000000000000000000",
        "00000000000000000000000000000000",
        "",
        "0388dace60b6a392f328c2b971b2fe78",
        "ab6e47d42cec13bdf53a67b21257bddf",
    ),
    (
        "McGrew-Viega GCM Appendix B Test Case 3",
        "feffe9928665731c6d6a8f9467308308",
        "cafebabefacedbaddecaf888",
        "d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
        "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b391aafd255",
        "",
        "42831ec2217774244b7221b784d0d49ce3aa212f2c02a4e035c17e2329aca12e"
        "21d514b25466931c7d8f6a5aac84aa051ba30b396a0aac973d58e091473f5985",
        "4d5c2af327cd64a62cf35abd2ba6fab4",
    ),
    (
        # 60 bytes of plaintext against 20 of AAD: the case where the GHASH
        # length block carries two different non-zero bit counts, and where
        # both operands need zero-padding to a block.
        "McGrew-Viega GCM Appendix B Test Case 4",
        "feffe9928665731c6d6a8f9467308308",
        "cafebabefacedbaddecaf888",
        "d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
        "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b39",
        "feedfacedeadbeeffeedfacedeadbeefabaddad2",
        "42831ec2217774244b7221b784d0d49ce3aa212f2c02a4e035c17e2329aca12e"
        "21d514b25466931c7d8f6a5aac84aa051ba30b396a0aac973d58e091",
        "5bc94fbc3221a5db94fae95ae7121a47",
    ),
]

#: CAVP publishes AES-128 GCM at IVlen 8 and 1024 bits as well as 96, and this
#: profile takes 96 only. These are the two lengths from that file, so the
#: refusal below is measured against the sizes NIST actually validates rather
#: than against invented ones. `Count=0` of `[IVlen=1024 PTlen=128 AADlen=0]`
#: was checked to reproduce under OpenSSL before being cited here.
NIST_NON_96_BIT_IV_LENGTHS = [1, 128]


def _body(length: int) -> bytes:
    """`length` bytes that are not all the same, so a misplaced block shows."""
    return bytes((index * 7 + 3) & 0xFF for index in range(length))


def _openssl(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """`ciphertext || tag` according to OpenSSL, which is not this repository."""
    return AESGCM(key).encrypt(nonce, plaintext, aad or None)


def _dispatch_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    return _core.aes128gcm_encrypt(key, nonce, plaintext, aad)


def _dispatch_decrypt(key: bytes, nonce: bytes, message: bytes, aad: bytes) -> bytes | None:
    return _core.aes128gcm_decrypt(key, nonce, message, aad)


@pytest.mark.parametrize("vector", NIST_VECTORS, ids=lambda vector: vector[0])
def test_both_paths_reproduce_the_published_vectors(vector: tuple[str, ...]) -> None:
    label, *parts = vector
    key, nonce, plaintext, aad, ciphertext, tag = (bytes.fromhex(part) for part in parts)
    assert _scalar_encrypt(key, nonce, plaintext, aad) == ciphertext + tag, label
    if ARMS:
        assert _dispatch_encrypt(key, nonce, plaintext, aad) == ciphertext + tag, label


@pytest.mark.parametrize("vector", NIST_VECTORS, ids=lambda vector: vector[0])
def test_both_paths_recover_the_published_plaintexts(vector: tuple[str, ...]) -> None:
    label, *parts = vector
    key, nonce, plaintext, aad, ciphertext, tag = (bytes.fromhex(part) for part in parts)
    assert _scalar_decrypt(key, nonce, ciphertext + tag, aad) == plaintext, label
    if ARMS:
        assert _dispatch_decrypt(key, nonce, ciphertext + tag, aad) == plaintext, label


@pytest.mark.parametrize("nonce_length", NIST_NON_96_BIT_IV_LENGTHS)
def test_the_non_96_bit_ivs_nist_publishes_are_refused_rather_than_guessed(
    nonce_length: int,
) -> None:
    nonce = bytes(nonce_length)
    with pytest.raises(PushError, match="96-bit nonce"):
        aes128gcm_encrypt(KEY, nonce, b"payload")
    if ARMS:
        with pytest.raises(ValueError, match="96-bit nonce"):
            _core.aes128gcm_encrypt(KEY, nonce, b"payload", b"")


@needs_hardware
@pytest.mark.parametrize("length", LENGTHS)
def test_both_paths_match_openssl_on_every_block_boundary(length: int) -> None:
    plaintext = _body(length)
    expected = _openssl(KEY, NONCE, plaintext, b"")
    assert _scalar_encrypt(KEY, NONCE, plaintext, b"") == expected
    assert _dispatch_encrypt(KEY, NONCE, plaintext, b"") == expected


@needs_hardware
@pytest.mark.parametrize("aad_length", AAD_LENGTHS)
def test_both_paths_match_openssl_on_additional_data(aad_length: int) -> None:
    aad = bytes((index * 13 + 1) & 0xFF for index in range(aad_length))
    for length in (0, 1, 16, 17, 4000):
        plaintext = _body(length)
        expected = _openssl(KEY, NONCE, plaintext, aad)
        assert _scalar_encrypt(KEY, NONCE, plaintext, aad) == expected
        assert _dispatch_encrypt(KEY, NONCE, plaintext, aad) == expected


#: Length ceilings for the fuzz, chosen so the loop's *iterations* pay for the
#: coverage and its *bytes* do not.
#:
#: What this test adds over the deterministic boundary sweep is random
#: combinations of key, nonce, length, and AAD. The lengths are not where the
#: structure lives. The accelerated path steps four blocks
#: (64 bytes) at a time, then one, then a zero-padded tail, so 512 bytes runs
#: eight four-block steps and every remainder class; nothing new happens at
#: 2000. The sizes that *are* structurally interesting at the top end -- 1000,
#: 4000, 4079, 4096 against AAD up to 1000 -- are covered exhaustively and
#: deterministically by `LENGTHS` and `AAD_LENGTHS` above, which is the right
#: place for them: a fuzz that reaches a case one run in ten is not coverage.
MAX_FUZZ_PLAINTEXT = 512
MAX_FUZZ_AAD = 96


@needs_hardware
def test_both_paths_match_openssl_under_a_seeded_fuzz() -> None:
    rng = random.Random(0xAE5C_C4)
    for _ in range(400):
        key = bytes(rng.randrange(256) for _ in range(16))
        nonce = bytes(rng.randrange(256) for _ in range(12))
        plaintext = bytes(rng.randrange(256) for _ in range(rng.randrange(0, MAX_FUZZ_PLAINTEXT)))
        aad = bytes(rng.randrange(256) for _ in range(rng.randrange(0, MAX_FUZZ_AAD)))
        expected = _openssl(key, nonce, plaintext, aad)
        detail = f"{len(plaintext)} bytes with {len(aad)} bytes of AAD"
        assert _dispatch_encrypt(key, nonce, plaintext, aad) == expected, f"selected: {detail}"
        assert _scalar_encrypt(key, nonce, plaintext, aad) == expected, f"scalar: {detail}"
        assert _dispatch_decrypt(key, nonce, expected, aad) == plaintext
        assert _scalar_decrypt(key, nonce, expected, aad) == plaintext


@needs_hardware
def test_the_tag_is_the_half_that_fails_on_its_own() -> None:
    plaintext = bytes(range(256)) * 4
    expected = _openssl(KEY, NONCE, plaintext, b"aad")
    for label, encrypt in (("scalar-c", _scalar_encrypt), ("selected", _dispatch_encrypt)):
        message = encrypt(KEY, NONCE, plaintext, b"aad")
        assert message[:-TAG_BYTES] == expected[:-TAG_BYTES], f"{label} counter mode"
        assert message[-TAG_BYTES:] == expected[-TAG_BYTES:], f"{label} GHASH"


@needs_hardware
@pytest.mark.parametrize("length", [0, 1, 16, 17, 64, 4000])
def test_both_refuse_a_tag_altered_in_any_bit(length: int) -> None:
    plaintext = _body(length)
    message = _openssl(KEY, NONCE, plaintext, b"")
    for index in range(TAG_BYTES):
        for bit in (0x01, 0x40, 0x80):
            altered = bytearray(message)
            altered[len(message) - TAG_BYTES + index] ^= bit
            assert _dispatch_decrypt(KEY, NONCE, bytes(altered), b"") is None
            assert _scalar_decrypt(KEY, NONCE, bytes(altered), b"") is None


@needs_hardware
def test_both_refuse_an_altered_ciphertext() -> None:
    plaintext = bytes(range(64))
    message = _openssl(KEY, NONCE, plaintext, b"")
    for index in range(len(plaintext)):
        altered = bytearray(message)
        altered[index] ^= 0x01
        assert _dispatch_decrypt(KEY, NONCE, bytes(altered), b"") is None
        assert _scalar_decrypt(KEY, NONCE, bytes(altered), b"") is None


@needs_hardware
def test_both_refuse_additional_data_that_changed_in_flight() -> None:
    message = _openssl(KEY, NONCE, b"payload", b"origin=a")
    assert _dispatch_decrypt(KEY, NONCE, message, b"origin=b") is None
    assert _scalar_decrypt(KEY, NONCE, message, b"origin=b") is None
    assert _dispatch_decrypt(KEY, NONCE, message, b"") is None
    assert _scalar_decrypt(KEY, NONCE, message, b"") is None


@needs_hardware
@pytest.mark.parametrize("key_length", [0, 15, 17, 32])
def test_the_selected_path_refuses_a_key_that_is_not_128_bits(key_length: int) -> None:
    with pytest.raises(ValueError, match="16-byte key"):
        _core.aes128gcm_encrypt(bytes(key_length), NONCE, b"x", b"")


@needs_hardware
@pytest.mark.parametrize("nonce_length", [0, 11, 13, 16])
def test_the_selected_path_refuses_a_nonce_that_is_not_96_bits(nonce_length: int) -> None:
    with pytest.raises(ValueError, match="96-bit nonce"):
        _core.aes128gcm_encrypt(KEY, bytes(nonce_length), b"x", b"")


@needs_hardware
def test_the_selected_path_refuses_a_message_shorter_than_its_tag() -> None:
    with pytest.raises(ValueError, match="at least its 16-byte tag"):
        _core.aes128gcm_decrypt(KEY, NONCE, bytes(15), b"")


def test_dispatch_always_has_a_complete_arm() -> None:
    assert set(ARMS) in ({"aesni", "pclmul"}, {"scalar"})


def test_the_facade_actually_calls_the_bound_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath._webpush as webpush

    seen: list[tuple[str, bytes, bytes, bytes, bytes]] = []

    def encrypt_recorder(key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        seen.append(("encrypt", key, nonce, data, aad))
        return b"\x00" * (len(data) + TAG_BYTES)

    def decrypt_recorder(key: bytes, nonce: bytes, data: bytes, aad: bytes) -> bytes:
        seen.append(("decrypt", key, nonce, data, aad))
        return b"recovered"

    monkeypatch.setattr(webpush._core, "aes128gcm_encrypt", encrypt_recorder)
    monkeypatch.setattr(webpush._core, "aes128gcm_decrypt", decrypt_recorder)
    assert aes128gcm_encrypt(KEY, NONCE, b"body", b"extra") == b"\x00" * (4 + TAG_BYTES)
    assert aes128gcm_decrypt(KEY, NONCE, bytes(TAG_BYTES + 4), b"extra") == b"recovered"
    assert seen == [
        ("encrypt", KEY, NONCE, b"body", b"extra"),
        ("decrypt", KEY, NONCE, bytes(TAG_BYTES + 4), b"extra"),
    ]


@pytest.mark.parametrize("length", [0, 1, 15, 16, 17, 63, 64, 65, 1000])
def test_the_facade_produces_what_openssl_produces(length: int) -> None:
    plaintext = _body(length)
    message = aes128gcm_encrypt(KEY, NONCE, plaintext, b"aad")
    assert len(message) == length + TAG_BYTES
    assert message == _openssl(KEY, NONCE, plaintext, b"aad")
    assert aes128gcm_decrypt(KEY, NONCE, message, b"aad") == plaintext


def test_the_facade_refuses_an_altered_message_by_name() -> None:
    message = bytearray(aes128gcm_encrypt(KEY, NONCE, b"body"))
    message[-1] ^= 0x80
    with pytest.raises(PushError, match="does not authenticate this message"):
        aes128gcm_decrypt(KEY, NONCE, bytes(message))


def test_the_facade_refuses_a_message_shorter_than_a_tag() -> None:
    with pytest.raises(PushError, match="carries a 16-byte tag"):
        aes128gcm_decrypt(KEY, NONCE, bytes(TAG_BYTES - 1))


def test_a_plaintext_past_the_mode_is_refused_rather_than_encrypted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wreath._webpush as webpush

    assert MAX_GCM_PLAINTEXT_BYTES == (1 << 36) - 32
    monkeypatch.setattr(webpush, "MAX_GCM_PLAINTEXT_BYTES", 8)
    assert aes128gcm_encrypt(KEY, NONCE, bytes(8)) is not None
    with pytest.raises(PushError, match="at most 8 bytes under one key and nonce"):
        aes128gcm_encrypt(KEY, NONCE, bytes(9))
    with pytest.raises(PushError, match="at most 8 bytes under one key and nonce"):
        aes128gcm_decrypt(KEY, NONCE, bytes(TAG_BYTES + 9))
