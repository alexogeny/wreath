"""Authorization: roles, permissions, policies, and authorization decisions.

Authorization determines *what* an authenticated identity may do. Establishing
identity lives in `wreath.auth`. Both are thin public facades over the
private `wreath._auth` implementation package.

`LiveDocument` is re-exported here because `permission_document`
returns one: it is the per-principal document primitive -- an `ETag` and a
change stream -- and permissions are its first caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._auth.backends import AuthorizationProvider
    from ._auth.cedar import CedarAuthorizer, CedarEngine
    from ._auth.cedar_engine import (
        CedarEntity,
        CedarParseError,
        CedarPolicies,
        CedarSchema,
        EntityUid,
    )
    from ._auth.decorators import authorize, permissions, roles
    from ._auth.geofence import PrecisionLadder, Regions, coarsen
    from ._auth.models import AuthorizationDecision
    from ._auth.permissions import (
        PERMISSION_CHANNEL,
        declared_actions,
        permission_document,
        permissions_router,
    )
    from ._auth.principal import (
        ANY_SCOPE,
        Limits,
        Narrowing,
        Principal,
        human,
        member_of,
        on_plan,
        with_entitlements,
    )
    from ._auth.requirements import AuthorizationVocabulary, AuthRequirement
    from ._livedoc import LiveDocument

__all__ = [
    "ANY_SCOPE",
    "PERMISSION_CHANNEL",
    "AuthRequirement",
    "AuthorizationDecision",
    "AuthorizationProvider",
    "AuthorizationVocabulary",
    "CedarAuthorizer",
    "CedarEngine",
    "CedarEntity",
    "CedarParseError",
    "CedarPolicies",
    "CedarSchema",
    "EntityUid",
    "Limits",
    "LiveDocument",
    "Narrowing",
    "PrecisionLadder",
    "Principal",
    "Regions",
    "authorize",
    "coarsen",
    "declared_actions",
    "human",
    "member_of",
    "on_plan",
    "permission_document",
    "permissions",
    "permissions_router",
    "roles",
    "with_entitlements",
]

_EXPORTS = {
    "ANY_SCOPE": "principal",
    "PERMISSION_CHANNEL": "permissions",
    "AuthRequirement": "requirements",
    "AuthorizationDecision": "models",
    "AuthorizationProvider": "backends",
    "AuthorizationVocabulary": "requirements",
    "CedarAuthorizer": "cedar",
    "CedarEngine": "cedar",
    "CedarEntity": "cedar_engine",
    "CedarParseError": "cedar_engine",
    "CedarPolicies": "cedar_engine",
    "CedarSchema": "cedar_engine",
    "EntityUid": "cedar_engine",
    "Limits": "principal",
    "LiveDocument": "../_livedoc",
    "Narrowing": "principal",
    "PrecisionLadder": "geofence",
    "Principal": "principal",
    "Regions": "geofence",
    "authorize": "decorators",
    "coarsen": "geofence",
    "declared_actions": "permissions",
    "human": "principal",
    "member_of": "principal",
    "on_plan": "principal",
    "permission_document": "permissions",
    "permissions": "decorators",
    "permissions_router": "permissions",
    "roles": "decorators",
    "with_entitlements": "principal",
}

_MODULE_EXPORTS = {
    "../_livedoc": ("LiveDocument",),
    "backends": ("AuthorizationProvider",),
    "cedar": ("CedarAuthorizer", "CedarEngine"),
    "cedar_engine": (
        "CedarEntity",
        "CedarParseError",
        "CedarPolicies",
        "CedarSchema",
        "EntityUid",
    ),
    "decorators": ("authorize", "permissions", "roles"),
    "geofence": ("PrecisionLadder", "Regions", "coarsen"),
    "models": ("AuthorizationDecision",),
    "permissions": (
        "PERMISSION_CHANNEL",
        "declared_actions",
        "permission_document",
        "permissions_router",
    ),
    "principal": (
        "ANY_SCOPE",
        "Limits",
        "Narrowing",
        "Principal",
        "human",
        "member_of",
        "on_plan",
        "with_entitlements",
    ),
    "requirements": ("AuthorizationVocabulary", "AuthRequirement"),
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    path = f"._auth.{module}" if module != "../_livedoc" else "._livedoc"
    loaded = import_module(path, __package__)
    namespace = globals()
    for export in _MODULE_EXPORTS[module]:
        namespace[export] = getattr(loaded, export)
    return namespace[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
