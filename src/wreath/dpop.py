"""OAuth DPoP proof validation and sender-constrained token binding (RFC 9449)."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from ._auth.jwt import (
    FAMILY,
    SUPPORTED,
    JwtError,
    _parse_compact,
    _verify_signature,
    jwk_thumbprint,
    key_from_jwk,
)
from ._b64 import b64url_encode
from ._capability_map import CapabilityMap

__all__ = ["DPoPProof", "DPoPRefusal", "DPoPVerifier"]

_PRIVATE_JWK_MEMBERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth", "k"})


class DPoPRefusal(Exception):
    """A DPoP proof was not valid for the request carrying it."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DPoPProof:
    """A verified proof and the public-key binding it establishes."""

    jti: str
    jkt: str
    jwk: Mapping[str, Any]
    issued_at: int


def _target_uri(uri: str, *, proof: bool) -> str:
    try:
        parsed = urlsplit(uri)
        port = parsed.port
    except ValueError as error:
        raise DPoPRefusal("invalid-target-uri", f"DPoP target URI is malformed: {error}") from None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DPoPRefusal(
            "invalid-target-uri",
            "DPoP target URI must be an absolute HTTP(S) URI without credentials",
        )
    if proof and (parsed.query or parsed.fragment):
        raise DPoPRefusal(
            "invalid-target-uri",
            "the DPoP htu claim must omit query and fragment components",
        )
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    authority = host if port in (None, default_port) else f"{host}:{port}"
    normalized = SplitResult(parsed.scheme.lower(), authority, parsed.path or "/", "", "")
    return urlunsplit(normalized)


class DPoPVerifier:
    """Verify request-bound DPoP proofs with bounded, expiring replay state.

    Replay entries belong to this verifier instance. A deployment with multiple
    workers supplies a shared verifier adapter at its boundary if it requires
    cross-process replay detection; there is no process-global security state.
    """

    __slots__ = ("_algorithms", "_clock_skew", "_max_age", "_replay")

    def __init__(
        self,
        *,
        algorithms: tuple[str, ...] = ("ES256", "EdDSA"),
        max_age: int = 300,
        clock_skew: int = 30,
        max_entries: int = 10_000,
    ) -> None:
        if max_age <= 0:
            raise ValueError("DPoP max_age must be positive")
        if clock_skew < 0:
            raise ValueError("DPoP clock_skew must not be negative")
        frozen = frozenset(algorithms)
        if not frozen:
            raise ValueError("DPoP algorithms must contain at least one asymmetric algorithm")
        for algorithm in frozen:
            if algorithm not in SUPPORTED or FAMILY[algorithm] == "HS":
                raise ValueError(
                    f"DPoP algorithm {algorithm!r} must be a supported asymmetric JWS algorithm"
                )
        self._algorithms = frozen
        self._max_age = int(max_age)
        self._clock_skew = int(clock_skew)
        self._replay = CapabilityMap(
            max_entries=max_entries,
            ttl=float(self._max_age + self._clock_skew),
            overflow="refuse",
        )

    @property
    def algorithms(self) -> tuple[str, ...]:
        return tuple(sorted(self._algorithms))

    def verify(
        self,
        proof: str,
        *,
        method: str,
        uri: str,
        access_token: str | None = None,
        expected_jkt: str | None = None,
        nonce: str | None = None,
        now: int | None = None,
    ) -> DPoPProof:
        """Validate one DPoP proof and atomically spend its replay identifier."""
        try:
            header, claims, signing_input, signature = _parse_compact(proof)
        except (TypeError, ValueError, KeyError):
            raise DPoPRefusal(
                "malformed-proof", "DPoP header must contain one compact JWT"
            ) from None
        if header.get("typ") != "dpop+jwt":
            raise DPoPRefusal("wrong-type", "DPoP proof typ must be 'dpop+jwt'")
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in self._algorithms:
            raise DPoPRefusal(
                "unsupported-algorithm",
                "DPoP proof alg must name a configured asymmetric JWS algorithm",
            )
        jwk = header.get("jwk")
        if not isinstance(jwk, Mapping):
            raise DPoPRefusal("missing-key", "DPoP proof header needs a public jwk object")
        private = sorted(_PRIVATE_JWK_MEMBERS.intersection(jwk))
        if private:
            raise DPoPRefusal(
                "private-key",
                f"DPoP proof jwk must not contain private key material: {', '.join(private)}",
            )
        try:
            key = key_from_jwk(jwk)
            jkt = jwk_thumbprint(jwk)
            valid_signature = _verify_signature(algorithm, key, signing_input, signature)
        except (JwtError, KeyError, TypeError, ValueError, OverflowError):
            raise DPoPRefusal("invalid-key", "DPoP proof contains an invalid public jwk") from None
        if not valid_signature:
            raise DPoPRefusal("invalid-signature", "DPoP proof signature does not match its jwk")

        jti = claims.get("jti")
        htm = claims.get("htm")
        htu = claims.get("htu")
        issued_at = claims.get("iat")
        if not isinstance(jti, str) or not jti:
            raise DPoPRefusal("missing-claim", "DPoP proof jti must be a non-empty string")
        if not isinstance(htm, str) or not htm:
            raise DPoPRefusal("missing-claim", "DPoP proof htm must be a non-empty string")
        if not isinstance(htu, str) or not htu:
            raise DPoPRefusal("missing-claim", "DPoP proof htu must be a non-empty string")
        if isinstance(issued_at, bool) or not isinstance(issued_at, int):
            raise DPoPRefusal("missing-claim", "DPoP proof iat must be an integer timestamp")
        if not hmac.compare_digest(htm, method):
            raise DPoPRefusal(
                "method-mismatch",
                f"DPoP proof HTTP method {htm!r} does not match request method {method!r}",
            )
        if not hmac.compare_digest(_target_uri(htu, proof=True), _target_uri(uri, proof=False)):
            raise DPoPRefusal("uri-mismatch", "DPoP proof target URI does not match this request")

        moment = int(time.time()) if now is None else now
        if issued_at < moment - self._max_age or issued_at > moment + self._clock_skew:
            raise DPoPRefusal(
                "stale-proof",
                "DPoP proof creation time is outside the accepted freshness window",
            )
        if nonce is not None and claims.get("nonce") != nonce:
            raise DPoPRefusal("nonce-mismatch", "DPoP proof nonce does not match the server nonce")
        if access_token is not None:
            try:
                expected_ath = b64url_encode(hashlib.sha256(access_token.encode("ascii")).digest())
            except UnicodeEncodeError:
                raise DPoPRefusal(
                    "invalid-access-token", "a DPoP access token must contain only ASCII"
                ) from None
            ath = claims.get("ath")
            if not isinstance(ath, str) or not hmac.compare_digest(ath, expected_ath):
                raise DPoPRefusal(
                    "access-token-mismatch",
                    "DPoP proof access token hash does not match the presented token",
                )
        if expected_jkt is not None and not hmac.compare_digest(jkt, expected_jkt):
            raise DPoPRefusal(
                "key-mismatch",
                "DPoP proof public key does not match the key bound to the access token",
            )

        replay_key = (jkt, jti)
        if not self._replay.claim(replay_key, now=float(moment)):
            if self._replay.peek(replay_key, now=float(moment)) is not None:
                raise DPoPRefusal(
                    "replayed-proof", "this DPoP proof has already been used"
                )
            raise DPoPRefusal(
                "replay-capacity",
                "DPoP replay protection is at capacity and cannot safely accept this proof",
            )
        return DPoPProof(
            jti=jti,
            jkt=jkt,
            jwk=MappingProxyType(dict(jwk)),
            issued_at=issued_at,
        )
