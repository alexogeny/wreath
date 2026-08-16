"""Compatibility names for the one configured-HTTP contract vocabulary.

Middleware and first-class policy both contribute headers and responses to the
same OpenAPI operation.  They therefore use the same value types; keeping a
second pair here let the two vocabularies drift while consumers had to accept
either class.
"""

from __future__ import annotations

from ..middleware.base import BEHAVIOURS, HeaderSpec, MiddlewareContract

# Public compatibility name.  An alias, not a subclass: a contract produced by
# either configuration layer is the same value and must pass one exact type
# check in OpenAPI/typegen consumers.
PolicyContract = MiddlewareContract


__all__ = ["BEHAVIOURS", "HeaderSpec", "PolicyContract"]
