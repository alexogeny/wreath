"""Immutable authentication and authorization models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Identity:
    id: str
    type: str = "User"
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    claims: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Credentials:
    scheme: str
    value: str


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: str | None = None
    diagnostics: tuple[str, ...] = ()
