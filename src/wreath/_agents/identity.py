from __future__ import annotations

from typing import Any

from .._auth.models import qualified_identity_value


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


def principal_scope_id(principal: Any, *, label: str = "agent") -> str:
    value = principal_id(principal, label=label)
    identity = getattr(principal, "identity", None)
    owner = identity if identity is not None else principal
    namespace = getattr(owner, "namespace", "")
    return qualified_identity_value(str(namespace), value)


__all__ = ["principal_id", "principal_scope_id"]
