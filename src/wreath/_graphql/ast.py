"""The parsed shape of a GraphQL document.

Deliberately small: enough for queries and mutations over the ORM-derived
schema, and no more. Every node is a frozen slots dataclass, so a parsed and
validated document can be cached by hash and shared between requests without
anyone being able to mutate it underneath a concurrent execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Argument",
    "Document",
    "Field",
    "FragmentDefinition",
    "FragmentSpread",
    "InlineFragment",
    "Operation",
    "Selection",
    "SelectionSet",
    "Variable",
    "VariableDefinition",
]


@dataclass(frozen=True, slots=True)
class Variable:
    """A `$name` reference, resolved against the request's variables."""

    name: str


@dataclass(frozen=True, slots=True)
class Argument:
    name: str
    #: A literal (str/int/float/bool/None/list/dict) or a `Variable`.
    value: Any


@dataclass(frozen=True, slots=True)
class SelectionSet:
    selections: tuple[Selection, ...]


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    #: The response key -- the alias when given, else the field name.
    key: str
    arguments: tuple[Argument, ...] = ()
    selection_set: SelectionSet | None = None


@dataclass(frozen=True, slots=True)
class FragmentSpread:
    name: str


@dataclass(frozen=True, slots=True)
class InlineFragment:
    type_condition: str | None
    selection_set: SelectionSet


type Selection = Field | FragmentSpread | InlineFragment


@dataclass(frozen=True, slots=True)
class VariableDefinition:
    name: str
    type_name: str
    non_null: bool
    is_list: bool
    default: Any = None
    has_default: bool = False


@dataclass(frozen=True, slots=True)
class Operation:
    operation: str  # "query" | "mutation"
    name: str | None
    variables: tuple[VariableDefinition, ...]
    selection_set: SelectionSet


@dataclass(frozen=True, slots=True)
class FragmentDefinition:
    name: str
    type_condition: str
    selection_set: SelectionSet


@dataclass(frozen=True, slots=True)
class Document:
    operations: tuple[Operation, ...]
    fragments: dict[str, FragmentDefinition] = field(default_factory=dict)
    #: Measured while parsing, so callers never re-walk the tree to find out.
    depth: int = 0
    complexity: int = 0

    def operation(self, name: str | None = None) -> Operation:
        """The named operation, or the only one when a document has just one."""
        if name is not None:
            for operation in self.operations:
                if operation.name == name:
                    return operation
            raise KeyError(name)
        if len(self.operations) == 1:
            return self.operations[0]
        raise ValueError("this document defines several operations; name the one to run")
