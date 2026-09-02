from __future__ import annotations

from typing import Any


def principal_id(principal: Any, *, label: str = "agent") -> str:
    if isinstance(principal, str) and principal:
        return principal
    identity = getattr(principal, "identity", None)
    for owner in (principal, identity):
        for attribute in ("id", "subject"):
            value = getattr(owner, attribute, None)
            if isinstance(value, str) and value:
                return value
    raise ValueError(
        f"{label} principal must be a non-empty string, expose id/subject, "
        "or carry an identity that does"
    )


__all__ = ["principal_id"]
