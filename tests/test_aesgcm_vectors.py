"""Both AES-GCM paths, against answers nobody in this repository wrote.

`tests/test_aesgcm_parity.py` proves the hardware and Python arms agree.
Agreement is not correctness: two implementations written from the same reading
of NIST SP 800-38D can be wrong in the same place, and a differential test is
exactly the wrong instrument for that. So this file checks both of them against
two external references:

* the **SP 800-38D known-answer vectors** (McGrew and Viega's test cases 1-4,
  the AES-128 ones with a 96-bit IV), transcribed as literals. A vector is worth
  more than a library here, because it cannot be wrong in a way that tracks a
  bug -- it is a constant.
* **`cryptography`'s `AESGCM`**, which is OpenSSL. That is a *test* dependency
  and never imported by anything under `src/wreath`; the whole reason
  `wreath._webpush` exists is that Wreath's core takes no such dependency.

Test cases 5 and 6 are deliberately absent: their IVs are 64 and 480 bits, and
this profile takes a 96-bit nonce only -- which is what RFC 8291 and every
JWE-adjacent use of GCM specify, and what makes J0 the nonce with a counter
rather than a GHASH of the IV.
"""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from wreath._native import _core
from wreath._webpush import (
    TAG_BYTES,
    _aes128gcm_decrypt_pure,
    _aes128gcm_encrypt_pure,
    aes128gcm_encrypt,
)

ARMS: tuple[str, ...] = (
    () if _core is None else tuple(getattr(_core, "aesgcm_arms", tuple)())
)

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

#: The paths that exist on this machine. The pure one always does; the native
#: one is a capability of the CPU, so its absence is a real skip rather than a
#: parked test.
PATHS = [
    pytest.param(_aes128gcm_encrypt_pure, id="pure"),
    pytest.param(
        None if not ARMS else _core.aes128gcm_encrypt,
        id="native",
        marks=pytest.mark.skipif(
            not ARMS, reason="no AES-NI/PCLMULQDQ on this CPU or in this build"
        ),
    ),
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
    """`cryptography` is OpenSSL, and OpenSSL is not this repository."""
    key, nonce = os.urandom(16), os.urandom(12)
    plaintext, aad = os.urandom(size), os.urandom(aad_size)
    expected = AESGCM(key).encrypt(nonce, plaintext, aad or None)
    assert encrypt(key, nonce, plaintext, aad) == expected


def test_the_facade_produces_what_openssl_decrypts() -> None:
    """Whichever path this machine binds, read back by an independent receiver.

    The direction matters: an encryptor and a decryptor written together can
    agree on a wrong ciphertext, and only a foreign decryptor rules that out.
    """
    key, nonce = os.urandom(16), os.urandom(12)
    plaintext, aad = os.urandom(1234), os.urandom(19)
    message = aes128gcm_encrypt(key, nonce, plaintext, aad)
    assert AESGCM(key).decrypt(nonce, message, aad) == plaintext


@pytest.mark.parametrize("size", [0, 17, 4000])
def test_both_paths_decrypt_what_openssl_encrypted(size: int) -> None:
    """The other direction, so decryption is pinned to something foreign too."""
    key, nonce = os.urandom(16), os.urandom(12)
    plaintext, aad = os.urandom(size), os.urandom(11)
    message = AESGCM(key).encrypt(nonce, plaintext, aad or None)
    assert _aes128gcm_decrypt_pure(key, nonce, message, aad) == plaintext
    if ARMS:
        assert _core.aes128gcm_decrypt(key, nonce, message, aad) == plaintext


def test_openssl_refuses_a_message_whose_tag_this_module_altered() -> None:
    """The tag is authenticating, not a checksum -- confirmed from outside.

    Without this, a tag computed by some consistent-but-wrong rule would satisfy
    every test in this file that only compares Wreath to Wreath.
    """
    key, nonce = os.urandom(16), os.urandom(12)
    message = bytearray(aes128gcm_encrypt(key, nonce, b"payload"))
    message[-TAG_BYTES] ^= 0x01
    with pytest.raises(InvalidTag):
        AESGCM(key).decrypt(nonce, bytes(message), None)
