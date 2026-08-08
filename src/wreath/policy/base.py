"""Configuration and execution records shared by first-class HTTP policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

BEHAVIOURS: frozenset[str] = frozenset(
    {"idempotency-key", "retry-after", "etag", "csrf-token"}
)


@dataclass(frozen=True, slots=True)
class HeaderSpec:
    """One request or response header declared by configured policy."""

    name: str
    description: str = ""
    required: bool = False
    const: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyContract:
    """The HTTP surface one configured policy component contributes."""

    request_headers: tuple[HeaderSpec, ...] = ()
    response_headers: tuple[tuple[int | None, HeaderSpec], ...] = ()
    responses: tuple[tuple[int, Any], ...] = ()
    methods: frozenset[str] | None = None
    behaviours: frozenset[str] = frozenset()


__all__ = ["BEHAVIOURS", "HeaderSpec", "PolicyContract"]
