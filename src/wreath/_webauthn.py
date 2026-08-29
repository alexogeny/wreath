"""WebAuthn wire formats: CBOR, COSE keys, authenticator data, and signatures.

Everything in here is parsing, framing, and one signature check -- and **the signature check is
delegated, not implemented**. `wreath._auth._ecverify` already verifies ES256
over NIST P-256 and Ed25519 over edwards25519 without a dependency, written for
JWT and pinned against the RFC 8032 and NIST CAVP vectors, so this module reuses
it verbatim. There is no cryptographic algorithm here and there is not meant to
be one.

Everything a caller can be handed here is attacker-controlled -- a browser posts
it -- so the parsers are strict on purpose and every refusal is a `raise`
(`WebAuthnError`, a `ValueError`) rather than an `assert`, which `python -O`
deletes:

* **No indefinite-length CBOR, no tags, no floats, no duplicate map keys, and no
  trailing bytes.** CTAP2's canonical CBOR permits none of them, so accepting
  them only widens what a parser has to agree with a signer about.
* **A length is checked against the buffer before it is used**, so a four-byte
  length header cannot ask for four gigabytes.
* **The DER signature parser demands minimal encoding.** ECDSA signatures are
  malleable enough already; a parser that accepts a non-minimal integer accepts
  several encodings of one signature.
* **A public key that is not on the curve is refused** before any point
  arithmetic happens, because off-curve arithmetic is not ECDSA.
* **The origin allowlist is exact**, with one deliberate exception: an origin on
  a loopback host may carry any port, because `http://localhost:8000` is a
  secure context to every browser and no default set can enumerate the port a
  development server picked. `default_origins` and `origin_accepted` hold that
  line -- no other host over `http://`, no port off loopback, no wildcard.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ._auth._ecverify import on_p256_curve, verify_ed25519, verify_es256
from ._b64 import b64url_decode as _b64url_decode

#: Unpadded base64url, which is how WebAuthn writes bytes into JSON. Re-exported
#: rather than redefined: this module had its own copy of the three-step stdlib
#: chain, as did `_userkit`, `_webpush`, the session cookie and PKCE.
from ._b64 import b64url_encode as b64url_encode

__all__ = [
    "AuthenticatorData",
    "CoseKey",
    "StoredCredential",
    "WebAuthnError",
    "b64url_decode",
    "b64url_encode",
    "cbor_decode",
    "check_client_data",
    "check_rp_id_hash",
    "default_origins",
    "der_signature_to_raw",
    "is_loopback_host",
    "origin_accepted",
    "pack_credential",
    "parse_attestation_object",
    "parse_authenticator_data",
    "parse_cose_key",
    "unpack_credential",
    "verify_signature",
]


class WebAuthnError(ValueError):
    """A ceremony that did not verify, or a wire format that did not parse.

    A `ValueError`, so a caller that already funnels malformed input through one
    `except ValueError` keeps working. The message names what failed, which is
    for the server's own operator: `wreath.users.second_factor_router` answers
    every one of these with the same opaque body, so the detail never reaches the
    caller who triggered it.
    """


def b64url_decode(text: str) -> bytes:
    """The inverse, tolerant of missing padding and nothing else.

    `base64.urlsafe_b64decode` silently *discards* characters outside the
    alphabet, so a value with a space or a `+` in it decodes to something rather
    than failing. The alphabet is checked here first so that it fails.

    Raises:
        WebAuthnError: not a string, or not base64url.
    """
    if not isinstance(text, str):
        raise WebAuthnError("expected a base64url string")
    if not text:
        raise WebAuthnError("value is not base64url")
    try:
        # The alphabet check that used to sit here was a Python set scan over
        # every character of every payload; `_b64.b64url_decode` makes the same
        # refusal inside the decode loop. Empty stays a local check because this
        # caller rejects it and the shared decoder, like native `jose.c`,
        # answers `b""`.
        # `rstrip` because the set this replaced contained `=`, so a *padded*
        # value was accepted here and the shared decoder is unpadded-only.
        # Stripping first keeps every input that used to decode decoding, and
        # every one that used to fail failing: `"QQ=="` still yields a byte,
        # `"Q==="` still raises -- one character is not a base64 length either
        # way -- and `"Q=Q"` is still refused, now for holding `=` rather than
        # for failing to re-pad. Tightening what authentication accepts is not
        # a side effect worth taking on the way past.
        return _b64url_decode(text.rstrip("="))
    except ValueError as exc:  # binascii.Error is a ValueError
        raise WebAuthnError("value is not base64url") from exc


#: Attestation objects nest three deep (map -> map -> value). Eight is generous
#: and still bounds the recursion below, which is otherwise driven by input.
_CBOR_MAX_DEPTH = 8


def cbor_decode(data: bytes) -> Any:
    """Decode exactly one CBOR item from `data`, which must be all of it.

    Raises:
        WebAuthnError: any malformed, non-canonical, or unsupported encoding,
            including trailing bytes after the item.
    """
    value, index = cbor_decode_prefix(data, 0)
    if index != len(data):
        raise WebAuthnError("trailing bytes after the CBOR value")
    return value


def cbor_decode_prefix(data: bytes, index: int = 0) -> tuple[Any, int]:
    """Decode one CBOR item starting at `index`; returns it and the next index.

    Authenticator data puts the COSE public key inline, followed by optional
    extension data, so the only way to know where the key ends is to decode it.
    """
    return _cbor_item(data, index, 0)


def _cbor_head(data: bytes, index: int) -> tuple[int, int, int]:
    """The major type, the argument, and the index just past the header."""
    if index >= len(data):
        raise WebAuthnError("CBOR value is truncated")
    initial = data[index]
    major = initial >> 5
    minor = initial & 0x1F
    index += 1
    if minor < 24:
        return major, minor, index
    if minor == 31:
        raise WebAuthnError("indefinite-length CBOR is not accepted")
    if minor > 27:
        raise WebAuthnError(f"reserved CBOR additional information: {minor}")
    width = 1 << (minor - 24)
    if len(data) - index < width:
        raise WebAuthnError("CBOR length is truncated")
    return major, int.from_bytes(data[index : index + width], "big"), index + width


def _cbor_simple(data: bytes, index: int) -> tuple[Any, int]:
    minor = data[index] & 0x1F
    if minor == 20:
        return False, index + 1
    if minor == 21:
        return True, index + 1
    if minor == 22:
        return None, index + 1
    if minor in (25, 26, 27):
        raise WebAuthnError("CBOR floats are not accepted")
    raise WebAuthnError(f"unsupported CBOR simple value: {minor}")


def _cbor_item(data: bytes, index: int, depth: int) -> tuple[Any, int]:
    if depth > _CBOR_MAX_DEPTH:
        raise WebAuthnError("CBOR nesting is too deep")
    if index >= len(data):
        raise WebAuthnError("CBOR value is truncated")
    if data[index] >> 5 == 7:
        return _cbor_simple(data, index)
    major, argument, index = _cbor_head(data, index)
    if major == 0:
        return argument, index
    if major == 1:
        return -1 - argument, index
    if major in (2, 3):
        # Checked against what is left rather than by slicing, so a length of
        # 2**63 is a refusal instead of an allocation.
        if argument > len(data) - index:
            raise WebAuthnError("CBOR string runs past the end of the buffer")
        chunk = data[index : index + argument]
        if major == 2:
            return chunk, index + argument
        try:
            return chunk.decode("utf-8"), index + argument
        except UnicodeDecodeError as exc:
            raise WebAuthnError("CBOR text is not valid UTF-8") from exc
    if major == 4:
        # Every element costs at least one byte, so a count larger than the
        # remaining buffer cannot be honoured whatever the elements are.
        if argument > len(data) - index:
            raise WebAuthnError("CBOR array is longer than the buffer")
        items = []
        for _ in range(argument):
            item, index = _cbor_item(data, index, depth + 1)
            items.append(item)
        return items, index
    if major == 5:
        if argument > (len(data) - index) // 2:
            raise WebAuthnError("CBOR map is longer than the buffer")
        mapping: dict[Any, Any] = {}
        for _ in range(argument):
            key, index = _cbor_item(data, index, depth + 1)
            if not isinstance(key, (int, str)) or isinstance(key, bool):
                raise WebAuthnError("CBOR map keys must be integers or text")
            if key in mapping:
                raise WebAuthnError(f"duplicate CBOR map key: {key!r}")
            mapping[key], index = _cbor_item(data, index, depth + 1)
        return mapping, index
    raise WebAuthnError("CBOR tags are not accepted")


#: COSE algorithm identifiers. Only the first two are verified here; RS256 is
#: named so that refusing it can say which algorithm it refused.
ES256 = -7
EDDSA = -8
RS256 = -257

_COSE_KTY_OKP = 1
_COSE_KTY_EC2 = 2
_COSE_CRV_P256 = 1
_COSE_CRV_ED25519 = 6


@dataclass(frozen=True, slots=True)
class CoseKey:
    """A parsed, validated WebAuthn public key: ES256 or Ed25519, nothing else.

    `x`/`y` are the P-256 affine coordinates for ES256 and `x` is the 32-byte
    compressed point for Ed25519. Both forms are checked at parse time, so a
    `CoseKey` that exists is one `verify_signature` can use.
    """

    algorithm: int
    x: bytes
    y: bytes = b""


def parse_cose_key(data: bytes | dict[Any, Any]) -> CoseKey:
    """Parse a COSE_Key, from its CBOR bytes or an already-decoded map.

    Raises:
        WebAuthnError: an algorithm other than ES256 or Ed25519 (named in the
            message), a key type or curve that does not match the algorithm, a
            coordinate of the wrong length, or a P-256 point that is not on the
            curve.
    """
    decoded = cbor_decode(data) if isinstance(data, (bytes, bytearray)) else data
    if not isinstance(decoded, dict):
        raise WebAuthnError("a COSE key must be a CBOR map")
    algorithm = decoded.get(3)
    if algorithm == RS256:
        raise WebAuthnError(
            "this authenticator offered RS256 (COSE alg -257); wreath verifies "
            "ES256 (-7) and Ed25519 (-8) only"
        )
    if algorithm not in (ES256, EDDSA):
        raise WebAuthnError(f"unsupported COSE algorithm: {algorithm!r}")
    kty = decoded.get(1)
    curve = decoded.get(-1)
    x = decoded.get(-2)
    if not isinstance(x, (bytes, bytearray)):
        raise WebAuthnError("the COSE key has no public coordinate")
    if algorithm == EDDSA:
        if kty != _COSE_KTY_OKP or curve != _COSE_CRV_ED25519:
            raise WebAuthnError("EdDSA here means OKP over Ed25519 and nothing else")
        if len(x) != 32:
            raise WebAuthnError("an Ed25519 public key is 32 bytes")
        return CoseKey(algorithm=EDDSA, x=bytes(x))
    if kty != _COSE_KTY_EC2 or curve != _COSE_CRV_P256:
        raise WebAuthnError("ES256 here means EC2 over P-256 and nothing else")
    y = decoded.get(-3)
    if not isinstance(y, (bytes, bytearray)):
        raise WebAuthnError("the COSE key has no y coordinate")
    if len(x) != 32 or len(y) != 32:
        raise WebAuthnError("a P-256 coordinate is 32 bytes")
    if not on_p256_curve(int.from_bytes(x, "big"), int.from_bytes(y, "big")):
        # Not pedantry: off-curve arithmetic is not ECDSA, and the point is
        # attacker-supplied at registration.
        raise WebAuthnError("the public key is not a point on P-256")
    return CoseKey(algorithm=ES256, x=bytes(x), y=bytes(y))


def der_signature_to_raw(signature: bytes) -> bytes:
    """Convert `SEQUENCE { r INTEGER, s INTEGER }` to the fixed 64-byte r||s form.

    WebAuthn ES256 signatures are ASN.1 DER, where JWS ES256 -- which
    `_ecverify.verify_es256` takes -- is two fixed-width big-endian integers.
    This is the whole difference between the two, and it is framing rather than
    cryptography.

    Minimal encoding is required. A parser that accepts `02 02 00 7f` alongside
    `02 01 7f` accepts two encodings of one signature, which is a malleability
    nobody needs to hand out for free.

    Raises:
        WebAuthnError: anything that is not exactly that structure.
    """
    if len(signature) < 8 or signature[0] != 0x30:
        raise WebAuthnError("an ES256 signature must be a DER SEQUENCE")
    length = signature[1]
    if length & 0x80:
        # r and s are at most 33 bytes each, so the sequence is under 128 bytes
        # and DER's long form cannot legitimately appear.
        raise WebAuthnError("DER long-form length is not accepted here")
    if length != len(signature) - 2:
        raise WebAuthnError("the DER SEQUENCE length does not match the signature")
    r, index = _der_integer(signature, 2)
    s, index = _der_integer(signature, index)
    if index != len(signature):
        raise WebAuthnError("trailing bytes after the DER signature")
    if not 0 < r < 1 << 256 or not 0 < s < 1 << 256:
        raise WebAuthnError("a P-256 signature component is out of range")
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def _der_integer(data: bytes, index: int) -> tuple[int, int]:
    if len(data) - index < 2 or data[index] != 0x02:
        raise WebAuthnError("expected a DER INTEGER")
    length = data[index + 1]
    index += 2
    if length == 0 or length & 0x80 or length > len(data) - index:
        raise WebAuthnError("malformed DER INTEGER length")
    chunk = data[index : index + length]
    if chunk[0] & 0x80:
        raise WebAuthnError("a signature component must not be negative")
    if length > 1 and chunk[0] == 0x00 and not chunk[1] & 0x80:
        raise WebAuthnError("non-minimal DER INTEGER")
    return int.from_bytes(chunk, "big"), index + length


def verify_signature(key: CoseKey, signed: bytes, signature: bytes) -> bool:
    """Whether `signature` is `key`'s over `signed`. Delegates to `_ecverify`."""
    if key.algorithm == EDDSA:
        return verify_ed25519(key.x, signed, signature)
    return verify_es256(
        int.from_bytes(key.x, "big"),
        int.from_bytes(key.y, "big"),
        signed,
        der_signature_to_raw(signature),
    )


_FLAG_UP = 0x01
_FLAG_UV = 0x04
_FLAG_BE = 0x08
_FLAG_BS = 0x10
_FLAG_AT = 0x40
_FLAG_ED = 0x80

#: 32 bytes of RP ID hash, one of flags, four of signature counter.
_AUTH_DATA_HEADER = 37

#: The largest COSE_Key this build will read out of attested credential data.
#: An ES256 key is 77 bytes on the wire and an Ed25519 one is 46, so this is two
#: orders of magnitude of headroom -- and it is a bound rather than a comment
#: because attested credential data is **not signed** under `none` attestation.
#: `parse_cose_key` ignores COSE labels it does not read, so without this a
#: caller could hang any amount of padding off an otherwise valid key and have
#: it stored verbatim in `SecondFactor.material`.
MAX_COSE_KEY_BYTES = 1024


@dataclass(frozen=True, slots=True)
class AuthenticatorData:
    """The authenticator's own signed statement about a ceremony.

    Fixed 37-byte header, then attested credential data when the AT flag is set
    (registration) and extension data when ED is set. The flags are the part
    that matters to policy: `user_present` is the touch, and `user_verified` is
    the PIN or the fingerprint.
    """

    rp_id_hash: bytes
    flags: int
    sign_count: int
    aaguid: bytes = b""
    credential_id: bytes = b""
    public_key: bytes = field(default=b"", repr=False)

    @property
    def user_present(self) -> bool:
        return bool(self.flags & _FLAG_UP)

    @property
    def user_verified(self) -> bool:
        return bool(self.flags & _FLAG_UV)

    @property
    def backup_eligible(self) -> bool:
        return bool(self.flags & _FLAG_BE)

    @property
    def backed_up(self) -> bool:
        return bool(self.flags & _FLAG_BS)

    @property
    def attested(self) -> bool:
        return bool(self.flags & _FLAG_AT)


def parse_authenticator_data(data: bytes) -> AuthenticatorData:
    """Parse authenticator data, with or without attested credential data.

    Raises:
        WebAuthnError: too short, a credential id longer than the buffer, a
            credential id over 1023 bytes (WebAuthn's own bound), a COSE key
            that does not parse, or bytes left over that no flag accounts for.
    """
    if len(data) < _AUTH_DATA_HEADER:
        raise WebAuthnError("authenticator data is too short")
    rp_id_hash = data[:32]
    flags = data[32]
    sign_count = struct.unpack(">I", data[33:37])[0]
    index = _AUTH_DATA_HEADER
    aaguid = credential_id = public_key = b""
    if flags & _FLAG_AT:
        if len(data) - index < 18:
            raise WebAuthnError("attested credential data is truncated")
        aaguid = data[index : index + 16]
        credential_id_length = struct.unpack(">H", data[index + 16 : index + 18])[0]
        index += 18
        if credential_id_length > 1023:
            raise WebAuthnError("a credential id may not exceed 1023 bytes")
        if credential_id_length > len(data) - index:
            raise WebAuthnError("the credential id runs past the end of the buffer")
        credential_id = data[index : index + credential_id_length]
        index += credential_id_length
        start = index
        decoded, index = cbor_decode_prefix(data, start)
        # Validated here rather than at first use: an unusable key is a refused
        # registration, and refusing it before anything is stored is the point.
        parse_cose_key(decoded)
        public_key = data[start:index]
        if len(public_key) > MAX_COSE_KEY_BYTES:
            raise WebAuthnError(f"a COSE public key may not exceed {MAX_COSE_KEY_BYTES} bytes")
    if flags & _FLAG_ED:
        # Decoded and discarded: nothing here reads an extension, but the bytes
        # have to be consumed to know that the buffer ends where it should.
        _, index = cbor_decode_prefix(data, index)
    if index != len(data):
        raise WebAuthnError("trailing bytes after the authenticator data")
    return AuthenticatorData(
        rp_id_hash=rp_id_hash,
        flags=flags,
        sign_count=sign_count,
        aaguid=aaguid,
        credential_id=credential_id,
        public_key=public_key,
    )


def check_rp_id_hash(auth_data: AuthenticatorData, rp_id: str) -> None:
    """Refuse authenticator data signed for a different relying party.

    Raises:
        WebAuthnError: the hash is not SHA-256 of `rp_id`.
    """
    expected = hashlib.sha256(rp_id.encode("utf-8")).digest()
    if not hmac.compare_digest(expected, auth_data.rp_id_hash):
        raise WebAuthnError("the authenticator signed for a different RP ID")


def parse_attestation_object(data: bytes) -> AuthenticatorData:
    """Parse an attestation object and return the authenticator data inside it.

    **Only `none` attestation is accepted**, which is what the plan's non-goals
    settle: verifying an attestation statement means a metadata service, a
    network dependency, and a different product. Since
    `begin_webauthn_registration` asks for `attestation: "none"`, and the client
    is required to replace the statement with a none one when it does, any other
    format arriving here means the ceremony was not the one that was begun.

    Raises:
        WebAuthnError: not a CBOR map, missing members, a format other than
            `none`, or authenticator data carrying no attested credential.
    """
    decoded = cbor_decode(data)
    if not isinstance(decoded, dict):
        raise WebAuthnError("an attestation object must be a CBOR map")
    fmt = decoded.get("fmt")
    raw = decoded.get("authData")
    if not isinstance(raw, (bytes, bytearray)):
        raise WebAuthnError("the attestation object carries no authenticator data")
    if fmt != "none":
        raise WebAuthnError(
            f"attestation format {fmt!r} is not accepted; wreath requests and "
            "accepts none attestation"
        )
    auth_data = parse_authenticator_data(bytes(raw))
    if not auth_data.credential_id:
        raise WebAuthnError("the registration carries no attested credential data")
    return auth_data


#: The two loopback names. 127.0.0.0/8 is matched arithmetically below rather
#: than listed, and `::1` appears in both the bare and the bracketed form
#: because an RP ID is a bare host while an origin brackets an IPv6 literal.
_LOOPBACK_NAMES = frozenset(("localhost", "::1", "[::1]"))


def is_loopback_host(host: str) -> bool:
    """Whether a browser treats `host` as a secure context over plain `http://`.

    `localhost`, `::1`, and anything in 127.0.0.0/8 -- and **nothing else**.
    Not `foo.localhost`, which is a different host that browsers happen to treat
    as trustworthy too, and emphatically not `localhost.example.com`, which is
    somebody else's name that merely begins the same way.
    """
    name = host.lower()
    if name in _LOOPBACK_NAMES:
        return True
    parts = name.split(".")
    if len(parts) != 4 or parts[0] != "127":
        return False
    return all(part.isdigit() and len(part) <= 3 and int(part) <= 255 for part in parts)


def default_origins(rp_id: str) -> tuple[str, ...]:
    """The origins a relying party accepts when its caller named none.

    `https://{rp_id}`, which is right for a site served at its apex over TLS --
    **and `http://` as well when `rp_id` is loopback**. That exception is not a
    convenience: `http://localhost:8000` is the first setup anybody runs, it is
    a secure context as far as every browser is concerned, and WebAuthn genuinely
    works there, so an https-only default made the single most common development
    configuration fail on a check that had nothing wrong with it.

    It is loopback and only loopback. There is no wildcard here, and no other
    host is ever admitted over `http://` -- an origin allowlist that widens by
    default is the vulnerability the ceremony exists to prevent.

    Ports are not in the default set because there is no way to enumerate them;
    `origin_accepted` lets a *loopback* origin carry any port instead, which is
    what makes `http://localhost:8000` match `http://localhost` and what nothing
    off loopback gets.
    """
    if not is_loopback_host(rp_id):
        return (f"https://{rp_id}",)
    host = f"[{rp_id}]" if ":" in rp_id else rp_id
    return (f"https://{host}", f"http://{host}")


def _authority(origin: str) -> tuple[str, str, str] | None:
    """`(scheme, host, port)` for a bare `scheme://host[:port]`, or None.

    Deliberately not `urlsplit`: this is used to decide whether an origin may be
    widened, so anything with a path, userinfo, or a second colon in it has to
    fall out as unparseable rather than be normalized into something that
    matches. `None` means "no widening", never "close enough".
    """
    scheme, _, rest = origin.partition("://")
    if not scheme:
        return None
    if rest.startswith("["):
        host, closed, tail = rest.partition("]")
        if not closed or (tail and not tail.startswith(":")):
            return None
        host, port = host[1:], tail[1:]
    else:
        host, _, port = rest.partition(":")
    if not host or not host.isascii() or not port.isascii():
        return None
    # A colon is legal only inside the brackets of an IPv6 literal, which the
    # branch above has already stripped; anywhere else it is a second authority
    # separator and the string is not a bare origin.
    if any(character in host for character in "/?#@[]"):
        return None
    if any(character in port for character in "/?#@[]:"):
        return None
    return scheme, host, port


def origin_accepted(origin: str, accepted: Sequence[str]) -> bool:
    """Whether `origin` is one of `accepted`. Exact, but for a loopback port.

    An exact string match first, which is what every non-loopback origin ever
    gets. The one relaxation: an origin on a **loopback host** matches an
    accepted entry that names the same scheme and host with no port at all,
    because a development server picks its port and no default set can enumerate
    them. Nothing is given up by it -- the credential is scoped to the RP ID
    either way, and a browser only ever sends a loopback credential to a
    loopback origin -- and nothing else is widened: a different host, a
    different scheme, or a port that is not digits all fail.
    """
    if origin in accepted:
        return True
    parsed = _authority(origin)
    if parsed is None:
        return False
    scheme, host, port = parsed
    if not port.isdigit() or not is_loopback_host(host):
        return False
    bare = f"{scheme}://[{host}]" if ":" in host else f"{scheme}://{host}"
    return bare in accepted


@dataclass(frozen=True, slots=True)
class ClientData:
    """The browser's own statement about the ceremony, already checked."""

    type: str
    origin: str


def check_client_data(
    client_data_json: bytes,
    *,
    expected_type: str,
    challenge: bytes,
    origins: tuple[str, ...],
) -> ClientData:
    """Check `type`, `challenge` and `origin`, each on its own terms.

    Three separate refusals rather than one combined check, because they defend
    against three different things: the type stops an assertion being replayed
    into a registration, the challenge stops a recorded ceremony being replayed
    at all, and the origin is the reason the ceremony exists -- a signature
    collected by a phishing site names that site here, and so does not match.
    The origin is matched by `origin_accepted`, which is exact for every host
    except a loopback one, where the port is allowed to vary.

    Raises:
        WebAuthnError: not JSON, not an object, or any of the three mismatching.
    """
    try:
        decoded = json.loads(client_data_json)
    except ValueError as exc:
        raise WebAuthnError("client data is not JSON") from exc
    if not isinstance(decoded, dict):
        raise WebAuthnError("client data is not a JSON object")
    if decoded.get("type") != expected_type:
        raise WebAuthnError(
            f"client data type is {decoded.get('type')!r}, expected {expected_type!r}"
        )
    presented = decoded.get("challenge")
    if not isinstance(presented, str):
        raise WebAuthnError("client data carries no challenge")
    if not hmac.compare_digest(b64url_decode(presented), challenge):
        raise WebAuthnError("the ceremony answered a different challenge")
    origin = decoded.get("origin")
    if not isinstance(origin, str) or not origin_accepted(origin, origins):
        raise WebAuthnError(f"origin {origin!r} is not one this relying party accepts")
    if decoded.get("crossOrigin") is True:
        # The origin above is the *frame's*, so an embedded ceremony passes it
        # while the user is looking at somebody else's page.
        raise WebAuthnError("a cross-origin ceremony is not accepted")
    return ClientData(type=expected_type, origin=origin)


#: Magic and version for `SecondFactor.material` on a webauthn credential. The
#: stored shape is a `bytes` blob by design (stage one settled the record), so
#: it names itself: a row written by a later format is rejected rather than
#: misread as this one.
_MATERIAL_MAGIC = b"wa1"
_MATERIAL_HEADER = struct.Struct(">3sBBHH")


@dataclass(frozen=True, slots=True)
class StoredCredential:
    """What a `SecondFactor` of kind `webauthn` carries in its `material`."""

    credential_id: bytes
    public_key: bytes = field(repr=False)
    user_verified: bool = False


def pack_credential(credential_id: bytes, public_key: bytes, *, user_verified: bool) -> bytes:
    """Encode a registered credential for `SecondFactor.material`.

    `user_verified` records whether the *registration* was verified (a PIN or a
    biometric rather than a bare touch). It is recorded rather than enforced,
    because whether that matters is a policy question and policy lives above
    this module.

    Raises:
        WebAuthnError: a credential id or key that will not fit the header's
            16-bit lengths, or an empty one. A `WebAuthnError` rather than a bare
            `ValueError` -- it is a subclass, so nothing catching `ValueError`
            changes -- because a caller that funnels the rest of the ceremony
            through `except WebAuthnError` would otherwise answer 500 to an
            oversized key instead of refusing the registration.
    """
    if not credential_id or len(credential_id) > 0xFFFF:
        raise WebAuthnError("a credential id must be 1..65535 bytes")
    if not public_key or len(public_key) > 0xFFFF:
        raise WebAuthnError("a public key must be 1..65535 bytes")
    header = _MATERIAL_HEADER.pack(
        _MATERIAL_MAGIC, 1, int(user_verified), len(credential_id), len(public_key)
    )
    return header + credential_id + public_key


def unpack_credential(material: bytes) -> StoredCredential:
    """Decode what `pack_credential` wrote.

    Raises:
        WebAuthnError: the blob is truncated, not this format, or a version this
            build does not know.
    """
    if len(material) < _MATERIAL_HEADER.size:
        raise WebAuthnError("stored credential material is truncated")
    magic, version, flags, id_length, key_length = _MATERIAL_HEADER.unpack(
        material[: _MATERIAL_HEADER.size]
    )
    if magic != _MATERIAL_MAGIC:
        raise WebAuthnError("stored credential material is not a webauthn credential")
    if version != 1:
        raise WebAuthnError(f"unknown webauthn credential material version: {version}")
    body = material[_MATERIAL_HEADER.size :]
    if len(body) != id_length + key_length:
        raise WebAuthnError("stored credential material is truncated")
    return StoredCredential(
        credential_id=body[:id_length],
        public_key=body[id_length:],
        user_verified=bool(flags & 1),
    )
