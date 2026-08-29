from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from wreath._native import _core
from wreath._webpush import (
    TAG_BYTES,
    aes128gcm_encrypt,
)

_scalar_encrypt = _core._aes128gcm_encrypt_scalar
_scalar_decrypt = _core._aes128gcm_decrypt_scalar

ARMS: tuple[str, ...] = () if _core is None else tuple(getattr(_core, "aesgcm_arms", tuple)())

#: (key, nonce, plaintext, aad, ciphertext, tag), all hex.
#: NIST SP 800-38D / "The Galois/Counter Mode of Operation (GCM)", cases 1-4.
SP800_38D_VECTORS = [
    (
        "00000000000000000000000000000000",
        "000000000000000000000000",
        "",
        "",
        "",
        "58e2fccefa7e3061367f1d57a4e7455a",
    ),
    (
        "00000000000000000000000000000000",
        "000000000000000000000000",
        "00000000000000000000000000000000",
        "",
        "0388dace60b6a392f328c2b971b2fe78",
        "ab6e47d42cec13bdf53a67b21257bddf",
    ),
    (
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

#: The scalar kernel is addressed explicitly so an x86 test host cannot hide
#: a non-x86 regression behind the hardware dispatcher.
PATHS = [
    pytest.param(_scalar_encrypt, id="scalar-c"),
    pytest.param(_core.aes128gcm_encrypt, id="selected"),
]


@pytest.mark.parametrize("encrypt", PATHS)
@pytest.mark.parametrize("vector", SP800_38D_VECTORS, ids=lambda v: f"len{len(v[2]) // 2}")
def test_both_paths_reproduce_the_nist_vectors(encrypt, vector: tuple[str, ...]) -> None:
    key, nonce, plaintext, aad, ciphertext, tag = (bytes.fromhex(part) for part in vector)
    assert encrypt(key, nonce, plaintext, aad) == ciphertext + tag


@pytest.mark.parametrize("encrypt", PATHS)
@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 31, 32, 33, 63, 64, 65, 1000, 4000])
@pytest.mark.parametrize("aad_size", [0, 16, 37])
def test_both_paths_match_openssl(encrypt, size: int, aad_size: int) -> None:
    key, nonce = os.urandom(16), os.urandom(12)
    plaintext, aad = os.urandom(size), os.urandom(aad_size)
    expected = AESGCM(key).encrypt(nonce, plaintext, aad or None)
    assert encrypt(key, nonce, plaintext, aad) == expected


def test_the_facade_produces_what_openssl_decrypts() -> None:
    key, nonce = os.urandom(16), os.urandom(12)
    plaintext, aad = os.urandom(1234), os.urandom(19)
    message = aes128gcm_encrypt(key, nonce, plaintext, aad)
    assert AESGCM(key).decrypt(nonce, message, aad) == plaintext


@pytest.mark.parametrize("size", [0, 17, 4000])
def test_both_paths_decrypt_what_openssl_encrypted(size: int) -> None:
    key, nonce = os.urandom(16), os.urandom(12)
    plaintext, aad = os.urandom(size), os.urandom(11)
    message = AESGCM(key).encrypt(nonce, plaintext, aad or None)
    assert _scalar_decrypt(key, nonce, message, aad) == plaintext
    if ARMS:
        assert _core.aes128gcm_decrypt(key, nonce, message, aad) == plaintext


def test_openssl_refuses_a_message_whose_tag_this_module_altered() -> None:
    key, nonce = os.urandom(16), os.urandom(12)
    message = bytearray(aes128gcm_encrypt(key, nonce, b"payload"))
    message[-TAG_BYTES] ^= 0x01
    with pytest.raises(InvalidTag):
        AESGCM(key).decrypt(nonce, bytes(message), None)
