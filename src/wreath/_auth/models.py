"""Immutable authentication and authorization models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .principal import Limits, Narrowing


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

    `claims` is unrelated and predates both: it is the token's own claims (OIDC
    and friends), carried verbatim for an application to read. It is not an
    authorization input and nothing here derives authority from it.
    """

    id: str
    type: str = "User"
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    claims: Mapping[str, object] = field(default_factory=dict)
    limits: Limits | None = None
    narrowing: Narrowing | None = None


@dataclass(frozen=True, slots=True)
class Credentials:
    scheme: str
    value: str


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: str | None = None
    diagnostics: tuple[str, ...] = ()
