"""Routing with two interchangeable compiled backends.

The default ``"decision"`` backend compiles the route set into a decision tree
that tests the cheapest, most-discriminating feature first (HTTP method, then
segment count, then selected segment values) so a request classifies in a few
hash lookups and only one route is fully verified. The ``"trie"`` backend keeps
the earlier left-to-right segment trie.

The ``"bitset"`` backend gives every route in a (method, segment-count) group a
bit and intersects one mask per segment position, so a parameter route no longer
folds into every literal branch the way the decision tree needs it to. That
folding is what makes the tree grow super-linearly with the parameter fraction;
the bitset stays linear. See ``docs/plans/bitset-routing.md`` for the
measurements.

All three run in C when the extension is available, with pure-Python twins
otherwise, and all are behaviourally identical — the tests assert parity across
every combination.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from ._native import _core

if TYPE_CHECKING:
    from .request import Request

Handler = Callable[["Request"], Awaitable[Any]]
RoutingMode = Literal["decision", "trie", "bitset"]

if _core is not None:
    _TABLES: dict[str, Any] = {
        "decision": _core.DecisionRouteTable,
        "trie": _core.RouteTable,
        "bitset": _core.BitsetRouteTable,
    }
else:
    from ._pure.dtbitset import BitsetRouteTable
    from ._pure.dtrouter import DecisionRouteTable
    from ._pure.router import RouteTable

    _TABLES = {
        "decision": DecisionRouteTable,
        "trie": RouteTable,
        "bitset": BitsetRouteTable,
    }

#: Backends carrying the full classify/resolve protocol; "trie" matches only.
_CLASSIFYING = frozenset({"decision", "bitset"})


class Router:
    __slots__ = ("_mode", "_table")

    def __init__(self, mode: RoutingMode = "decision") -> None:
        try:
            table_type = _TABLES[mode]
        except KeyError:
            raise ValueError(f"unknown routing mode: {mode!r}") from None
        self._mode = mode
        self._table = table_type()

    def add(
        self,
        path: str,
        method: str,
        handler: Handler,
        access_clauses: tuple[int, ...] = (0,),
    ) -> None:
        table: Any = self._table
        if self._mode in _CLASSIFYING:
            table.add(path, method.upper(), handler, access_clauses)
        else:
            table.add(path, method.upper(), handler)

    def classify(self, method: str, path: str) -> tuple[int, Any]:
        """Classify one path traversal into miss, public match, or protected ticket."""
        if self._mode in _CLASSIFYING:
            table: Any = self._table
            return table.classify(method, path)
        matched = self._table.match(method, path)
        return (0, None) if matched is None else (1, matched)

    def resolve(self, ticket: Any, caller_mask: int) -> Any:
        """Resolve a protected ticket without restarting path search."""
        if self._mode in _CLASSIFYING:
            table: Any = self._table
            return table.resolve(ticket, caller_mask)
        return ticket

    def match(
        self, method: str, path: str, caller_mask: int = 0
    ) -> tuple[Handler | None, dict[str, str] | None]:
        table: Any = self._table
        matched = (
            table.match(method, path, caller_mask)
            if self._mode in _CLASSIFYING
            else table.match(method, path)
        )
        if matched is None:
            return None, None
        return matched
