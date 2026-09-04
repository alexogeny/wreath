from __future__ import annotations

from typing import Any

from .._auth.models import qualified_identity_key, qualified_identity_value


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


def principal_partition(principal: Any, *, label: str = "agent") -> tuple[str, str]:
    identity = getattr(principal, "identity", None)
    owner = identity if identity is not None else principal
    value = principal_id(owner, label=label)
    namespace = getattr(owner, "namespace", "")
    identity_type = getattr(owner, "type", "")
    if not isinstance(namespace, str):
        raise ValueError(f"{label} principal namespace must be a string")
    if not isinstance(identity_type, str):
        raise ValueError(f"{label} principal type must be a string")
    return value, qualified_identity_key(identity_type, namespace, value)


def principal_partition_id(principal: Any, *, label: str = "agent") -> str:
    return principal_partition(principal, label=label)[1]


__all__ = [
    "principal_id",
    "principal_partition",
    "principal_partition_id",
    "principal_scope_id",
]
