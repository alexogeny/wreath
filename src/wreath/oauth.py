"""Issuing OAuth 2.1 tokens, for a deployment that decided to.

Wreath verifies bearer tokens with `wreath.auth.JwtVerifier` and issues them for
deployments that act as their own identity provider.

**The first obligation is that what this mints is what wreath already verifies.**
An issuer whose output its own verifier rejects is two features rather than one,
so `tests/oauth/` drives a minted token straight through `JwtVerifier` and
`BearerTokenBackend`.

## Two signers, and how to choose

**`Es256Signer` is the one most deployments want.** It signs with ECDSA on
P-256, publishes a real key set from `jwks()`, and adds **no dependency** --
`wreath._webpush` already signs ES256 VAPID tokens with hedged nonces over the
standard library, and `wreath.auth`'s `JwtVerifier` already verifies ES256
against an `EcPublicKey`. Both halves of the loop were already in the tree; this
module was written claiming they were not, which was wrong.

**HS256 is the default**, and that is a choice about ceremony rather than about
strength. A shared secret needs no key to generate, store, rotate or publish,
which is right when the issuer and the resource server are the same deployment
reading the same configuration. Its cost is that there is nothing safe to
publish, so `jwks()` is honestly empty -- an `oct` entry carrying `k` would hand
every reader the ability to mint tokens.

So: **the moment anything outside this deployment verifies your tokens, pass
`signer=Es256Signer.generate()`**, keep the private scalar in configuration, and
`jwks()` becomes a real key set.

The asymmetric signer does substantially more arithmetic than HMAC. It is the
right trade at a login and the wrong operation to repeat on every application
request. A deployment that needs both public verifiability and volume should
issue long-lived tokens and refresh them rarely, which is what the shape is for
anyway.

## The three replay defences, and why each is separate

* **An authorization code is single use**, and redeeming it twice **revokes the
  token the first redemption issued**. Refusing the second alone leaves the
  attacker's token live if they got there first, so the only safe answer to "two
  parties hold this code" is that neither keeps anything.
* **A refresh token rotates**, and presenting a rotated one revokes the whole
  chain. Rotation without reuse detection is rotation that tells you nothing.
* **PKCE is required.** `plain` is refused: a challenge that *is* the verifier
  protects nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from base64 import urlsafe_b64encode
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, is_dataclass
from typing import Any, ClassVar, Final, cast
from urllib.parse import urlsplit

from ._json import dumps as _json_dumps
from ._json import loads as _json_loads
from .binding import ValidationError, validate

__all__ = [
    "TOKEN_INTROSPECTION_JWT_MEDIA_TYPE",
    "AuthorizationServer",
    "ClientRegistration",
    "Es256Signer",
    "IssuedToken",
    "OAuthRefusal",
]

TOKEN_INTROSPECTION_JWT_MEDIA_TYPE: Final = b"application/token-introspection+jwt"


class OAuthRefusal(Exception):
    """A grant this server will not make, and why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ClientRegistration:
    """One registered client.

    `redirect_uris` are matched **exactly**. Prefix matching on a redirect URI
    is an open redirect, and an open redirect on the authorization endpoint is
    an authorization-code exfiltration: `https://app.example.evil/cb` has
    `https://app.example` as a prefix.
    """

    client_id: str
    redirect_uris: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    #: A confidential client may use the client-credentials grant. A public one
    #: (a browser or a mobile app, which cannot keep a secret) may not.
    confidential: bool = False
    client_secret: bytes | str | None = None
    dpop_bound_access_tokens: bool = False
    authorization_detail_types: tuple[str, ...] = ()
    introspection_signed_response_alg: str | None = None
    introspection_encrypted_response_alg: str | None = None
    introspection_encrypted_response_enc: str | None = None

    def __post_init__(self) -> None:
        if (
            self.introspection_encrypted_response_enc is not None
            and self.introspection_encrypted_response_alg is None
        ):
            raise ValueError(
                "introspection_encrypted_response_enc requires "
                "introspection_encrypted_response_alg"
            )
        if not self.confidential:
            if self.client_secret is not None:
                raise ValueError("a public OAuth client cannot hold a client secret")
            return
        if self.client_secret is None:
            raise ValueError("a confidential OAuth client requires a client secret")
        material = (
            self.client_secret.encode("utf-8")
            if isinstance(self.client_secret, str)
            else bytes(self.client_secret)
        )
        if len(material) < 32:
            raise ValueError("an OAuth client secret must contain at least 32 bytes")
        object.__setattr__(self, "client_secret", material)


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """One access token and what it carries."""

    access_token: str
    subject: str | None
    audience: str
    scope: tuple[str, ...]
    expires_at: float
    #: The tenant this token may act within, or `""`. It composes with
    #: `wreath.tenancy` rather than around it: a token minted inside one tenant
    #: must not read another's data, whatever roles the bearer holds.
    tenant: str = ""
    refresh_token: str = ""
    token_type: str = "Bearer"
    authorization_details: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class _Code:
    client_id: str
    subject: str
    scope: tuple[str, ...]
    challenge: str
    redirect_uri: str
    tenant: str
    issued_at: float
    authorization_details: tuple[dict[str, Any], ...]
    #: The access token this code produced, once redeemed. Kept so a second
    #: redemption can revoke it -- which is the only safe answer when two
    #: parties demonstrably hold the same code.
    issued_token: str = ""


@dataclass(frozen=True, slots=True)
class _Refresh:
    token: str
    subject: str
    #: Every token in this rotation chain, so reuse revokes all of them rather
    #: than only the one presented.
    chain: str
    issued_at: float
    audience: str
    scope: tuple[str, ...]
    tenant: str
    client_id: str
    dpop_jkt: str
    authorization_details: tuple[dict[str, Any], ...]


def _b64(raw: bytes) -> str:
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


#: An unpadded base64url SHA-256 digest is exactly this long. RFC 7636 §4.2
#: allows a `code_challenge` of 43-128 characters, but that range exists to
#: accommodate `plain`, where the challenge *is* the verifier -- and `plain` is
#: refused here. Under S256 there is one length, and anything else is either a
#: verifier sent in the challenge's place or a value nothing produced.
_S256_CHALLENGE_LENGTH: Final = 43


def _check_challenge(challenge: str) -> None:
    """Refuse a `code_challenge` that is not an S256 digest.

    Empty challenges are refused. Under S256 the challenge is an unpadded
    base64url SHA-256 digest with exactly 43 ASCII characters.
    """
    if len(challenge) != _S256_CHALLENGE_LENGTH or not challenge.isascii():
        raise OAuthRefusal(
            "weak-pkce",
            "code_challenge must be an unpadded base64url SHA-256 digest of the "
            f"verifier, {_S256_CHALLENGE_LENGTH} characters; PKCE is required and "
            "'plain' is refused, because a challenge that is the verifier protects "
            "nothing",
        )


@dataclass(frozen=True, slots=True)
class Es256Signer:
    """Sign with ECDSA on P-256, and publish a key set anybody can verify against.

    Built entirely from primitives already in the tree: `wreath._webpush` signs
    ES256 VAPID tokens with a hedged nonce over the standard library, and
    `wreath.auth.JwtVerifier` verifies ES256 against an `EcPublicKey`. Nothing
    here is a new dependency, and nothing here is new cryptography -- reaching
    for what exists rather than beside it.

    `private` is the P-256 scalar. Keep it in configuration and treat it the way
    you treat a signing key: rotating it invalidates every token in flight, so
    publish the successor in `jwks()` alongside the incumbent before switching.
    """

    private: int
    algorithm: ClassVar[str] = "ES256"
    #: Names this key in the JWS header and in the key set, so a verifier can
    #: pick during a rotation instead of trying every key.
    kid: str = "wreath-es256"

    def __post_init__(self) -> None:
        from ._curves import P256_N

        if (
            isinstance(self.private, bool)
            or not isinstance(self.private, int)
            or not 1 <= self.private < P256_N
        ):
            raise ValueError("P-256 scalar is in [1, n)")

    @classmethod
    def generate(cls, *, kid: str = "wreath-es256") -> Es256Signer:
        """Mint a fresh key. Store `private_bytes`; it is not recoverable."""
        from ._curves import P256_N

        return cls(secrets.randbelow(P256_N - 1) + 1, kid)

    @classmethod
    def from_bytes(cls, private: bytes, *, kid: str = "wreath-es256") -> Es256Signer:
        return cls(int.from_bytes(private, "big"), kid)

    @property
    def private_bytes(self) -> bytes:
        return self.private.to_bytes(32, "big")

    def _public_point(self) -> tuple[int, int]:
        from ._webpush import P256_G, _mul

        return cast(tuple[int, int], _mul(self.private, P256_G))

    def public_jwks(self) -> list[dict[str, str]]:
        """This key as JWKS entries -- the public half only, by construction.

        There is no way to spell the private scalar in this shape, which is the
        difference from the HMAC case: the thing that would be dangerous to
        publish is simply not among the fields.
        """
        x, y = self._public_point()
        return [
            {
                "kty": "EC",
                "crv": "P-256",
                "alg": "ES256",
                "use": "sig",
                "kid": self.kid,
                "x": _b64(x.to_bytes(32, "big")),
                "y": _b64(y.to_bytes(32, "big")),
            }
        ]

    def encode(self, claims: Mapping[str, Any]) -> str:
        return self.encode_with_type(claims, "JWT")

    def encode_with_type(self, claims: Mapping[str, Any], typ: str) -> str:
        from ._webpush import _ecdsa_sign

        header = _b64(_json_dumps({"alg": "ES256", "typ": typ, "kid": self.kid}))
        payload = _b64(_json_dumps(dict(sorted(claims.items()))))
        signing_input = f"{header}.{payload}".encode("ascii")
        signature = _ecdsa_sign(self.private, hashlib.sha256(signing_input).digest())
        return f"{header}.{payload}.{_b64(signature)}"

    def verifying_key(self) -> Any:
        """The key `JwtVerifier` takes, built **from the key set this publishes**.

        Deriving it from `public_jwks()` rather than from the private scalar a
        second time is deliberate: the key a caller verifies with and the key
        this server advertises are then the same object by construction, so they
        cannot drift into disagreeing. It also means the published entry is
        exercised on every verify rather than only by whoever reads the endpoint.
        """
        from .auth import key_from_jwk

        return key_from_jwk(self.public_jwks()[0])


class AuthorizationServer:
    """An OAuth 2.1 authorization server over wreath's own JWT vocabulary."""

    __slots__ = (
        "_chains",
        "_authorization_detail_types",
        "_clients",
        "_code_ttl",
        "_codes",
        "_issued",
        "_issuer",
        "_lifetime",
        "_refresh",
        "_refresh_ttl",
        "_revoked",
        "_secret",
        "_signer",
        "_signing_alg",
        "_signing_seconds",
        "_spent",
        "_verifying_key",
    )

    def __init__(
        self,
        *,
        issuer: str,
        secret: bytes | str = b"",
        clients: Iterable[ClientRegistration] = (),
        lifetime: float = 3600.0,
        code_ttl: float = 60.0,
        refresh_ttl: float = 30 * 24 * 3600.0,
        signer: Any = None,
        authorization_detail_types: Mapping[str, type] | None = None,
    ) -> None:
        normalized_issuer = issuer.rstrip("/")
        parsed_issuer = urlsplit(normalized_issuer)
        if (
            parsed_issuer.scheme != "https"
            or parsed_issuer.hostname is None
            or parsed_issuer.username is not None
            or parsed_issuer.query
            or parsed_issuer.fragment
        ):
            raise ValueError(
                "OAuth issuer must be an absolute HTTPS URL without credentials, "
                "a query, or a fragment"
            )
        self._issuer = normalized_issuer
        # Generated when absent so a test or a single-process deployment needs
        # no ceremony. A fleet must pass one: two workers with different secrets
        # issue tokens neither can verify.
        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        if raw and len(raw) < 32:
            raise ValueError("OAuth HMAC signing secret must contain at least 32 bytes")
        self._secret = raw or secrets.token_bytes(32)
        self._signer = signer
        self._signing_alg = self._resolve_signing_algorithm()
        self._verifying_key = None
        detail_types = dict(authorization_detail_types or {})
        for detail_name, model in detail_types.items():
            if not isinstance(detail_name, str) or not detail_name:
                raise ValueError("OAuth authorization detail type names must be non-empty strings")
            if not isinstance(model, type) or not is_dataclass(model):
                raise ValueError(
                    f"OAuth authorization detail type {detail_name!r} must map to a dataclass type"
                )
        self._authorization_detail_types = detail_types
        self._clients: dict[str, ClientRegistration] = {}
        for client in clients:
            self.register(client)
        self._lifetime = lifetime
        self._code_ttl = code_ttl
        if refresh_ttl <= 0:
            raise ValueError("OAuth refresh_ttl must be positive")
        self._refresh_ttl = refresh_ttl
        self._codes: dict[str, _Code] = {}
        self._refresh: dict[str, _Refresh] = {}
        self._chains: dict[str, list[str]] = {}
        #: Refresh token -> the chain it belonged to, kept after rotation spends
        #: it. Without this a reused token's chain is unknowable: `rotate` pops
        #: the record, so the reuse branch has nothing to revoke.
        self._spent: dict[str, tuple[str, str, str]] = {}
        self._revoked: set[str] = set()
        self._issued = 0
        self._signing_seconds = 0.0

    def metadata(self) -> dict[str, Any]:
        """RFC 8414 metadata, so a client configures itself instead of being told."""
        return {
            "issuer": self._issuer,
            "authorization_endpoint": f"{self._issuer}/oauth/authorize",
            "token_endpoint": f"{self._issuer}/oauth/token",
            "introspection_endpoint": f"{self._issuer}/oauth/introspect",
            "jwks_uri": f"{self._issuer}/oauth/jwks",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            "authorization_response_iss_parameter_supported": True,
            "dpop_signing_alg_values_supported": ["ES256", "EdDSA"],
            "authorization_details_types_supported": sorted(self._authorization_detail_types),
            "introspection_signing_alg_values_supported": [self._signing_algorithm()],
        }

    def authorization_response(
        self,
        *,
        code: str | None = None,
        error: str | None = None,
        state: str | None = None,
    ) -> dict[str, str]:
        """Build RFC 9207 authorization response parameters.

        The issuer is present on both success and error responses. Keeping this
        in the authorization server prevents endpoint glue from remembering the
        mix-up defence on one branch and forgetting it on another.
        """
        if (code is None) == (error is None):
            raise ValueError("an authorization response needs exactly one of code or error")
        response = {"iss": self._issuer}
        if code is not None:
            response["code"] = code
        elif error is not None:
            response["error"] = error
        if state is not None:
            response["state"] = state
        return response

    def jwks(self) -> dict[str, Any]:
        """The public keys, which for an HMAC signer are none.

        **An empty key set here is a fact, not an omission.** HS256 has no
        public half to publish, and emitting the shared secret in a JWKS -- which
        is what a `k`-carrying `oct` entry would be -- would hand every reader
        the ability to mint tokens. A deployment whose tokens third parties
        verify passes `signer=` and gets its keys here.
        """
        if self._signer is None:
            return {"keys": []}
        return {"keys": list(self._signer.public_jwks())}

    def register(self, client: ClientRegistration) -> None:
        if client.introspection_encrypted_response_alg is not None:
            raise ValueError(
                f"client {client.client_id!r} requests introspection response encryption, "
                "which is not supported"
            )
        requested_alg = client.introspection_signed_response_alg
        if requested_alg is not None:
            if not client.confidential:
                raise ValueError(
                    f"client {client.client_id!r} must be confidential to receive "
                    "token introspection responses"
                )
            signing_alg = self._signing_algorithm()
            if requested_alg != signing_alg:
                raise ValueError(
                    f"client {client.client_id!r} requests introspection signing algorithm "
                    f"{requested_alg!r}; this server signs with {signing_alg!r}"
                )
            if self._signer is not None and not callable(
                getattr(self._signer, "encode_with_type", None)
            ):
                raise ValueError(
                    "the configured OAuth signer must expose encode_with_type(claims, typ) "
                    "for JWT token introspection responses"
                )
            if self._verifying_key is None:
                if self._signer is None:
                    from ._auth.jwt import SymmetricKey

                    self._verifying_key = SymmetricKey(self._secret)
                else:
                    verifying_key = getattr(self._signer, "verifying_key", None)
                    if not callable(verifying_key):
                        raise ValueError(
                            "the configured OAuth signer must expose verifying_key() "
                            "for JWT token introspection"
                        )
                    self._verifying_key = verifying_key()
        unknown = sorted(
            set(client.authorization_detail_types) - set(self._authorization_detail_types)
        )
        if unknown:
            raise ValueError(
                f"client {client.client_id!r} names {', '.join(unknown)} as an unknown "
                "authorization detail type"
            )
        self._clients[client.client_id] = client

    def _signing_algorithm(self) -> str:
        return self._signing_alg

    def _resolve_signing_algorithm(self) -> str:
        if self._signer is None:
            return "HS256"
        declared = getattr(self._signer, "algorithm", None)
        if isinstance(declared, str) and declared:
            return declared
        keys = self._signer.public_jwks()
        if not isinstance(keys, list) or not keys:
            raise ValueError("the configured OAuth signer must publish a signing algorithm")
        algorithm = keys[0].get("alg")
        if not isinstance(algorithm, str) or not algorithm:
            raise ValueError("the configured OAuth signer JWK must name its alg")
        return algorithm

    def validate_authorization_details(
        self, authorization_details: Any
    ) -> tuple[dict[str, Any], ...]:
        """Validate and normalize one RFC 9396 `authorization_details` value."""
        details = authorization_details
        if isinstance(details, str):
            if len(details) > 64 * 1024:
                raise OAuthRefusal(
                    "invalid-authorization-details",
                    "authorization_details exceeds the 64 KiB validation limit",
                )
            try:
                details = _json_loads(details)
            except ValueError:
                raise OAuthRefusal(
                    "invalid-authorization-details",
                    "authorization_details must be a JSON array of objects",
                ) from None
        if not isinstance(details, (list, tuple)):
            raise OAuthRefusal(
                "invalid-authorization-details",
                "authorization_details must be a JSON array of objects",
            )
        if len(details) > 64:
            raise OAuthRefusal(
                "invalid-authorization-details",
                "authorization_details may contain at most 64 objects",
            )
        normalized: list[dict[str, Any]] = []
        for index, detail in enumerate(details):
            if not isinstance(detail, Mapping):
                raise OAuthRefusal(
                    "invalid-authorization-details",
                    f"authorization_details[{index}] must be an object",
                )
            detail_type = detail.get("type")
            if not isinstance(detail_type, str) or not detail_type:
                raise OAuthRefusal(
                    "invalid-authorization-details",
                    f"authorization_details[{index}].type must be a non-empty string",
                )
            model = self._authorization_detail_types.get(detail_type)
            if model is None:
                raise OAuthRefusal(
                    "invalid-authorization-details",
                    f"authorization_details[{index}] names unknown type {detail_type!r}",
                )
            body = {name: value for name, value in detail.items() if name != "type"}
            try:
                validate(model, body, ("authorization_details", index))
            except ValidationError as error:
                first = error.errors[0]
                location = ".".join(str(part) for part in first["loc"])
                raise OAuthRefusal(
                    "invalid-authorization-details",
                    f"authorization_details does not conform at {location}: {first['msg']}",
                ) from None
            except (TypeError, ValueError) as error:
                raise OAuthRefusal(
                    "invalid-authorization-details",
                    f"authorization_details[{index}] is invalid: {error}",
                ) from None
            normalized.append({"type": detail_type, **body})
        return tuple(normalized)

    def _client_authorization_details(
        self,
        client: ClientRegistration,
        authorization_details: Any,
    ) -> tuple[dict[str, Any], ...]:
        if authorization_details in (None, (), []):
            return ()
        details = self.validate_authorization_details(authorization_details)
        allowed = set(client.authorization_detail_types)
        outside = sorted({detail["type"] for detail in details} - allowed)
        if outside:
            raise OAuthRefusal(
                "invalid-authorization-details",
                f"client {client.client_id!r} is not registered for authorization detail type "
                + ", ".join(outside),
            )
        return details

    def _client(self, client_id: str) -> ClientRegistration:
        client = self._clients.get(client_id)
        if client is None:
            raise OAuthRefusal("unknown-client", f"no client registered as {client_id!r}")
        return client

    def _authenticate_client(
        self,
        client_id: str,
        client_secret: bytes | str | None,
    ) -> ClientRegistration:
        client = self._client(client_id)
        if not client.confidential:
            if client_secret is not None:
                raise OAuthRefusal(
                    "invalid-client",
                    "a public OAuth client must not present a client secret",
                )
            return client
        expected = client.client_secret
        supplied = (
            client_secret.encode("utf-8") if isinstance(client_secret, str) else client_secret
        )
        if (
            not isinstance(expected, bytes)
            or not isinstance(supplied, bytes)
            or not hmac.compare_digest(expected, supplied)
        ):
            raise OAuthRefusal(
                "invalid-client",
                "the confidential client's client secret is missing or invalid",
            )
        return client

    def _require_dpop(self, client_id: str, dpop_jkt: str) -> None:
        if client_id and self._client(client_id).dpop_bound_access_tokens and not dpop_jkt:
            raise OAuthRefusal(
                "invalid-dpop-proof",
                f"client {client_id!r} requires a DPoP proof for every token request",
            )

    def authorize(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        challenge_method: str = "S256",
    ) -> ClientRegistration:
        """Check an authorization request before anything is minted for it."""
        client = self._client(client_id)
        if challenge_method != "S256":
            raise OAuthRefusal(
                "weak-pkce",
                "code_challenge_method must be S256; 'plain' sends the verifier as "
                "the challenge, which protects nothing",
            )
        # Exact, never prefix. `https://app.example.evil/cb` starts with
        # `https://app.example`, and an open redirect on this endpoint is an
        # authorization-code exfiltration.
        if redirect_uri not in client.redirect_uris:
            raise OAuthRefusal(
                "redirect_uri-mismatch",
                f"redirect_uri {redirect_uri!r} is not one this client registered; it "
                "is matched exactly, because a prefix match here is an open redirect "
                "and an open redirect here leaks the code",
            )
        return client

    def issue_code(
        self,
        *,
        client_id: str,
        subject: str,
        challenge: str,
        redirect_uri: str,
        scope: Iterable[str] = (),
        tenant: str = "",
        authorization_details: Any = (),
        now: float | None = None,
    ) -> str:
        """Mint one authorization code, bound to a client, a URI and a challenge.

        `challenge` and `redirect_uri` carry **no defaults**. The challenge must
        be an S256 digest and the redirect URI must exactly match the client's
        registration; neither security control is optional.

        Raises:
            OAuthRefusal: unknown client, a scope outside its registration, a
                `redirect_uri` it did not register, or a challenge that is not
                an S256 digest.
        """
        client = self._client(client_id)
        rich_details = self._client_authorization_details(client, authorization_details)
        _check_challenge(challenge)
        # The same exact match `authorize` makes, made again here: `authorize`
        # is a separate call and nothing obliges a caller to have made it.
        if redirect_uri not in client.redirect_uris:
            raise OAuthRefusal(
                "redirect_uri-mismatch",
                f"redirect_uri {redirect_uri!r} is not one this client registered",
            )
        wanted = tuple(scope)
        outside = sorted(set(wanted) - set(client.scopes))
        if outside:
            raise OAuthRefusal(
                "invalid-scope",
                f"client {client_id!r} is not registered for scope "
                f"{', '.join(outside)}; a client cannot ask for more than it was "
                "granted at registration",
            )
        code = secrets.token_urlsafe(32)
        self._codes[code] = _Code(
            client_id=client_id,
            subject=subject,
            scope=wanted,
            challenge=challenge,
            redirect_uri=redirect_uri,
            tenant=tenant,
            issued_at=time.time() if now is None else now,
            authorization_details=rich_details,
        )
        return code

    def redeem(
        self,
        code: str,
        *,
        verifier: str,
        client_id: str,
        client_secret: bytes | str | None = None,
        redirect_uri: str,
        dpop_jkt: str = "",
        now: float | None = None,
    ) -> IssuedToken:
        """Exchange a code once. A second exchange revokes what the first issued.

        Every parameter is required, and each one is a check RFC 6749 §4.1.3
        asks of the token endpoint. `_Code` carried `client_id`, `redirect_uri`
        and `issued_at` from the beginning; none of the three was read, so a
        leaked code was redeemable indefinitely, by any registered client,
        against any URI.

        Raises:
            OAuthRefusal: no such code, a code already redeemed (which revokes
                the token the first redemption issued), a code older than
                `code_ttl`, a different client, a different `redirect_uri`, or a
                verifier that does not match the challenge.
        """
        self._authenticate_client(client_id, client_secret)
        self._require_dpop(client_id, dpop_jkt)
        record = self._codes.get(code)
        if record is None:
            raise OAuthRefusal("unknown-code", "no such authorization code; it was never issued")
        if record.issued_token:
            # Two parties hold this code and only one of them is the client.
            # Refusing the second alone would leave the attacker's token live if
            # they redeemed first, so neither keeps anything.
            self._revoked.add(record.issued_token)
            del self._codes[code]
            raise OAuthRefusal(
                "code-replayed",
                "this authorization code has already been redeemed; the token issued "
                "by the first redemption has been revoked, because two parties holding "
                "one code means one of them is not the client",
            )
        moment = time.time() if now is None else now
        if moment - record.issued_at > self._code_ttl:
            # An authorization code is a bearer credential that travels through
            # a browser redirect, so it reaches referrer headers, proxy logs and
            # history. RFC 6749 §4.1.2 puts its maximum lifetime at ten minutes
            # and recommends one; this defaults to sixty seconds, which is
            # generous for a redirect that is already in flight.
            del self._codes[code]
            raise OAuthRefusal(
                "code-expired",
                "this authorization code has expired; it was issued more than "
                f"{self._code_ttl:g}s ago, and a code that never goes stale is a "
                "password in a browser log",
            )
        if record.client_id != client_id:
            raise OAuthRefusal(
                "client-mismatch",
                "this authorization code was issued to a different client; a code "
                "redeemable by any registered client is a code the interceptor can "
                "redeem",
            )
        if record.redirect_uri != redirect_uri:
            raise OAuthRefusal(
                "redirect_uri-mismatch",
                "this authorization code was issued for a different redirect_uri",
            )
        # Unconditional. The `if record.challenge:` this replaces meant an empty
        # challenge skipped PKCE altogether, and `issue_code` now refuses to mint
        # one -- but the check being unconditional is what makes that true for a
        # code minted by an older build, too.
        offered = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
        if not hmac.compare_digest(offered, record.challenge):
            raise OAuthRefusal(
                "pkce-mismatch",
                "the code_verifier does not match the challenge this code was issued against",
            )
        token = self.issue_access(
            subject=record.subject,
            audience=record.client_id,
            scope=record.scope,
            tenant=record.tenant,
            now=now,
            with_refresh=True,
            refresh_client_id=record.client_id,
            authorization_details=record.authorization_details,
            dpop_jkt=dpop_jkt,
            client_id=client_id,
        )
        self._codes[code] = _Code(
            client_id=record.client_id,
            subject=record.subject,
            scope=record.scope,
            challenge=record.challenge,
            redirect_uri=record.redirect_uri,
            tenant=record.tenant,
            issued_at=record.issued_at,
            authorization_details=record.authorization_details,
            issued_token=token.access_token,
        )
        return token

    def issue_access(
        self,
        *,
        subject: str | None,
        audience: str,
        scope: Iterable[str] = (),
        tenant: str = "",
        now: float | None = None,
        with_refresh: bool = False,
        refresh_client_id: str = "",
        dpop_jkt: str = "",
        client_id: str = "",
        authorization_details: Any = (),
    ) -> IssuedToken:
        """Mint one access token. What comes out is what `JwtVerifier` verifies."""
        self._require_dpop(client_id, dpop_jkt)
        rich_details = (
            self.validate_authorization_details(authorization_details)
            if authorization_details not in (None, (), [])
            else ()
        )
        moment = time.time() if now is None else now
        expires = moment + self._lifetime
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "aud": audience,
            "iat": int(moment),
            "exp": int(expires),
            "jti": secrets.token_urlsafe(12),
        }
        if subject is not None:
            claims["sub"] = subject
        if client_id:
            claims["client_id"] = client_id
        wanted = tuple(scope)
        if wanted:
            claims["scope"] = " ".join(wanted)
        if tenant:
            claims["tenant"] = tenant
        if dpop_jkt:
            claims["cnf"] = {"jkt": dpop_jkt}
        if rich_details:
            claims["authorization_details"] = list(rich_details)
        refresh = chain = ""
        if with_refresh and subject is not None:
            minted = self.issue_refresh(
                subject=subject,
                audience=audience,
                scope=wanted,
                tenant=tenant,
                client_id=refresh_client_id,
                dpop_jkt=dpop_jkt,
                authorization_details=rich_details,
                now=moment,
            )
            refresh, chain = minted.token, minted.chain
        access = self._encode(claims)
        if chain:
            self._chains.setdefault(chain, []).append(access)
        return IssuedToken(
            access_token=access,
            subject=subject,
            audience=audience,
            scope=wanted,
            expires_at=expires,
            tenant=tenant,
            refresh_token=refresh,
            token_type="DPoP" if dpop_jkt else "Bearer",
            authorization_details=rich_details,
        )

    def client_credentials(
        self,
        *,
        client_id: str,
        client_secret: bytes | str | None = None,
        subject: str | None = None,
        scope: Iterable[str] = (),
        dpop_jkt: str = "",
        authorization_details: Any = (),
    ) -> IssuedToken:
        """A machine token. It carries no `sub`, and asking for one is refused.

        A machine token with a subject is a machine that can act as a person,
        and every audit trail downstream then attributes its writes to them.
        """
        client = self._client(client_id)
        if subject is not None:
            raise OAuthRefusal(
                "subject-on-client-credentials",
                "the client-credentials grant has no resource owner, so it cannot "
                "carry a subject; a machine token naming a person is a machine that "
                "can act as one",
            )
        if not client.confidential:
            raise OAuthRefusal(
                "public-client",
                f"client {client_id!r} is public and cannot keep a secret, so it "
                "cannot use the client-credentials grant",
            )
        self._authenticate_client(client_id, client_secret)
        self._require_dpop(client_id, dpop_jkt)
        rich_details = self._client_authorization_details(client, authorization_details)
        wanted = tuple(scope) or client.scopes
        outside = sorted(set(wanted) - set(client.scopes))
        if outside:
            raise OAuthRefusal(
                "invalid-scope",
                f"client {client_id!r} is not registered for scope {', '.join(outside)}",
            )
        return self.issue_access(
            subject=None,
            audience=client_id,
            scope=wanted,
            dpop_jkt=dpop_jkt,
            client_id=client_id,
            authorization_details=rich_details,
        )

    def issue_refresh(
        self,
        *,
        subject: str,
        audience: str = "",
        scope: Iterable[str] = (),
        tenant: str = "",
        client_id: str = "",
        dpop_jkt: str = "",
        authorization_details: Any = (),
        now: float | None = None,
    ) -> _Refresh:
        chain = secrets.token_urlsafe(12)
        rich_details = (
            self.validate_authorization_details(authorization_details)
            if authorization_details not in (None, (), [])
            else ()
        )
        record = _Refresh(
            token=secrets.token_urlsafe(32),
            subject=subject,
            chain=chain,
            issued_at=time.time() if now is None else now,
            audience=audience or self._issuer,
            scope=tuple(scope),
            tenant=tenant,
            client_id=client_id,
            dpop_jkt=dpop_jkt,
            authorization_details=rich_details,
        )
        self._refresh[record.token] = record
        self._chains[chain] = []
        return record

    def rotate(
        self,
        refresh: _Refresh | str,
        *,
        audience: str = "",
        client_id: str | None = None,
        client_secret: bytes | str | None = None,
        dpop_jkt: str = "",
        now: float | None = None,
    ) -> IssuedToken:
        """Exchange a refresh token for a new pair. Reuse revokes the whole chain."""
        token = refresh if isinstance(refresh, str) else refresh.token
        record = self._refresh.get(token)
        if record is None:
            spent = self._spent.get(token)
            if spent is not None:
                chain, spent_client_id, spent_dpop_jkt = spent
                self._authenticate_refresh_client(
                    spent_client_id,
                    client_id=client_id,
                    client_secret=client_secret,
                )
                if spent_dpop_jkt and not hmac.compare_digest(spent_dpop_jkt, dpop_jkt):
                    raise OAuthRefusal(
                        "invalid-dpop-proof",
                        "this spent refresh token was bound to a different DPoP key",
                    )
                self.revoke_chain(chain)
            raise OAuthRefusal(
                "refresh-reused",
                "this refresh token has already been rotated or was never issued; "
                "every token in its chain has been revoked, because a rotated token "
                "being presented again means somebody else has a copy",
            )
        self._authenticate_refresh_client(
            record.client_id,
            client_id=client_id,
            client_secret=client_secret,
        )
        if record.dpop_jkt and not hmac.compare_digest(record.dpop_jkt, dpop_jkt):
            raise OAuthRefusal(
                "invalid-dpop-proof",
                "this refresh token can only be rotated with the same DPoP key",
            )
        if audience and audience != record.audience:
            raise OAuthRefusal(
                "audience-mismatch",
                "a refresh token cannot change the audience of its original grant",
            )
        moment = time.time() if now is None else now
        if moment - record.issued_at > self._refresh_ttl:
            del self._refresh[token]
            self.revoke_chain(record.chain)
            raise OAuthRefusal(
                "refresh-expired",
                f"this refresh token expired after its {self._refresh_ttl:g}s lifetime",
            )
        del self._refresh[token]
        issued = self.issue_access(
            subject=record.subject,
            audience=record.audience,
            scope=record.scope,
            tenant=record.tenant,
            dpop_jkt=record.dpop_jkt,
            client_id=record.client_id,
            authorization_details=record.authorization_details,
            now=moment,
        )
        successor = _Refresh(
            token=secrets.token_urlsafe(32),
            subject=record.subject,
            chain=record.chain,
            issued_at=moment,
            audience=record.audience,
            scope=record.scope,
            tenant=record.tenant,
            client_id=record.client_id,
            dpop_jkt=record.dpop_jkt,
            authorization_details=record.authorization_details,
        )
        self._refresh[successor.token] = successor
        self._spent[token] = (record.chain, record.client_id, record.dpop_jkt)
        self._chains.setdefault(record.chain, []).append(issued.access_token)
        return IssuedToken(
            access_token=issued.access_token,
            subject=issued.subject,
            audience=issued.audience,
            scope=issued.scope,
            expires_at=issued.expires_at,
            tenant=issued.tenant,
            refresh_token=successor.token,
            token_type=issued.token_type,
            authorization_details=issued.authorization_details,
        )

    def _authenticate_refresh_client(
        self,
        bound_client_id: str,
        *,
        client_id: str | None,
        client_secret: bytes | str | None,
    ) -> None:
        if not bound_client_id:
            if client_id is not None or client_secret is not None:
                raise OAuthRefusal(
                    "invalid-client",
                    "this refresh token is not bound to a registered OAuth client",
                )
            return
        if client_id != bound_client_id:
            raise OAuthRefusal(
                "invalid-client",
                "this refresh token is not bound to that OAuth client",
            )
        self._authenticate_client(bound_client_id, client_secret)

    def revoke_chain(self, chain: str) -> int:
        issued = self._chains.pop(chain, [])
        self._revoked.update(issued)
        for token, record in list(self._refresh.items()):
            if record.chain == chain:
                del self._refresh[token]
        # Spent tokens go too, or a third presentation of an already-revoked
        # token would try to revoke a chain that is no longer there and read as
        # a fresh incident.
        for token, (spent_chain, _client_id, _dpop_jkt) in list(self._spent.items()):
            if spent_chain == chain:
                del self._spent[token]
        return len(issued)

    def is_revoked(self, access_token: str) -> bool:
        """The `RevocationCheck` `JwtVerifier` already takes."""
        return access_token in self._revoked

    def introspection_jwt(
        self,
        token: str,
        *,
        client_id: str,
        client_secret: bytes | str | None,
        now: float | None = None,
    ) -> str:
        """Return an RFC 9701 JWT token-introspection response."""
        client = self._authenticate_client(client_id, client_secret)
        if client.introspection_signed_response_alg is None:
            raise OAuthRefusal(
                "invalid-client",
                f"client {client_id!r} is not registered for JWT token introspection",
            )
        moment = time.time() if now is None else now
        response = {
            "iss": self._issuer,
            "aud": client_id,
            "iat": int(moment),
            "token_introspection": self._introspection_claims(
                token,
                audience=client_id,
                scopes=client.scopes,
                now=moment,
            ),
        }
        return self._encode(
            response,
            typ="token-introspection+jwt",
            count_issued=False,
        )

    def _introspection_claims(
        self,
        token: str,
        *,
        audience: str,
        scopes: tuple[str, ...],
        now: float,
    ) -> dict[str, Any]:
        from ._auth.jwt import _parse_compact, _verify_signature

        try:
            header, claims, signing_input, signature = _parse_compact(token)
            algorithm = self._signing_algorithm()
            if header.get("alg") != algorithm or header.get("typ") != "JWT":
                return {"active": False}
            if self._verifying_key is None:
                raise RuntimeError(
                    "JWT token introspection client registered without a verifying key"
                )
            if not _verify_signature(
                algorithm,
                self._verifying_key,
                signing_input,
                signature,
            ):
                return {"active": False}
        except (KeyError, OverflowError, TypeError, ValueError):
            return {"active": False}

        issuer = claims.get("iss")
        expires = claims.get("exp")
        token_audience = claims.get("aud")
        if isinstance(token_audience, str):
            audiences = (token_audience,)
        elif isinstance(token_audience, list) and all(
            isinstance(item, str) for item in token_audience
        ):
            audiences = tuple(token_audience)
        else:
            return {"active": False}
        if (
            issuer != self._issuer
            or isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or expires <= now
            or audience not in audiences
            or token in self._revoked
        ):
            return {"active": False}
        introspection = dict(claims)
        scope = introspection.get("scope")
        if scopes and isinstance(scope, str):
            allowed_scopes = frozenset(scopes)
            relevant = [item for item in scope.split() if item in allowed_scopes]
            if relevant:
                introspection["scope"] = " ".join(relevant)
            else:
                introspection.pop("scope")
        introspection["active"] = True
        return introspection

    def counters(self) -> Any:
        """What signing has cost, so the ceiling is watched rather than warned about.

        `wreath.metrics.collect` gathers by asking anything that offers
        `counters()`, so these reach a dashboard with no second registration.

        ES256 signing is synchronous CPU work, so the request issuing a token
        waits for it even when unrelated requests do not. Divide
        `signing_nanoseconds` by elapsed wall-clock nanoseconds for the fraction
        of a core this issuer is spending; a threshold baked in here would be
        one guess applied to every deployment's latency budget.
        """
        from .metrics import Counters

        return Counters(
            subsystem="oauth",
            instance=self._issuer,
            values={
                "tokens_issued": self._issued,
                "signing_nanoseconds": int(self._signing_seconds * 1_000_000_000),
            },
        )

    def _encode(
        self,
        claims: Mapping[str, Any],
        *,
        typ: str = "JWT",
        count_issued: bool = True,
    ) -> str:
        if self._signer is not None:
            started = time.perf_counter()
            try:
                if typ == "JWT":
                    return self._signer.encode(claims)
                return self._signer.encode_with_type(claims, typ)
            finally:
                # Counted around the asymmetric path only. The HMAC path is
                # ~12us and measuring it would cost a meaningful fraction of
                # what it measures, which is the trap `AGENTS.md` names about
                # cProfile one order of magnitude down.
                self._signing_seconds += time.perf_counter() - started
                if count_issued:
                    self._issued += 1
        if count_issued:
            self._issued += 1
        header = _b64(_json_dumps({"alg": "HS256", "typ": typ}))
        payload = _b64(_json_dumps(dict(sorted(claims.items()))))
        signing_input = f"{header}.{payload}".encode("ascii")
        signature = _b64(hmac.new(self._secret, signing_input, hashlib.sha256).digest())
        return f"{header}.{payload}.{signature}"

    @property
    def secret(self) -> bytes:
        """The HMAC secret, for building the verifier that reads these tokens."""
        return self._secret
