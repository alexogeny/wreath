"""OIDC provider: discovery, a JWKS-backed bearer verifier, and endpoints.

An `OidcProvider` is registered like an HTTP client (`app.oidc_provider`)
and discovered during lifespan startup. Its `bearer_verifier()` returns an
async `Verifier` for `wreath.auth.BearerTokenBackend`; the resulting
`Identity` flows straight into the existing Cedar authorizer mappers.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import urlsplit

from .jwks import JwksCache
from .jwt import (
    IdentityMapper,
    default_identity,
    freeze_algorithms,
    freeze_audiences,
    peek_header,
    verify_jwt,
)
from .models import Identity

__all__ = ["OidcProvider"]

_MAX_DISCOVERY_BYTES = 64 * 1024


def _default_ports(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _same_origin_path(issuer: str, url: str) -> str:
    """Return the path(+query) of `url`, requiring it to share `issuer`'s
    origin. This is the anti-SSRF pin: every endpoint we fetch must live on the
    exact issuer origin, so a tampered discovery document cannot redirect us."""
    iss = urlsplit(issuer)
    target = urlsplit(url)
    same = (
        target.scheme == iss.scheme
        and target.hostname == iss.hostname
        and (target.port or _default_ports(target.scheme))
        == (iss.port or _default_ports(iss.scheme))
    )
    if not same:
        raise ValueError(
            f"OIDC endpoint {url!r} is not on the pinned issuer origin"
        )
    path = target.path or "/"
    return f"{path}?{target.query}" if target.query else path


class OidcProvider:
    """One discovered OIDC identity provider."""

    __slots__ = (
        "_algorithms",
        "_audiences",
        "_cache",
        "_client",
        "_identity",
        "_leeway",
        "_required",
        "authorization_endpoint",
        "issuer",
        "jwks_uri",
        "name",
        "token_endpoint",
    )

    def __init__(
        self,
        name: str,
        *,
        issuer: str,
        audience: str | Sequence[str] | None,
        http_client: Any,
        algorithms: Iterable[str] = ("RS256",),
        leeway: int = 60,
        required: Iterable[str] = ("exp",),
        identity: IdentityMapper = default_identity,
    ) -> None:
        self.name = name
        self.issuer = issuer.rstrip("/")
        self._client = http_client
        self._algorithms = freeze_algorithms(algorithms)
        self._audiences = freeze_audiences(audience)
        self._leeway = int(leeway)
        self._required = tuple(required)
        self._identity = identity
        self._cache: JwksCache | None = None
        self.jwks_uri: str | None = None
        self.token_endpoint: str | None = None
        self.authorization_endpoint: str | None = None

    async def discover(self) -> None:
        """Fetch the discovery document and prefetch the JWKS. Run at startup."""
        config_path = _same_origin_path(
            self.issuer, f"{self.issuer}/.well-known/openid-configuration"
        )
        response = await self._client.get(config_path)
        if response.status != 200:
            raise RuntimeError(
                f"OIDC discovery for {self.name!r} failed: HTTP {response.status}"
            )
        if len(response.body) > _MAX_DISCOVERY_BYTES:
            raise ValueError("OIDC discovery document exceeds size cap")
        document = json.loads(response.body)
        if document.get("issuer") != self.issuer:
            raise ValueError(
                "OIDC discovery 'issuer' does not match the configured issuer"
            )
        self.jwks_uri = document["jwks_uri"]
        self.token_endpoint = document.get("token_endpoint")
        self.authorization_endpoint = document.get("authorization_endpoint")
        jwks_path = _same_origin_path(self.issuer, self.jwks_uri)
        self._cache = JwksCache(http_client=self._client, jwks_path=jwks_path)
        await self._cache.prefetch()

    def bearer_verifier(self):  # returns an async Verifier
        """Return an async `Verifier` closing over this provider's JWKS cache."""

        async def verify(token: str) -> Identity | None:
            cache = self._cache
            if cache is None:
                return None  # not yet discovered; fail closed
            header = peek_header(token)
            if header is None:
                return None
            kid = header.get("kid")
            key = await cache.resolve(kid if isinstance(kid, str) else None)
            if key is None:
                return None
            return verify_jwt(
                token,
                key_resolver=lambda _header: key,
                algorithms=self._algorithms,
                issuer=self.issuer,
                audiences=self._audiences,
                leeway=self._leeway,
                required=self._required,
                identity=self._identity,
            )

        return verify
