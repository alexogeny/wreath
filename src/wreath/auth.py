"""Authentication: identities, credentials, and authentication backends.

Authentication establishes *who* a request is. Authorization — what an identity
is permitted to do — lives in `wreath.authorization`. Both are thin public
facades over the private `wreath._auth` implementation package.
"""

from __future__ import annotations

from ._auth.backends import AuthenticationBackend, BearerTokenBackend
from ._auth.decorators import authenticated, identify
from ._auth.jwt import (
    JwtError,
    JwtVerifier,
    RsaPublicKey,
    SymmetricKey,
    UnsupportedAlgorithm,
    default_identity,
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
    "key_from_jwk",
    "key_from_pem",
]
