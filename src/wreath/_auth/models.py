"""Immutable authentication and authorization models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .principal import Limits, Narrowing

_EMPTY_ATTRIBUTES: Mapping[str, object] = MappingProxyType({})


def _freeze_authority_value(value: object, active: set[int]) -> object:
    if not isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return value
    marker = id(value)
    if marker in active:
        raise ValueError("Identity attributes must not contain cycles")
    active.add(marker)
    try:
        if isinstance(value, Mapping):
            return MappingProxyType(
                {key: _freeze_authority_value(item, active) for key, item in value.items()}
            )
        if isinstance(value, (list, tuple)):
            return tuple(_freeze_authority_value(item, active) for item in value)
        return frozenset(_freeze_authority_value(item, active) for item in value)
    finally:
        active.remove(marker)


def qualified_identity_value(namespace: str, value: str) -> str:
    return f"{len(namespace)}:{namespace}{value}" if namespace else value


def qualified_identity_key(identity_type: str, namespace: str, value: str) -> str:
    identity_value = f"{len(namespace)}:{namespace}{value}"
    return f"{len(identity_type)}:{identity_type}{identity_value}"


@dataclass(frozen=True, slots=True)
class Identity:
    """Who is calling, and everything that can only *reduce* what they may do.

    `limits` and `narrowing` are the composed principal reaching the
    authorizer (`wreath._auth.principal`). Both default to absent and absent
    means "no restriction beyond what the providers say", so an ordinary
    un-delegated request pays nothing for either.

    Neither field can grant. A `Limits` is intersected with what a provider
    resolved; a `Narrowing` is refused before the engine is consulted and has
    the delegating principal's own decision evaluated as a conjunct. That is
    checked by property test rather than asserted here -- see
    `tests/test_principal_narrow.py`.

    `attributes` is the provisioned account data Cedar may read from the
    principal entity: status, account kind, or validity bounds. Machine
    accounts are ordinary accounts with different attributes and policies, not
    a privileged identity class. `claims` remains unrelated: it is the token's
    own untrusted/application-facing claims (OIDC and friends), carried
    verbatim, and nothing derives authority from it.

    `namespace` distinguishes locally unique ids issued by different identity
    providers without changing the application-facing `id`. JWT identities use
    their verified `iss`; session bridges must carry it forward.
    """

    id: str
    type: str = "User"
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    claims: Mapping[str, object] = field(default_factory=dict)
    limits: Limits | None = None
    narrowing: Narrowing | None = None
    attributes: Mapping[str, object] = _EMPTY_ATTRIBUTES
    namespace: str = ""
    authority_roles: frozenset[str] = field(init=False)
    authority_permissions: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        roles = self.roles
        if roles.__class__ is not frozenset:
            roles = frozenset(roles)
            object.__setattr__(self, "roles", roles)
        permissions = self.permissions
        if permissions.__class__ is not frozenset:
            permissions = frozenset(permissions)
            object.__setattr__(self, "permissions", permissions)
        if self.attributes is not _EMPTY_ATTRIBUTES:
            object.__setattr__(
                self,
                "attributes",
                _freeze_authority_value(self.attributes, set()),
            )
        object.__setattr__(
            self,
            "authority_roles",
            frozenset(qualified_identity_value(self.namespace, item) for item in roles),
        )
        object.__setattr__(
            self,
            "authority_permissions",
            frozenset(qualified_identity_value(self.namespace, item) for item in permissions),
        )


@dataclass(frozen=True, slots=True)
class Credentials:
    scheme: str
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: str | None = None
    diagnostics: tuple[str, ...] = ()
