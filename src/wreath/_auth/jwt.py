"""JWT verification and its Python-facing key and policy types.

Compact parsing, registered-claim checks, HMAC verification, and RSA encoded
message validation are implemented by the JOSE library. The elliptic-curve
algorithms are provided by the curve library.

Algorithm confusion is prevented structurally: a verifier's algorithm allow-list
is frozen at construction, the token `alg` may only *select from* that list
(so `alg=none` or any unlisted alg is rejected before any verify runs), and
each key is bound to exactly one algorithm *family* (HS / RSA / EC / OKP) — a
symmetric secret can never satisfy an RS*/PS*/ES256/EdDSA check, and vice versa.

The implementations are checked against RFC 8032 and NIST known-answer vectors.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .._b64 import B64URL_ALPHABET, b64url_encode
from .._b64 import b64url_decode as _b64url_decode
from .._json import dumps as _json_dumps
from .._json import loads as _json_loads
from .._native import _core
from ._ecverify import on_p256_curve, verify_ed25519, verify_es256
from .models import Identity

# The `jose.c` entry points, resolved once at import.
_native_parse = _core.jose_parse
_native_hs = _core.jose_verify_hs
_native_claims = _core.jose_validate_claims

__all__ = [
    "EcPublicKey",
    "RevocationCheck",
    "InvalidToken",
    "JwtError",
    "JwtVerifier",
    "OkpPublicKey",
    "RsaPublicKey",
    "SymmetricKey",
    "UnsupportedAlgorithm",
    "default_identity",
    "key_from_jwk",
    "key_from_pem",
    "jwk_thumbprint",
    "jwk_thumbprint_uri",
]

#: Minimum length for an HMAC secret given as a bare string. RFC 8725 §3.5 and
#: RFC 2104 both put the floor at the hash output size; SymmetricKey(...) stays
#: unchecked, so a caller who genuinely holds a short key can still say so.
MIN_HMAC_KEY_BYTES = 32

#: Minimum RSA modulus. NIST SP 800-131A has disallowed anything below this
#: since 2014, and a JWKS is fetched from a remote party -- an endpoint that
#: advertises a 512-bit key is either broken or hostile, and verifying against
#: one is worse than failing to verify at all.
MIN_RSA_MODULUS_BITS = 2048

# Hard caps (decoded bytes) to keep a hostile token from feeding a giant string
# to the JSON parser or allocating without bound.
_MAX_SEGMENT_BYTES = 16 * 1024
# Absolute compact-token ceiling, mirroring native jose.c JOSE_ABS_MAX_TOKEN.
_MAX_TOKEN_BYTES = 1 << 20

#: The alphabet a compact-serialization segment may contain. RFC 7515 §2 writes
#: them as base64url **without** padding. Accepting padding or non-alphabet bytes
#: would allow multiple compact strings to represent the same signed token.
#:
#: `_parse_compact` is the only reader: `_b64url_decode` applies the same set
#: itself. Both now come from `wreath._b64`, which is where this module's own
#: strict decoder was lifted to so the session cookie and the WebAuthn payloads
#: could share it. For a while the lift-out happened and this module was not
#: switched over, so one decoder shipped as two -- the exact drift the shared
#: copy exists to prevent.
_B64URL_SEGMENT = B64URL_ALPHABET


class JwtError(Exception):
    """Base class for JWT construction/configuration errors."""


class UnsupportedAlgorithm(JwtError):
    """A requested algorithm is not implemented in this build."""


class InvalidToken(JwtError):
    """A token could not be constructed for signing (not raised on verify)."""


# Algorithm registry. ES*/EdDSA are intentionally absent from this cut and must
# raise rather than silently pass; the structure leaves room to add them later.

_HS = {"HS256": "sha256", "HS384": "sha384", "HS512": "sha512"}
_RS = {"RS256": "sha256", "RS384": "sha384", "RS512": "sha512"}
_PS = {"PS256": "sha256", "PS384": "sha384", "PS512": "sha512"}
#: ECDSA over NIST P-256 (SHA-256). ES384/512 need P-384/P-521 curves, still deferred.
_EC = {"ES256": "sha256"}
#: EdDSA over edwards25519. "EdDSA" (RFC 8037) and "Ed25519" both name it.
_OKP = frozenset({"EdDSA", "Ed25519"})

# Algorithms recognised but deliberately not implemented in this cut. Requesting
# one is a loud configuration error, never a silent accept.
_DEFERRED = frozenset({"ES384", "ES512", "ES256K", "Ed448", "none"})

_FAMILY = {
    **{a: "HS" for a in _HS},
    **{a: "RSA" for a in _RS},
    **{a: "RSA" for a in _PS},
    **{a: "EC" for a in _EC},
    **{a: "OKP" for a in _OKP},
}
_HASH = {**_HS, **_RS, **_PS, **_EC}
_SUPPORTED = frozenset(_FAMILY)

# DER DigestInfo prefixes for EMSA-PKCS1-v1.5 (RFC 8017 §9.2, notes).
_DIGEST_INFO = {
    "sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
    "sha384": bytes.fromhex("3041300d060960864801650304020205000430"),
    "sha512": bytes.fromhex("3051300d060960864801650304020305000440"),
}


# Keys. Each key knows the one family it may be used with.


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


@dataclass(frozen=True, slots=True)
class EcPublicKey:
    """A NIST P-256 public key (affine coordinates). Usable only for ES256."""

    x: int
    y: int
    family: str = field(default="EC", init=False)


@dataclass(frozen=True, slots=True)
class OkpPublicKey:
    """An Ed25519 public key (32 raw bytes). Usable only for EdDSA."""

    public: bytes
    family: str = field(default="OKP", init=False)


#: Any key an algorithm family can be verified with.
JwtKey = SymmetricKey | RsaPublicKey | EcPublicKey | OkpPublicKey

_THUMBPRINT_MEMBERS = {
    "EC": ("crv", "kty", "x", "y"),
    "OKP": ("crv", "kty", "x"),
    "RSA": ("e", "kty", "n"),
    "oct": ("k", "kty"),
}


def jwk_thumbprint(jwk: Mapping[str, Any], *, hash_name: str = "sha-256") -> str:
    """Return the RFC 7638 thumbprint of one JWK.

    SHA-256 is the mandatory-to-implement algorithm for both RFC 7638 and
    RFC 9278. The JSON input is reduced to the required members for its key
    type, lexicographically ordered, encoded without whitespace, and hashed.
    """
    if hash_name != "sha-256":
        raise ValueError("JWK thumbprint hash_name must be 'sha-256'")
    kty = jwk.get("kty")
    members = _THUMBPRINT_MEMBERS.get(kty)
    if members is None:
        raise ValueError(f"JWK thumbprint does not support key type {kty!r}")
    required: dict[str, str] = {}
    for name in members:
        value = jwk.get(name)
        if not isinstance(value, str):
            raise ValueError(
                f"JWK thumbprint requires string member {name!r} for key type {kty!r}"
            )
        required[name] = value
    canonical = _json_dumps(required)
    return b64url_encode(hashlib.sha256(canonical).digest())


def jwk_thumbprint_uri(jwk: Mapping[str, Any], *, hash_name: str = "sha-256") -> str:
    """Return the RFC 9278 URI form of a JWK thumbprint."""
    thumbprint = jwk_thumbprint(jwk, hash_name=hash_name)
    return f"urn:ietf:params:oauth:jwk-thumbprint:{hash_name}:{thumbprint}"


def key_from_jwk(jwk: Mapping[str, Any]) -> JwtKey:
    """Build a key from a single JWK. Supports `oct`/`RSA`/`EC`/`OKP`."""
    kty = jwk.get("kty")
    if kty == "oct":
        return SymmetricKey(_b64url_decode(jwk["k"]))
    if kty == "RSA":
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
        if n <= 0 or e <= 0:
            raise JwtError("invalid RSA JWK parameters")
        if n.bit_length() < MIN_RSA_MODULUS_BITS:
            raise JwtError(
                f"RSA modulus is {n.bit_length()} bits; at least "
                f"{MIN_RSA_MODULUS_BITS} are required"
            )
        return RsaPublicKey(n, e)
    if kty == "EC":
        if jwk.get("crv") != "P-256":
            raise UnsupportedAlgorithm(f"EC curve {jwk.get('crv')!r} is not supported (P-256 only)")
        x = int.from_bytes(_b64url_decode(jwk["x"]), "big")
        y = int.from_bytes(_b64url_decode(jwk["y"]), "big")
        if not on_p256_curve(x, y):
            raise JwtError("EC JWK point is not on the P-256 curve")
        return EcPublicKey(x, y)
    if kty == "OKP":
        if jwk.get("crv") != "Ed25519":
            raise UnsupportedAlgorithm(
                f"OKP curve {jwk.get('crv')!r} is not supported (Ed25519 only)"
            )
        return OkpPublicKey(_b64url_decode(jwk["x"]))
    raise JwtError(f"unsupported JWK key type: {kty!r}")


def key_from_pem(pem: str | bytes) -> RsaPublicKey:
    """Parse an RSA public key from a PEM `PUBLIC KEY` (SPKI) or
    `RSA PUBLIC KEY` (PKCS#1) block, without a third-party dependency."""
    text = pem.decode("ascii") if isinstance(pem, (bytes, bytearray)) else pem
    body = []
    kind = "spki"
    for raw_line in text.splitlines():
        line = raw_line.strip()
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
    if n.bit_length() < MIN_RSA_MODULUS_BITS:
        raise JwtError(
            f"RSA modulus is {n.bit_length()} bits; at least {MIN_RSA_MODULUS_BITS} are required"
        )
    return RsaPublicKey(n, e)


def _der_read_tlv(data: bytes, pos: int) -> tuple[int, bytes, int]:
    """Return (tag, value_bytes, next_pos) for the TLV at `pos`."""
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


# Primitive helpers. Parsing, HS verification and claim validation are `jose.c`;
# RSA, EC and Ed25519 verification are Python over the stdlib -- see the module
# docstring for why each is where it is.


def _parse_compact(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    """Split + decode + JSON-parse a compact JWS. Raises ValueError if malformed."""
    header_bytes, payload_bytes, signing_input, signature = _native_parse(token, _MAX_SEGMENT_BYTES)
    header = _json_loads(header_bytes)
    claims = _json_loads(payload_bytes)
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise ValueError("JWT header and payload must be JSON objects")
    return header, claims, signing_input, signature


def peek_header(token: str) -> dict[str, Any] | None:
    """Decode only the JOSE header (for `kid`/`alg` lookup before verify).

    Returns None on any malformation. Does not validate the signature — callers
    must still run `verify_jwt`.
    """
    try:
        first = token.split(".", 1)[0]
        if not first or len(first) > ((_MAX_SEGMENT_BYTES * 4 + 2) // 3):
            return None
        header = _json_loads(_b64url_decode(first))
    except (ValueError, KeyError):
        return None
    return header if isinstance(header, dict) else None


def _verify_hs(alg: str, secret: bytes, signing_input: bytes, signature: bytes) -> bool:
    digestmod = _HASH[alg]
    return bool(_native_hs(digestmod, secret, signing_input, signature))
    expected = hmac.new(secret, signing_input, digestmod).digest()
    return hmac.compare_digest(expected, signature)


def _verify_rs(key: RsaPublicKey, hash_name: str, signing_input: bytes, signature: bytes) -> bool:
    digest = hashlib.new(hash_name, signing_input).digest()
    constructor = getattr(hashlib, hash_name)
    return bool(
        _core.jose_verify_rsa(key.n, key.e, key.size, constructor, digest, signature, False)
    )


def _verify_ps(key: RsaPublicKey, hash_name: str, signing_input: bytes, signature: bytes) -> bool:
    digest = hashlib.new(hash_name, signing_input).digest()
    constructor = getattr(hashlib, hash_name)
    return bool(_core.jose_verify_rsa(key.n, key.e, key.size, constructor, digest, signature, True))


def _verify_signature(
    alg: str,
    key: JwtKey,
    signing_input: bytes,
    signature: bytes,
) -> bool:
    family = _FAMILY[alg]
    if family == "HS":
        if not isinstance(key, SymmetricKey):
            return False
        return _verify_hs(alg, key.secret, signing_input, signature)
    if family == "EC":
        if not isinstance(key, EcPublicKey):
            return False
        return verify_es256(key.x, key.y, signing_input, signature)
    if family == "OKP":
        if not isinstance(key, OkpPublicKey):
            return False
        return verify_ed25519(key.public, signing_input, signature)
    if not isinstance(key, RsaPublicKey):
        return False
    hash_name = _HASH[alg]
    if alg in _PS:
        return _verify_ps(key, hash_name, signing_input, signature)
    return _verify_rs(key, hash_name, signing_input, signature)


# Identity mapping and the public verifier.


def default_identity(claims: Mapping[str, Any]) -> Identity:
    """Map standard/Cognito claims onto a wreath `Identity`.

    `sub` -> id; `roles`/`cognito:groups`/`groups` -> roles;
    space-delimited `scope` -> permissions; the full claim set is retained.
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
KeyResolver = Callable[[Mapping[str, Any]], "JwtKey | None"]
#: `revoked(claims) -> bool` -- whether this token has been cancelled since it
#: was issued. See `verify_jwt`.
RevocationCheck = Callable[[Mapping[str, Any]], bool]


def _reason_valid(
    claims: Mapping[str, Any],
    *,
    now: int,
    leeway: int,
    issuer: str | None,
    audiences: frozenset[str],
    required: tuple[str, ...],
) -> int:
    return int(_native_claims(dict(claims), now, leeway, issuer, audiences, required))


class _AudienceUnset:
    __slots__ = ()


_AUDIENCE_UNSET = _AudienceUnset()


class JwtVerifier:
    """A callable `Verifier` for `BearerTokenBackend`.

    Holds a fixed key (static-key case). The JWKS case uses the lower-level
    `verify_jwt` with a key resolver; see `wreath._auth.jwks`.
    `audience` must be explicit. Pass `None` only when another layer performs
    the audience check, as `MCPAuth` does.
    """

    __slots__ = (
        "_algorithms",
        "_audiences",
        "_identity",
        "_issuer",
        "_key",
        "_leeway",
        "_required",
        "_revoked",
    )

    def __init__(
        self,
        *,
        algorithms: Iterable[str],
        key: JwtKey | bytes | str,
        issuer: str | None = None,
        audience: str | Sequence[str] | None | _AudienceUnset = _AUDIENCE_UNSET,
        leeway: int = 60,
        required: Iterable[str] = ("exp",),
        identity: IdentityMapper = default_identity,
        revoked: RevocationCheck | None = None,
    ) -> None:
        self._algorithms = _freeze_algorithms(algorithms)
        self._key = _coerce_key(key)
        # Enforce key/alg family agreement once, at construction: every allowed
        # algorithm must match the key's family, so a token can never coax an
        # RSA key into an HS* verify (or vice versa).
        for alg in self._algorithms:
            if _FAMILY[alg] != self._key.family:
                raise UnsupportedAlgorithm(
                    f"algorithm {alg!r} is incompatible with the configured {self._key.family} key"
                )
        self._issuer = issuer
        if isinstance(audience, _AudienceUnset):
            raise ValueError(
                "audience must be configured; pass audience='service' or "
                "audience=None only when another layer validates it"
            )
        self._audiences = _compile_audiences(audience)
        self._leeway = int(leeway)
        self._required = tuple(required)
        self._identity = identity
        self._revoked = revoked

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
            revoked=self._revoked,
        )


def verify_jwt(
    token: str,
    *,
    key_resolver: KeyResolver,
    algorithms: frozenset[str],
    issuer: str | None,
    audiences: frozenset[str],
    leeway: int,
    required: tuple[str, ...],
    identity: IdentityMapper,
    now: int | None = None,
    revoked: RevocationCheck | None = None,
) -> Identity | None:
    """Verify a compact JWS and return an Identity, or None on any failure.

    Returns None (never raises) for every authentication failure so the bearer
    backend can issue a challenge without leaking which check failed.

    `revoked(claims)` is the seam for cancelling a token before it expires.
    Nothing ships behind it -- no `jti` cache, no store -- because a real one
    is a lookup on the busiest path in the framework and that is the
    application's call to make. Without it a stolen token stays valid until
    `exp`, which is why short lifetimes remain the primary answer.

    It runs **after** the signature and the registered claims, so a hook only
    ever sees claims that were genuinely issued; and a hook that *raises*
    denies, because a revocation store that is unreachable must not be a
    revocation store that says yes.
    """
    import time

    try:
        header, claims, signing_input, signature = _parse_compact(token)
    except (ValueError, KeyError):
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
    except ValueError, OverflowError:
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

    if revoked is not None:
        try:
            if revoked(claims):
                return None
        except Exception:  # noqa: BLE001 - user callback; resolves to DENY
            # `revoked` is application code (a cache lookup, a database read)
            # and may raise anything. It resolves fail-closed: an unreachable
            # revocation list is not a licence to accept the token. Narrowing
            # this is not possible -- the set of failures is the caller's, not
            # ours -- which is what makes it the exceptional minority.
            return None

    try:
        return identity(claims)
    except KeyError, ValueError:
        return None


# small construction-time helpers


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


def _compile_audiences(audience: str | Sequence[str] | None) -> frozenset[str]:
    return frozenset(_freeze_audiences(audience))


def _coerce_key(key: JwtKey | bytes | bytearray | str) -> JwtKey:
    if isinstance(key, (SymmetricKey, RsaPublicKey, EcPublicKey, OkpPublicKey)):
        return key
    if isinstance(key, (bytes, bytearray)):
        return SymmetricKey(bytes(key))
    if isinstance(key, str):
        stripped = key.lstrip()
        if stripped.startswith("-----BEGIN"):
            return key_from_pem(key)
        # A bare string secret is treated as an HMAC key (UTF-8). It is the only
        # key form a caller can supply by typing a word, so it is the one that
        # needs a floor: an HS256 verifier is exactly as strong as this string.
        encoded = key.encode("utf-8")
        if len(encoded) < MIN_HMAC_KEY_BYTES:
            raise JwtError(
                f"an HMAC secret must contain at least {MIN_HMAC_KEY_BYTES} bytes; "
                "pass SymmetricKey(...) explicitly to use a shorter one"
            )
        return SymmetricKey(encoded)
    raise JwtError(f"unsupported key type: {type(key)!r}")


# Re-exported for the JWKS layer without importing private names there.
FAMILY = _FAMILY
SUPPORTED = _SUPPORTED
freeze_algorithms = _freeze_algorithms
freeze_audiences = _freeze_audiences
compile_audiences = _compile_audiences
