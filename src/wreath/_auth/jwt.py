"""JWT verification: a thin facade over the native ``jose`` accelerators.

The hot, non-crypto-inventing pieces (base64url, compact split, HS* HMAC, and
registered-claim checks) run in ``wreath._native._core`` when it is present, with
stdlib fallbacks so verification also works under ``WREATH_PURE=1``. RSA (RS*/PS*)
verification is done here with CPython's bigint ``pow`` and the stdlib — RSA
verify is a public-key operation whose only risk is correctness, not timing, and
its padding checks are far safer read against the stdlib than hand-written in C.

Algorithm confusion is prevented structurally: a verifier's algorithm allow-list
is frozen at construction, the token ``alg`` may only *select from* that list
(so ``alg=none`` or any unlisted alg is rejected before any verify runs), and
each key is bound to exactly one algorithm *family* — a symmetric secret can
never satisfy an RS*/PS* check and an RSA public key can never satisfy an HS*
check.

TODO(pure-twin): a byte-identical ``wreath._pure.jose`` twin and a Project
Wycheproof + ``cryptography`` differential-oracle harness are deferred per the
C-first directive. The review fork MUST run RSA/PSS test vectors against an
external oracle before this is trusted in production.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .._native import _core
from .models import Identity

# Resolve the native accelerators once, tolerating a _core built before jose.c
# existed (each falls back to a stdlib implementation below). Mirrors the Cedar
# engine's getattr-with-None-fallback selection.
_native_b64 = getattr(_core, "jose_b64url_decode", None) if _core is not None else None
_native_parse = getattr(_core, "jose_parse", None) if _core is not None else None
_native_hs = getattr(_core, "jose_verify_hs", None) if _core is not None else None
_native_claims = getattr(_core, "jose_validate_claims", None) if _core is not None else None

__all__ = [
    "InvalidToken",
    "JwtError",
    "JwtVerifier",
    "RsaPublicKey",
    "SymmetricKey",
    "UnsupportedAlgorithm",
    "default_identity",
    "key_from_jwk",
    "key_from_pem",
]

# Hard caps (decoded bytes) to keep a hostile token from feeding a giant string
# to the JSON parser or allocating without bound.
_MAX_SEGMENT_BYTES = 16 * 1024
# Absolute compact-token ceiling, mirroring native jose.c JOSE_ABS_MAX_TOKEN.
_MAX_TOKEN_BYTES = 1 << 20


class JwtError(Exception):
    """Base class for JWT construction/configuration errors."""


class UnsupportedAlgorithm(JwtError):
    """A requested algorithm is not implemented in this build."""


class InvalidToken(JwtError):
    """A token could not be constructed for signing (not raised on verify)."""


# ---------------------------------------------------------------------------
# Algorithm registry. ES*/EdDSA are intentionally absent from this cut and must
# raise rather than silently pass; the structure leaves room to add them later.
# ---------------------------------------------------------------------------

_HS = {"HS256": "sha256", "HS384": "sha384", "HS512": "sha512"}
_RS = {"RS256": "sha256", "RS384": "sha384", "RS512": "sha512"}
_PS = {"PS256": "sha256", "PS384": "sha384", "PS512": "sha512"}

# Algorithms recognised but deliberately not implemented in this cut. Requesting
# one is a loud configuration error, never a silent accept.
_DEFERRED = frozenset(
    {"ES256", "ES384", "ES512", "ES256K", "EdDSA", "Ed25519", "Ed448", "none"}
)

_FAMILY = {**{a: "HS" for a in _HS}, **{a: "RSA" for a in _RS}, **{a: "RSA" for a in _PS}}
_HASH = {**_HS, **_RS, **_PS}
_SUPPORTED = frozenset(_FAMILY)

# DER DigestInfo prefixes for EMSA-PKCS1-v1.5 (RFC 8017 §9.2, notes).
_DIGEST_INFO = {
    "sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
    "sha384": bytes.fromhex("3041300d060960864801650304020205000430"),
    "sha512": bytes.fromhex("3051300d060960864801650304020305000440"),
}


# ---------------------------------------------------------------------------
# Keys. Each key knows the one family it may be used with.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SymmetricKey:
    """An HMAC shared secret. Usable only for HS256/384/512."""

    secret: bytes
    family: str = field(default="HS", init=False)


@dataclass(frozen=True, slots=True)
class RsaPublicKey:
    """An RSA public key (modulus/exponent). Usable only for RS*/PS*."""

    n: int
    e: int
    family: str = field(default="RSA", init=False)

    @property
    def size(self) -> int:
        """Modulus length in bytes (k)."""
        return (self.n.bit_length() + 7) // 8


def key_from_jwk(jwk: Mapping[str, Any]) -> SymmetricKey | RsaPublicKey:
    """Build a key from a single JWK. Supports ``oct`` (HMAC) and ``RSA``."""
    kty = jwk.get("kty")
    if kty == "oct":
        return SymmetricKey(_b64url_decode(jwk["k"]))
    if kty == "RSA":
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
        if n <= 0 or e <= 0:
            raise JwtError("invalid RSA JWK parameters")
        return RsaPublicKey(n, e)
    if kty in {"EC", "OKP"}:
        raise UnsupportedAlgorithm(
            f"JWK key type {kty!r} (EC/EdDSA) is not supported in this build"
        )
    raise JwtError(f"unsupported JWK key type: {kty!r}")


def key_from_pem(pem: str | bytes) -> RsaPublicKey:
    """Parse an RSA public key from a PEM ``PUBLIC KEY`` (SPKI) or
    ``RSA PUBLIC KEY`` (PKCS#1) block, without a third-party dependency."""
    text = pem.decode("ascii") if isinstance(pem, (bytes, bytearray)) else pem
    body = []
    kind = "spki"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("-----BEGIN"):
            kind = "pkcs1" if "RSA PUBLIC KEY" in line else "spki"
            continue
        if line.startswith("-----END") or not line:
            continue
        body.append(line)
    der = base64.b64decode("".join(body))
    n, e = _rsa_public_from_der(der, kind)
    if n <= 0 or e <= 0:
        raise JwtError("invalid RSA public key")
    return RsaPublicKey(n, e)


# ---- minimal DER reader (RSA public keys only) ----------------------------


def _der_read_tlv(data: bytes, pos: int) -> tuple[int, bytes, int]:
    """Return (tag, value_bytes, next_pos) for the TLV at ``pos``."""
    tag = data[pos]
    pos += 1
    length = data[pos]
    pos += 1
    if length & 0x80:
        num = length & 0x7F
        if num == 0 or num > 4:
            raise JwtError("unsupported DER length")
        length = int.from_bytes(data[pos : pos + num], "big")
        pos += num
    value = data[pos : pos + length]
    if len(value) != length:
        raise JwtError("truncated DER value")
    return tag, value, pos + length


def _der_int(value: bytes) -> int:
    return int.from_bytes(value, "big")


def _rsa_public_from_der(der: bytes, kind: str) -> tuple[int, int]:
    if kind == "spki":
        # SPKI: SEQ { SEQ { OID, params }, BIT STRING { RSAPublicKey } }
        _, seq, _ = _der_read_tlv(der, 0)
        _, _alg, after_alg = _der_read_tlv(seq, 0)
        tag, bitstring, _ = _der_read_tlv(seq, after_alg)
        if tag != 0x03:  # BIT STRING
            raise JwtError("SPKI missing subjectPublicKey bit string")
        # First byte of a BIT STRING is the count of unused bits (expected 0).
        inner = bitstring[1:]
        der = inner
    # PKCS#1 RSAPublicKey: SEQ { INTEGER n, INTEGER e }
    _, seq, _ = _der_read_tlv(der, 0)
    tag_n, n_bytes, after_n = _der_read_tlv(seq, 0)
    tag_e, e_bytes, _ = _der_read_tlv(seq, after_n)
    if tag_n != 0x02 or tag_e != 0x02:
        raise JwtError("RSAPublicKey expects two INTEGERs")
    return _der_int(n_bytes), _der_int(e_bytes)


# ---------------------------------------------------------------------------
# Primitive helpers: native when available, stdlib fallback otherwise.
# ---------------------------------------------------------------------------


def _b64url_decode(data: str) -> bytes:
    if _native_b64 is not None:
        return _native_b64(data)
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _parse_compact(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    """Split + decode + JSON-parse a compact JWS. Raises ValueError if malformed."""
    if _native_parse is not None:
        header_bytes, payload_bytes, signing_input, signature = _native_parse(
            token, _MAX_SEGMENT_BYTES
        )
    else:
        # Enforce the same hard size caps as native jose_parse so the DoS guard
        # (a giant segment fed to the JSON parser) holds under WREATH_PURE=1 and
        # whenever _core is unavailable, not only on the native path.
        if len(token) > _MAX_TOKEN_BYTES:
            raise ValueError("compact JWT exceeds maximum size")
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            raise ValueError("compact JWT must have three non-empty segments")
        # Bound each base64url segment before decoding (decoded ~= 3/4 * encoded).
        max_segment_b64 = (_MAX_SEGMENT_BYTES * 4) // 3 + 4
        if any(len(part) > max_segment_b64 for part in parts):
            raise ValueError("JWT segment exceeds size cap")
        header_bytes = _b64url_decode(parts[0])
        payload_bytes = _b64url_decode(parts[1])
        signature = _b64url_decode(parts[2])
        signing_input = (parts[0] + "." + parts[1]).encode("ascii")
    header = json.loads(header_bytes)
    claims = json.loads(payload_bytes)
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise ValueError("JWT header and payload must be JSON objects")
    return header, claims, signing_input, signature


def peek_header(token: str) -> dict[str, Any] | None:
    """Decode only the JOSE header (for ``kid``/``alg`` lookup before verify).

    Returns None on any malformation. Does not validate the signature — callers
    must still run :func:`verify_jwt`.
    """
    try:
        first = token.split(".", 1)[0]
        if not first:
            return None
        header = json.loads(_b64url_decode(first))
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
    return header if isinstance(header, dict) else None


def _verify_hs(alg: str, secret: bytes, signing_input: bytes, signature: bytes) -> bool:
    digestmod = _HASH[alg]
    if _native_hs is not None:
        return bool(_native_hs(digestmod, secret, signing_input, signature))
    expected = hmac.new(secret, signing_input, digestmod).digest()
    return hmac.compare_digest(expected, signature)


# ---- RSA verification (stdlib bigint + hashlib) ---------------------------


def _mgf1(seed: bytes, length: int, hash_name: str) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.new(hash_name, seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:length])


def _verify_rs(
    key: RsaPublicKey, hash_name: str, signing_input: bytes, signature: bytes
) -> bool:
    k = key.size
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= key.n:
        return False
    m = pow(s, key.e, key.n)
    em = m.to_bytes(k, "big")
    digest = hashlib.new(hash_name, signing_input).digest()
    t = _DIGEST_INFO[hash_name] + digest
    if k < len(t) + 11:
        return False
    ps_len = k - len(t) - 3
    expected = b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def _verify_ps(
    key: RsaPublicKey, hash_name: str, signing_input: bytes, signature: bytes
) -> bool:
    k = key.size
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= key.n:
        return False
    em_bits = key.n.bit_length() - 1
    em_len = (em_bits + 7) // 8
    m = pow(s, key.e, key.n)
    try:
        em = m.to_bytes(em_len, "big")
    except OverflowError:
        return False
    h_len = hashlib.new(hash_name).digest_size
    s_len = h_len  # JWA fixes the PSS salt length to the hash length.
    m_hash = hashlib.new(hash_name, signing_input).digest()
    if em_len < h_len + s_len + 2:
        return False
    if em[-1] != 0xBC:
        return False
    masked_db = em[: em_len - h_len - 1]
    h = em[em_len - h_len - 1 : em_len - 1]
    top_bits = 8 * em_len - em_bits
    if top_bits and (masked_db[0] & (0xFF << (8 - top_bits)) & 0xFF):
        return False
    db_mask = _mgf1(h, em_len - h_len - 1, hash_name)
    db = bytes(a ^ b for a, b in zip(masked_db, db_mask))
    if top_bits:
        db = bytes([db[0] & (0xFF >> top_bits)]) + db[1:]
    pad_len = em_len - h_len - s_len - 2
    if any(db[i] != 0 for i in range(pad_len)):
        return False
    if db[pad_len] != 0x01:
        return False
    salt = db[-s_len:] if s_len else b""
    m_prime = (b"\x00" * 8) + m_hash + salt
    h_prime = hashlib.new(hash_name, m_prime).digest()
    return hmac.compare_digest(h, h_prime)


def _verify_signature(
    alg: str,
    key: SymmetricKey | RsaPublicKey,
    signing_input: bytes,
    signature: bytes,
) -> bool:
    family = _FAMILY[alg]
    # Structural anti-confusion: the key's family must match the alg's family.
    if key.family != family:
        return False
    if family == "HS":
        assert isinstance(key, SymmetricKey)
        return _verify_hs(alg, key.secret, signing_input, signature)
    assert isinstance(key, RsaPublicKey)
    hash_name = _HASH[alg]
    if alg in _PS:
        return _verify_ps(key, hash_name, signing_input, signature)
    return _verify_rs(key, hash_name, signing_input, signature)


# ---------------------------------------------------------------------------
# Identity mapping and the public verifier.
# ---------------------------------------------------------------------------


def default_identity(claims: Mapping[str, Any]) -> Identity:
    """Map standard/Cognito claims onto a wreath ``Identity``.

    ``sub`` -> id; ``roles``/``cognito:groups``/``groups`` -> roles;
    space-delimited ``scope`` -> permissions; the full claim set is retained.
    """
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise ValueError("token is missing a string 'sub' claim")
    raw_roles = claims.get("roles") or claims.get("cognito:groups") or claims.get("groups") or ()
    if isinstance(raw_roles, str):
        raw_roles = raw_roles.split()
    roles = frozenset(str(role) for role in raw_roles)
    scope = claims.get("scope")
    permissions = frozenset(scope.split()) if isinstance(scope, str) else frozenset()
    return Identity(id=subject, roles=roles, permissions=permissions, claims=dict(claims))


IdentityMapper = Callable[[Mapping[str, Any]], Identity]
KeyResolver = Callable[[Mapping[str, Any]], "SymmetricKey | RsaPublicKey | None"]


def _reason_valid(
    claims: Mapping[str, Any],
    *,
    now: int,
    leeway: int,
    issuer: str | None,
    audiences: tuple[str, ...],
    required: tuple[str, ...],
) -> int:
    if _native_claims is not None:
        return int(
            _native_claims(dict(claims), now, leeway, issuer, audiences, required)
        )
    # Fallback mirrors jose.c's reason codes (0 == valid).
    def as_int(name: str) -> int | None:
        value = claims.get(name)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise _Malformed
        return value

    try:
        exp = as_int("exp")
        if exp is not None and now - leeway >= exp:
            return 1
        nbf = as_int("nbf")
        if nbf is not None and nbf > now + leeway:
            return 2
        iat = as_int("iat")
        if iat is not None and iat > now + leeway:
            return 3
    except _Malformed:
        return 7
    if issuer is not None and claims.get("iss") != issuer:
        return 4
    if audiences:
        aud = claims.get("aud")
        aud_set = {aud} if isinstance(aud, str) else set(aud) if isinstance(aud, list) else set()
        if not aud_set & set(audiences):
            return 5
    for name in required:
        if name not in claims:
            return 6
    return 0


class _Malformed(Exception):
    pass


class JwtVerifier:
    """A callable ``Verifier`` for :class:`BearerTokenBackend`.

    Holds a fixed key (static-key case). The JWKS case uses the lower-level
    :func:`verify_jwt` with a key resolver; see :mod:`wreath._auth.jwks`.
    """

    __slots__ = (
        "_algorithms",
        "_audiences",
        "_identity",
        "_issuer",
        "_key",
        "_leeway",
        "_required",
    )

    def __init__(
        self,
        *,
        algorithms: Iterable[str],
        key: SymmetricKey | RsaPublicKey | bytes | str,
        issuer: str | None = None,
        audience: str | Sequence[str] | None = None,
        leeway: int = 60,
        required: Iterable[str] = ("exp",),
        identity: IdentityMapper = default_identity,
    ) -> None:
        self._algorithms = _freeze_algorithms(algorithms)
        self._key = _coerce_key(key)
        # Enforce key/alg family agreement once, at construction: every allowed
        # algorithm must match the key's family, so a token can never coax an
        # RSA key into an HS* verify (or vice versa).
        for alg in self._algorithms:
            if _FAMILY[alg] != self._key.family:
                raise UnsupportedAlgorithm(
                    f"algorithm {alg!r} is incompatible with the configured "
                    f"{self._key.family} key"
                )
        self._issuer = issuer
        self._audiences = _freeze_audiences(audience)
        self._leeway = int(leeway)
        self._required = tuple(required)
        self._identity = identity

    def __call__(self, token: str) -> Identity | None:
        return verify_jwt(
            token,
            key_resolver=lambda _header: self._key,
            algorithms=self._algorithms,
            issuer=self._issuer,
            audiences=self._audiences,
            leeway=self._leeway,
            required=self._required,
            identity=self._identity,
        )


def verify_jwt(
    token: str,
    *,
    key_resolver: KeyResolver,
    algorithms: frozenset[str],
    issuer: str | None,
    audiences: tuple[str, ...],
    leeway: int,
    required: tuple[str, ...],
    identity: IdentityMapper,
    now: int | None = None,
) -> Identity | None:
    """Verify a compact JWS and return an Identity, or None on any failure.

    Returns None (never raises) for every authentication failure so the bearer
    backend can issue a challenge without leaking which check failed.
    """
    import time

    try:
        header, claims, signing_input, signature = _parse_compact(token)
    except (ValueError, KeyError, json.JSONDecodeError):
        return None

    alg = header.get("alg")
    # The token's alg may only *select from* the frozen allow-list. This is the
    # single line that defeats alg=none and algorithm-substitution attacks.
    if not isinstance(alg, str) or alg not in algorithms:
        return None
    if alg not in _SUPPORTED:  # defence in depth; deferred algs never pass
        return None

    key = key_resolver(header)
    if key is None:
        return None

    try:
        if not _verify_signature(alg, key, signing_input, signature):
            return None
    except (ValueError, OverflowError):
        return None

    reason = _reason_valid(
        claims,
        now=int(time.time()) if now is None else now,
        leeway=leeway,
        issuer=issuer,
        audiences=audiences,
        required=required,
    )
    if reason != 0:
        return None

    try:
        return identity(claims)
    except (KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------
# small construction-time helpers
# ---------------------------------------------------------------------------


def _freeze_algorithms(algorithms: Iterable[str]) -> frozenset[str]:
    frozen = frozenset(algorithms)
    if not frozen:
        raise JwtError("at least one algorithm is required")
    for alg in frozen:
        if alg in _DEFERRED:
            raise UnsupportedAlgorithm(
                f"algorithm {alg!r} is not supported in this build "
                "(HS/RS/PS only; ES*/EdDSA are a planned fast-follow)"
            )
        if alg not in _SUPPORTED:
            raise UnsupportedAlgorithm(f"unknown algorithm: {alg!r}")
    return frozen


def _freeze_audiences(audience: str | Sequence[str] | None) -> tuple[str, ...]:
    if audience is None:
        return ()
    if isinstance(audience, str):
        return (audience,)
    return tuple(audience)


def _coerce_key(key: SymmetricKey | RsaPublicKey | bytes | str) -> SymmetricKey | RsaPublicKey:
    if isinstance(key, (SymmetricKey, RsaPublicKey)):
        return key
    if isinstance(key, (bytes, bytearray)):
        return SymmetricKey(bytes(key))
    if isinstance(key, str):
        stripped = key.lstrip()
        if stripped.startswith("-----BEGIN"):
            return key_from_pem(key)
        # A bare string secret is treated as an HMAC key (UTF-8).
        return SymmetricKey(key.encode("utf-8"))
    raise JwtError(f"unsupported key type: {type(key)!r}")


# Re-exported for the JWKS layer without importing private names there.
FAMILY = _FAMILY
SUPPORTED = _SUPPORTED
freeze_algorithms = _freeze_algorithms
freeze_audiences = _freeze_audiences
