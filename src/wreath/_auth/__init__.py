"""Authentication and compiled authorization primitives."""

from .backends import AuthenticationBackend, AuthorizationProvider, BearerTokenBackend
from .cedar import CedarAuthorizer, CedarEngine
from .decorators import authenticated, authorize, permissions, roles
from .models import AuthorizationDecision, Credentials, Identity
from .requirements import AuthRequirement

__all__ = [
    "AuthRequirement",
    "AuthenticationBackend",
    "AuthorizationDecision",
    "AuthorizationProvider",
    "BearerTokenBackend",
    "CedarAuthorizer",
    "CedarEngine",
    "Credentials",
    "Identity",
    "authenticated",
    "authorize",
    "permissions",
    "roles",
]
