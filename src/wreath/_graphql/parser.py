"""Bounded GraphQL document parsing."""

from __future__ import annotations

from typing import Any

from .._native import _core
from .ast import (
    Argument,
    Document,
    Field,
    FragmentDefinition,
    FragmentSpread,
    InlineFragment,
    Operation,
    SelectionSet,
    Variable,
    VariableDefinition,
)

__all__ = ["GraphQLSyntaxError", "Limits", "parse"]

DEFAULT_MAX_STEPS = 200_000


class GraphQLSyntaxError(Exception):
    """A malformed document or one that exceeded a safety limit."""

    def __init__(self, message: str, *, code: str = "syntax", position: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.position = position


class Limits:
    """Read-only safety bounds for one parse."""

    max_aliases: int
    max_complexity: int
    max_depth: int
    max_document_bytes: int
    max_steps: int

    __slots__ = (
        "max_aliases",
        "max_complexity",
        "max_depth",
        "max_document_bytes",
        "max_steps",
    )

    def __init__(
        self,
        *,
        max_depth: int = 12,
        max_complexity: int = 1000,
        max_aliases: int = 50,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_document_bytes: int = 16 * 1024,
    ) -> None:
        for name, value in (
            ("max_depth", max_depth),
            ("max_complexity", max_complexity),
            ("max_aliases", max_aliases),
            ("max_steps", max_steps),
            ("max_document_bytes", max_document_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be positive integer")
        object.__setattr__(self, "max_depth", max_depth)
        object.__setattr__(self, "max_complexity", max_complexity)
        object.__setattr__(self, "max_aliases", max_aliases)
        object.__setattr__(self, "max_steps", max_steps)
        object.__setattr__(self, "max_document_bytes", max_document_bytes)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("GraphQL parser limits are immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("GraphQL parser limits are immutable")


DEFAULT_LIMITS = Limits()


_CONFIG = (
    Argument,
    Document,
    Field,
    FragmentDefinition,
    FragmentSpread,
    InlineFragment,
    Operation,
    SelectionSet,
    Variable,
    VariableDefinition,
    GraphQLSyntaxError,
)


def parse(source: str, limits: Limits = DEFAULT_LIMITS) -> Document:
    """Parse ``source`` while enforcing its resource bounds."""
    if not isinstance(source, str):
        raise GraphQLSyntaxError("a GraphQL document must be a string")
    return _core.graphql_parse(source, limits, _CONFIG)
