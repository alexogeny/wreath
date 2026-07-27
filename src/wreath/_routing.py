"""Routing with three interchangeable compiled backends.

The `"decision"` backend compiles the route set into a decision tree that
tests the cheapest, most-discriminating feature first (HTTP method, then segment
count, then selected segment values) so a request classifies in a few hash
lookups and only one route is fully verified. The `"trie"` backend keeps the
earlier left-to-right segment trie.

The default `"bitset"` backend gives every route in a (method, segment-count) group a
bit and intersects one mask per segment position, so a parameter route no longer
folds into every literal branch the way the decision tree needs it to. That
folding is what makes the tree grow super-linearly with the parameter fraction;
the bitset stays linear. See `docs/plans/bitset-routing.md` for the
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

#: What a route decorator accepts, and what `RouteDefinition.endpoint` holds: the
#: handler as the user declared it. Its first parameter is the `Request`; any
#: further parameters are bound by name from the path, query, headers, or body
#: during startup compilation. That per-route shape cannot be spelled in the type
#: system, hence `...`. Narrowing this to `[Request]` would reject the framework's
#: own documented signature -- `async def hello(request: Request, name: str)` --
#: and every handler in the guides with it.
Handler = Callable[..., Awaitable[Any]]

#: What the dispatcher calls once compilation has bound the extra parameters away.
#: Assignable to `Handler`, so the route table can hold either: it is loaded with
#: declared handlers at registration and reloaded with compiled ones at startup.
CompiledHandler = Callable[["Request"], Awaitable[Any]]

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


def check_placeholders(path: str) -> None:
    """Refuse a placeholder syntax the matcher does not implement.

    Every backend reads a placeholder as the whole segment between braces and
    matches exactly one segment, so a converter suffix -- `{key:path}`, the
    Starlette and Flask spelling for a greedy trailing match -- was accepted at
    registration and then behaved as a parameter literally named `key:path`. A
    multi-segment request 404'd because the extra separators matched nothing,
    and a single-segment one bound nothing and 422'd on the missing parameter.
    Both blame the caller for a declaration the framework never supported.

    Raises:
        ValueError: A placeholder carries a converter suffix, or is empty.
    """
    for segment in path.split("/"):
        if not (segment.startswith("{") and segment.endswith("}")):
            continue
        name = segment[1:-1]
        if not name:
            raise ValueError(f"empty path placeholder in {path!r}")
        if ":" in name:
            converter = name.split(":", 1)[1]
            raise ValueError(
                f"path placeholder {segment!r} in {path!r} carries a converter "
                f"suffix ({converter!r}); wreath placeholders name a parameter "
                f"and match exactly one segment. Write '{{{name.split(':', 1)[0]}}}' "
                "and declare the type on the handler parameter "
                "(Annotated[int, Path()]). A value spanning '/' cannot be a path "
                "parameter -- carry it in the query string instead."
            )


class Router:
    __slots__ = ("_mode", "_table")

    def __init__(self, mode: RoutingMode = "bitset") -> None:
        try:
            table_type = _TABLES[mode]
        except KeyError:
            raise ValueError(f"unknown routing mode: {mode!r}") from None
        self._mode = mode
        self._table = table_type()

    def compile(self) -> None:
        """Eagerly compile a backend that exposes a startup compiler."""
        compiler = getattr(self._table, "compile", None)
        if compiler is not None:
            compiler()

    def add(
        self,
        path: str,
        method: str,
        handler: Handler,
        access_clauses: tuple[int, ...] = (0,),
    ) -> None:
        check_placeholders(path)
        table: Any = self._table
        if self._mode in _CLASSIFYING:
            table.add(path, method.upper(), handler, access_clauses)
        else:
            table.add(path, method.upper(), handler)

    def classify(self, method: str, path: str) -> tuple[int, Any]:
        """Classify one path traversal into miss, public match, or protected ticket.

        The `"trie"` backend stores no capability clauses, so it has no protected
        class to report and this shim answers every hit as a public match. Nothing
        is lost by that: a trie route's requirement is enforced by the dispatcher's
        authorization stage instead, which is the branch `_CLASSIFYING` selects.
        """
        if self._mode in _CLASSIFYING:
            table: Any = self._table
            return table.classify(method, path)
        matched = self._table.match(method, path)
        return (0, None) if matched is None else (1, matched)

    def resolve(self, ticket: Any, caller_mask: int) -> Any:
        """Resolve a protected ticket without restarting path search.

        The `"trie"` shim returns its argument unchanged rather than filtering on
        `caller_mask`, because `classify` above issues no ticket for that backend.
        The pair keeps one shape for a caller that does not branch on the mode.
        """
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
