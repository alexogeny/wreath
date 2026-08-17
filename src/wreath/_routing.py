"""Routing through one capability-aware native policy table.

The policy table groups routes by method and segment count, then intersects
compiled literal, parameter, and capability masks in strongest-discriminator
order. It returns either a public route or an opaque protected continuation;
authentication resolves that continuation without routing a second time or
exposing a handler first.

`PolicyRouteTable` is the sole implementation and `"policy"` is the sole mode.
Old experimental mode spellings are refused at construction instead of
silently selecting a compatibility path.
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
#: The return type is `Awaitable[Any] | Any` because a handler may be `def` as
#: well as `async def` -- dispatch calls it and awaits only what came back
#: awaitable. See `docs/guides/routing.md#synchronous-handlers`.
Handler = Callable[..., Awaitable[Any] | Any]

#: What the dispatcher calls once compilation has bound the extra parameters away.
#: Assignable to `Handler`, so the route table can hold either: it is loaded with
#: declared handlers at registration and reloaded with compiled ones at startup.
CompiledHandler = Callable[["Request"], Awaitable[Any] | Any]

RoutingMode = Literal["policy"]


def check_placeholders(path: str) -> None:
    """Refuse a placeholder syntax the matcher does not implement.

    Every backend reads an ordinary placeholder as the whole segment between
    braces. A final `{key:path}` placeholder is the one supported converter and
    greedily captures the remaining path. Reject partial braces, empty names,
    unknown converters, and non-final greedy placeholders at registration so
    the policy compiler never has to guess at a malformed declaration.

    Raises:
        ValueError: A placeholder is malformed or uses an unsupported converter.
    """
    for segment in path.split("/"):
        starts = segment.startswith("{")
        ends = segment.endswith("}")
        if starts != ends:
            raise ValueError("path parameters must occupy an entire segment")
        if not starts:
            continue
        name = segment[1:-1]
        if not name:
            raise ValueError(f"empty path placeholder in {path!r}")
        if ":" in name:
            parameter, converter = name.split(":", 1)
            if not parameter:
                raise ValueError(f"empty path placeholder in {path!r}")
            if converter != "path":
                raise ValueError(
                    f"unknown path converter {converter!r} in {path!r}; "
                    "the only converter is 'path'"
                )
            if segment != path.rstrip("/").split("/")[-1]:
                raise ValueError("a {name:path} placeholder must be the final path segment")


class Router:
    __slots__ = ("_mode", "_table")

    def __init__(self, mode: RoutingMode = "policy") -> None:
        if mode != "policy":
            raise ValueError(f"unknown routing mode {mode!r}; use 'policy'")
        self._mode = "policy"
        self._table = _core.PolicyRouteTable()

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
        *,
        host: str | None = None,
    ) -> None:
        check_placeholders(path)
        table: Any = self._table
        if host is not None or ":path}" in path:
            table.add_dynamic(
                path,
                method.upper(),
                None if host is None else host.lower(),
                handler,
                access_clauses,
            )
        else:
            table.add(path, method.upper(), handler, access_clauses)

    def classify(self, method: str, path: str) -> tuple[int, Any]:
        """Classify one path traversal into miss, public match, or protected ticket.

        Every route follows this protocol.
        """
        table: Any = self._table
        return table.classify(method, path)

    def classify_request(self, method: str, path: str, host: str) -> tuple[int, Any]:
        """Classify host, ordinary, and greedy route facts in precedence order."""
        table: Any = self._table
        return table.classify_request(method, path, host)

    def resolve(self, ticket: Any, caller_mask: int) -> Any:
        """Resolve a protected ticket without restarting path search.

        The continuation was produced by this same table and is never rerouted.
        """
        table: Any = self._table
        return table.resolve(ticket, caller_mask)

    def match(
        self, method: str, path: str, caller_mask: int = 0
    ) -> tuple[Handler | None, dict[str, str] | None]:
        table: Any = self._table
        matched = table.match(method, path, caller_mask)
        if matched is None:
            return None, None
        return matched
