"""Pure-Python authorization helpers mirrored by `wreath._native._core`."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def build_capability_mask(
    capabilities: dict[str, int], roles: Iterable[str], permissions: Iterable[str]
) -> int:
    """Build an identity mask from the application capability registry."""
    mask = capabilities["authenticated"]
    for role in roles:
        mask |= capabilities.get(f"role:{role}", 0)
    for permission in permissions:
        mask |= capabilities.get(f"permission:{permission}", 0)
    return mask


def normalize_authorization_decision(result: Any, decision_type: type[Any]) -> Any:
    """Normalize a Cedar-engine result, denying unrecognized shapes by default."""
    if isinstance(result, decision_type):
        return result
    if isinstance(result, bool):
        return decision_type(result, "cedar")
    allowed = bool(getattr(result, "allowed", False))
    diagnostics = tuple(str(item) for item in getattr(result, "diagnostics", ()))
    return decision_type(allowed, "cedar", diagnostics)
