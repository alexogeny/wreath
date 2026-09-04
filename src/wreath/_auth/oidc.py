"""OIDC provider: discovery, a JWKS-backed bearer verifier, and endpoints.

An `OidcProvider` is registered like an HTTP client (`app.oidc_provider`)
and discovered during lifespan startup. Its `bearer_verifier()` returns an
async `Verifier` for `wreath.auth.BearerTokenBackend`; the resulting
`Identity` flows straight into the existing Cedar authorizer mappers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from .. import _json
from .jwks import JwksCache
from .jwt import (
    IdentityMapper,
    compile_audiences,
    default_identity,
    freeze_algorithms,
    peek_header,
    verify_jwt,
)
from .models import Identity

__all__ = ["OidcProvider"]

_MAX_DISCOVERY_BYTES = 64 * 1024
_USE_PROVIDER_AUDIENCES = object()
_MISSING = object()
_ENDPOINT_ORIGIN_ERROR = (
    "OIDC endpoint is not on the pinned issuer origin; expected the issuer's "
    "scheme, host, and port without credentials or a fragment"
)


def _matches_token_class(
    token_type: object,
    claims: object,
    *,
    login_token: bool,
) -> bool:
    if not isinstance(claims, Mapping):
        return False
    token_use = claims.get("token_use", _MISSING)
    claim_type = claims.get("token_type", _MISSING)
    if token_use is not _MISSING and claim_type is not _MISSING:
        if token_use != claim_type:
            return False
        use = token_use
    elif token_use is not _MISSING:
        use = token_use
    else:
        use = claim_type
    if use is not _MISSING and not isinstance(use, str):
        return False
    if use is _MISSING:
        use = None
    if login_token:
        return token_type in (None, "JWT") and use in (None, "id")
    if token_type == "at+jwt":
        return use in (None, "access")
    return token_type in (None, "JWT") and use == "access"


def _default_ports(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _has_url_controls(value: str) -> bool:
    return any(ord(character) < 0x21 or 0x7F <= ord(character) <= 0x9F for character in value)


def _require_same_origin(issuer: str, url: str) -> str:
    """Return `url` unchanged, once it is known to be on `issuer`'s origin.

    This is the anti-SSRF pin, and it is the whole of it: the origin comparison
    lives here and every discovered endpoint goes through it. Two callers want
    two different things out of it, and only the comparison is shared --
    `_same_origin_path` reduces a *fetched* endpoint to the path its
    origin-pinned HTTP client takes, while the authorization endpoint is a
    **browser redirect target** and has to stay an absolute URL, so it uses this
    one directly. Reducing that one to a path would send the caller to this
    application's own `/authorize` instead of the provider's.

    Raises:
        ValueError: `url` is not on the issuer's scheme/host/port.
    """
    if _has_url_controls(url):
        raise ValueError(_ENDPOINT_ORIGIN_ERROR)
    try:
        iss = urlsplit(issuer)
        target = urlsplit(url)
        issuer_explicit_port = iss.port
        target_explicit_port = target.port
        if issuer_explicit_port == 0 or target_explicit_port == 0:
            raise ValueError("port zero is not a network endpoint")
        issuer_port = (
            issuer_explicit_port
            if issuer_explicit_port is not None
            else _default_ports(iss.scheme)
        )
        target_port = (
            target_explicit_port
            if target_explicit_port is not None
            else _default_ports(target.scheme)
        )
    except ValueError as error:
        raise ValueError(_ENDPOINT_ORIGIN_ERROR) from error
    same = (
        target.scheme == iss.scheme
        and target.hostname == iss.hostname
        and target.username is None
        and not target.fragment
        and target_port == issuer_port
    )
    if not same:
        raise ValueError(_ENDPOINT_ORIGIN_ERROR)
    return url


def _same_origin_path(issuer: str, url: str) -> str:
    """Return the path(+query) of `url`, requiring it to share `issuer`'s
    origin, so a tampered discovery document cannot redirect a fetch."""
    target = urlsplit(_require_same_origin(issuer, url))
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

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "issuer" and hasattr(self, "issuer"):
            raise AttributeError("OIDC issuer is the pinned trust origin and cannot be changed")
        if (
            name in ("authorization_endpoint", "jwks_uri", "token_endpoint")
            and value is not None
        ):
            issuer = getattr(self, "issuer", None)
            if not isinstance(value, str):
                raise ValueError(_ENDPOINT_ORIGIN_ERROR)
            if issuer is not None:
                _require_same_origin(issuer, value)
        object.__setattr__(self, name, value)

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
        try:
            parsed_issuer = urlsplit(self.issuer)
            _ = parsed_issuer.port
        except ValueError as error:
            raise ValueError(
                "OIDC issuer must be an absolute HTTPS URL without credentials"
            ) from error
        if (
            _has_url_controls(self.issuer)
            or parsed_issuer.scheme != "https"
            or parsed_issuer.hostname is None
            or parsed_issuer.username is not None
            or parsed_issuer.port == 0
            or parsed_issuer.query
            or parsed_issuer.fragment
        ):
            raise ValueError("OIDC issuer must be an absolute HTTPS URL without credentials")
        try:
            parsed_issuer.hostname.encode("idna")
        except UnicodeError as error:
            raise ValueError(
                "OIDC issuer must be an absolute HTTPS URL without credentials"
            ) from error
        client_origin = getattr(http_client, "origin", None)
        if client_origin is not None:
            try:
                _require_same_origin(self.issuer, f"{client_origin}/")
            except ValueError as error:
                raise ValueError(
                    "OIDC HTTP client origin must match the issuer origin's scheme, "
                    "host, and port without credentials or a fragment"
                ) from error
        self._client = http_client
        self._algorithms = freeze_algorithms(algorithms)
        self._audiences = compile_audiences(audience)
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
            raise RuntimeError(f"OIDC discovery for {self.name!r} failed: HTTP {response.status}")
        if len(response.body) > _MAX_DISCOVERY_BYTES:
            raise ValueError("OIDC discovery document exceeds size cap")
        document = _json.loads(response.body)
        if document.get("issuer") != self.issuer:
            raise ValueError("OIDC discovery 'issuer' does not match the configured issuer")
        jwks_uri = document["jwks_uri"]
        jwks_path = _same_origin_path(self.issuer, jwks_uri)
        token_endpoint = document.get("token_endpoint")
        if token_endpoint is not None:
            _require_same_origin(self.issuer, token_endpoint)
        # This stays absolute because the browser is sent there; the fetched
        # token and JWKS endpoints are reduced to paths by their origin-pinned client.
        authorization_endpoint = document.get("authorization_endpoint")
        if authorization_endpoint is not None:
            _require_same_origin(self.issuer, authorization_endpoint)
        cache = JwksCache(http_client=self._client, jwks_path=jwks_path)
        await cache.prefetch()
        self.jwks_uri = jwks_uri
        self.token_endpoint = token_endpoint
        self.authorization_endpoint = authorization_endpoint
        self._cache = cache

    def bearer_verifier(self, *, audience: Any = _USE_PROVIDER_AUDIENCES):
        """Return an async `Verifier` closing over this provider's JWKS cache."""

        login_token = audience is not _USE_PROVIDER_AUDIENCES
        required = self._required
        if login_token:
            missing = tuple(
                claim for claim in ("exp", "iat", "sub") if claim not in required
            )
            required = (*required, *missing)
        audiences = (
            self._audiences if audience is _USE_PROVIDER_AUDIENCES else compile_audiences(audience)
        )

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
            resolved = verify_jwt(
                token,
                key_resolver=lambda _header: key,
                algorithms=self._algorithms,
                issuer=self.issuer,
                audiences=audiences,
                leeway=self._leeway,
                required=required,
                identity=self._identity,
            )
            if resolved is None:
                return None
            if not _matches_token_class(
                header.get("typ"),
                resolved.claims,
                login_token=login_token,
            ):
                return None
            if login_token and not _matches_authorized_party(resolved.claims, audiences):
                return None
            return resolved

        return verify


def _matches_authorized_party(claims: Mapping[str, Any], audiences: frozenset[str]) -> bool:
    audience = claims.get("aud")
    multiple = isinstance(audience, (list, tuple)) and len(audience) > 1
    authorized = claims.get("azp")
    if authorized is None:
        return not multiple
    return isinstance(authorized, str) and authorized in audiences
