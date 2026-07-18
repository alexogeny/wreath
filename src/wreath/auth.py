"""Authentication: identities, credentials, and authentication backends.

Authentication establishes *who* a request is. Authorization — what an identity
is permitted to do — lives in :mod:`wreath.authorization`. Both are thin public
facades over the private ``wreath._auth`` implementation package.
"""

from __future__ import annotations

from ._auth.backends import AuthenticationBackend, BearerTokenBackend
from ._auth.decorators import authenticated
from ._auth.models import Credentials, Identity

__all__ = [
    "AuthenticationBackend",
    "BearerTokenBackend",
    "Credentials",
    "Identity",
    "authenticated",
]
