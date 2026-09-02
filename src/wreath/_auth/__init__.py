"""Authentication and compiled authorization primitives."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backends import (
        AuthenticationBackend,
        AuthorizationProvider,
        BearerTokenBackend,
    )
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

_EXPORTS = {
    "AuthRequirement": "requirements",
    "AuthenticationBackend": "backends",
    "AuthorizationDecision": "models",
    "AuthorizationProvider": "backends",
    "BearerTokenBackend": "backends",
    "CedarAuthorizer": "cedar",
    "CedarEngine": "cedar",
    "Credentials": "models",
    "Identity": "models",
    "authenticated": "decorators",
    "authorize": "decorators",
    "permissions": "decorators",
    "roles": "decorators",
}

_MODULE_EXPORTS = {
    "requirements": ("AuthRequirement",),
    "backends": (
        "AuthenticationBackend",
        "AuthorizationProvider",
        "BearerTokenBackend",
    ),
    "models": ("AuthorizationDecision", "Credentials", "Identity"),
    "cedar": ("CedarAuthorizer", "CedarEngine"),
    "decorators": ("authenticated", "authorize", "permissions", "roles"),
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    loaded = import_module(f".{module}", __name__)
    namespace = globals()
    for export in _MODULE_EXPORTS[module]:
        namespace[export] = getattr(loaded, export)
    return namespace[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
