"""Pure-Python twin of the native BitsetRouteTable.

Behavioural twin, not a transliteration. The native side hand-rolls an
open-addressed byte-keyed hash so matching never builds a `str` per segment;
here a plain dict is both faster and clearer, so the layout differs and only the
observable behaviour is held identical -- `tests/test_routing_parity.py` asserts
that across both implementations.

The shape that matters is shared: inside one (method, segment-count) group each
route gets a bit, indexed in (specificity, registration order) so bit order is
priority order. Matching intersects one mask per segment position:

    survivors &= literal[position].get(segment, 0) | param[position]

A parameter contributes its bit at every position, so nothing folds into every
branch the way the decision tree must, and the compiled form stays linear in the
route count. Positions are tested strongest-discriminator-first and the pass
stops once one route can still match.
"""

from __future__ import annotations

from typing import Any

_PARAM = None


class _Route:
    __slots__ = (
        "access_clauses",
        "handler",
        "method",
        "order",
        "param_names",
        "segs",
        "specificity",
    )

    def __init__(
        self,
        method: str,
        segs: tuple[str | None, ...],
        param_names: tuple[str, ...],
        handler: Any,
        access_clauses: tuple[int, ...],
        order: int,
    ) -> None:
        self.method = method
        self.segs = segs
        self.param_names = param_names
        self.handler = handler
        self.access_clauses = access_clauses
        self.order = order
        self.specificity = sum(1 for s in segs if s is not _PARAM)


class _Group:
    """One (method, segment-count) group, compiled to bitmasks."""

    __slots__ = ("literal", "order", "param", "public", "routes")

    def __init__(self, routes: list[_Route], nseg: int) -> None:
        # Bit order is priority order: specificity first, then registration.
        self.routes = sorted(routes, key=lambda r: (-r.specificity, r.order))
        self.literal: list[dict[str, int]] = [{} for _ in range(nseg)]
        self.param: list[int] = [0] * nseg
        self.public = 0
        for i, route in enumerate(self.routes):
            bit = 1 << i
            for p in range(nseg):
                seg = route.segs[p]
                if seg is _PARAM:
                    self.param[p] |= bit
                else:
                    self.literal[p][seg] = self.literal[p].get(seg, 0) | bit
            if 0 in route.access_clauses:
                self.public |= bit
        self.order = self._plan(nseg)

    def _plan(self, nseg: int) -> list[int]:
        """Positions worth testing, strongest discriminator first.

        Survival is sum(size^2)/total^2 over the branches a position induces,
        with parameter routes counted into every branch. A position every route
        parameterises cannot narrow anything, so it is dropped here rather than
        intersected on every request.
        """
        scored: list[tuple[float, int]] = []
        for p in range(nseg):
            if not self.literal[p]:
                continue
            params = self.param[p].bit_count()
            sizes = [m.bit_count() + params for m in self.literal[p].values()]
            if params:
                sizes.append(params)
            total = sum(sizes)
            if not total:
                continue
            survival = sum(s * s for s in sizes) / (total * total)
            if survival >= 1.0:
                continue
            scored.append((survival, p))
        scored.sort()
        return [p for _, p in scored]


def _eligible(clauses: tuple[int, ...], caller_mask: int) -> bool:
    return any(clause & ~caller_mask == 0 for clause in clauses)


class BitsetRouteTable:
    """One-pass bitset route table."""

    __slots__ = ("_groups", "_routes", "_static")

    def __init__(self) -> None:
        self._routes: list[_Route] = []
        # method -> path -> (match_result, clauses). A fully-literal route is
        # more specific than any parameter route, so a reachable hit wins
        # outright, for one hash of the whole path rather than one per segment.
        self._static: dict[str, dict[str, tuple[Any, tuple[int, ...]]]] = {}
        self._groups: dict[tuple[str, int], _Group] = {}

    def add(
        self,
        path: str,
        method: str,
        handler: Any,
        access_clauses: tuple[int, ...] = (0,),
    ) -> None:
        if not path.startswith("/"):
            raise ValueError("route paths must begin with '/'")
        raw = path[1:].split("/")
        if "{" not in path:
            by_path = self._static.setdefault(method, {})
            if path in by_path:
                raise ValueError(f"duplicate route: {method} {path}")
            by_path[path] = ((handler, None), access_clauses)
            return

        segs: list[str | None] = []
        names: list[str] = []
        for seg in raw:
            if len(seg) >= 2 and seg[0] == "{" and seg[-1] == "}":
                name = seg[1:-1]
                if not name or "{" in name or "}" in name:
                    raise ValueError(f"invalid path parameter: {seg!r}")
                names.append(name)
                segs.append(_PARAM)
            elif "{" in seg or "}" in seg:
                raise ValueError("path parameters must occupy an entire segment")
            else:
                segs.append(seg)

        signature = (method, tuple("" if s is _PARAM else s for s in segs),
                     tuple(s is _PARAM for s in segs))
        for existing in self._routes:
            other = (existing.method,
                     tuple("" if s is _PARAM else s for s in existing.segs),
                     tuple(s is _PARAM for s in existing.segs))
            if other == signature:
                # Same literal/parameter shape is the same route; parameter
                # names do not distinguish it. "duplicate" is the static case.
                raise ValueError(f"conflicting route: {method} {path}")

        self._routes.append(
            _Route(method, tuple(segs), tuple(names), handler, access_clauses,
                   len(self._routes))
        )
        self._groups.clear()  # recompiled lazily

    def _group(self, method: str, nseg: int) -> _Group | None:
        key = (method, nseg)
        group = self._groups.get(key)
        if group is not None:
            return group
        members = [r for r in self._routes if r.method == method and len(r.segs) == nseg]
        if not members:
            return None
        group = _Group(members, nseg)
        self._groups[key] = group
        return group

    def _scan(
        self,
        method: str,
        path: str,
        caller_mask: int,
        ticket: list[Any] | None,
    ) -> tuple[Any, dict[str, str] | None] | None:
        by_path = self._static.get(method)
        if by_path is not None:
            entry = by_path.get(path)
            if entry is not None:
                result, clauses = entry
                if _eligible(clauses, caller_mask):
                    return result
                if ticket is not None:
                    ticket.append(entry)
                # Unreachable: fall through. A static route this caller cannot
                # reach must not shadow a parameter route it can (ADR 0015).

        segs = path[1:].split("/")
        group = self._group(method, len(segs))
        if group is None:
            return None

        survivors = (1 << len(group.routes)) - 1
        for p in group.order:
            survivors &= group.literal[p].get(segs[p], 0) | group.param[p]
            if survivors == 0 or survivors & (survivors - 1) == 0:
                break  # none, or one: nothing further can narrow it

        while survivors:
            bit = survivors & -survivors
            index = bit.bit_length() - 1
            survivors ^= bit
            route = group.routes[index]
            # The intersection can leave a route matching every tested position
            # but not an untested one, so verify before accepting -- and a route
            # that does not match must never reach a ticket.
            if any(
                s is not _PARAM and s != segs[p]
                for p, s in enumerate(route.segs)
            ):
                continue
            result = (
                route.handler,
                {n: segs[p] for n, p in zip(
                    route.param_names,
                    [i for i, s in enumerate(route.segs) if s is _PARAM],
                    strict=True,
                )} or None,
            )
            if _eligible(route.access_clauses, caller_mask):
                return result
            if ticket is not None:
                ticket.append((result, route.access_clauses))
        return None

    def _dispatch(
        self, method: str, path: str, caller_mask: int, ticket: list[Any] | None
    ) -> tuple[Any, dict[str, str] | None] | None:
        found = self._scan(method, path, caller_mask, ticket)
        if found is None and method == "HEAD":
            found = self._scan("GET", path, caller_mask, ticket)
        return found

    def match(
        self, method: str, path: str, caller_mask: int = 0
    ) -> tuple[Any, dict[str, str] | None] | None:
        return self._dispatch(method, path, caller_mask, None)

    def classify(self, method: str, path: str) -> tuple[int, Any]:
        ticket: list[Any] = []
        found = self._dispatch(method, path, 0, ticket)
        if found is not None:
            return 1, found
        if not ticket:
            return 0, None
        return 2, tuple(ticket)

    def resolve(
        self, ticket: tuple[Any, ...], caller_mask: int
    ) -> tuple[Any, dict[str, str] | None] | None:
        for result, clauses in ticket:
            if _eligible(clauses, caller_mask):
                return result
        return None

    def probe(
        self, method: str, path: str, all_capability_mask: int
    ) -> tuple[int, Any]:
        """Compatibility classifier that never exposes protected tickets."""
        classification, payload = self.classify(method, path)
        return (classification, payload) if classification == 1 else (classification, None)


__all__ = ["BitsetRouteTable"]
