"""Authorization: roles, permissions, policies, and authorization decisions.

Authorization determines *what* an authenticated identity may do. Establishing
identity lives in :mod:`wreath.auth`. Both are thin public facades over the
private ``wreath._auth`` implementation package.
"""

from __future__ import annotations

from ._auth.backends import AuthorizationProvider
from ._auth.cedar import CedarAuthorizer, CedarEngine
from ._auth.decorators import authorize, permissions, roles
from ._auth.models import AuthorizationDecision
from ._auth.requirements import AuthRequirement

__all__ = [
    "AuthRequirement",
    "AuthorizationDecision",
    "AuthorizationProvider",
    "CedarAuthorizer",
    "CedarEngine",
    "authorize",
    "permissions",
    "roles",
]
