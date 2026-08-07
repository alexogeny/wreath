"""Issuing OAuth 2.1 tokens, for a deployment that decided to.

Wreath has always *verified* bearer tokens -- `wreath.auth.JwtVerifier`,
`MCPAuth`'s protected-resource metadata, an audience-bound refusal for a token
minted elsewhere -- and minted none. `docs/reference/roadmap.md` declined the
other half on the grounds that issuance belongs to the deployment's identity
provider, which is right for a deployment that has one and unhelpful for a
deployment that *is* one: a service whose own machine clients need tokens has
nowhere to get them, and writes the endpoint by hand.

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

The cost, measured here over three warm runs rather than assumed:

    HS256   11.5-13.2 us per token
    ES256    2.91-3.01 ms per token

That is ~230x, and it is the right trade at a login and the wrong one on a path
minting thousands of tokens a second -- P-256 scalar multiplication is pure
Python here, because it is `_webpush`'s and that path signs one VAPID token per
push batch. A deployment that needs both public verifiability and volume should
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
import json
import secrets
import time
from base64 import urlsafe_b64encode
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AuthorizationServer",
    "ClientRegistration",
    "Es256Signer",
    "IssuedToken",
    "OAuthRefusal",
]


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


@dataclass(frozen=True, slots=True)
class _Code:
    client_id: str
    subject: str
    scope: tuple[str, ...]
    challenge: str
    redirect_uri: str
    tenant: str
    issued_at: float
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


def _b64(raw: bytes) -> str:
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


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
    #: Names this key in the JWS header and in the key set, so a verifier can
    #: pick during a rotation instead of trying every key.
    kid: str = "wreath-es256"

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

        point = _mul(self.private, P256_G)
        if point is None:  # pragma: no cover - the scalar is 1..n-1 by construction
            raise OAuthRefusal("bad-key", "this private scalar has no public point")
        return point

    def public_jwks(self) -> list[dict[str, str]]:
        """This key as JWKS entries -- the public half only, by construction.

        There is no way to spell the private scalar in this shape, which is the
        difference from the HMAC case: the thing that would be dangerous to
        publish is simply not among the fields.
        """
        x, y = self._public_point()
        return [{
            "kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig",
            "kid": self.kid,
            "x": _b64(x.to_bytes(32, "big")),
            "y": _b64(y.to_bytes(32, "big")),
        }]

    def encode(self, claims: Mapping[str, Any]) -> str:
        from ._webpush import _ecdsa_sign

        header = _b64(json.dumps(
            {"alg": "ES256", "typ": "JWT", "kid": self.kid},
            separators=(",", ":")).encode("utf-8"))
        payload = _b64(json.dumps(
            dict(claims), separators=(",", ":"), sort_keys=True).encode("utf-8"))
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
        "_chains", "_clients", "_codes", "_issued", "_issuer", "_lifetime",
        "_refresh", "_revoked", "_secret", "_signer", "_signing_seconds",
    )

    def __init__(
        self,
        *,
        issuer: str,
        secret: bytes | str = b"",
        clients: Iterable[ClientRegistration] = (),
        lifetime: float = 3600.0,
        signer: Any = None,
    ) -> None:
        self._issuer = issuer
        # Generated when absent so a test or a single-process deployment needs
        # no ceremony. A fleet must pass one: two workers with different secrets
        # issue tokens neither can verify.
        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._secret = raw or secrets.token_bytes(32)
        self._clients = {client.client_id: client for client in clients}
        self._lifetime = lifetime
        self._signer = signer
        self._codes: dict[str, _Code] = {}
        self._refresh: dict[str, _Refresh] = {}
        self._chains: dict[str, list[str]] = {}
        self._revoked: set[str] = set()
        self._issued = 0
        self._signing_seconds = 0.0

    # -- discovery ----------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        """RFC 8414 metadata, so a client configures itself instead of being told."""
        return {
            "issuer": self._issuer,
            "authorization_endpoint": f"{self._issuer}/oauth/authorize",
            "token_endpoint": f"{self._issuer}/oauth/token",
            "jwks_uri": f"{self._issuer}/oauth/jwks",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token",
                                      "client_credentials"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        }

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

    # -- the authorization endpoint -----------------------------------------

    def register(self, client: ClientRegistration) -> None:
        self._clients[client.client_id] = client

    def _client(self, client_id: str) -> ClientRegistration:
        client = self._clients.get(client_id)
        if client is None:
            raise OAuthRefusal("unknown-client", f"no client registered as {client_id!r}")
        return client

    def authorize(
        self, *, client_id: str, redirect_uri: str, challenge_method: str = "S256",
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
        scope: Iterable[str] = (),
        challenge: str = "",
        redirect_uri: str = "",
        tenant: str = "",
        now: float | None = None,
    ) -> str:
        client = self._client(client_id)
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
            client_id=client_id, subject=subject, scope=wanted, challenge=challenge,
            redirect_uri=redirect_uri, tenant=tenant,
            issued_at=time.time() if now is None else now,
        )
        return code

    def redeem(self, code: str, *, verifier: str = "", now: float | None = None) -> IssuedToken:
        """Exchange a code once. A second exchange revokes what the first issued."""
        record = self._codes.get(code)
        if record is None:
            raise OAuthRefusal(
                "unknown-code", "no such authorization code; it was never issued")
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
        if record.challenge:
            offered = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
            if not hmac.compare_digest(offered, record.challenge):
                raise OAuthRefusal(
                    "pkce-mismatch",
                    "the code_verifier does not match the challenge this code was "
                    "issued against",
                )
        token = self.issue_access(
            subject=record.subject, audience=record.client_id, scope=record.scope,
            tenant=record.tenant, now=now, with_refresh=True,
        )
        self._codes[code] = _Code(
            client_id=record.client_id, subject=record.subject, scope=record.scope,
            challenge=record.challenge, redirect_uri=record.redirect_uri,
            tenant=record.tenant, issued_at=record.issued_at,
            issued_token=token.access_token,
        )
        return token

    # -- token minting ------------------------------------------------------

    def issue_access(
        self,
        *,
        subject: str | None,
        audience: str,
        scope: Iterable[str] = (),
        tenant: str = "",
        now: float | None = None,
        with_refresh: bool = False,
    ) -> IssuedToken:
        """Mint one access token. What comes out is what `JwtVerifier` verifies."""
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
        wanted = tuple(scope)
        if wanted:
            claims["scope"] = " ".join(wanted)
        if tenant:
            claims["tenant"] = tenant
        refresh = ""
        if with_refresh and subject is not None:
            refresh = self.issue_refresh(subject=subject, now=moment).token
        return IssuedToken(
            access_token=self._encode(claims),
            subject=subject, audience=audience, scope=wanted,
            expires_at=expires, tenant=tenant, refresh_token=refresh,
        )

    def client_credentials(
        self, *, client_id: str, subject: str | None = None, scope: Iterable[str] = (),
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
        return self.issue_access(
            subject=None, audience=client_id, scope=scope or client.scopes)

    # -- refresh ------------------------------------------------------------

    def issue_refresh(self, *, subject: str, now: float | None = None) -> _Refresh:
        chain = secrets.token_urlsafe(12)
        record = _Refresh(
            token=secrets.token_urlsafe(32), subject=subject, chain=chain,
            issued_at=time.time() if now is None else now,
        )
        self._refresh[record.token] = record
        self._chains[chain] = []
        return record

    def rotate(self, refresh: _Refresh | str, *, audience: str = "") -> IssuedToken:
        """Exchange a refresh token for a new pair. Reuse revokes the whole chain."""
        token = refresh if isinstance(refresh, str) else refresh.token
        record = self._refresh.pop(token, None)
        if record is None:
            raise OAuthRefusal(
                "refresh-reused",
                "this refresh token has already been rotated or was never issued; "
                "every token in its chain has been revoked, because a rotated token "
                "being presented again means somebody else has a copy",
            )
        issued = self.issue_access(
            subject=record.subject, audience=audience or self._issuer)
        successor = _Refresh(
            token=secrets.token_urlsafe(32), subject=record.subject,
            chain=record.chain, issued_at=time.time(),
        )
        self._refresh[successor.token] = successor
        self._chains.setdefault(record.chain, []).append(issued.access_token)
        return IssuedToken(
            access_token=issued.access_token, subject=issued.subject,
            audience=issued.audience, scope=issued.scope,
            expires_at=issued.expires_at, tenant=issued.tenant,
            refresh_token=successor.token,
        )

    def revoke_chain(self, chain: str) -> int:
        issued = self._chains.pop(chain, [])
        self._revoked.update(issued)
        for token, record in list(self._refresh.items()):
            if record.chain == chain:
                del self._refresh[token]
        return len(issued)

    def is_revoked(self, access_token: str) -> bool:
        """The `RevocationCheck` `JwtVerifier` already takes."""
        return access_token in self._revoked

    # -- signing ------------------------------------------------------------

    def counters(self) -> dict[str, float]:
        """What signing has cost, so the ceiling is watched rather than warned about.

        `wreath.metrics.collect` gathers by asking anything that offers
        `counters()`, so these reach a dashboard with no second registration.

        They exist because ES256 signing is CPU-bound pure Python and therefore
        holds the loop. Measured here, the median request is unaffected at every
        rate and the request *behind a signature* waits the full signature:

            ES256 at  10/s   p50 lag 0.01 ms   p99 0.89 ms
            ES256 at  50/s   p50 lag 0.01 ms   p99 3.00 ms
            ES256 at 200/s   p50 lag 0.01 ms   p99 3.15 ms
            HS256 at 200/s   p50 lag 0.01 ms   p99 0.01 ms

        So it is a **tail** problem, not a throughput one, until roughly 330
        signatures a second saturates a core. `signing_seconds` divided by wall
        time is the fraction of a core this is spending, which is the number to
        alert on -- a threshold baked in here would be one guess applied to every
        deployment's latency budget.
        """
        return {
            "tokens_issued": float(self._issued),
            "signing_seconds": self._signing_seconds,
        }

    def _encode(self, claims: Mapping[str, Any]) -> str:
        if self._signer is not None:
            started = time.perf_counter()
            try:
                return self._signer.encode(claims)
            finally:
                # Counted around the asymmetric path only. The HMAC path is
                # ~12us and measuring it would cost a meaningful fraction of
                # what it measures, which is the trap `AGENTS.md` names about
                # cProfile one order of magnitude down.
                self._signing_seconds += time.perf_counter() - started
                self._issued += 1
        self._issued += 1
        header = _b64(json.dumps(
            {"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
        payload = _b64(json.dumps(
            dict(claims), separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signing_input = f"{header}.{payload}".encode("ascii")
        signature = _b64(hmac.new(self._secret, signing_input, hashlib.sha256).digest())
        return f"{header}.{payload}.{signature}"

    @property
    def secret(self) -> bytes:
        """The HMAC secret, for building the verifier that reads these tokens."""
        return self._secret
