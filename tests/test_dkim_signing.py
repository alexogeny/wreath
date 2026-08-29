from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from wreath._dkim import (
    DkimError,
    DkimSigner,
    Ed25519Key,
    RsaKey,
    _parse_headers,
    _split_message,
    canonicalize_body_relaxed,
    canonicalize_header_relaxed,
    ed25519_public_key,
    load_private_key,
)

MESSAGE = (
    b"From: sender@example.com\r\n"
    b"To: recipient@example.net\r\n"
    b"Subject: a test\r\n"
    b"Date: Mon, 02 Aug 2026 12:00:00 +0000\r\n"
    b"\r\n"
    b"Hello there.\r\n"
)


@pytest.fixture(scope="module")
def rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


@pytest.fixture(scope="module")
def rsa_pkcs1_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode("ascii")


@pytest.fixture(scope="module")
def ed25519_pem() -> str:
    key = ed25519.Ed25519PrivateKey.generate()
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def test_relaxed_header_matches_rfc_6376_example() -> None:
    assert canonicalize_header_relaxed("A", " X") == b"a:X\r\n"
    assert canonicalize_header_relaxed("B ", " Y\t\r\n\tZ  ") == b"b:Y Z\r\n"


def test_relaxed_body_matches_rfc_6376_example() -> None:
    assert canonicalize_body_relaxed(b" C \r\nD \t E\r\n\r\n\r\n") == b" C\r\nD E\r\n"


def test_relaxed_body_of_an_empty_body_is_empty() -> None:
    assert canonicalize_body_relaxed(b"") == b""
    assert canonicalize_body_relaxed(b"\r\n\r\n") == b""


def test_relaxed_body_appends_one_crlf_to_an_unterminated_body() -> None:
    assert canonicalize_body_relaxed(b"text") == b"text\r\n"


def test_folded_and_unfolded_headers_canonicalise_identically() -> None:
    folded = canonicalize_header_relaxed("Subject", " a very long\r\n subject line")
    flat = canonicalize_header_relaxed("Subject", " a very long subject line")
    assert folded == flat


def _signing_input(signer: DkimSigner, message: bytes, header: str) -> tuple[bytes, bytes]:
    """Rebuild what was signed, and pull the signature out of `header`."""
    unfolded = header.replace("\r\n\t", " ")
    encoded = unfolded[unfolded.rindex("b=") + 2 :].replace(" ", "")
    signature = base64.b64decode(encoded + "=" * (-len(encoded) % 4))

    block, _ = _split_message(message)
    fields = _parse_headers(block)
    present = {name for name, _ in fields}
    signed = [n for n in dict.fromkeys(("from", *signer.headers)) if n in present]
    remaining: dict[str, list[str]] = {}
    for name, value in fields:
        remaining.setdefault(name, []).append(value)
    canonical = bytearray()
    for name in signed:
        if remaining.get(name):
            canonical += canonicalize_header_relaxed(name, remaining[name].pop())
    tags = unfolded[: unfolded.rindex("b=") + 2]
    canonical += canonicalize_header_relaxed("dkim-signature", tags).rstrip(b"\r\n")
    return bytes(canonical), signature


def test_rsa_signature_verifies_under_cryptography(rsa_pem: str) -> None:
    signer = DkimSigner("example.com", "sel", load_private_key(rsa_pem))
    header = signer.sign(MESSAGE)
    signed, signature = _signing_input(signer, MESSAGE, header)
    public = serialization.load_pem_private_key(rsa_pem.encode(), password=None).public_key()
    public.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())


def test_pkcs1_and_pkcs8_are_both_readable(rsa_pkcs1_pem: str) -> None:
    signer = DkimSigner("example.com", "sel", load_private_key(rsa_pkcs1_pem))
    header = signer.sign(MESSAGE)
    signed, signature = _signing_input(signer, MESSAGE, header)
    public = serialization.load_pem_private_key(rsa_pkcs1_pem.encode(), password=None).public_key()
    public.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())


def test_ed25519_signature_verifies_under_cryptography(ed25519_pem: str) -> None:
    signer = DkimSigner("example.com", "sel", load_private_key(ed25519_pem))
    assert signer.algorithm == "ed25519-sha256"
    header = signer.sign(MESSAGE)
    signed, signature = _signing_input(signer, MESSAGE, header)
    public = serialization.load_pem_private_key(ed25519_pem.encode(), password=None).public_key()
    public.verify(signature, signed)


def test_ed25519_public_key_derivation_matches_cryptography(ed25519_pem: str) -> None:
    key = load_private_key(ed25519_pem)
    assert isinstance(key, Ed25519Key)
    reference = serialization.load_pem_private_key(ed25519_pem.encode(), password=None)
    assert ed25519_public_key(key.seed) == reference.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def test_a_tampered_body_breaks_the_signature(rsa_pem: str) -> None:
    signer = DkimSigner("example.com", "sel", load_private_key(rsa_pem))
    original = signer.sign(MESSAGE, now=1_800_000_000)
    tampered = signer.sign(MESSAGE.replace(b"Hello there.", b"Goodbye."), now=1_800_000_000)
    assert _tag(original, "bh") != _tag(tampered, "bh")


def test_signed_headers_are_listed_in_the_h_tag(rsa_pem: str) -> None:
    signer = DkimSigner("example.com", "sel", load_private_key(rsa_pem))
    listed = _tag(signer.sign(MESSAGE), "h").split(":")
    assert listed[0] == "from"
    assert "subject" in listed
    # Only headers the message actually carries: signing an absent header with
    # `h=` naming it tells a verifier to hash a field that is not there.
    assert "cc" not in listed


def _tag(header: str, name: str) -> str:
    for part in header.replace("\r\n\t", " ").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.replace(" ", "")
    raise AssertionError(f"no {name}= tag in {header!r}")


def test_a_message_with_no_body_separator_is_refused(rsa_pem: str) -> None:
    signer = DkimSigner("example.com", "sel", load_private_key(rsa_pem))
    with pytest.raises(DkimError, match="no header/body separator"):
        signer.sign(b"From: a@b.c\r\nSubject: no body")


def test_unreadable_pem_is_refused_by_name() -> None:
    with pytest.raises(DkimError, match="no PKCS#1 or PKCS#8 private key"):
        load_private_key("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")


def test_an_rsa_key_too_small_for_sha256_is_refused() -> None:
    tiny = RsaKey(n=(1 << 255) | 1, e=65537, d=3)
    with pytest.raises(DkimError, match="too small to sign"):
        DkimSigner("example.com", "sel", tiny).sign(MESSAGE)


def test_a_corrupted_signature_is_caught_before_it_is_returned(rsa_pem: str) -> None:
    real = load_private_key(rsa_pem)
    assert isinstance(real, RsaKey)
    mismatched = RsaKey(
        n=real.n, e=3, d=real.d, p=real.p, q=real.q, dp=real.dp, dq=real.dq, qinv=real.qinv
    )
    with pytest.raises(DkimError, match="failed its own verification"):
        DkimSigner("example.com", "sel", mismatched).sign(MESSAGE)


# A corrupt or wrong-format key file is an ordinary operational event -- a
# truncated copy, an encrypted key, an EC key where an RSA one was meant. Each
# refusal below names what was actually wrong, because "could not load key" sends
# the reader to inspect the wrong thing.


def _pem(label: str, der: bytes) -> str:
    body = base64.b64encode(der).decode("ascii")
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"


def test_a_pkcs8_body_that_is_not_a_sequence_is_refused() -> None:
    with pytest.raises(DkimError, match="not a SEQUENCE"):
        load_private_key(_pem("PRIVATE KEY", b"\x02\x01\x00"))


def test_a_pkcs8_algorithm_identifier_that_is_not_a_sequence_is_refused() -> None:
    # SEQUENCE { INTEGER 0, INTEGER 0 } -- version present, algorithm the wrong tag.
    der = bytes([0x30, 0x06, 0x02, 0x01, 0x00, 0x02, 0x01, 0x00])
    with pytest.raises(DkimError, match="AlgorithmIdentifier is not a SEQUENCE"):
        load_private_key(_pem("PRIVATE KEY", der))


def test_a_pkcs8_algorithm_that_is_not_an_oid_is_refused() -> None:
    inner = bytes([0x30, 0x03, 0x02, 0x01, 0x00])  # SEQUENCE { INTEGER 0 }
    der = bytes([0x30, 3 + len(inner), 0x02, 0x01, 0x00]) + inner
    with pytest.raises(DkimError, match="not an OBJECT IDENTIFIER"):
        load_private_key(_pem("PRIVATE KEY", der))


def test_an_unsupported_pkcs8_algorithm_is_refused_by_name() -> None:
    oid = bytes([0x06, 0x07, 0x2A, 0x86, 0x48, 0xCE, 0x3D, 0x02, 0x01])  # id-ecPublicKey
    algorithm = bytes([0x30, len(oid)]) + oid
    key = bytes([0x04, 0x01, 0x00])
    contents = bytes([0x02, 0x01, 0x00]) + algorithm + key
    with pytest.raises(DkimError, match="unsupported PKCS#8 key algorithm"):
        load_private_key(_pem("PRIVATE KEY", bytes([0x30, len(contents)]) + contents))


def test_an_ed25519_key_of_the_wrong_length_is_refused() -> None:
    oid = bytes([0x06, 0x03, 0x2B, 0x65, 0x70])  # id-Ed25519
    algorithm = bytes([0x30, len(oid)]) + oid
    inner = bytes([0x04, 0x04]) + b"\x00" * 4  # a 4-byte seed, not 32
    key = bytes([0x04, len(inner)]) + inner
    contents = bytes([0x02, 0x01, 0x00]) + algorithm + key
    with pytest.raises(DkimError, match="32-byte OCTET STRING"):
        load_private_key(_pem("PRIVATE KEY", bytes([0x30, len(contents)]) + contents))


def test_truncated_der_is_refused_rather_than_half_read() -> None:
    with pytest.raises(DkimError, match="truncated DER"):
        load_private_key(_pem("PRIVATE KEY", bytes([0x30, 0x40, 0x02])))


def test_signing_with_a_short_ed25519_seed_is_refused() -> None:
    signer = DkimSigner("example.com", "sel", Ed25519Key(b"\x00" * 8))
    with pytest.raises(DkimError, match="seed is 32 bytes"):
        signer.sign(MESSAGE)


def test_deriving_a_public_key_from_a_short_seed_is_refused() -> None:
    with pytest.raises(DkimError, match="seed is 32 bytes"):
        ed25519_public_key(b"\x00" * 8)


def test_a_pkcs8_private_key_that_is_not_an_octet_string_is_refused() -> None:
    oid = bytes([0x06, 0x03, 0x2B, 0x65, 0x70])  # id-Ed25519
    algorithm = bytes([0x30, len(oid)]) + oid
    not_an_octet_string = bytes([0x02, 0x01, 0x00])  # INTEGER where OCTET STRING belongs
    contents = bytes([0x02, 0x01, 0x00]) + algorithm + not_an_octet_string
    with pytest.raises(DkimError, match="privateKey is not an OCTET STRING"):
        load_private_key(_pem("PRIVATE KEY", bytes([0x30, len(contents)]) + contents))
