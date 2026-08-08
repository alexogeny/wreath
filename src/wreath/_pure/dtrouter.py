"""Pure-Python twin of the native decision-tree route table.

Instead of walking a path left-to-right like a trie, this compiles the route
set into a decision tree that tests the cheapest, most-discriminating feature
first: HTTP method, then segment count, then selected segment values (compared
through a hash). Each test discards most candidates at once; when one (or a
few) remain, the full route is verified once and parameters are captured.

Static routes take a hash-table fast path checked before the tree.
"""

from __future__ import annotations

from typing import Any

from .authz import build_compiled_capability_mask

# A compiled route: literal segments as str, parameter slots as None.
Pattern = tuple[str | None, ...]


class _Route:
    __slots__ = ("access_clauses", "handler", "param_names", "segments", "specificity")

    def __init__(
        self,
        segments: Pattern,
        param_names: tuple[str, ...],
        handler: Any,
        access_clauses: tuple[int, ...],
    ) -> None:
        self.segments = segments
        self.param_names = param_names
        self.handler = handler
        self.access_clauses = access_clauses
        self.specificity = sum(s is not None for s in segments)


class _StaticRoute:
    __slots__ = ("access_clauses", "handler")

    def __init__(self, handler: Any, access_clauses: tuple[int, ...]) -> None:
        self.handler = handler
        self.access_clauses = access_clauses


type _ClassifiedRoute = _Route | _StaticRoute


class _Decision:
    __slots__ = ("access_clauses", "branches", "position", "wildcard")

    def __init__(
        self,
        position: int,
        branches: dict[str, Any],
        wildcard: Any,
        access_clauses: tuple[int, ...],
    ) -> None:
        self.position = position
        self.branches = branches
        self.wildcard = wildcard
        self.access_clauses = access_clauses


class _Leaf:
    __slots__ = ("access_clauses", "candidates")

    def __init__(self, candidates: list[_Route]) -> None:
        # Most specific (most literals) first, so literal routes win over
        # parameter routes that also reach this leaf.
        self.candidates = sorted(candidates, key=lambda r: r.specificity, reverse=True)
        self.access_clauses = tuple(
            clause for route in candidates for clause in route.access_clauses
        )


class DecisionRouteTable:
    """Route table compiling to a decision tree; trie-compatible interface."""

    __slots__ = ("_dirty", "_dynamic", "_seen", "_static", "_trees")

    def __init__(self) -> None:
        self._static: dict[str, dict[str, Any]] = {}
        self._dynamic: dict[str, list[_Route]] = {}
        # (method, literal-signature) guards against duplicate/conflicting routes.
        self._seen: set[tuple[str, Pattern]] = set()
        self._trees: dict[str, dict[int, Any]] = {}
        self._dirty = False

    def add(
        self,
        path: str,
        method: str,
        handler: Any,
        access_clauses: tuple[int, ...] = (0,),
    ) -> None:
        if not access_clauses or any(
            type(clause) is not int or clause < 0 for clause in access_clauses
        ):
            raise ValueError("access clauses must be a non-empty tuple of non-negative integers")
        if not path.startswith("/"):
            raise ValueError("route paths must begin with '/'")

        if "{" not in path:
            by_path = self._static.setdefault(method, {})
            if path in by_path:
                raise ValueError(f"duplicate route: {method} {path}")
            by_path[path] = (handler, access_clauses)
            return

        segments: list[str | None] = []
        names: list[str] = []
        for segment in path[1:].split("/"):
            if len(segment) >= 2 and segment.startswith("{") and segment.endswith("}"):
                name = segment[1:-1]
                if not name or "{" in name or "}" in name:
                    raise ValueError(f"invalid path parameter: '{segment}'")
                segments.append(None)
                names.append(name)
            elif "{" in segment or "}" in segment:
                raise ValueError("path parameters must occupy an entire segment")
            else:
                segments.append(segment)

        pattern = tuple(segments)
        signature = (method, pattern)
        if signature in self._seen:
            raise ValueError(f"conflicting route: {method} {path}")
        self._seen.add(signature)
        self._dynamic.setdefault(method, []).append(
            _Route(pattern, tuple(names), handler, access_clauses)
        )
        self._dirty = True

    def match(
        self, method: str, path: str, caller_mask: int = 0
    ) -> tuple[Any, dict[str, str] | None] | None:
        static = self._static.get(method)
        if static is not None:
            entry = static.get(path)
            if entry is not None and _eligible(entry[1], caller_mask):
                return entry[0], None
        if method == "HEAD":
            static = self._static.get("GET")
            if static is not None:
                entry = static.get(path)
                if entry is not None and _eligible(entry[1], caller_mask):
                    return entry[0], None

        if self._dirty:
            self._compile()

        # path[1:].split("/") keeps trie-identical semantics ("/" -> [""]).
        segments = path[1:].split("/")
        result = self._match_method(method, segments, caller_mask)
        if result is None and method == "HEAD":
            result = self._match_method("GET", segments, caller_mask)
        return result

    def probe(
        self, method: str, path: str, all_capability_mask: int
    ) -> tuple[int, tuple[Any, dict[str, str] | None] | None]:
        """Compatibility classifier that does not expose protected tickets."""
        classification, payload = self.classify(method, path)
        return (classification, payload) if classification == 1 else (classification, None)

    def classify(self, method: str, path: str) -> tuple[int, Any]:
        """Classify and capture candidates in one path-tree traversal."""
        candidates: list[tuple[_ClassifiedRoute, dict[str, str] | None]] = []
        methods = (method, "GET") if method == "HEAD" else (method,)
        for current_method in methods:
            static = self._static.get(current_method)
            if static is not None:
                entry = static.get(path)
                if entry is not None:
                    handler, clauses = entry
                    if _eligible(clauses, 0):
                        return 1, (handler, None)
                    candidates.append((_StaticRoute(handler, clauses), None))
            if self._dirty:
                self._compile()
            segments = path[1:].split("/")
            candidates.extend(self._classify_method(current_method, segments))
            for route, params in candidates:
                if _eligible(route.access_clauses, 0):
                    return 1, (route.handler, params)
        return (0, None) if not candidates else (2, tuple(candidates))

    def resolve(
        self,
        ticket: tuple[tuple[_ClassifiedRoute, dict[str, str] | None], ...],
        caller_mask: int,
    ) -> tuple[Any, dict[str, str] | None] | None:
        """Resolve a classification ticket without restarting route search."""
        for route, params in ticket:
            if _eligible(route.access_clauses, caller_mask):
                return route.handler, params
        return None

    def resolve_identity(
        self,
        ticket: tuple[tuple[_ClassifiedRoute, dict[str, str] | None], ...],
        descriptor: tuple[int, dict[str, int], dict[str, int]],
        roles: Any,
        permissions: Any,
    ) -> tuple[Any, dict[str, str] | None] | None:
        return self.resolve(
            ticket,
            build_compiled_capability_mask(descriptor, roles, permissions),
        )

    def _classify_method(
        self, method: str, segments: list[str]
    ) -> list[tuple[_Route, dict[str, str] | None]]:
        by_count = self._trees.get(method)
        if by_count is None:
            return []
        node = by_count.get(len(segments))
        if node is None:
            return []
        while type(node) is _Decision:
            branch = node.branches.get(segments[node.position])
            node = branch if branch is not None else node.wildcard
            if node is None:
                return []
        matches: list[tuple[_Route, dict[str, str] | None]] = []
        for route in node.candidates:
            params = _verify(route, segments)
            if params is not None:
                matches.append((route, params))
        return matches

    def _match_method(
        self, method: str, segments: list[str], caller_mask: int
    ) -> tuple[Any, dict[str, str] | None] | None:
        by_count = self._trees.get(method)
        if by_count is None:
            return None
        node = by_count.get(len(segments))
        if node is None or not _eligible(node.access_clauses, caller_mask):
            return None
        while type(node) is _Decision:
            branch = node.branches.get(segments[node.position])
            node = branch if branch is not None else node.wildcard
            if node is None or not _eligible(node.access_clauses, caller_mask):
                return None
        for route in node.candidates:
            if not _eligible(route.access_clauses, caller_mask):
                continue
            params = _verify(route, segments)
            if params is not None:
                return route.handler, params
        return None

    def _compile(self) -> None:
        trees: dict[str, dict[int, Any]] = {}
        for method, routes in self._dynamic.items():
            by_count: dict[int, Any] = {}
            grouped: dict[int, list[_Route]] = {}
            for route in routes:
                grouped.setdefault(len(route.segments), []).append(route)
            for count, group in grouped.items():
                by_count[count] = _build(group, frozenset(), count)
            trees[method] = by_count
        self._trees = trees
        self._dirty = False


def _eligible(clauses: tuple[int, ...], caller_mask: int) -> bool:
    return any(caller_mask & required == required for required in clauses)


def _verify(route: _Route, segments: list[str]) -> dict[str, str] | None:
    values: list[str] = []
    for expected, actual in zip(route.segments, segments, strict=True):
        if expected is None:
            values.append(actual)
        elif expected != actual:
            return None
    if not route.param_names:
        return {}
    return dict(zip(route.param_names, values, strict=True))


def _build(candidates: list[_Route], used: frozenset[int], nseg: int) -> Any:
    if len(candidates) <= 1:
        return _Leaf(candidates)

    best_position = -1
    best_score = -1
    for position in range(nseg):
        if position in used:
            continue
        literal_count = 0
        distinct: set[str] = set()
        for route in candidates:
            value = route.segments[position]
            if value is not None:
                literal_count += 1
                distinct.add(value)
        if literal_count == 0:
            continue
        # Prefer positions that fan out widely and leave few undiscriminated
        # (parameter) routes behind.
        score = len(distinct) * 1000 + literal_count
        if score > best_score:
            best_score = score
            best_position = position

    if best_position < 0:
        return _Leaf(candidates)

    wildcard_routes = [r for r in candidates if r.segments[best_position] is None]
    groups: dict[str, list[_Route]] = {}
    for route in candidates:
        value = route.segments[best_position]
        if value is not None:
            groups.setdefault(value, []).append(route)

    child_used = used | {best_position}
    # Parameter routes match any value, so they belong in every literal branch
    # too; folding them in keeps matching backtrack-free.
    branches = {
        value: _build(group + wildcard_routes, child_used, nseg)
        for value, group in groups.items()
    }
    wildcard = _build(wildcard_routes, child_used, nseg) if wildcard_routes else None
    return _Decision(
        best_position,
        branches,
        wildcard,
        tuple(clause for route in candidates for clause in route.access_clauses),
    )
