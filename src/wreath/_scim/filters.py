"""SCIM filters from RFC 7644 section 3.4.2.2.

Filters are parsed with bounded depth and length, then evaluated against the
SCIM representation returned by the resource endpoint. An attribute the
provider does not hold is refused rather than treated as an empty result: the
latter tells a provisioning client that a user is absent and can cause it to
create a duplicate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .._native import _core

__all__ = [
    "MAX_DEPTH",
    "MAX_LENGTH",
    "Compare",
    "Filter",
    "FilterError",
    "Group",
    "Logical",
    "Negate",
    "ValuePath",
    "matches",
    "parse",
    "select",
    "values_at",
]

MAX_LENGTH = 2048
MAX_DEPTH = 16
COMPARISON = frozenset({"eq", "ne", "co", "sw", "ew", "gt", "ge", "lt", "le"})


class FilterError(ValueError):
    """A filter is malformed or names an attribute this provider lacks."""

    __slots__ = ("detail",)

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Compare:
    """An attribute comparison, or a presence test when ``op == "pr"``."""

    path: str
    op: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class Logical:
    """A conjunction or disjunction."""

    op: str
    left: Filter
    right: Filter


@dataclass(frozen=True, slots=True)
class Negate:
    """A negated filter."""

    operand: Filter


@dataclass(frozen=True, slots=True)
class Group:
    """A parenthesized filter, retained so the source shape is representable."""

    operand: Filter


@dataclass(frozen=True, slots=True)
class ValuePath:
    """An existential predicate over a multi-valued attribute."""

    path: str
    predicate: Filter


type Filter = Compare | Logical | Negate | Group | ValuePath

_TYPES = (Compare, Logical, Negate, Group, ValuePath, FilterError, Mapping)


def parse(source: str, *, attributes: frozenset[str] | None = None) -> Filter:
    """Parse ``source`` and refuse attributes outside ``attributes``."""
    return _core.scim_parse(source, attributes, _TYPES)


def _compile(node: Filter) -> Any:
    return _core.scim_compile(node, _TYPES)


def values_at(resource: Any, path: str) -> list[Any]:
    """Resolve a case-insensitive path, flattening list-valued steps."""
    return _core.scim_values_at(resource, path, _TYPES)


def matches(node: Filter, resource: Any) -> bool:
    """Whether ``resource`` satisfies ``node``."""
    return _core.scim_matches(_compile(node), resource, _TYPES)


def select(node: Filter, resources: Sequence[Any], *, invert: bool = False) -> list[Any]:
    """Resources matching ``node``, or non-matches when ``invert`` is true."""
    return _core.scim_filter(_compile(node), resources, _TYPES, invert)
