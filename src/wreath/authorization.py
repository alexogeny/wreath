"""Authorization: roles, permissions, policies, and authorization decisions.

Authorization determines *what* an authenticated identity may do. Establishing
identity lives in `wreath.auth`. Both are thin public facades over the
private `wreath._auth` implementation package.

`LiveDocument` is re-exported here because `permission_document`
returns one: it is the per-principal document primitive -- an `ETag` and a
change stream -- and permissions are its first caller.
"""

from __future__ import annotations

from ._auth.backends import AuthorizationProvider
from ._auth.cedar import CedarAuthorizer, CedarEngine
from ._auth.cedar_engine import CedarEntity, CedarParseError, CedarPolicies, EntityUid
from ._auth.decorators import authorize, permissions, roles
from ._auth.models import AuthorizationDecision
from ._auth.permissions import (
    PERMISSION_CHANNEL,
    declared_actions,
    permission_document,
    permissions_router,
)
from ._auth.requirements import AuthRequirement
from ._livedoc import LiveDocument

__all__ = [
    "PERMISSION_CHANNEL",
    "AuthRequirement",
    "AuthorizationDecision",
    "AuthorizationProvider",
    "CedarAuthorizer",
    "CedarEngine",
    "CedarEntity",
    "CedarParseError",
    "CedarPolicies",
    "EntityUid",
    "LiveDocument",
    "authorize",
    "declared_actions",
    "permission_document",
    "permissions",
    "permissions_router",
    "roles",
]
