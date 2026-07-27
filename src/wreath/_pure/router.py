"""Pure-Python twin of the native RouteTable segment trie."""

from __future__ import annotations

from typing import Any


class _Node:
    __slots__ = ("kids", "param_kid", "routes")

    def __init__(self) -> None:
        self.kids: dict[str, _Node] = {}
        self.param_kid: _Node | None = None
        # method -> (handler, parameter name tuple)
        self.routes: dict[str, tuple[Any, tuple[str, ...]]] | None = None


class RouteTable:
    """Segment trie matching the semantics of wreath._native._core.RouteTable.

    Paths split as `path[1:].split("/")` so trailing slashes stay
    significant. Static segments win over parameters at each level, with
    backtracking so parameter routes still match when a static branch
    dead-ends. HEAD falls back to GET.
    """

    __slots__ = ("_root",)

    def __init__(self) -> None:
        self._root = _Node()

    def add(self, path: str, method: str, handler: Any) -> None:
        if not path.startswith("/"):
            raise ValueError("route paths must begin with '/'")

        segments = path[1:].split("/")
        if len(segments) > 255:
            raise ValueError("route path has too many segments")

        names: list[str] = []
        node = self._root
        for segment in segments:
            if len(segment) >= 2 and segment.startswith("{") and segment.endswith("}"):
                name = segment[1:-1]
                if not name or "{" in name or "}" in name:
                    raise ValueError(f"invalid path parameter: '{segment}'")
                names.append(name)
                if node.param_kid is None:
                    node.param_kid = _Node()
                node = node.param_kid
            elif "{" in segment or "}" in segment:
                raise ValueError("path parameters must occupy an entire segment")
            else:
                kid = node.kids.get(segment)
                if kid is None:
                    kid = node.kids[segment] = _Node()
                node = kid

        if node.routes is None:
            node.routes = {}
        if method in node.routes:
            kind = "conflicting" if names else "duplicate"
            raise ValueError(f"{kind} route: {method} {path}")
        node.routes[method] = (handler, tuple(names))

    def match(self, method: str, path: str) -> tuple[Any, dict[str, str] | None] | None:
        if not path.startswith("/"):
            return None
        segments = path[1:].split("/")
        if len(segments) > 255:
            return None
        values: list[str] = []
        node = self._match_node(self._root, segments, 0, method, values)
        used = method
        if node is None and method == "HEAD":
            used = "GET"
            values.clear()
            node = self._match_node(self._root, segments, 0, used, values)
        if node is None or node.routes is None:
            return None
        handler, names = node.routes[used]
        if not names:
            return handler, None
        return handler, dict(zip(names, values, strict=True))

    def _match_node(
        self,
        node: _Node,
        segments: list[str],
        index: int,
        method: str,
        values: list[str],
    ) -> _Node | None:
        if index == len(segments):
            if node.routes is not None and method in node.routes:
                return node
            return None
        segment = segments[index]
        kid = node.kids.get(segment)
        if kid is not None:
            found = self._match_node(kid, segments, index + 1, method, values)
            if found is not None:
                return found
        if node.param_kid is not None:
            values.append(segment)
            found = self._match_node(node.param_kid, segments, index + 1, method, values)
            if found is not None:
                return found
            values.pop()
        return None
