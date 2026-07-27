"""Composable route modules flattened when their definitions are consumed.

This is the only public routing surface. A `Router` collects `RouteDefinition`
records; an application consumes them with `app.include_router(router)`, and the
matcher that dispatches them is internal and not addressable from here.

    from typing import Annotated
    from wreath.binding import Query
    from wreath.router import Router

    llamas = Router(prefix="/llamas", tags=["llamas"])

    @llamas.get("/")
    async def list_llamas(
        limit: Annotated[int, Query(minimum=1, maximum=100)] = 20,
    ) -> dict:
        return {"limit": limit}

Handler-parameter markers ride inside `Annotated` and the default stays an
ordinary Python default — `limit: Annotated[int, Query(...)] = 20`, never
`limit: int = Query(20)`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Any

from ._auth.requirements import AuthRequirement, SetRequirement, merge_requirements
from ._routing import Handler, check_placeholders
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
    check_placeholders(path)
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
    """One route and everything inherited into it, resolved and frozen.

    This is what `Router.routes` yields and what an application consumes: a flat
    record with the prefixes already folded into `path` and the inherited
    middleware, tags, dependencies, and permissions already merged in. Nothing
    here is looked up again at request time, so reading a definition tells you
    exactly what the route will do.

    Inherited entries come first in each tuple, outermost include first, with
    the route's own values last — the order they were applied in. A definition
    is immutable; build a changed copy with `dataclasses.replace`.

    Args:
        path: The full path, prefixes already applied.
        methods: Uppercased HTTP methods this route answers.
        endpoint: The handler function.
        middleware: Route middleware, inherited first, then the route's own.
        tags: Grouping tags for OpenAPI and generated clients, inherited first.
        summary: One-line OpenAPI summary; the handler docstring becomes the description.
        dependencies: Depends markers resolved before the handler runs.
        requirement: The merged authentication and authorization requirement.
        operation_id: Explicit client-facing operation id, or None to derive one.
    """

    path: str
    methods: tuple[str, ...]
    endpoint: Handler
    middleware: tuple[Middleware, ...] = ()
    tags: tuple[str, ...] = ()
    summary: str | None = None
    dependencies: tuple[Depends, ...] = ()
    requirement: AuthRequirement = AuthRequirement()
    #: An explicit client-facing operation identifier. When `None` the typegen
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

    Everything passed here is inherited by every route the router registers, and
    by every router it includes:

        api = Router(prefix="/api", tags=["api"], permissions=["api::use"])
        api.include_router(llamas, prefix="/v1")

    A prefix must begin with `/`; a trailing slash is stripped, so `"/api/"` and
    `"/api"` are the same router and neither produces a doubled slash. The empty
    prefix means no prefix. `permissions` requires **all** of the named
    permissions and implies authentication; an empty `permissions` adds no
    requirement at all. Inheritance only ever adds — an included router cannot
    drop a permission, a middleware, or a dependency its parent imposed.

    Args:
        prefix: Path prefix for every route, beginning with a slash.
        tags: Tags added to every route, for OpenAPI grouping and client generation.
        middleware: Middleware wrapping every route, outermost first.
        dependencies: Depends markers resolved before every handler in this router.
        permissions: Permissions every route requires, all of them, implying authentication.
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
        """Every route this router owns, flattened, with all inheritance applied.

        Included routers are folded in here rather than at `include_router()`
        time, so this is where a nested prefix becomes a full path and a parent's
        tags, middleware, dependencies, and permissions land on a child's route.
        The result is computed on each access and is a fresh tuple; hold it in a
        local when iterating it more than once.

        Routes appear in registration order, with an included router's routes at
        the position it was included.
        """
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
        """Register a handler for `path` under one or more methods.

        This is the general form; `get()`, `post()`, `put()`, `patch()` and
        `delete()` are one-line wrappers over it, and any other method — `HEAD`,
        `OPTIONS`, `TRACE` — is reached by naming it here. Methods are
        uppercased, so `methods=("get",)` and `methods=("GET",)` are the same.

        The decorator returns the handler unchanged, so a handler stays an
        ordinary callable and can be registered on more than one path or called
        directly from a test.

        `path` must begin with a slash. A `path` of `"/"` on a prefixed router
        registers the prefix itself — `Router(prefix="/llamas").get("/")` binds
        `/llamas`, not `/llamas/`.

        Handler parameters declare where each value is read from with a marker
        inside `Annotated`, keeping the default an ordinary Python default:

            @router.get("/llamas/{id}")
            async def show(
                id: int,
                limit: Annotated[int, Query(minimum=1, maximum=100)] = 20,
            ) -> dict: ...

        The router's own prefix, tags, middleware, dependencies, and permissions
        are applied at decoration time, and prefixes from any router that later
        includes this one are applied on top when `routes` is read. Everything
        passed here is added to what the router already imposes; nothing given
        here can remove it. `permissions` requires **all** of the names it lists
        and implies authentication.

        Args:
            path: Route path beginning with a slash, appended to the router prefix.
            methods: HTTP methods to bind, uppercased for you.
            middleware: Middleware for this route, after the router's, outermost first.
            tags: Tags added after the router's tags.
            summary: One-line OpenAPI summary; the handler docstring becomes the description.
            dependencies: Depends markers resolved before this handler runs.
            permissions: Additional permissions required, all of them.
            operation_id: Explicit operation id; when omitted one is derived from method and path.

        Returns:
            A decorator that registers the handler and returns it unchanged.

        Raises:
            ValueError: The path does not begin with a slash.
        """
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
        """Register a handler for `path` on GET; keywords are those of `route()`."""
        return self.route(path, methods=("GET",), **metadata)

    def post(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        """Register a handler for `path` on POST; keywords are those of `route()`."""
        return self.route(path, methods=("POST",), **metadata)

    def put(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        """Register a handler for `path` on PUT; keywords are those of `route()`."""
        return self.route(path, methods=("PUT",), **metadata)

    def patch(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        """Register a handler for `path` on PATCH; keywords are those of `route()`."""
        return self.route(path, methods=("PATCH",), **metadata)

    def delete(self, path: str, **metadata: Any) -> Callable[[Handler], Handler]:
        """Register a handler for `path` on DELETE; keywords are those of `route()`."""
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
        """Mount another router's routes inside this one.

        The routes taken are the ones `router` holds **at this moment**: the
        entry stores a snapshot tuple, so anything registered on `router`
        afterwards will not appear here. Build a router fully, then include it.

        Paths compose outside in. A route keeps the prefix of the router that
        declared it, then gains `prefix`, then this router's own prefix, so
        `Router(prefix="/api").include_router(llamas, prefix="/v1")` turns
        `llamas`'s `/{id}` — already `/llamas/{id}` if `llamas` has that prefix —
        into `/api/v1/llamas/{id}`. Nesting composes the same way to any depth.

        Tags, middleware, dependencies, and permissions are inherited: this
        router's own values, then the ones passed here, then whatever the
        included route already carried. Inheritance only adds — an included
        router cannot shed a permission or a middleware imposed above it, and
        `permissions` here requires all of the names it lists on every route
        mounted by this call.

        Nothing is flattened yet. The composition happens when `routes` is read,
        which is why including a router is cheap and why the same router can be
        included at two prefixes without either copy affecting the other.

        Args:
            router: The router whose current routes are mounted.
            prefix: Extra prefix between this router's prefix and the route path.
            tags: Tags added to every mounted route.
            middleware: Middleware added to every mounted route, outside the route's own.
            dependencies: Depends markers added to every mounted route.
            permissions: Permissions required on every mounted route, all of them.

        Raises:
            ValueError: The prefix is non-empty and does not begin with a slash.
        """
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
