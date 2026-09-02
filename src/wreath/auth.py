"""Authentication: identities, credentials, and authentication backends.

Authentication establishes *who* a request is. Authorization — what an identity
is permitted to do — lives in `wreath.authorization`. Both are thin public
facades over the private `wreath._auth` implementation package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._auth.backends import AuthenticationBackend, BearerTokenBackend
    from ._auth.decorators import (
        authenticated,
        identify,
        oauth_step_up,
        public,
        second_factor,
    )
    from ._auth.jwt import (
        JwtError,
        JwtVerifier,
        RsaPublicKey,
        SymmetricKey,
        UnsupportedAlgorithm,
        default_identity,
        jwk_thumbprint,
        jwk_thumbprint_uri,
        key_from_jwk,
        key_from_pem,
    )
    from ._auth.models import Credentials, Identity
    from ._auth.oauth2 import ClientCredentials
    from ._auth.oidc import OidcProvider
    from ._auth.session_backend import CompositeBackend, SessionIdentityBackend

__all__ = [
    "AuthenticationBackend",
    "BearerTokenBackend",
    "ClientCredentials",
    "CompositeBackend",
    "Credentials",
    "Identity",
    "JwtError",
    "JwtVerifier",
    "OidcProvider",
    "RsaPublicKey",
    "SessionIdentityBackend",
    "SymmetricKey",
    "UnsupportedAlgorithm",
    "authenticated",
    "default_identity",
    "identify",
    "jwk_thumbprint",
    "jwk_thumbprint_uri",
    "key_from_jwk",
    "key_from_pem",
    "oauth_step_up",
    "public",
    "second_factor",
]

_EXPORTS = {
    "AuthenticationBackend": "backends",
    "BearerTokenBackend": "backends",
    "ClientCredentials": "oauth2",
    "CompositeBackend": "session_backend",
    "Credentials": "models",
    "Identity": "models",
    "JwtError": "jwt",
    "JwtVerifier": "jwt",
    "OidcProvider": "oidc",
    "RsaPublicKey": "jwt",
    "SessionIdentityBackend": "session_backend",
    "SymmetricKey": "jwt",
    "UnsupportedAlgorithm": "jwt",
    "authenticated": "decorators",
    "default_identity": "jwt",
    "identify": "decorators",
    "jwk_thumbprint": "jwt",
    "jwk_thumbprint_uri": "jwt",
    "key_from_jwk": "jwt",
    "key_from_pem": "jwt",
    "oauth_step_up": "decorators",
    "public": "decorators",
    "second_factor": "decorators",
}

_MODULE_EXPORTS = {
    "backends": ("AuthenticationBackend", "BearerTokenBackend"),
    "decorators": (
        "authenticated",
        "identify",
        "oauth_step_up",
        "public",
        "second_factor",
    ),
    "jwt": (
        "JwtError",
        "JwtVerifier",
        "RsaPublicKey",
        "SymmetricKey",
        "UnsupportedAlgorithm",
        "default_identity",
        "jwk_thumbprint",
        "jwk_thumbprint_uri",
        "key_from_jwk",
        "key_from_pem",
    ),
    "models": ("Credentials", "Identity"),
    "oauth2": ("ClientCredentials",),
    "oidc": ("OidcProvider",),
    "session_backend": ("CompositeBackend", "SessionIdentityBackend"),
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    loaded = import_module(f"._auth.{module}", __package__)
    namespace = globals()
    for export in _MODULE_EXPORTS[module]:
        namespace[export] = getattr(loaded, export)
    return namespace[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
