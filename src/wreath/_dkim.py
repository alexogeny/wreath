"""DKIM signing (RFC 6376), stdlib-only.

Wreath already sends the two messages whose delivery failure is most expensive --
email verification and password reset -- and until this module existed it sent
them unsigned. Since May 2026 that is not a deliverability preference: Google,
Yahoo and Microsoft reject non-compliant bulk mail with a permanent 550, and
above 5,000 messages a day SPF, DKIM *and* DMARC alignment are all required.

Two algorithms, both required by RFC 8463 / RFC 6376:

* `rsa-sha256` -- RSASSA-PKCS1-v1_5, the one every verifier accepts.
* `ed25519-sha256` -- RFC 8463, smaller keys, not yet universally verified. Send
  both when you use it; a verifier that does not know `ed25519-sha256` treats the
  signature as absent rather than as broken.

**A wrong signature is worse than no signature**, because it is an authenticated
*failure* rather than an unauthenticated message: DMARC sees a DKIM result of
`fail` instead of `none`. Two consequences shape this module. Canonicalisation is
written against the RFC's own text rather than against what one verifier happens
to accept, and every signature is verified with the public exponent before it is
returned -- one cheap modexp that turns a corrupted signing key, a bit flip, or a
CRT fault into a raised error instead of mail that arrives pre-condemned.

Ed25519 key derivation and signing are complete fixed-width native operations.
Their portable C implementation owns SHA-512, scalar reduction and the
constant-shape fixed-base multiplication; no Python integer holds secret field
or scalar material.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field
from typing import Final

from ._native import _core

__all__ = [
    "DkimError",
    "DkimSigner",
    "Ed25519Key",
    "RsaKey",
    "canonicalize_body_relaxed",
    "canonicalize_header_relaxed",
    "load_private_key",
]

#: Headers signed when a caller names none. `From` is mandatory per RFC 6376
#: §5.4; the rest are the set every major verifier expects to be covered, and
#: omitting `Subject` in particular invites a replay that keeps the signature
#: valid while changing what the reader sees.
DEFAULT_SIGNED_HEADERS: Final = (
    "from",
    "to",
    "cc",
    "subject",
    "date",
    "message-id",
    "mime-version",
    "content-type",
    "content-transfer-encoding",
    "list-unsubscribe",
    "list-unsubscribe-post",
)

#: RFC 8017 §9.2, the DER `DigestInfo` prefix for SHA-256.
_SHA256_DIGEST_INFO: Final = bytes.fromhex("3031300d060960864801650304020105000420")

#: RFC 6376 §3.5: a header field is at most 998 octets before folding.
_MAX_LINE = 78


class DkimError(Exception):
    """A key could not be read, or a signature failed its own verification."""


_WSP_RUN = re.compile(rb"[ \t]+")


def canonicalize_header_relaxed(name: str, value: str) -> bytes:
    """One header field in `relaxed` form, ending in CRLF.

    RFC 6376 §3.4.2: lower-case the field name, unfold the value, collapse each
    run of whitespace to a single space, drop whitespace either side of the
    colon, and strip trailing whitespace.

    Unfolding happens *before* the whitespace collapse, which is the step that
    is easy to get backwards: a value folded across three lines and a value on
    one line must canonicalise identically, or a message that any MTA refolds in
    transit arrives with a signature over bytes nobody can reproduce.
    """
    unfolded = value.replace("\r\n", "").replace("\n", "").replace("\r", "")
    collapsed = _WSP_RUN.sub(b" ", unfolded.encode("utf-8"))
    return name.strip().lower().encode("ascii") + b":" + collapsed.strip() + b"\r\n"


canonicalize_body_relaxed = _core.dkim_canonicalize_body


@dataclass(frozen=True, slots=True)
class RsaKey:
    """An RSA private key, as the integers RFC 8017 signs with.

    `p`, `q`, `dp`, `dq` and `qinv` are optional: when all five are present the
    signature is computed through the Chinese Remainder Theorem, which is about
    four times less modular exponentiation than the straight `pow(m, d, n)` and
    is what every real key file already carries.
    """

    n: int
    e: int
    d: int = field(repr=False)
    p: int = field(default=0, repr=False)
    q: int = field(default=0, repr=False)
    dp: int = field(default=0, repr=False)
    dq: int = field(default=0, repr=False)
    qinv: int = field(default=0, repr=False)

    @property
    def size_bytes(self) -> int:
        return (self.n.bit_length() + 7) // 8


@dataclass(frozen=True, slots=True)
class Ed25519Key:
    """An Ed25519 private key: the 32-byte seed, per RFC 8032."""

    seed: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.seed, (bytes, bytearray)):
            raise TypeError("Ed25519 private seed must be bytes")
        object.__setattr__(self, "seed", bytes(self.seed))


def _der_read(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Read one DER TLV at `offset`; return (tag, contents, next offset)."""
    if offset + 2 > len(data):
        raise DkimError("truncated DER")
    tag = data[offset]
    length = data[offset + 1]
    cursor = offset + 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or cursor + count > len(data):
            raise DkimError("unsupported DER length")
        length = int.from_bytes(data[cursor : cursor + count], "big")
        cursor += count
    if cursor + length > len(data):
        raise DkimError("truncated DER")
    return tag, data[cursor : cursor + length], cursor + length


def _der_ints(sequence: bytes, count: int) -> list[int]:
    values: list[int] = []
    offset = 0
    for _ in range(count):
        tag, contents, offset = _der_read(sequence, offset)
        if tag != 0x02:
            raise DkimError("expected a DER INTEGER")
        values.append(int.from_bytes(contents, "big"))
    return values


def _pem_body(text: str, label: str) -> bytes | None:
    marker = f"-----BEGIN {label}-----"
    start = text.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = text.find(f"-----END {label}-----", start)
    if end < 0:
        raise DkimError(f"unterminated PEM block for {label}")
    return base64.b64decode("".join(text[start:end].split()))


def load_private_key(pem: str | bytes) -> RsaKey | Ed25519Key:
    """Read a PEM private key: PKCS#1 RSA, or PKCS#8 wrapping RSA or Ed25519.

    Raises:
        DkimError: the text is not a PEM key this module can sign with.
    """
    text = pem.decode("ascii") if isinstance(pem, bytes) else pem
    body = _pem_body(text, "RSA PRIVATE KEY")
    if body is not None:
        return _rsa_from_pkcs1(body)
    body = _pem_body(text, "PRIVATE KEY")
    if body is None:
        raise DkimError("no PKCS#1 or PKCS#8 private key found in the PEM text")
    tag, contents, _ = _der_read(body, 0)
    if tag != 0x30:
        raise DkimError("PKCS#8 key is not a SEQUENCE")
    # PrivateKeyInfo ::= { version INTEGER, algorithm AlgorithmIdentifier,
    #                      privateKey OCTET STRING }
    _, _, offset = _der_read(contents, 0)
    alg_tag, algorithm, offset = _der_read(contents, offset)
    if alg_tag != 0x30:
        raise DkimError("PKCS#8 AlgorithmIdentifier is not a SEQUENCE")
    oid_tag, oid, _ = _der_read(algorithm, 0)
    if oid_tag != 0x06:
        raise DkimError("PKCS#8 algorithm is not an OBJECT IDENTIFIER")
    key_tag, key_bytes, _ = _der_read(contents, offset)
    if key_tag != 0x04:
        raise DkimError("PKCS#8 privateKey is not an OCTET STRING")
    if oid == bytes.fromhex("2a864886f70d010101"):  # rsaEncryption
        return _rsa_from_pkcs1(key_bytes)
    if oid == bytes.fromhex("2b6570"):  # id-Ed25519
        seed_tag, seed, _ = _der_read(key_bytes, 0)
        if seed_tag != 0x04 or len(seed) != 32:
            raise DkimError("Ed25519 private key is not a 32-byte OCTET STRING")
        return Ed25519Key(seed)
    raise DkimError("unsupported PKCS#8 key algorithm")


def _rsa_from_pkcs1(der: bytes) -> RsaKey:
    tag, contents, _ = _der_read(der, 0)
    if tag != 0x30:
        raise DkimError("RSAPrivateKey is not a SEQUENCE")
    values = _der_ints(contents, 9)
    _, n, e, d, p, q, dp, dq, qinv = values
    if n <= 0 or d <= 0:
        raise DkimError("RSAPrivateKey is missing its modulus or exponent")
    return RsaKey(n=n, e=e, d=d, p=p, q=q, dp=dp, dq=dq, qinv=qinv)


def _pkcs1_encode(digest: bytes, size: int) -> bytes:
    suffix = _SHA256_DIGEST_INFO + digest
    padding = size - len(suffix) - 3
    if padding < 8:
        raise DkimError("RSA key is too small to sign a SHA-256 digest")
    return b"\x00\x01" + b"\xff" * padding + b"\x00" + suffix


def _rsa_sign(key: RsaKey, message: bytes) -> bytes:
    encoded = _pkcs1_encode(hashlib.sha256(message).digest(), key.size_bytes)
    m = int.from_bytes(encoded, "big")
    if key.p and key.q and key.dp and key.dq and key.qinv:
        m1 = pow(m, key.dp, key.p)
        m2 = pow(m, key.dq, key.q)
        h = (key.qinv * (m1 - m2)) % key.p
        signature = (m2 + h * key.q) % key.n
    else:
        signature = pow(m, key.d, key.n)
    # Verify what we are about to publish. One modexp with e=65537 is a rounding
    # error next to the signing cost, and it is the difference between a raised
    # error and mail that arrives with DKIM=fail -- which DMARC treats as worse
    # than unsigned. It also catches a CRT computation corrupted by a fault,
    # which is the classic way a private key leaks out of an RSA implementation.
    if pow(signature, key.e, key.n) != m:
        raise DkimError("RSA signature failed its own verification")
    return signature.to_bytes(key.size_bytes, "big")


def _ed25519_sign(seed: bytes, message: bytes) -> bytes:
    if len(seed) != 32:
        raise DkimError("an Ed25519 seed is 32 bytes")
    return _core.curve_ed_sign(seed, message)


def ed25519_public_key(seed: bytes) -> bytes:
    """The 32-byte public key for an Ed25519 `seed`, for publishing in DNS."""
    if len(seed) != 32:
        raise DkimError("an Ed25519 seed is 32 bytes")
    return _core.curve_ed_public_key(seed)


def _fold(value: str) -> str:
    """Fold a long header value at whitespace, per RFC 5322 §2.2.3.

    Folding is safe for a DKIM signature because `relaxed` header
    canonicalisation unfolds before it hashes, so the verifier sees what the
    signer hashed however the line was broken.
    """
    out: list[str] = []
    line = ""
    for token in value.split(" "):
        if line and len(line) + 1 + len(token) > _MAX_LINE:
            out.append(line)
            line = " " + token
        else:
            line = f"{line} {token}" if line else token
    out.append(line)
    return "\r\n\t".join(out)


@dataclass(frozen=True, slots=True)
class DkimSigner:
    """Signs outgoing mail for one `domain`/`selector` pair.

    The public half goes in DNS at `<selector>._domainkey.<domain>` as a TXT
    record; `wreath.doctor.check_email_deliverability` reports whether it is
    actually there, which is the failure this class is otherwise silent about.

    Args:
        domain: The signing domain, which must align with the `From` address
            for DMARC to pass.
        selector: Names the DNS record holding the public key.
        key: An `RsaKey` or `Ed25519Key`, usually from `load_private_key`.
        headers: Which header fields to sign, lower-cased. Defaults to
            `DEFAULT_SIGNED_HEADERS`; `from` is added if a caller omits it.
    """

    domain: str
    selector: str
    key: RsaKey | Ed25519Key = field(repr=False)
    headers: tuple[str, ...] = DEFAULT_SIGNED_HEADERS

    @property
    def algorithm(self) -> str:
        return "ed25519-sha256" if isinstance(self.key, Ed25519Key) else "rsa-sha256"

    def sign(self, message: bytes, *, now: float | None = None) -> str:
        """Return the `DKIM-Signature` header value for a serialised `message`.

        `message` is the full RFC 5322 message with CRLF line endings, exactly
        as it will be handed to the MTA. Sign the bytes you send: re-serialising
        after signing is the most common way a valid signature becomes an
        invalid one.

        Raises:
            DkimError: the message has no header/body separator, or the
                signature failed its own verification.
        """
        header_block, body = _split_message(message)
        fields = _parse_headers(header_block)
        present = {name for name, _ in fields}
        signed = tuple(dict.fromkeys(("from", *(h.lower() for h in self.headers))))
        signed = tuple(name for name in signed if name in present)
        body_hash = base64.b64encode(
            hashlib.sha256(canonicalize_body_relaxed(body)).digest()
        ).decode("ascii")

        stamp = int(time.time() if now is None else now)
        tags = (
            f"v=1; a={self.algorithm}; c=relaxed/relaxed; d={self.domain}; "
            f"s={self.selector}; t={stamp}; h={':'.join(signed)}; bh={body_hash}; b="
        )
        # Each signed field is taken from the bottom of the header block up, so a
        # message carrying two `Received` lines signs the newest first. RFC 6376
        # §5.4.2; getting it wrong only shows on messages with duplicate fields,
        # which is why it is stated rather than assumed.
        remaining = dict[str, list[str]]()
        for name, value in fields:
            remaining.setdefault(name, []).append(value)
        canonical = bytearray()
        for name in signed:
            values = remaining.get(name)
            if values:
                canonical += canonicalize_header_relaxed(name, values.pop())
        # The DKIM-Signature header itself is signed with an empty `b=` and,
        # uniquely, without the trailing CRLF.
        canonical += canonicalize_header_relaxed("dkim-signature", tags).rstrip(b"\r\n")

        if isinstance(self.key, Ed25519Key):
            raw = _ed25519_sign(self.key.seed, bytes(canonical))
        else:
            raw = _rsa_sign(self.key, bytes(canonical))
        return _fold(tags + base64.b64encode(raw).decode("ascii"))


def _split_message(message: bytes) -> tuple[bytes, bytes]:
    for separator in (b"\r\n\r\n", b"\n\n"):
        index = message.find(separator)
        if index >= 0:
            return message[:index], message[index + len(separator) :]
    raise DkimError("message has no header/body separator")


def _parse_headers(block: bytes) -> list[tuple[str, str]]:
    """Split a header block into (lower-cased name, raw folded value) pairs."""
    text = block.decode("utf-8", "surrogateescape").replace("\r\n", "\n")
    fields: list[tuple[str, str]] = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and fields:
            name, value = fields[-1]
            fields[-1] = (name, f"{value}\r\n{line}")
        elif ":" in line:
            name, _, value = line.partition(":")
            fields.append((name.strip().lower(), value))
    return fields


def dkim_record(signer: DkimSigner) -> str:
    """The TXT record `selector._domainkey.domain` must publish for `signer`.

    Printed by `wreath doctor` when the record is missing, so the fix is in the
    same output as the finding rather than in a search engine.
    """
    if isinstance(signer.key, Ed25519Key):
        public = base64.b64encode(ed25519_public_key(signer.key.seed)).decode("ascii")
        return f"v=DKIM1; k=ed25519; p={public}"
    return "v=DKIM1; k=rsa; p=<the base64 SubjectPublicKeyInfo for this key>"


def constant_time_equal(left: bytes, right: bytes) -> bool:
    """`hmac.compare_digest`, re-exported so callers need not import `hmac`."""
    return hmac.compare_digest(left, right)
