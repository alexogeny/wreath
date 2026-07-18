"""Shared helpers to exercise every routing implementation.

Six: the C and pure-Python versions of the decision-tree table, the trie table,
and the bitset table. Tests import these to assert behavioural parity and the
performance ordering. Adding a backend here enrols it in every parity test.
"""

from __future__ import annotations

from typing import Any, Protocol

from wreath._native import _core
from wreath._pure.dtbitset import BitsetRouteTable as PureBitset
from wreath._pure.dtrouter import DecisionRouteTable as PureDecision
from wreath._pure.router import RouteTable as PureTrie


class RouteTableLike(Protocol):
    def add(self, path: str, method: str, handler: Any) -> None: ...
    def match(
        self, method: str, path: str
    ) -> tuple[Any, dict[str, str] | None] | None: ...


NATIVE = _core is not None

# name -> table factory; native entries are absent when the extension is unbuilt.
IMPLS: dict[str, Any] = {
    "py-dt": PureDecision,
    "py-trie": PureTrie,
    "py-bitset": PureBitset,
}
if NATIVE:
    IMPLS["c-dt"] = _core.DecisionRouteTable
    IMPLS["c-trie"] = _core.RouteTable
    IMPLS["c-bitset"] = _core.BitsetRouteTable


def build(factory: Any, routes: list[tuple[str, str]]) -> RouteTableLike:
    table = factory()
    for index, (method, path) in enumerate(routes):
        table.add(path, method, index)
    return table


def normalize(
    result: tuple[Any, dict[str, str] | None] | None,
) -> tuple[Any, dict[str, str] | None] | None:
    """Collapse (handler, {}) / (handler, None) so the two backends compare."""
    if result is None:
        return None
    handler, params = result
    return (handler, params if params else None)
