"""Shared helpers to exercise every routing implementation.

Three: the decision-tree table, the trie table and the bitset table. Tests import
these to assert behavioural parity and the performance ordering. Adding a backend
here enrols it in every parity test.

Parity across three *different algorithms* answering the same question is the
oracle: an input the three disagree on is a defect in whichever one is odd.
"""

from __future__ import annotations

from typing import Any, Protocol

from wreath._native import _core


class RouteTableLike(Protocol):
    def add(self, path: str, method: str, handler: Any) -> None: ...
    def match(
        self, method: str, path: str
    ) -> tuple[Any, dict[str, str] | None] | None: ...


# name -> table factory.
IMPLS: dict[str, Any] = {
    "c-dt": _core.DecisionRouteTable,
    "c-trie": _core.RouteTable,
    "c-bitset": _core.BitsetRouteTable,
}


def build(factory: Any, routes: list[tuple[str, str]]) -> RouteTableLike:
    table = factory()
    for index, (method, path) in enumerate(routes):
        table.add(path, method, index)
    return table


def normalize(
    result: tuple[Any, dict[str, str] | None] | None,
) -> tuple[Any, dict[str, str] | None] | None:
    """Collapse (handler, {}) / (handler, None) so the backends compare."""
    if result is None:
        return None
    handler, params = result
    return (handler, params if params else None)
