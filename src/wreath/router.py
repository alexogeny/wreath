"""Composable route modules flattened when their definitions are consumed."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Any

from ._auth.requirements import AuthRequirement, SetRequirement, merge_requirements
from ._routing import Handler
from .binding import Depends
from .middleware.base import Middleware


def _prefix(value: str) -> str:
    if not value:
        return ""
    if not value.startswith("/"):
        raise ValueError("router prefixes must begin with '/'")
    if value != "/" and value.endswith("/"):
        value = value.rstrip("/")
    return value


def _path(prefix: str, path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("route paths must begin with '/'")
    if not prefix:
        return path
    if path == "/":
        return prefix or "/"
    return prefix + path


def _permission_requirement(permissions: Iterable[str]) -> AuthRequirement:
    values = frozenset(permissions)
    if not values:
        return AuthRequirement()
    return AuthRequirement(
        authenticated=True,
        permission_checks=(SetRequirement(values, "all"),),
    )



@dataclass(frozen=True, slots=True)
class RouteDefinition:
    path: str
    methods: tuple[str, ...]
    endpoint: Handler
    middleware: tuple[Middleware, ...] = ()
    tags: tuple[str, ...] = ()
    summary: str | None = None
    dependencies: tuple[Depends, ...] = ()
    requirement: AuthRequirement = AuthRequirement()
    #: An explicit client-facing operation identifier. When ``None`` the typegen
    #: and OpenAPI layers derive a deterministic id from method and path.
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class _IncludedRoutes:
    """An immutable snapshot edge flattened only when routes are consumed."""

    entries: tuple[RouteDefinition | _IncludedRoutes, ...]
    parent_prefix: str
    include_prefix: str
    tags: tuple[str, ...]
    middleware: tuple[Middleware, ...]
    dependencies: tuple[Depends, ...]
    requirement: AuthRequirement


def _flatten_routes(
    entries: tuple[RouteDefinition | _IncludedRoutes, ...],
) -> tuple[RouteDefinition, ...]:
    flattened: list[RouteDefinition] = []
    stack: list[
        tuple[
            Iterator[RouteDefinition | _IncludedRoutes],
            tuple[_IncludedRoutes, ...],
        ]
    ] = [(iter(entries), ())]
    while stack:
        iterator, wrappers = stack[-1]
        try:
            entry = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        if isinstance(entry, _IncludedRoutes):
            stack.append((iter(entry.entries), (*wrappers, entry)))
            continue

        path = entry.path
        tags: list[str] = []
        middleware: list[Middleware] = []
        dependencies: list[Depends] = []
        requirements: list[AuthRequirement] = []
        for wrapper in wrappers:
            tags.extend(wrapper.tags)
            middleware.extend(wrapper.middleware)
            dependencies.extend(wrapper.dependencies)
            requirements.append(wrapper.requirement)
        for wrapper in reversed(wrappers):
            path = _path(wrapper.include_prefix, path)
            path = _path(wrapper.parent_prefix, path)
        flattened.append(
            replace(
                entry,
                path=path,
                tags=(*tags, *entry.tags),
                middleware=(*middleware, *entry.middleware),
                dependencies=(*dependencies, *entry.dependencies),
                requirement=merge_requirements(*requirements, entry.requirement),
            )
        )
    return tuple(flattened)


class Router:
    """A reusable collection of routes and inherited route metadata.

    Routers are declarative modules, not request-time sub-applications. Including
    one takes a snapshot of its definitions and folds prefixes, middleware,
    dependencies, tags, and permissions into each resulting route.
    """

    __slots__ = (
        "_dependencies",
        "_middleware",
        "_prefix",
        "_requirement",
        "_routes",
        "_tags",
    )

    def __init__(
        self,
        *,
        prefix: str = "",
        tags: Iterable[str] = (),
        middleware: Iterable[Middleware] = (),
        dependencies: Iterable[Depends] = (),
        permissions: Iterable[str] = (),
    ) -> None:
        self._prefix = _prefix(prefix)
        self._tags = tuple(tags)
        self._middleware = tuple(middleware)
        self._dependencies = tuple(dependencies)
        self._requirement = _permission_requirement(permissions)
        self._routes: list[RouteDefinition | _IncludedRoutes] = []

    @property
    def routes(self) -> tuple[RouteDefinition, ...]:
        return _flatten_routes(tuple(self._routes))

    def route(
        self,
        path: str,
        *,
        methods: Iterable[str],
        middleware: Iterable[Middleware] = (),
        tags: Iterable[str] = (),
        summary: str | None = None,
        dependencies: Iterable[Depends] = (),
        permissions: Iterable[str] = (),
        operation_id: str | None = None,
    ) -> Callable[[Handler], Handler]:
        full_path = _path(self._prefix, path)
        route_methods = tuple(method.upper() for method in methods)
        route_middleware = self._middleware + tuple(middleware)
        route_tags = self._tags + tuple(tags)
        route_dependencies = self._dependencies + tuple(dependencies)
        requirement = merge_requirements(
            self._requirement, _permission_requirement(permissions)
        )

        def register(handler: Handler) -> Handler:
            self._routes.append(
                RouteDefinition(
                    full_path,
                    route_methods,
                    handler,
                    route_middleware,
                    route_tags,
                    summary,
                    route_dependencies,
                    requirement,
                    operation_id,
                )
            )
            return handler

        return register

    def get(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        return self.route(path, methods=("GET",), **metadata)

    def post(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        return self.route(path, methods=("POST",), **metadata)

    def put(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        return self.route(path, methods=("PUT",), **metadata)

    def patch(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        return self.route(path, methods=("PATCH",), **metadata)

    def delete(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        return self.route(path, methods=("DELETE",), **metadata)

    def include_router(
        self,
        router: Router,
        *,
        prefix: str = "",
        tags: Iterable[str] = (),
        middleware: Iterable[Middleware] = (),
        dependencies: Iterable[Depends] = (),
        permissions: Iterable[str] = (),
    ) -> None:
        include_prefix = _prefix(prefix)
        include_tags = tuple(tags)
        include_middleware = tuple(middleware)
        include_dependencies = tuple(dependencies)
        include_requirement = _permission_requirement(permissions)
        self._routes.append(
            _IncludedRoutes(
                tuple(router._routes),
                self._prefix,
                include_prefix,
                self._tags + include_tags,
                self._middleware + include_middleware,
                self._dependencies + include_dependencies,
                merge_requirements(self._requirement, include_requirement),
            )
        )


__all__ = ["RouteDefinition", "Router"]
